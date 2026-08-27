from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from .adapter import WorkerAdapter
from .agentflow import AgentFlowPack
from .context import bounded_context
from .evidence import (content_identity_async as _content_identity,
                       evidence_tokens as _evidence_tokens,
                       followed_scheduled_action as _followed_scheduled_action,
                       round_evidence_hash as _round_evidence_hash,
                       writer_fingerprints as _writer_fingerprints)
from .models import (HoundEnvironmentError, RoundState, RunContract, RunState, RunStatus,
                     WorkerSpec, dump, transition, validate_contract)
from .pack import PackController
from .policy import should_pack, terminal
from .prompts import scout_prompt, verifier_prompt, writer_prompt
from .recovery import (build_resume_context, elapsed_since as _elapsed_since,
                       guard_other_run_writers,
                       recover_interrupted, recover_interrupted_async,
                       save_round_checkpoint)
from .review import (resume_review, review_result,
                     verifier_failure as _verifier_failure)
from .store import RunStore, artifact_sha256_async, atomic_json, read_jsonl, utc_now
from .verification import (capture_git_baseline, changed_paths_since_baseline,
                           path_gate, verification_signature,
                           verify_commands)


class Engine:
    def __init__(self, workspace: Path, adapter: WorkerAdapter, config: dict | None = None):
        self.workspace = workspace.resolve()
        self.store = RunStore(self.workspace)
        self.adapter = adapter
        self.config = config or {}
        agentflow = self.config.get("agentflow", {})
        self.agentflow = AgentFlowPack(agentflow.get("executable", "agentflow"))
        self.pack = PackController(self)
        context = self.config.get("context", {})
        self.context_limit = int(context.get("maximum_prompt_bytes", 65536))
        self.trace_limit = int(context.get("maximum_trace_excerpt_bytes", 8192))
        self.recent_rounds = int(context.get("retain_recent_rounds", 4))
        self.baseline: dict[str, Any] = {}

    def new_run(self, contract: RunContract) -> RunState:
        validate_contract(contract)
        if Path(contract.workspace).resolve() != self.workspace:
            raise ValueError("contract workspace does not match Engine workspace")
        now = utc_now()
        stamp = now[:19].replace("-", "").replace(":", "").replace("T", "-")
        state = RunState(f"{stamp}-{uuid.uuid4().hex[:8]}", RunStatus.CREATED, now, now)
        self.store.create(state, contract)
        return state

    async def run(self, state: RunState, contract: RunContract, resume: bool = False) -> RunState:
        validate_contract(contract)
        if Path(contract.workspace).resolve() != self.workspace:
            raise ValueError("contract workspace does not match Engine workspace")
        with self.store.workspace_lock():
            guard_other_run_writers(self.store.runs, state.run_id)
            prior_active = state.active_seconds + _elapsed_since(state.active_started_at)
            started = time.monotonic()
            cancel_path = self.store.run_dir(state.run_id) / "cancel.requested"

            def finish_startup(status: RunStatus, message: str) -> RunState:
                state.active_seconds = prior_active + time.monotonic() - started
                state.active_started_at = ""
                return self._finish(state, status, message)

            if resume and not terminal(state.status.value):
                if cancel_path.exists():
                    return finish_startup(RunStatus.CANCELLED, "cancel requested")
                remaining = (contract.budget.max_wall_minutes * 60 - prior_active -
                             (time.monotonic() - started))
                if remaining <= 0:
                    return finish_startup(
                        RunStatus.BUDGET_EXHAUSTED, "wall-clock budget exhausted")
                try:
                    async with asyncio.timeout(remaining):
                        recovered = await recover_interrupted_async(
                            self, state, cancel_path)
                except TimeoutError:
                    if cancel_path.exists():
                        return finish_startup(
                            RunStatus.CANCELLED, "cancelled during interrupted recovery")
                    return finish_startup(
                        RunStatus.BUDGET_EXHAUSTED, "wall-clock budget exhausted")
                if not recovered or cancel_path.exists():
                    return finish_startup(
                        RunStatus.CANCELLED, "cancelled during interrupted recovery")
            state.active_seconds = prior_active
            state.active_started_at = utc_now()
            self.store.save_state(state)
            try:
                if state.status is RunStatus.CREATED:
                    transition(state, RunStatus.BASELINE)
                    self.store.save_state(state)
                if cancel_path.exists():
                    return self._finish(state, RunStatus.CANCELLED, "cancel requested")
                remaining = (contract.budget.max_wall_minutes * 60 - prior_active -
                             (time.monotonic() - started))
                if remaining <= 0:
                    return self._finish(
                        state, RunStatus.BUDGET_EXHAUSTED, "wall-clock budget exhausted")
                baseline_path = self.store.run_dir(state.run_id) / "baseline.json"
                if baseline_path.exists():
                    value = json.loads(baseline_path.read_text(encoding="utf-8"))
                    if not isinstance(value, dict):
                        raise ValueError("baseline must be a JSON object")
                    self.baseline = value
                elif not terminal(state.status.value):
                    try:
                        async with asyncio.timeout(remaining):
                            self.baseline = await capture_git_baseline(
                                self.workspace, baseline_path, cancel_path,
                                include_ignored=bool(
                                    contract.allowed_paths or contract.forbidden_paths))
                    except TimeoutError:
                        if cancel_path.exists():
                            return self._finish(
                                state, RunStatus.CANCELLED, "cancelled during baseline capture")
                        self._mark_budget_terminations(
                            state, baseline_path.parent / f"{baseline_path.stem}-git")
                        return self._finish(
                            state, RunStatus.BUDGET_EXHAUSTED, "wall-clock budget exhausted")
                if resume and not terminal(state.status.value):
                    if cancel_path.exists():
                        return self._finish(state, RunStatus.CANCELLED, "cancel requested")
                    remaining = (contract.budget.max_wall_minutes * 60 - prior_active -
                                 (time.monotonic() - started))
                    if remaining <= 0:
                        return self._finish(
                            state, RunStatus.BUDGET_EXHAUSTED, "wall-clock budget exhausted")
                    try:
                        async with asyncio.timeout(remaining):
                            await self._resume_review(state, contract)
                    except TimeoutError:
                        if cancel_path.exists():
                            return self._finish(
                                state, RunStatus.CANCELLED, "cancelled during final review resume")
                        self._mark_budget_terminations(state)
                        return self._finish(
                            state, RunStatus.BUDGET_EXHAUSTED, "wall-clock budget exhausted")
                while not terminal(state.status.value):
                    if cancel_path.exists():
                        return self._finish(state, RunStatus.CANCELLED, "cancel requested")
                    reason = self._round_terminal_reason(state, contract)
                    if reason:
                        return self._finish(state, RunStatus.BLOCKED, reason)
                    remaining = contract.budget.max_wall_minutes * 60 - prior_active - (time.monotonic() - started)
                    if remaining <= 0:
                        return self._finish(state, RunStatus.BUDGET_EXHAUSTED, "wall-clock budget exhausted")
                    if state.round >= contract.budget.max_rounds:
                        return self._finish(state, RunStatus.BUDGET_EXHAUSTED, "round budget exhausted")
                    if state.total_workers >= contract.budget.max_total_workers:
                        return self._finish(state, RunStatus.BUDGET_EXHAUSTED, "worker budget exhausted")
                    try:
                        async with asyncio.timeout(remaining):
                            await self._round(state, contract)
                    except TimeoutError:
                        if cancel_path.exists():
                            return self._finish(
                                state, RunStatus.CANCELLED, "cancelled during run timeout")
                        self._mark_budget_terminations(state)
                        return self._finish(state, RunStatus.BUDGET_EXHAUSTED, "wall-clock budget exhausted")
                return state
            finally:
                state.active_seconds = prior_active + time.monotonic() - started
                state.active_started_at = ""
                self.store.save_state(state)

    async def _round(self, state: RunState, contract: RunContract) -> None:
        state.round += 1
        number = state.round
        round_dir = self.store.run_dir(state.run_id) / "rounds" / f"{number:03d}"
        cancel_path = self.store.run_dir(state.run_id) / "cancel.requested"
        self._transition(state, RunStatus.WORKING)
        self.store.event(state.run_id, "round_started", round=number)
        self.store.save_state(state)
        resume_path = round_dir / "writer-resume.json"
        recovered = json.loads(resume_path.read_text(encoding="utf-8")) \
            if resume_path.exists() else None
        if recovered is not None and (not isinstance(recovered, dict) or
                                      recovered.get("schema_version") != 1 or
                                      not isinstance(recovered.get("writer"), dict) or
                                      not isinstance(recovered.get("intent"), dict)):
            raise ValueError("invalid resumed Writer checkpoint")
        intent = recovered.get("intent", {}) if recovered else {}
        git_diff = (intent["git_diff"] if recovered else
                    await changed_paths_since_baseline(
                        self.workspace, self.baseline, round_dir / "pre-writer-git", cancel_path))
        if self._cancelled(state, cancel_path, "cancelled during change inspection"):
            return
        context, omitted = self._context(state, contract, git_diff or [])
        atomic_json(round_dir / "context.json", {"included": context, "omitted": omitted})
        pre_writer_identity = (intent["git_identity"] if recovered else
                               await _content_identity(self.workspace, git_diff, cancel_path) or "")
        if self._cancelled(state, cancel_path, "cancelled during content inspection"):
            return
        evidence_before = (intent["evidence_hash"] if recovered else _round_evidence_hash(
            state, {}, [], [], git_diff=git_diff or [], git_identity=pre_writer_identity))
        scheduled = (intent["scheduled_fingerprint"] if recovered else
                     state.rounds[-1].get("next_fingerprint", "") if state.rounds else "")
        refused = bool(not recovered and scheduled and any(
            item.get("fingerprint") == scheduled and item.get("status") == "failed" and
            item.get("evidence_hash") == evidence_before
            for item in self.store.fingerprints(state.run_id)))
        if recovered:
            writer = recovered["writer"]
        elif refused:
            writer = {
                "status": "continue", "summary": "repeated failed action refused before execution",
                "evidence": [], "changed_files": [], "commands_run": [],
                "verification_observed": [], "next_action": "collect new evidence before retrying",
                "parallel_request": {"needed": True, "reason": "failed fingerprint repeated",
                                     "missions": ["find evidence for a different safe strategy",
                                                  "disprove the failed strategy"]},
                "blocker": "identical failed action without new evidence",
                "refused_fingerprint": scheduled,
            }
            self.store.event(state.run_id, "action_refused", fingerprint=scheduled,
                             reason="failed fingerprint repeated without new evidence")
        else:
            atomic_json(round_dir / "writer-intent.json", {
                "schema_version": 1, "round": number,
                "evidence_hash": evidence_before,
                "scheduled_fingerprint": scheduled,
                "git_diff": git_diff or [], "git_identity": pre_writer_identity,
            })
            writer = await self._writer(state, contract, round_dir, context)
            if self._cancelled(state, cancel_path, "cancelled during writer execution"):
                return
        verification = []
        issues: list[str] = []
        if refused:
            issues.append("writer action refused: failed fingerprint repeated without new evidence")
        else:
            if contract.verify:
                self._transition(state, RunStatus.VERIFYING)
                self.store.save_state(state)
                verification = await verify_commands(
                    contract.verify, self.workspace, round_dir / "verification",
                    contract.verify_timeout_seconds, contract.expected_exit_code, cancel_path)
                if self._cancelled(state, cancel_path, "cancelled during machine verification"):
                    return
            issues = await path_gate(
                self.workspace, contract.required_files, contract.allowed_paths, contract.forbidden_paths,
                round_dir / "path-gate", cancel_path, baseline=self.baseline)
            if self._cancelled(state, cancel_path, "cancelled during path verification"):
                return
            if not contract.verify:
                issues.insert(0, "at least one machine verification command is required")
        passed = (not refused and bool(contract.verify) and
                  all(v.passed for v in verification) and not issues)
        signature = ""
        previous_signature = state.last_verification_signature
        if not refused:
            signature = (verification_signature(verification, round_dir / "verification")
                         if verification else "no-verification")
            if not passed and signature == state.last_verification_signature:
                state.repeated_verification_failures += 1
            elif not passed:
                state.repeated_verification_failures = 1
            else:
                state.repeated_verification_failures = 0
            state.last_verification_signature = signature
        post_writer_git = await changed_paths_since_baseline(
            self.workspace, self.baseline, round_dir / "post-writer-git", cancel_path)
        if self._cancelled(state, cancel_path, "cancelled during change inspection"):
            return
        post_writer_identity = await _content_identity(
            self.workspace, post_writer_git, cancel_path) or ""
        if self._cancelled(state, cancel_path, "cancelled during content inspection"):
            return
        evidence_hash = _round_evidence_hash(
            state, writer, verification, issues,
            verification_signature=signature, git_diff=post_writer_git or [],
            git_identity=post_writer_identity)
        executed, next_fingerprint = _writer_fingerprints(
            state, contract.objective, contract.verify, writer, scheduled)
        next_action = writer.get("next_action", "")
        repeated_next = bool(next_fingerprint and state.rounds and
                             next_fingerprint == state.rounds[-1].get("next_fingerprint"))
        idle = (refused or evidence_hash == evidence_before or
                post_writer_identity == pre_writer_identity or
                bool(previous_signature and signature == previous_signature) or
                repeated_next or not next_fingerprint)
        state.idle_rounds = state.idle_rounds + 1 if idle else 0
        scouts: list[dict] = []
        pack_ran = False
        fingerprint_record = (None if refused else {
            "fingerprint": executed, "status": "passed" if passed else "failed",
            "evidence_hash": evidence_hash})
        checkpoint = dump(RoundState(
            number=number, status="verified" if passed else "continue", writer=writer,
            verification=verification, verification_signature=signature,
            path_issues=issues, scouts=scouts, pack_evidence=False, idle=idle,
            fingerprint=executed, next_fingerprint=next_fingerprint,
            scheduled_action=next_action if isinstance(next_action, str) else "",
            evidence_hash=evidence_hash, git_diff=post_writer_git or [],
            git_identity=post_writer_identity))
        save_round_checkpoint(self, state, checkpoint, fingerprint_record)
        self.store.save_state(state)
        if fingerprint_record:
            self.store.fingerprint(state.run_id, executed, fingerprint_record["status"], evidence_hash)
        if ((not passed or writer.get("status") != "candidate_done" or
             state.repeated_verifier_failures >= 2) and
                state.round < contract.budget.max_rounds and
                should_pack(contract.pack, state, writer) and
                state.total_workers + 2 < contract.budget.max_total_workers):
            pack_values = {**context,
                           "current_verification": [dump(item) for item in verification],
                           "current_path_issues": issues,
                           "current_verification_signature": signature,
                           "current_git_diff": post_writer_git or []}
            pack_context, pack_omitted = bounded_context([
                (1 if name in {"objective", "acceptance", "current_verification",
                               "current_path_issues"} else 3, name, value)
                for name, value in pack_values.items()
            ], max(512, self.context_limit - len(
                scout_prompt(contract.objective, "", {}).encode()) - 1024))
            atomic_json(round_dir / "pack-context.json", {
                "included": pack_context, "omitted": pack_omitted})
            pack_ran = True
            scouts = await self._pack(state, contract, round_dir, pack_context, writer)
            if terminal(state.status.value):
                return
            if self._cancelled(state, cancel_path, "cancelled during Pack execution"):
                return
        final_evidence_hash = _round_evidence_hash(
            state, writer, verification, issues, scouts=scouts,
            verification_signature=signature,
            git_diff=post_writer_git or [],
            git_identity=post_writer_identity)
        scout_evidence = final_evidence_hash != evidence_hash
        if scout_evidence:
            state.idle_rounds = 0
        post_git = (await changed_paths_since_baseline(
            self.workspace, self.baseline, round_dir / "post-pack-git", cancel_path)
                    if pack_ran else post_writer_git)
        if self._cancelled(state, cancel_path, "cancelled during change inspection"):
            return
        post_git_identity = await _content_identity(
            self.workspace, post_git, cancel_path) or ""
        if self._cancelled(state, cancel_path, "cancelled during content inspection"):
            return
        ranking_path = round_dir / "pack-ranking.json"
        ranking = json.loads(ranking_path.read_text(encoding="utf-8")).get("ranking", []) \
            if ranking_path.exists() else []
        round_state = dump(RoundState(
            number=number, status="verified" if passed else "continue", writer=writer,
            verification=verification, verification_signature=signature,
            path_issues=issues, scouts=scouts,
            pack_evidence=scout_evidence, idle=idle, fingerprint=executed,
            next_fingerprint=next_fingerprint,
            scheduled_action=next_action if isinstance(next_action, str) else "",
            evidence_hash=final_evidence_hash,
            git_diff=post_git or [],
            git_identity=post_git_identity, pack_ranking=ranking))
        save_round_checkpoint(self, state, round_state, fingerprint_record)
        state.rounds.append(round_state)
        self.store.save_state(state)
        if passed and writer.get("status") == "candidate_done":
            if self._cancelled(state, cancel_path, "cancelled before final review"):
                return
            if state.total_workers >= contract.budget.max_total_workers:
                self._finish(state, RunStatus.BUDGET_EXHAUSTED, "worker budget exhausted before final Verifier")
                return
            atomic_json(self.store.run_dir(state.run_id) / "final" / "verification.json", {
                "round": number, "passed": True, "machine": [dump(v) for v in verification],
                "path_issues": issues, "git_diff": post_git or [],
            })
            verifier = await self._verifier(state, contract, round_dir, round_state)
            round_state["verifier"] = verifier
            self.store.save_state(state)
            if not self._cancelled(
                    state, self.store.run_dir(state.run_id) / "cancel.requested",
                    "cancelled during final review"):
                self._review_result(state, verifier)
        reason = self._round_terminal_reason(state, contract)
        if reason and not terminal(state.status.value):
            self._finish(state, RunStatus.BLOCKED, reason)

    @staticmethod
    def _round_terminal_reason(state: RunState, contract: RunContract) -> str:
        if not state.rounds:
            return ""
        current = state.rounds[-1]
        writer = current.get("writer", {})
        no_pack_evidence = not current.get("pack_evidence")
        if writer.get("refused_fingerprint") and no_pack_evidence:
            return "failed action fingerprint repeated without new evidence"
        next_fingerprint = current.get("next_fingerprint", "")
        repeated_next = bool(next_fingerprint and len(state.rounds) > 1 and
                             next_fingerprint == state.rounds[-2].get("next_fingerprint"))
        if (writer.get("status") == "blocked" and no_pack_evidence and
                (not next_fingerprint or repeated_next)):
            return writer.get("blocker") or "no safe next action"
        if state.idle_rounds >= contract.budget.max_idle_rounds and no_pack_evidence:
            return "idle budget exhausted without new evidence"
        return ""

    async def _writer(self, state: RunState, contract: RunContract, round_dir: Path, context: dict) -> dict:
        workers = self.config.get("workers", {})
        model = self.config.get("codex", {}).get("writer_model", "")
        spec = WorkerSpec(f"writer-{state.round}", "writer", contract.objective, "workspace-write",
                          float(workers.get("writer_timeout_seconds", 3600)), model)
        prompt = self._checked_prompt(writer_prompt(contract.objective, context))
        artifact = round_dir / "writer"
        state.total_workers += 1
        self.store.save_state(state)
        outcome, result = await self.adapter.run(
            spec, prompt, self.workspace, artifact,
            self.store.run_dir(state.run_id) / "cancel.requested")
        if outcome.exit_code != 0 or outcome.timed_out or outcome.cancelled:
            reason = ("cancelled" if outcome.cancelled else "timed out" if outcome.timed_out else
                      f"failed with exit {outcome.exit_code}")
            result["status"] = "continue"
            result["summary"] = f"writer process {reason}: {result.get('summary', '')}"
        result.update({
            "artifact_dir": artifact.relative_to(self.store.run_dir(state.run_id)).as_posix(),
            "artifact_sha256": await artifact_sha256_async(
                artifact, self.store.run_dir(state.run_id) / "cancel.requested") or "",
        })
        atomic_json(artifact / "result.json", result)
        self.store.event(state.run_id, "worker_finished", worker_id=spec.worker_id,
                         role="writer", exit_code=outcome.exit_code)
        return result

    def _pack_missions(self, state: RunState, contract: RunContract, writer: dict) -> list[str]:
        return self.pack.missions(state, contract, writer)

    async def _pack(self, state: RunState, contract: RunContract, round_dir: Path,
                    context: dict, writer: dict) -> list[dict]:
        return await self.pack.run(state, contract, round_dir, context, writer)

    async def _pack_direct(self, state: RunState, contract: RunContract, round_dir: Path,
                           context: dict, writer: dict) -> list[dict]:
        return await self.pack.direct(state, contract, round_dir, context, writer)

    async def _verifier(self, state: RunState, contract: RunContract, round_dir: Path, context: dict) -> dict:
        if state.status is RunStatus.WORKING:
            self._transition(state, RunStatus.VERIFYING)
        self._transition(state, RunStatus.REVIEWING)
        self.store.save_state(state)
        spec = WorkerSpec(f"verifier-{state.round}", "verifier", contract.objective, "read-only",
                          float(self.config.get("workers", {}).get("verifier_timeout_seconds", 900)),
                          self.config.get("codex", {}).get("verifier_model", ""))
        state.total_workers += 1
        self.store.save_state(state)
        review_context, omitted = bounded_context([
            (1, "verification", context.get("verification", [])),
            (2, "path_issues", context.get("path_issues", [])),
            (3, "git_diff", context.get("git_diff", [])),
            (4, "writer", context.get("writer", {})),
            (5, "pack_digest", self.pack.digest([context])),
        ], max(512, self.context_limit - len(verifier_prompt(contract.objective, {}).encode())))
        review_context["omitted"] = omitted
        attempt = round_dir / "verifier"
        outcome, result = await self.adapter.run(
            spec, self._checked_prompt(verifier_prompt(contract.objective, review_context)),
            self.workspace, attempt,
            self.store.run_dir(state.run_id) / "cancel.requested")
        if outcome.exit_code != 0 or outcome.timed_out or outcome.cancelled:
            reason = ("cancelled" if outcome.cancelled else "timed out" if outcome.timed_out else
                      f"exit {outcome.exit_code}")
            result = _verifier_failure(f"verifier {reason}")
            result.update({"exit_code": outcome.exit_code, "timed_out": outcome.timed_out,
                           "cancelled": outcome.cancelled})
        result.update({
            "artifact_dir": attempt.relative_to(self.store.run_dir(state.run_id)).as_posix(),
            "artifact_sha256": await artifact_sha256_async(
                attempt, self.store.run_dir(state.run_id) / "cancel.requested") or "",
        })
        atomic_json(attempt / "result.json", result)
        atomic_json(self.store.run_dir(state.run_id) / "final" / "verifier.json",
                    {"round": state.round, **result})
        return result

    async def _resume_review(self, state: RunState, contract: RunContract) -> None:
        await resume_review(self, state, contract)

    def _review_result(self, state: RunState, verifier: dict) -> None:
        review_result(self, state, verifier)

    def _transition(self, state: RunState, status: RunStatus) -> None:
        if state.status is not status:
            transition(state, status)

    def _cancelled(self, state: RunState, path: Path, message: str) -> bool:
        if not path.exists():
            return False
        self._finish(state, RunStatus.CANCELLED, message)
        return True

    def _mark_budget_terminations(self, state: RunState, scope: Path | None = None) -> None:
        scope = scope or self.store.run_dir(state.run_id) / "rounds" / f"{state.round:03d}"
        for path in scope.glob("**/termination.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(record, dict) and record.get("reason") == "cancelled_for_run_shutdown":
                record["reason"] = "cancelled_for_budget"
                atomic_json(path, record)

    def _checked_prompt(self, prompt: str) -> str:
        size = len(prompt.encode())
        if size > self.context_limit:
            raise HoundEnvironmentError(
                f"worker prompt is {size} bytes; configured maximum is {self.context_limit}")
        return prompt

    def _context(self, state: RunState, contract: RunContract,
                 git_diff: list[str] | None = None) -> tuple[dict, list[str]]:
        recent = state.rounds[-self.recent_rounds:]
        older = state.rounds[:-self.recent_rounds]
        old_items = []
        for round_ in reversed(older):
            old_writer = round_.get("writer", {})
            if old_writer.get("artifact_dir") and old_writer.get("artifact_sha256"):
                old_items.append((0, f"{round_.get('number', 0)}-writer", {
                    "round": round_.get("number"), "artifact_dir": old_writer["artifact_dir"],
                    "artifact_sha256": old_writer["artifact_sha256"]}))
            for index, token in enumerate(sorted(_evidence_tokens([round_]))):
                old_items.append((0, f"{round_.get('number', 0)}-{index}",
                                  {"round": round_.get("number"), "evidence": token}))
        old_context, old_omitted = bounded_context(
            old_items, max(1024, self.context_limit // 5))
        latest_values: list[Any] = list(sorted(_evidence_tokens(recent[-1:])))
        latest_writer = recent[-1].get("writer", {}) if recent else {}
        if latest_writer.get("artifact_dir") and latest_writer.get("artifact_sha256"):
            latest_values.insert(0, {name: latest_writer[name] for name in
                                     ("artifact_dir", "artifact_sha256")})
        latest_items = [(0, str(index), value) for index, value in enumerate(latest_values, 1)]
        latest_context, latest_omitted = bounded_context(
            latest_items, max(1024, self.context_limit // 5))
        summaries = [{
            "number": round_.get("number"), "status": round_.get("status"),
            "writer": {name: round_.get("writer", {}).get(name) for name in
                       ("status", "summary", "next_action", "artifact_dir", "artifact_sha256")},
            "verifier": {name: round_.get("verifier", {}).get(name) for name in
                         ("verdict", "blocking_issues", "unverified_claims",
                          "required_next_action", "artifact_dir", "artifact_sha256")
                         if name in round_.get("verifier", {})},
            "path_issues": round_.get("path_issues", []), "idle": round_.get("idle", False),
        } for round_ in recent]
        items: list[tuple[int, str, Any]] = [
            (1, "objective", contract.objective),
            (1, "acceptance", {"verify": contract.verify,
                                "required_files": contract.required_files,
                                "allowed_paths": contract.allowed_paths,
                                "forbidden_paths": contract.forbidden_paths,
                                "expected_exit_code": contract.expected_exit_code,
                                "verify_timeout_seconds": contract.verify_timeout_seconds}),
            (2, "last_verification", recent[-1].get("verification", []) if recent else []),
            (2, "last_verifier", recent[-1].get("verifier", {}) if recent else {}),
            (3, "latest_evidence", list(latest_context.values())),
            (4, "current_git_diff", git_diff or []),
            (5, "forbidden_fingerprints", [x["fingerprint"] for x in
                 self.store.fingerprints(state.run_id) if x["status"] == "failed"][-20:]),
            (6, "recent_rounds", summaries),
            (7, "pack_digest", self.pack.digest(recent)),
            (8, "older_evidence", list(old_context.values())),
        ]
        recovery_path = self.store.run_dir(state.run_id) / "resume-context.json"
        if recovery_path.exists():
            recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
            if not isinstance(recovery, dict):
                raise ValueError("resume context must be an object")
            if recovery.get("recovered_round") == state.round - 1:
                items.append((3, "resume_recovery", recovery))
        culls = read_jsonl(self.store.run_dir(state.run_id) / "cull-decisions.jsonl")[-20:]
        if culls:
            items.append((8, "cull_decisions", culls))
        overhead = len(writer_prompt(contract.objective, {}).encode())
        context, omitted = bounded_context(items, max(512, self.context_limit - overhead))
        nested = ([f"latest_evidence.{name}" for name in latest_omitted] +
                  [f"older_evidence.{name}" for name in old_omitted])
        return context, nested + omitted

    def _resume_context(self, state: RunState) -> dict:
        return build_resume_context(self, state)

    def _recover_interrupted(self, state: RunState) -> None:
        recover_interrupted(self, state)

    def _finish(self, state: RunState, status: RunStatus, message: str) -> RunState:
        if terminal(state.status.value):
            return state
        summary = f"# Hound run {state.run_id}\n\nStatus: {status.value}\n\n{message}\n"
        (self.store.run_dir(state.run_id) / "summary.md").write_text(summary, encoding="utf-8")
        self.store.event(state.run_id, "run_finished", status=status.value, message=message)
        completed = replace(state, message=message)
        transition(completed, status)
        self.store.save_state(completed)
        state.status, state.message, state.updated_at = status, message, completed.updated_at
        return state
