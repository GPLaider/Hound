from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

from .models import CullDecision, HoundEnvironmentError
from .process import (codex_environment, redact, redact_artifacts_async,
                      resolve_executable, run_process, secret_values,
                      worker_environment)
from .prompts import scout_prompt
from .scoring import deterministic_score
from .store import append_jsonl, artifact_sha256_async, atomic_json, read_jsonl, utc_now
from .structured import last_json_object, matches_output_schema, normalize_result


class AgentFlowPackError(HoundEnvironmentError):
    pass


class AgentFlowPack:
    """Invoke installed AgentFlow as an optional read-only Pack runtime."""

    def __init__(self, executable: str = "agentflow", prefix: list[str] | None = None):
        self.executable, self.prefix = executable, prefix

    @staticmethod
    def _native_member_culling() -> bool:
        return os.name != "nt"

    def available(self) -> bool:
        return bool(self.prefix or resolve_executable(self.executable, Path.cwd()))

    async def run(self, objective: str, missions: list[str], context: dict,
                  workspace: Path, round_dir: Path, concurrency: int,
                  timeout: float, model: str = "", cancel_path: Path | None = None,
                  maximum_prompt_bytes: int = 65536,
                  maximum_trace_bytes: int = 8192,
                  culling_policy: dict | None = None,
                  controller_model: str = "",
                  controller_timeout: float | None = None) -> list[dict]:
        root = round_dir / "agentflow"
        pipeline_path = root / "pipeline.json"
        runs_dir = root / "runs"
        node_timeout = max(1, int(timeout) + 60)  # Hound owns timeout/cancellation first.
        members = []
        worker_map: dict[str, str] = {}
        suffix_width = max(1, len(str(len(missions))))
        for index, mission in enumerate(missions, 1):
            worker_id = f"scout-{round_dir.name.lstrip('0') or '0'}-{index}"
            prompt = redact(scout_prompt(objective, mission, context))
            if len(prompt.encode()) > maximum_prompt_bytes:
                raise AgentFlowPackError(
                    f"Scout prompt exceeds configured {maximum_prompt_bytes}-byte maximum")
            artifact = round_dir / "scouts" / worker_id
            artifact.mkdir(parents=True, exist_ok=True)
            (artifact / "prompt.txt").write_text(prompt, encoding="utf-8", newline="\n")
            (artifact / "prompt.sha256").write_text(
                hashlib.sha256(prompt.encode()).hexdigest(), encoding="ascii")
            node_id = f"scouts_{str(index - 1).zfill(suffix_width)}"
            worker_map[worker_id] = node_id
            members.append({"worker_id": worker_id, "mission": mission, "prompt": prompt})
        scout = {
            "id": "scouts", "agent": "codex", "prompt": "{{ item.prompt }}",
            "tools": "read_only", "capture": "trace", "timeout_seconds": node_timeout,
            "retries": 1, "fanout": {"values": members},
            "extra_args": (["--ignore-user-config", "--ephemeral", "-c",
                            'windows.sandbox="elevated"']
                           if os.name == "nt" else ["--ignore-user-config", "--ephemeral"]),
        }
        if model:
            scout["model"] = model
        nodes = [scout]
        controller_enabled = bool(culling_policy and self._native_member_culling())
        if controller_enabled:
            interval = max(1, int(math.ceil(max(
                culling_policy.get("minimum_runtime_seconds", 0),
                culling_policy.get("judge_interval_seconds", 0),
                culling_policy.get("cooldown_seconds", 0)))))
            controller = {
                "id": "scout_controller", "agent": "codex", "tools": "read_only",
                "capture": "trace",
                "timeout_seconds": max(1, int(controller_timeout or timeout)), "retries": 0,
                "schedule": {"every_seconds": interval,
                             "until_fanout_settles_from": "scouts",
                             "actuation": "output_json"},
                "prompt": self._controller_prompt(culling_policy, interval),
                "extra_args": scout["extra_args"],
            }
            selected_model = controller_model or model
            if selected_model:
                controller["model"] = selected_model
            nodes.append(controller)
        pipeline = {
            "name": f"hound-pack-{round_dir.name}",
            "description": "Hound-requested parallel read-only evidence hunt",
            "working_dir": str(workspace.resolve()),
            "concurrency": max(1, min(concurrency, len(members))),
            "fail_fast": False,
            "use_worktree": False,
            "nodes": nodes,
        }
        atomic_json(pipeline_path, pipeline)
        atomic_json(root / "missions.json", {
            "missions": missions, "worker_map": worker_map,
            "culling_policy": culling_policy or {}})
        atomic_json(root / "worker-map.json", worker_map)
        argv = [*(self.prefix or [self.executable]), "run", str(pipeline_path),
                "--runs-dir", str(runs_dir), "--max-concurrent-runs", "1",
                "--output", "json", "--preflight", "never"]
        environment = worker_environment() if self.prefix else codex_environment()
        try:
            try:
                outcome = await run_process(
                    argv, workspace, root / "launcher", timeout,
                    env=environment, cancel_path=cancel_path)
            except OSError as error:
                raise AgentFlowPackError(f"AgentFlow Pack could not start: {error}") from error
        finally:
            await redact_artifacts_async(runs_dir, secret_values(environment))
        atomic_json(root / "outcome.json", {
            "argv": argv, "exit_code": outcome.exit_code, "timed_out": outcome.timed_out,
            "cancelled": outcome.cancelled, "started_at": outcome.started_at,
            "finished_at": outcome.finished_at,
        })
        failed = outcome.exit_code != 0 or outcome.timed_out or outcome.cancelled
        if failed and cancel_path and cancel_path.exists():
            detail = outcome.stderr.strip()[-1000:] or outcome.stdout.strip()[-1000:]
            raise AgentFlowPackError(
                f"AgentFlow Pack failed: exit={outcome.exit_code}, timed_out={outcome.timed_out}, "
                f"cancelled={outcome.cancelled}: {detail}")
        record = last_json_object(outcome.stdout)
        if not isinstance(record, dict) or not isinstance(record.get("nodes"), dict):
            record = self._latest_record(runs_dir)
        if not record or not isinstance(record.get("nodes"), dict):
            detail = outcome.stderr.strip()[-1000:] or outcome.stdout.strip()[-1000:]
            raise AgentFlowPackError(
                f"AgentFlow Pack returned no recoverable run record: exit={outcome.exit_code}: {detail}")
        record = json.loads(redact(json.dumps(record, ensure_ascii=False)))
        atomic_json(root / "run.json", record)
        culls, violations = self._controller_audit(
            runs_dir, record, set(worker_map.values()), culling_policy)
        atomic_json(root / "controller-audit.json", {
            "enabled": controller_enabled, "culls": culls,
            "violations": violations})
        if violations:
            raise AgentFlowPackError(
                "AgentFlow controller violated the cancel-only policy: " + "; ".join(violations))
        results = await self._results(
            record, missions, round_dir, maximum_trace_bytes, cancel_path, worker_map, culls)
        failed = failed or str(record.get("status", "")).lower() != "completed"
        complete = not failed
        for result in results:
            result["agentflow_pack_complete"] = complete
            atomic_json(round_dir / "scouts" / result["worker_id"] / "result.json", result)
        if failed and not any(result["status"] == "completed" for result in results):
            raise AgentFlowPackError(
                f"AgentFlow Pack failed without completed Scout evidence: exit={outcome.exit_code}, "
                f"timed_out={outcome.timed_out}")
        return results

    async def recover(self, round_dir: Path, maximum_trace_bytes: int,
                      cancel_path: Path | None = None) -> list[dict]:
        """Import a completed native run after Hound itself was interrupted."""
        root = round_dir / "agentflow"
        missions_path = root / "missions.json"
        if not missions_path.exists():
            return []
        try:
            saved = json.loads(missions_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AgentFlowPackError("durable AgentFlow missions are malformed") from error
        missions = saved.get("missions") if isinstance(saved, dict) else None
        worker_map = saved.get("worker_map", {}) if isinstance(saved, dict) else {}
        culling_policy = saved.get("culling_policy", {}) if isinstance(saved, dict) else {}
        if (not isinstance(missions, list) or not missions or
                any(not isinstance(item, str) or not item.strip() for item in missions)):
            raise AgentFlowPackError("durable AgentFlow missions are malformed")
        record = None
        run_path = root / "run.json"
        if run_path.exists():
            try:
                value = json.loads(run_path.read_text(encoding="utf-8"))
                record = value if isinstance(value, dict) else None
            except (OSError, json.JSONDecodeError):
                record = None
        record = record or self._latest_record(root / "runs")
        if not isinstance(record, dict) or not isinstance(record.get("nodes"), dict):
            return []
        if (not isinstance(worker_map, dict) or
                any(not isinstance(key, str) or not isinstance(value, str)
                    for key, value in worker_map.items())):
            raise AgentFlowPackError("durable AgentFlow worker map is malformed")
        if not isinstance(culling_policy, dict):
            raise AgentFlowPackError("durable AgentFlow culling policy is malformed")
        record = json.loads(redact(json.dumps(record, ensure_ascii=False)))
        atomic_json(run_path, record)
        culls, violations = self._controller_audit(
            root / "runs", record, set(worker_map.values()), culling_policy)
        if violations:
            raise AgentFlowPackError(
                "durable AgentFlow controller audit failed: " + "; ".join(violations))
        results = await self._results(
            record, missions, round_dir, maximum_trace_bytes, cancel_path, worker_map, culls)
        complete = str(record.get("status", "")).lower() == "completed"
        for result in results:
            result["agentflow_pack_complete"] = complete
            atomic_json(round_dir / "scouts" / result["worker_id"] / "result.json", result)
        return results

    @staticmethod
    def _latest_record(runs_dir: Path) -> dict | None:
        records = sorted(runs_dir.glob("*/run.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in records:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                return value
        return None

    async def _results(self, record: dict, missions: list[str], round_dir: Path,
                       maximum_trace_bytes: int,
                       cancel_path: Path | None = None,
                       worker_map: dict[str, str] | None = None,
                       controller_culls: dict[str, dict] | None = None) -> list[dict]:
        results: list[dict] = []
        nodes = record["nodes"]
        for index, mission in enumerate(missions, 1):
            worker_id = f"scout-{round_dir.name.lstrip('0') or '0'}-{index}"
            node_id = (worker_map or {}).get(worker_id, worker_id)
            node = nodes.get(node_id, {}) if isinstance(nodes, dict) else {}
            node = node if isinstance(node, dict) else {}
            stdout = [str(line) for line in node.get("stdout_lines", [])]
            stderr = [str(line) for line in node.get("stderr_lines", [])]
            trace = [event for event in node.get("trace_events", []) if isinstance(event, dict)]
            final = node.get("final_response") or node.get("output") or "\n".join(stdout)
            if not isinstance(final, str):
                final = json.dumps(final, ensure_ascii=False)
            raw_status = str(node.get("status", "failed")).lower()
            exit_code = node.get("exit_code")
            status = ("completed" if raw_status == "completed" and exit_code in (0, None) else
                      "culled" if raw_status == "cancelled" and
                      node_id in (controller_culls or {}) else
                      "cancelled" if raw_status == "cancelled" else
                      "timed_out" if exit_code == 124 else
                      "interrupted" if raw_status in {"pending", "queued", "ready", "running", "retrying"}
                      else "failed")
            text = final + "\n" + json.dumps(trace, ensure_ascii=False)
            value = last_json_object(final)
            valid = matches_output_schema("scout", value)
            result = normalize_result(
                "scout", json.dumps(value, ensure_ascii=False) if valid else "")
            if not valid:
                result["parse_confidence"] = 0.1
            if status == "completed" and not valid:
                status = "failed"
            artifact = round_dir / "scouts" / worker_id
            result.update({
                "worker_id": worker_id, "mission": mission, "backend": "agentflow",
                "agentflow_run_id": record.get("id"), "agentflow_node_id": node_id,
                "status": status, "exit_code": exit_code,
                "artifact_dir": artifact.relative_to(round_dir.parents[1]).as_posix(),
                "progress_events": len(trace) or len(stdout),
                "score": deterministic_score(text, len(trace) or len(stdout), mission),
            })
            if status == "culled":
                result["cull_reason"] = controller_culls[node_id]["reason"]
            if status != "completed":
                raw = text.encode()
                result["partial_excerpt"] = (raw[-maximum_trace_bytes:].decode(
                    "utf-8", errors="replace") if len(raw) > maximum_trace_bytes else text)
            artifact.mkdir(parents=True, exist_ok=True)
            (artifact / "stdout.log").write_text("\n".join(stdout), encoding="utf-8")
            (artifact / "stderr.log").write_text("\n".join(stderr), encoding="utf-8")
            (artifact / "trace.jsonl").write_text(
                "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in trace), encoding="utf-8")
            (artifact / "final.txt").write_text(final, encoding="utf-8")
            result["artifact_sha256"] = (
                await artifact_sha256_async(artifact, cancel_path) or "")
            atomic_json(artifact / "result.json", result)
            if status == "culled":
                self._persist_cull(round_dir, result, controller_culls[node_id], record)
            results.append(result)
        return results

    @staticmethod
    def _controller_prompt(policy: dict, interval: int) -> str:
        return (
            "You are Hound's read-only Scout culling controller. Inspect only the fanout "
            "member state and artifact paths supplied by AgentFlow. Never inspect or kill PIDs, "
            "cancel the run, or request any action except member cancel. Emit strict JSON "
            "with analysis and actions. actions must be empty or contain at most one "
            '{"kind":"cancel","node_ids":["scouts_N"],"reason":"comparison"}. '
            "Current tick is {{ item.tick_number }} and started at {{ item.tick_started_at }}. "
            "Fanout totals: size={{ fanouts.scouts.size }}, "
            "completed={{ fanouts.scouts.summary.completed }}, "
            "running={{ fanouts.scouts.summary.running }}, "
            "without_output={{ fanouts.scouts.summary.without_output }}. Members: "
            "{% for scout in fanouts.scouts.nodes %}{{ scout.id }} status={{ scout.status }} "
            "stdout={{ scout.artifacts.stdout_log }} stderr={{ scout.artifacts.stderr_log }}; "
            "{% endfor %} "
            f"Tick 1 must always return an empty actions list. Later ticks are at least {interval} "
            f"seconds apart, so do not infer a shorter runtime. Preserve at "
            f"least {policy.get('survivors', 1)} uncancelled members, never cancel a member with "
            "unique evidence, and cancel only a clearly redundant member or one clearly lagging "
            f"after {policy.get('minimum_progress_events', 0)} expected progress events. Require a "
            f"0-100 evidence-score gap of at least {policy.get('minimum_score_gap', 0)}. The reason "
            "must name the compared members, their scores, and why no unique evidence is lost."
        )

    @staticmethod
    def _controller_audit(runs_dir: Path, record: dict, member_ids: set[str],
                          policy: dict | None) -> tuple[dict[str, dict], list[str]]:
        run_id = record.get("id")
        run_dir = next((path for path in runs_dir.iterdir()
                        if path.is_dir() and path.name == run_id), None) if runs_dir.exists() else None
        if run_dir is None:
            return {}, []
        violations: list[str] = []
        try:
            survivors = int((policy or {}).get("survivors", 1))
        except (TypeError, ValueError):
            return {}, ["controller policy has an invalid survivor floor"]
        maximum_culls = max(0, len(member_ids) - survivors)
        requested = 0
        requested_nodes: set[str] = set()
        action_root = run_dir / "artifacts" / "scout_controller"
        for path in sorted(action_root.glob("periodic-actions-tick-*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                violations.append(f"malformed controller action artifact {path.name}")
                continue
            actions = payload.get("actions", []) if isinstance(payload, dict) else None
            if not isinstance(actions, list):
                violations.append(f"malformed controller actions in {path.name}")
                continue
            targets = sum((len(action.get("node_ids", []))
                           for action in actions if isinstance(action, dict)), 0)
            if path.name.endswith("tick-1.json") and targets:
                violations.append("controller requested a cancellation on tick 1")
            if len(actions) > 1 or targets > 1:
                violations.append(f"controller requested multiple actions in {path.name}")
            for action in actions:
                if not isinstance(action, dict) or action.get("kind") != "cancel":
                    violations.append(f"controller requested a non-cancel action in {path.name}")
                    continue
                node_ids = action.get("node_ids", [])
                reason = action.get("reason")
                if (not isinstance(node_ids, list) or len(node_ids) != 1 or
                        node_ids[0] not in member_ids):
                    violations.append(f"controller targeted an invalid member in {path.name}")
                else:
                    requested_nodes.add(node_ids[0])
                if not isinstance(reason, str) or not reason.strip():
                    violations.append(f"controller omitted a cull reason in {path.name}")
                requested += len(node_ids) if isinstance(node_ids, list) else 0
        if requested > maximum_culls:
            violations.append("controller requests breach the survivor floor")

        culls: dict[str, dict] = {}
        try:
            events = read_jsonl(run_dir / "events.jsonl")
        except (OSError, ValueError):
            return {}, [*violations, "AgentFlow controller event log is malformed"]
        for event in events:
            if (event.get("type") != "node_control_actions_applied" or
                    event.get("node_id") != "scout_controller"):
                continue
            data = event.get("data", {})
            if not isinstance(data, dict) or data.get("watched_group") != "scouts":
                continue
            actions = data.get("actions", [])
            if isinstance(actions, list) and len(actions) > 1:
                violations.append("AgentFlow applied multiple cancels from one controller tick")
            for action in actions if isinstance(actions, list) else []:
                if not isinstance(action, dict) or action.get("kind") != "cancel":
                    violations.append("AgentFlow applied a non-cancel controller action")
                    continue
                node_id = action.get("node_id")
                if node_id not in member_ids:
                    violations.append("AgentFlow applied a cancel outside the Scout fanout")
                    continue
                if node_id not in requested_nodes:
                    violations.append("AgentFlow applied a cancel absent from controller artifacts")
                    continue
                reason = action.get("reason")
                culls[node_id] = {
                    "reason": reason.strip() if isinstance(reason, str) and reason.strip()
                    else "cancelled_by_agentflow_controller",
                    "timestamp": event.get("timestamp") or utc_now(),
                }
        if len(culls) > maximum_culls:
            violations.append("applied cancellations breach the survivor floor")
        return culls, list(dict.fromkeys(violations))

    @staticmethod
    def _persist_cull(round_dir: Path, result: dict, cull: dict, record: dict) -> None:
        worker_id, node_id = result["worker_id"], result["agentflow_node_id"]
        path = round_dir / "agentflow" / "cull-decisions.jsonl"
        timestamp = cull.get("timestamp") or utc_now()
        reason = cull.get("reason") or "cancelled_by_agentflow_controller"
        if not any(item.get("agentflow_node_id") == node_id for item in read_jsonl(path)):
            append_jsonl(path, {"worker_id": worker_id, "agentflow_node_id": node_id,
                                "reason": reason, "agentflow_run_id": record.get("id"),
                                "observed_at": timestamp})
        ledger = round_dir.parents[1] / "cull-decisions.jsonl"
        existing = read_jsonl(ledger)
        if not any(item.get("worker_id") == worker_id for item in existing):
            prefix = worker_id.rsplit("-", 1)[0] + "-"
            order = 1 + sum(str(item.get("worker_id", "")).startswith(prefix)
                            for item in existing)
            append_jsonl(ledger, CullDecision(
                worker_id, result.get("score", 0), reason, timestamp, order))
