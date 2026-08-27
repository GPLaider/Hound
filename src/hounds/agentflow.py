from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .models import HoundEnvironmentError
from .process import (codex_environment, redact, redact_artifacts_async,
                      resolve_executable, run_process, secret_values,
                      worker_environment)
from .prompts import scout_prompt
from .scoring import deterministic_score
from .store import artifact_sha256_async, atomic_json
from .structured import last_json_object, matches_output_schema, normalize_result


class AgentFlowPackError(HoundEnvironmentError):
    pass


class AgentFlowPack:
    """Invoke installed AgentFlow as an optional read-only Pack runtime."""

    def __init__(self, executable: str = "agentflow", prefix: list[str] | None = None):
        self.executable, self.prefix = executable, prefix

    def available(self) -> bool:
        return bool(self.prefix or resolve_executable(self.executable, Path.cwd()))

    async def run(self, objective: str, missions: list[str], context: dict,
                  workspace: Path, round_dir: Path, concurrency: int,
                  timeout: float, model: str = "", cancel_path: Path | None = None,
                  maximum_prompt_bytes: int = 65536,
                  maximum_trace_bytes: int = 8192) -> list[dict]:
        root = round_dir / "agentflow"
        pipeline_path = root / "pipeline.json"
        runs_dir = root / "runs"
        node_timeout = max(1, int(timeout) + 60)  # Hound owns timeout/cancellation first.
        nodes = []
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
            node = {
                "id": worker_id,
                "agent": "codex",
                "prompt": prompt,
                "tools": "read_only",
                "capture": "trace",
                "timeout_seconds": node_timeout,
                "retries": 1,
                "extra_args": (["--ignore-user-config", "--ephemeral", "-c",
                                'windows.sandbox="elevated"']
                               if os.name == "nt" else ["--ignore-user-config", "--ephemeral"]),
            }
            if model:
                node["model"] = model
            nodes.append(node)
        pipeline = {
            "name": f"hound-pack-{round_dir.name}",
            "description": "Hound-requested parallel read-only evidence hunt",
            "working_dir": str(workspace.resolve()),
            "concurrency": max(1, min(concurrency, len(nodes))),
            "fail_fast": False,
            "use_worktree": False,
            "nodes": nodes,
        }
        atomic_json(pipeline_path, pipeline)
        atomic_json(root / "missions.json", {"missions": missions})
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
        results = await self._results(
            record, missions, round_dir, maximum_trace_bytes, cancel_path)
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
        record = json.loads(redact(json.dumps(record, ensure_ascii=False)))
        atomic_json(run_path, record)
        results = await self._results(
            record, missions, round_dir, maximum_trace_bytes, cancel_path)
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
                       cancel_path: Path | None = None) -> list[dict]:
        results: list[dict] = []
        nodes = record["nodes"]
        for index, mission in enumerate(missions, 1):
            worker_id = f"scout-{round_dir.name.lstrip('0') or '0'}-{index}"
            node = nodes.get(worker_id, {}) if isinstance(nodes, dict) else {}
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
                "agentflow_run_id": record.get("id"), "agentflow_node_id": worker_id,
                "status": status, "exit_code": exit_code,
                "artifact_dir": artifact.relative_to(round_dir.parents[1]).as_posix(),
                "progress_events": len(trace) or len(stdout),
                "score": deterministic_score(text, len(trace) or len(stdout), mission),
            })
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
            results.append(result)
        return results
