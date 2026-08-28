from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .agentflow import AgentFlowPackError
from .context import bounded_context
from .models import CullDecision, RunContract, RunState, RunStatus, WorkerSpec
from .policy import parallel_missions
from .process import windows_process_table
from .prompts import judge_prompt, scout_prompt
from .scoring import deterministic_score, merge_semantic, unique_evidence
from .store import append_jsonl, artifact_sha256_async, atomic_json, read_jsonl, utc_now
from .structured import normalize_result

if TYPE_CHECKING:
    from .engine import Engine


class PackController:
    """Bounded read-only fan-out, live direct culling, and Writer synthesis."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def missions(self, state: RunState, contract: RunContract, writer: dict,
                 reserve_controller: bool = False) -> list[str]:
        requested = parallel_missions(writer)
        defaults = ["reproduce the failure and extract exact logs",
                    "trace relevant code paths and state transitions",
                    "try to disprove the current root-cause hypothesis"]
        reserved = 3 if reserve_controller else 2  # Writer, Verifier, optional controller.
        available = max(0, contract.budget.max_total_workers - state.total_workers - reserved)
        count = min(contract.pack.initial_workers, contract.pack.maximum_workers,
                    contract.pack.concurrency, available)
        missions: dict[str, str] = {}
        for mission in requested + defaults:
            mission = _tail_bytes(" ".join(mission.split()), 1000)
            missions.setdefault(mission.casefold(), mission)
        return list(missions.values())[:count]

    def _agentflow_plan(self, state: RunState, contract: RunContract,
                        writer: dict) -> tuple[list[str], bool]:
        if (contract.pack.semantic_judge and
                self.engine.agentflow._native_member_culling()):
            missions = self.missions(state, contract, writer, reserve_controller=True)
            if len(missions) > contract.pack.survivors:
                return missions, True
        return self.missions(state, contract, writer), False

    async def run(self, state: RunState, contract: RunContract, round_dir: Path,
                  context: dict, writer: dict) -> list[dict]:
        backend = contract.pack.backend
        configured = bool(self.engine.config.get("agentflow", {}).get("enabled", True))
        available = self.engine.agentflow.available() if backend != "direct" else False
        if backend == "agentflow" and not available:
            raise AgentFlowPackError("AgentFlow Pack requested but executable was not found")
        qualified = len(self._agentflow_plan(state, contract, writer)[0]) >= 2
        use_agentflow = backend == "agentflow" or (
            backend == "auto" and configured and available and qualified)
        if backend == "auto":
            self.engine.store.event(
                state.run_id, "pack_backend_selected",
                selected_backend="agentflow" if use_agentflow else "direct",
                qualified_parallel_pack=qualified,
                agentflow_configured=configured, agentflow_available=available)
        if use_agentflow:
            try:
                return await self.agentflow(state, contract, round_dir, context, writer)
            except AgentFlowPackError as error:
                self.engine.store.event(state.run_id, "agentflow_pack_failed", error=str(error))
                if (self.engine.store.run_dir(state.run_id) / "cancel.requested").exists():
                    self.engine._finish(state, RunStatus.CANCELLED, "cancelled during AgentFlow Pack")
                    return []
                if backend == "agentflow":
                    raise
                self.engine.store.event(state.run_id, "agentflow_pack_fallback", backend="direct")
        return await self.direct(state, contract, round_dir, context, writer)

    async def agentflow(self, state: RunState, contract: RunContract, round_dir: Path,
                        context: dict, writer: dict) -> list[dict]:
        self.engine._transition(state, RunStatus.SCOUTING)
        self.engine.store.save_state(state)
        missions, use_controller = self._agentflow_plan(state, contract, writer)
        if not missions:
            return []
        state.total_workers += len(missions) + int(use_controller)
        self.engine.store.save_state(state)
        self.engine.store.event(
            state.run_id, "pack_started", backend="agentflow",
            workers=([f"scout-{state.round}-{index}"
                      for index in range(1, len(missions) + 1)] +
                     (["scout-controller"] if use_controller else [])))
        culling_policy = {
            "minimum_runtime_seconds": contract.pack.minimum_runtime_seconds,
            "minimum_progress_events": contract.pack.minimum_progress_events,
            "judge_interval_seconds": contract.pack.judge_interval_seconds,
            "cooldown_seconds": contract.pack.cull_cooldown_seconds,
            "minimum_score_gap": contract.pack.minimum_score_gap,
            "survivors": contract.pack.survivors,
        } if use_controller else None
        results = await self.engine.agentflow.run(
            contract.objective, missions, context, self.engine.workspace, round_dir,
            contract.pack.concurrency,
            float(self.engine.config.get("workers", {}).get("scout_timeout_seconds", 900)),
            self.engine.config.get("codex", {}).get("scout_model", ""),
            self.engine.store.run_dir(state.run_id) / "cancel.requested",
            maximum_prompt_bytes=self.engine.context_limit,
            maximum_trace_bytes=self.engine.trace_limit,
            culling_policy=culling_policy,
            controller_model=self.engine.config.get("codex", {}).get("judge_model", ""),
            controller_timeout=float(
                self.engine.config.get("workers", {}).get("judge_timeout_seconds", 300)))
        ranked = sorted(results, key=lambda item: (
            item.get("score", 0), item.get("status") == "completed", item.get("worker_id", "")),
                        reverse=True)
        for order, item in enumerate(
                (result for result in results if result.get("status") == "culled"), 1):
            self.engine.store.event(
                state.run_id, "worker_culled", worker_id=item["worker_id"],
                agentflow_node_id=item.get("agentflow_node_id"), score=item.get("score", 0),
                reason=item.get("cull_reason"), order=order)
        eligible = [item for item in ranked if item.get("status") != "culled"]
        retained = {item["worker_id"]
                    for item in eligible[:max(1, contract.pack.survivors)]}
        for item in results:
            item["retained"] = item["worker_id"] in retained
            atomic_json(round_dir / "scouts" / item["worker_id"] / "result.json", item)
        self.engine._transition(state, RunStatus.SYNTHESIZING)
        ranking = [{"worker_id": item["worker_id"], "score": item.get("score", 0),
                    "status": item.get("status"), "retained": item["worker_id"] in retained}
                   for item in ranked]
        atomic_json(round_dir / "pack-ranking.json", {
            "backend": "agentflow", "ranking": ranking, "retained": sorted(retained)})
        atomic_json(round_dir / "synthesis.json", self.synthesis(
            state.run_id, "agentflow", results, ranking, context))
        self.engine.store.event(
            state.run_id, "pack_completed", backend="agentflow",
            agentflow_run_id=next((item.get("agentflow_run_id") for item in results), None),
            retained=sorted(retained))
        self.engine.store.save_state(state)
        return results

    async def direct(self, state: RunState, contract: RunContract, round_dir: Path,
                     context: dict, writer: dict) -> list[dict]:
        running: list[dict[str, Any]] = []
        try:
            results = await self._direct_run(state, contract, round_dir, context, writer, running)
        finally:
            active_error = sys.exc_info()[1]
            cancel_path = self.engine.store.run_dir(state.run_id) / "cancel.requested"
            reason = "cancelled_by_user" if cancel_path.exists() else "cancelled_for_run_shutdown"
            cleanup = await asyncio.gather(
                *(item["process"].terminate(reason) for item in running),
                return_exceptions=True)
            cleanup_errors = [str(result) for result in cleanup if isinstance(result, BaseException)]
            if cleanup_errors:
                self.engine.store.event(state.run_id, "pack_cleanup_failed", errors=cleanup_errors)
            survivors = [item for item in running
                         if item["process"].process and item["process"].process.returncode is None]
            if survivors:
                table = windows_process_table() if os.name == "nt" else {}
                details = []
                for item in survivors:
                    pid = item["process"].process.pid
                    process = table.get(pid)
                    details.append(f"PID={pid} PPID={process.parent_pid if process else 'unknown'} "
                                   f"executable={process.executable if process else item['process'].argv[0]}")
                raise RuntimeError("direct Pack cleanup left live worker(s): " + "; ".join(details))
            if cleanup_errors and active_error is None:
                raise RuntimeError("direct Pack cleanup failed: " + "; ".join(cleanup_errors))
        if cancel_path.exists():
            self.engine._finish(state, RunStatus.CANCELLED, "cancelled during direct Pack")
            return []
        return results

    async def _direct_run(self, state: RunState, contract: RunContract, round_dir: Path,
                          context: dict, writer: dict,
                          running: list[dict[str, Any]]) -> list[dict]:
        self.engine._transition(state, RunStatus.SCOUTING)
        self.engine.store.save_state(state)
        missions = self.missions(state, contract, writer)
        scout_root = round_dir / "scouts"
        for index, mission in enumerate(missions, 1):
            spec = WorkerSpec(
                f"scout-{state.round}-{index}", "scout", mission, "read-only",
                float(self.engine.config.get("workers", {}).get("scout_timeout_seconds", 900)),
                self.engine.config.get("codex", {}).get("scout_model", ""))
            artifact = scout_root / spec.worker_id
            prompt = self.engine._checked_prompt(
                scout_prompt(contract.objective, mission, context))
            state.total_workers += 1
            self.engine.store.save_state(state)
            managed, final_path = await self.engine.adapter.start(
                spec, prompt, self.engine.workspace, artifact)
            running.append({"spec": spec, "process": managed, "final": final_path,
                            "artifact": artifact, "culled": False, "score": 0.0})
        self.engine.store.event(
            state.run_id, "pack_started", backend="direct",
            workers=[item["spec"].worker_id for item in running])
        observed_at = time.monotonic()
        cancel_path = self.engine.store.run_dir(state.run_id) / "cancel.requested"
        if await cancellable_sleep(contract.pack.minimum_runtime_seconds, cancel_path):
            return []
        self.engine._transition(state, RunStatus.CULLING)
        self.engine.store.save_state(state)
        decisions = 0
        judged = False
        observation_deadline = observed_at + max(
            contract.pack.minimum_runtime_seconds, contract.pack.judge_interval_seconds)
        while True:
            live = self._live(running)
            candidates = [item for item in running if not item["culled"]]
            if not live or len(candidates) <= contract.pack.survivors:
                break
            for item in candidates:
                text = self.worker_trace(item)
                item["score"] = deterministic_score(
                    text, item["process"].progress_events, item["spec"].mission)
            now = time.monotonic()
            if (any(item["process"].progress_events < contract.pack.minimum_progress_events
                    for item in live) and now < observation_deadline):
                if await cancellable_sleep(min(0.1, observation_deadline - now), cancel_path):
                    return []
                continue
            ranked = sorted(candidates, key=self._candidate_key)
            protected = ranked[-contract.pack.survivors:]
            cullable = [item for item in ranked if item in live and item not in protected]
            gap = self._cull_gap(protected, cullable)
            if (cullable and gap < contract.pack.minimum_score_gap and
                    contract.pack.semantic_judge and not judged and
                    now - observed_at >= contract.pack.judge_interval_seconds and
                    len(live) < contract.pack.concurrency and
                    state.total_workers + 2 < contract.budget.max_total_workers):
                semantic = await self.judge(state, contract, round_dir, ranked)
                judged = True
                live = self._live(running)
                candidates = [item for item in running if not item["culled"]]
                ranked = sorted(candidates, key=self._candidate_key)
                for item in ranked:
                    worker_id = item["spec"].worker_id
                    item["score"] = merge_semantic(item["score"], semantic.get(worker_id))
                ranked.sort(key=self._candidate_key)
                protected = ranked[-contract.pack.survivors:]
                cullable = [item for item in ranked if item in live and item not in protected]
                gap = self._cull_gap(protected, cullable)
            if not cullable or gap < contract.pack.minimum_score_gap:
                if time.monotonic() < observation_deadline:
                    if await cancellable_sleep(
                            min(0.1, observation_deadline - time.monotonic()), cancel_path):
                        return []
                    continue
                break
            victim = cullable[0]
            reason = "outside survivor rank after minimum observation"
            texts = [self.worker_trace(item) for item in candidates if item is not victim]
            if unique_evidence(self.worker_trace(victim), texts):
                alternatives = [item for item in cullable[1:] if not unique_evidence(
                    self.worker_trace(item), [self.worker_trace(other) for other in candidates
                                              if other is not item])]
                if not alternatives:
                    break
                victim = alternatives[0]
                reason = "lowest non-unique alternative; unique evidence protected"
            if self._cull_gap(protected, [victim]) < contract.pack.minimum_score_gap:
                if time.monotonic() < observation_deadline:
                    if await cancellable_sleep(
                            min(0.1, observation_deadline - time.monotonic()), cancel_path):
                        return []
                    continue
                break
            if cancel_path.exists():
                return []
            if victim["process"].process.returncode is not None:
                continue
            stopped = await victim["process"].terminate("culled_by_supervisor")
            if not stopped:
                continue
            victim["culled"] = True
            decisions += 1
            decision = CullDecision(victim["spec"].worker_id, victim["score"], reason,
                                    utc_now(), decisions)
            append_jsonl(self.engine.store.run_dir(state.run_id) /
                         "cull-decisions.jsonl", decision)
            self.engine.store.event(
                state.run_id, "worker_culled", worker_id=victim["spec"].worker_id,
                score=victim["score"])
            if await cancellable_sleep(contract.pack.cull_cooldown_seconds, cancel_path):
                return []
        results = await self._results(state, running)
        ranked_results = sorted(results, key=lambda item: (
            item.get("score", 0), item.get("status") == "completed", item.get("worker_id", "")),
                                reverse=True)
        retained = {item["worker_id"] for item in ranked_results[:contract.pack.survivors]}
        ranking = []
        for item in ranked_results:
            item["retained"] = item["worker_id"] in retained
            atomic_json(round_dir / "scouts" / item["worker_id"] / "result.json", item)
            ranking.append({"worker_id": item["worker_id"], "score": item.get("score", 0),
                            "status": item.get("status"), "retained": item["retained"]})
        self.engine._transition(state, RunStatus.SYNTHESIZING)
        atomic_json(round_dir / "pack-ranking.json", {
            "backend": "direct", "ranking": ranking, "retained": sorted(retained)})
        atomic_json(round_dir / "synthesis.json", self.synthesis(
            state.run_id, "direct", results, ranking, context))
        self.engine.store.event(
            state.run_id, "pack_completed", backend="direct", retained=sorted(retained))
        self.engine.store.save_state(state)
        return results

    @staticmethod
    def _live(running: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [item for item in running if not item["culled"] and
                item["process"].process.returncode is None]

    @staticmethod
    def _candidate_key(item: dict[str, Any]) -> tuple[float, bool, str]:
        return (item["score"], item["process"].process.returncode == 0,
                item["spec"].worker_id)

    @staticmethod
    def _cull_gap(protected: list[dict[str, Any]],
                  cullable: list[dict[str, Any]]) -> float:
        if not protected or not cullable:
            return 0
        return protected[0]["score"] - cullable[0]["score"]

    async def _results(self, state: RunState, running: list[dict[str, Any]]) -> list[dict]:
        results = []
        run_dir = self.engine.store.run_dir(state.run_id)
        cancel_path = run_dir / "cancel.requested"
        decisions = {item.get("worker_id"): item for item in read_jsonl(
            run_dir / "cull-decisions.jsonl")}
        for item in running:
            outcome = await item["process"].wait(
                item["spec"].timeout_seconds,
                self.engine.store.run_dir(state.run_id) / "cancel.requested")
            result = self.engine.adapter.finish(
                item["spec"], outcome, item["final"], item["artifact"])
            trace = self.worker_trace(item)
            status = ("culled" if item["culled"] else "timed_out" if outcome.timed_out else
                      "cancelled" if outcome.cancelled else
                      "completed" if outcome.exit_code == 0 and
                      result.get("parse_confidence") == 1.0 else "failed")
            result.update({
                "worker_id": item["spec"].worker_id, "backend": "direct", "status": status,
                "score": deterministic_score(
                    trace, item["process"].progress_events, item["spec"].mission),
                "mission": item["spec"].mission,
                "artifact_dir": item["artifact"].relative_to(run_dir).as_posix(),
                "artifact_sha256": await artifact_sha256_async(
                    item["artifact"], cancel_path) or "",
                "progress_events": item["process"].progress_events,
                "elapsed_seconds": round(max(
                    0, time.monotonic() - item["process"].started_monotonic), 3),
            })
            if status != "completed":
                result["partial_excerpt"] = _tail_bytes(trace, self.engine.trace_limit)
            if item["spec"].worker_id in decisions:
                result["cull_reason"] = decisions[item["spec"].worker_id].get("reason", "")
            atomic_json(item["artifact"] / "result.json", result)
            results.append(result)
        return results

    async def judge(self, state: RunState, contract: RunContract, round_dir: Path,
                    ranked: list[dict[str, Any]]) -> dict[str, float]:
        snapshots = []
        for item in ranked:
            trace = self.worker_trace(item)
            parsed = normalize_result("scout", trace)
            others = [self.worker_trace(other) for other in ranked if other is not item]
            snapshots.append({
                "worker_id": item["spec"].worker_id, "mission": item["spec"].mission,
                "live_score": item["score"],
                "recent_trace": _tail_bytes(trace, self.engine.trace_limit),
                "evidence": parsed.get("evidence", []),
                "commands": parsed.get("commands_run", []),
                "duplicate": not unique_evidence(trace, others),
                "elapsed_seconds": round(max(
                    0, time.monotonic() - item["process"].started_monotonic), 3),
            })
        spec = WorkerSpec(
            f"judge-{state.round}", "judge", "rank scout evidence", "read-only",
            float(self.engine.config.get("workers", {}).get("judge_timeout_seconds", 300)),
            self.engine.config.get("codex", {}).get("judge_model", ""))
        state.total_workers += 1
        self.engine.store.save_state(state)
        outcome, result = await self.engine.adapter.run(
            spec, self.engine._checked_prompt(judge_prompt(contract.objective, snapshots)),
            self.engine.workspace, round_dir / "judge",
            self.engine.store.run_dir(state.run_id) / "cancel.requested")
        result.update({
            "worker_id": spec.worker_id, "role": "judge", "round": state.round,
            "status": "timed_out" if outcome.timed_out else
                      "cancelled" if outcome.cancelled else
                      "completed" if outcome.exit_code == 0 else "failed",
            "artifact_dir": (round_dir / "judge").relative_to(
                self.engine.store.run_dir(state.run_id)).as_posix(),
            "artifact_sha256": await artifact_sha256_async(
                round_dir / "judge",
                self.engine.store.run_dir(state.run_id) / "cancel.requested") or "",
        })
        atomic_json(round_dir / "judge" / "result.json", result)
        self.engine.store.event(
            state.run_id, "worker_finished", worker_id=spec.worker_id,
            role="judge", exit_code=outcome.exit_code)
        if outcome.exit_code != 0 or outcome.timed_out or outcome.cancelled:
            return {}
        return {item["worker_id"]: float(item["semantic_score"])
                for item in result.get("ranking", [])
                if isinstance(item, dict) and item.get("worker_id") and
                isinstance(item.get("semantic_score"), (int, float)) and
                not isinstance(item.get("semantic_score"), bool) and
                math.isfinite(item["semantic_score"])}

    def worker_trace(self, item: dict[str, Any]) -> str:
        half = max(1, self.engine.trace_limit // 2)
        return "\n".join(filter(None, (
            _tail_file(item["artifact"] / "trace.jsonl", half),
            _tail_file(item["final"], half),
        )))

    def synthesis(self, run_id: str, backend: str, results: list[dict],
                  ranking: list[dict], context: dict) -> dict:
        recommendations: dict[str, str] = {}
        hypotheses: dict[str, Any] = {}
        scouts = []
        decisions = {item.get("worker_id"): item for item in read_jsonl(
            self.engine.store.run_dir(run_id) / "cull-decisions.jsonl")}
        for result in _unique_scouts(results):
            action = result.get("recommended_next_action")
            if isinstance(action, str) and action.strip():
                recommendations.setdefault(action.casefold(), action)
            for hypothesis in result.get("hypotheses", []):
                key = json.dumps(hypothesis, ensure_ascii=False, sort_keys=True)
                hypotheses.setdefault(key, hypothesis)
            worker_id = result.get("worker_id")
            scouts.append({name: result.get(name) for name in (
                "worker_id", "mission", "status", "score", "retained", "summary", "evidence",
                "hypotheses", "unique_contribution", "recommended_next_action", "artifact_dir",
                "artifact_sha256", "partial_excerpt") if name in result} |
                ({"cull_reason": decisions[worker_id].get("reason")} if worker_id in decisions else {}))
        return {"backend": backend, "current_git_diff": context.get("current_git_diff", []),
                "ranking": ranking, "scouts": scouts,
                "deduplicated_hypotheses": list(hypotheses.values()),
                "recommended_next_actions": list(recommendations.values())}

    def digest(self, rounds: list[dict]) -> dict:
        entries: list[tuple[int, str, Any]] = []
        seen: set[str] = set()
        for round_ in reversed(rounds):
            number = round_.get("number", 0)
            ranking = round_.get("pack_ranking", [])
            if ranking:
                entries.append((0, f"round-{number}-ranking", {
                    "round": number, "ranking": ranking}))
            for scout in _unique_scouts(round_.get("scouts", [])):
                if not isinstance(scout, dict):
                    continue
                signal = _scout_signal(scout)
                if signal in seen:
                    continue
                seen.add(signal)
                worker_id = str(scout.get("worker_id", "scout"))
                entries.append((1, f"round-{number}-{worker_id}", {
                    "round": number, **{name: scout.get(name) for name in (
                        "worker_id", "mission", "status", "score", "retained", "summary",
                        "evidence", "hypotheses", "unique_contribution",
                        "recommended_next_action", "artifact_dir", "artifact_sha256", "partial_excerpt",
                        "cull_reason")
                        if name in scout}}))
        kept, omitted = bounded_context(entries, max(1024, self.engine.context_limit // 3))
        return {"items": list(kept.values()), "omitted": omitted}


def _scout_signal(result: dict) -> str:
    evidence = result.get("evidence", [])
    hypotheses = result.get("hypotheses", [])
    material = [evidence, hypotheses] if evidence or hypotheses else [
        result.get("partial_excerpt", ""), result.get("summary", "")]
    return " ".join(json.dumps(material, ensure_ascii=False, sort_keys=True).casefold().split())


def _unique_scouts(results: list[dict]) -> list[dict]:
    ordered = sorted((item for item in results if isinstance(item, dict)), key=lambda item: (
        bool(item.get("retained")), item.get("score", 0),
        item.get("status") == "completed", str(item.get("worker_id", ""))), reverse=True)
    seen: set[str] = set()
    unique = []
    for item in ordered:
        signal = _scout_signal(item)
        if signal not in seen:
            seen.add(signal)
            unique.append(item)
    return unique


async def cancellable_sleep(seconds: float, cancel_path: Path) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if cancel_path.exists():
            return True
        await asyncio.sleep(min(0.1, deadline - time.monotonic()))
    return cancel_path.exists()


def _tail_bytes(text: str, maximum: int) -> str:
    raw = text.encode()
    return raw[-maximum:].decode("utf-8", errors="replace") if len(raw) > maximum else text


def _tail_file(path: Path, maximum: int) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        handle.seek(max(0, handle.tell() - maximum))
        return handle.read().decode("utf-8", errors="replace")
