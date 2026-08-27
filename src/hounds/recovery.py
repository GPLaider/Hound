from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .evidence import round_evidence_hash
from .models import HoundResumeRetryError, RunState, RunStatus, validate_state
from .process import (redact, redact_artifacts, redact_artifacts_async,
                      windows_process_identity, windows_process_table)
from .store import (artifact_sha256, artifact_sha256_async, atomic_json, pid_alive,
                    read_jsonl, utc_now)
from .structured import last_json_object, normalize_result

if TYPE_CHECKING:
    from .engine import Engine


def elapsed_since(value: str) -> float:
    if not value:
        return 0.0
    try:
        started = datetime.fromisoformat(value)
    except ValueError:
        return 0.0
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - started).total_seconds())


_RECOVERABLE_STATUSES = {
    RunStatus.WORKING, RunStatus.SCOUTING, RunStatus.CULLING,
    RunStatus.SYNTHESIZING, RunStatus.VERIFYING, RunStatus.REVIEWING,
    RunStatus.FAILED_INTERNAL,
}


def save_round_checkpoint(engine: Engine, state: RunState, round_state: dict,
                          fingerprint_record: dict | None) -> None:
    atomic_json(engine.store.run_dir(state.run_id) / "rounds" /
                f"{state.round:03d}" / "checkpoint.json", {
        "schema_version": 1, "round": round_state,
        "run": {name: getattr(state, name) for name in (
            "idle_rounds", "repeated_verification_failures", "last_verification_signature",
            "repeated_verifier_failures", "last_verifier_signature")},
        "fingerprint": fingerprint_record,
    })


def _restore_round_checkpoint(engine: Engine, state: RunState) -> None:
    if not state.round or any(item.get("number") == state.round for item in state.rounds):
        return
    path = engine.store.run_dir(state.run_id) / "rounds" / f"{state.round:03d}" / "checkpoint.json"
    if not path.exists():
        return
    value = json.loads(path.read_text(encoding="utf-8"))
    round_state = value.get("round") if isinstance(value, dict) else None
    if (value.get("schema_version") != 1 or not isinstance(round_state, dict) or
            round_state.get("number") != state.round):
        raise ValueError("invalid durable round checkpoint")
    run = value.get("run", {})
    if not isinstance(run, dict):
        raise ValueError("invalid durable round checkpoint counters")
    record = value.get("fingerprint")
    if record is not None and (not isinstance(record, dict) or
            any(not isinstance(record.get(name), str) or not record.get(name)
                for name in ("fingerprint", "status", "evidence_hash")) or
            record.get("status") not in {"passed", "failed"}):
        raise ValueError("invalid durable round checkpoint fingerprint")
    names = ("idle_rounds", "repeated_verification_failures", "last_verification_signature",
             "repeated_verifier_failures", "last_verifier_signature")
    updates = {name: run[name] for name in names if name in run}
    candidate = replace(state, rounds=[*state.rounds, round_state], **updates)
    validate_state(candidate)
    state.rounds.append(round_state)
    for name, value in updates.items():
        setattr(state, name, value)
    if isinstance(record, dict) and not any(
            all(item.get(name) == record.get(name) for name in
                ("fingerprint", "status", "evidence_hash"))
            for item in engine.store.fingerprints(state.run_id)):
        engine.store.fingerprint(state.run_id, record["fingerprint"], record["status"],
                                 record["evidence_hash"])
    engine.store.event(state.run_id, "round_checkpoint_recovered", round=state.round)


def _restore_writer_checkpoint(engine: Engine, state: RunState,
                               artifact_hashes: dict[str, str] | None) -> None:
    if not state.round or any(item.get("number") == state.round for item in state.rounds):
        return
    run_dir = engine.store.run_dir(state.run_id)
    round_dir = run_dir / "rounds" / f"{state.round:03d}"
    intent_path = round_dir / "writer-intent.json"
    result_path = round_dir / "writer" / "result.json"
    final_path = round_dir / "writer" / "final.txt"
    if not intent_path.exists() or not (result_path.exists() or final_path.exists()):
        return
    try:
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("invalid durable Writer checkpoint") from error
    raw = ""
    if result_path.exists():
        try:
            candidate = result_path.read_text(encoding="utf-8")
            value = json.loads(candidate)
            if isinstance(value, dict) and value.get("status") in {
                    "continue", "candidate_done", "blocked"}:
                raw = candidate
        except (OSError, json.JSONDecodeError):
            pass
    if not raw and final_path.exists():
        try:
            raw = final_path.read_text(encoding="utf-8")
        except OSError as error:
            raise ValueError("invalid durable Writer checkpoint") from error
    if not raw or last_json_object(raw) is None:
        return
    fingerprint_hash = intent.get("evidence_hash") if isinstance(intent, dict) else None
    scheduled = intent.get("scheduled_fingerprint") if isinstance(intent, dict) else None
    git_diff = intent.get("git_diff") if isinstance(intent, dict) else None
    git_identity = intent.get("git_identity") if isinstance(intent, dict) else None
    if (not isinstance(intent, dict) or intent.get("schema_version") != 1 or
            intent.get("round") != state.round or
            not isinstance(fingerprint_hash, str) or len(fingerprint_hash) != 64 or
            any(char not in "0123456789abcdef" for char in fingerprint_hash) or
            not isinstance(scheduled, str) or (scheduled and (
                len(scheduled) != 64 or any(char not in "0123456789abcdef" for char in scheduled))) or
            not isinstance(git_diff, list) or
            any(not isinstance(item, str) for item in git_diff) or
            not isinstance(git_identity, str)):
        raise ValueError("invalid durable Writer checkpoint")
    writer = normalize_result("writer", raw)
    artifact_dir = (round_dir / "writer").relative_to(run_dir).as_posix()
    writer.update({"artifact_dir": artifact_dir,
                   "artifact_sha256": _resume_artifact_hash(
                       run_dir, artifact_dir, artifact_hashes)})
    round_number = state.round
    atomic_json(round_dir / "writer-resume.json", {
        "schema_version": 1, "writer": writer, "intent": intent})
    state.round -= 1
    engine.store.event(state.run_id, "writer_checkpoint_recovered", round=round_number)


def _restore_pack_checkpoint(state: RunState, scouts: list[dict]) -> None:
    if not state.rounds or state.rounds[-1].get("pack_evidence"):
        return
    current = state.rounds[-1]
    if not scouts:
        return
    value = round_evidence_hash(
        replace(state, rounds=state.rounds[:-1]), current.get("writer", {}),
        current.get("verification", []), current.get("path_issues", []), scouts=scouts,
        verification_signature=current.get("verification_signature", ""),
        git_diff=current.get("git_diff", []), git_identity=current.get("git_identity", ""))
    if value != current.get("evidence_hash"):
        current.update({"scouts": scouts, "pack_evidence": True, "evidence_hash": value})
        state.idle_rounds = 0

def build_resume_context(engine: Engine, state: RunState,
                         artifact_hashes: dict[str, str] | None = None) -> dict:
    run_dir = engine.store.run_dir(state.run_id)
    round_dir = run_dir / "rounds" / f"{state.round:03d}"
    try:
        previous = json.loads((run_dir / "resume-context.json").read_text(encoding="utf-8"))
        previous = previous if isinstance(previous, dict) else {}
    except (OSError, json.JSONDecodeError):
        previous = {}
    records: dict[str, dict] = {}
    for path in sorted(round_dir.glob("**/interrupted.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            records[str(path.parent.relative_to(run_dir))] = value
    for path in sorted(round_dir.glob("**/result.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            records[str(path.parent.relative_to(run_dir))] = value
    for item in previous.get("workers", []):
        if not isinstance(item, dict):
            continue
        artifact_dir = item.get("artifact_dir")
        relative = Path(artifact_dir) if isinstance(artifact_dir, str) else None
        if (relative is not None and not relative.is_absolute() and
                ".." not in relative.parts):
            records.setdefault(artifact_dir, item)

    old_omitted = previous.get("omitted", {})
    old_omitted = old_omitted if isinstance(old_omitted, dict) else {}
    old_count = lambda name: (old_omitted.get(name, 0)
                              if isinstance(old_omitted.get(name, 0), int) and
                              old_omitted.get(name, 0) >= 0 else 0)
    digest: dict[str, Any] = {
        "recovered_round": state.round, "workers": [], "cull_decisions": [],
        "omitted": {name: old_count(name) for name in (
            "workers", "partial_excerpts", "stderr_excerpts", "cull_decisions")
                    if old_count(name)},
    }
    size = lambda: len(json.dumps(digest, ensure_ascii=False).encode())
    if size() > engine.trace_limit:
        return {"recovered_round": state.round,
                "omitted": {"workers": len(records), "all_context": True}}
    record_items = list(records.items())
    for index, (artifact_dir, record) in enumerate(record_items):
        item = {"artifact_dir": artifact_dir, "status": record.get("status", "completed")}
        for name in ("worker_id", "role", "summary", "evidence", "recommended_next_action",
                     "command", "exit_code", "passed", "timed_out", "artifact_sha256"):
            if name in record:
                item[name] = record[name]
        if "artifact_sha256" not in item:
            item["artifact_sha256"] = _resume_artifact_hash(
                run_dir, artifact_dir, artifact_hashes)
        digest["workers"].append(item)
        if size() > engine.trace_limit:
            digest["workers"].pop()
            digest["omitted"]["workers"] = (
                digest["omitted"].get("workers", 0) + len(record_items) - index)
            break

    prefix = f"scout-{state.round}-"
    decisions = [decision for decision in read_jsonl(run_dir / "cull-decisions.jsonl")
                 if str(decision.get("worker_id", "")).startswith(prefix)]
    seen = {json.dumps(decision, ensure_ascii=False, sort_keys=True) for decision in decisions}
    for decision in previous.get("cull_decisions", []):
        if not isinstance(decision, dict):
            continue
        key = json.dumps(decision, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            decisions.append(decision)
    for index, decision in enumerate(decisions):
        digest["cull_decisions"].append(decision)
        if size() > engine.trace_limit:
            digest["cull_decisions"].pop()
            digest["omitted"]["cull_decisions"] = (
                digest["omitted"].get("cull_decisions", 0) + len(decisions) - index)
            break

    excerpt_limit = max(1, engine.trace_limit // max(1, len(digest["workers"]) * 2))

    def tail(path: Path) -> str:
        if not path.exists():
            return ""
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - excerpt_limit))
            return redact(handle.read().decode("utf-8", errors="replace")).strip()

    def fit_excerpt(item: dict, name: str, text: str, omitted_name: str) -> None:
        item[name] = text
        while size() > engine.trace_limit and item[name]:
            raw = item[name].encode()
            cut = min(len(raw), max(1, size() - engine.trace_limit))
            item[name] = raw[cut:].decode("utf-8", errors="replace")
        if size() > engine.trace_limit or not item[name]:
            item.pop(name, None)
            digest["omitted"][omitted_name] = digest["omitted"].get(omitted_name, 0) + 1

    for item in digest["workers"]:
        artifact = run_dir / item["artifact_dir"]
        for name in ("final.txt", "trace.jsonl", "stdout.log"):
            excerpt = tail(artifact / name)
            if excerpt:
                fit_excerpt(item, "partial_excerpt", excerpt, "partial_excerpts")
                break
        stderr = tail(artifact / "stderr.log")
        if stderr:
            fit_excerpt(item, "stderr_excerpt", stderr, "stderr_excerpts")
    return digest


async def recover_interrupted_async(engine: Engine, state: RunState,
                                    cancel_path: Path | None = None) -> bool:
    """Prepare hashes off-loop, then commit recovery only after all checks succeed."""
    if state.status not in _RECOVERABLE_STATUSES:
        return True
    run_dir = engine.store.run_dir(state.run_id)
    if cancel_path is not None and cancel_path.exists():
        return False
    launches = _validated_unfinished_launches(run_dir)
    if not await redact_artifacts_async(
            run_dir / "rounds", cancel_path=cancel_path):
        return False
    round_dir = run_dir / "rounds" / f"{state.round:03d}"
    recovered = await engine.agentflow.recover(
        round_dir, engine.trace_limit, cancel_path)
    if recovered:
        engine.store.event(state.run_id, "agentflow_pack_recovered",
                           round=state.round, workers=len(recovered))
    hashes: dict[str, str] = {}
    for artifact in _resume_hash_paths(run_dir, state):
        value = await artifact_sha256_async(artifact, cancel_path)
        if value is None:
            return False
        hashes[artifact.relative_to(run_dir).as_posix()] = value
    if cancel_path is not None and cancel_path.exists():
        return False
    recover_interrupted(engine, state, hashes, artifacts_redacted=True,
                        validated_launches=launches)
    return True


def recover_interrupted(engine: Engine, state: RunState,
                        artifact_hashes: dict[str, str] | None = None,
                        artifacts_redacted: bool = False,
                        validated_launches: list[tuple[Path, dict, int, int]] | None = None) -> None:
    if state.status not in _RECOVERABLE_STATUSES:
        return
    run_dir = engine.store.run_dir(state.run_id)
    launches = (validated_launches if validated_launches is not None else
                _validated_unfinished_launches(run_dir))
    if not artifacts_redacted:
        _redact_resume_artifacts(run_dir)
    interrupted: list[dict] = []
    for launch_path, launch, worker_pid, recorded_parent in launches:
        record = {"status": "interrupted", "recovered_at": utc_now(),
                  "artifact_dir": launch_path.parent.relative_to(run_dir).as_posix(),
                  "worker_pid": worker_pid, "worker_ppid": recorded_parent,
                  "worker_executable": launch.get("worker_executable", "unknown")}
        atomic_json(launch_path.parent / "interrupted.json", record)
        interrupted.append(record)
    _restore_round_checkpoint(engine, state)
    _restore_writer_checkpoint(engine, state, artifact_hashes)
    for prompt in run_dir.glob("rounds/**/prompt.txt"):
        if not (prompt.parent / "result.json").exists():
            artifact_dir = prompt.parent.relative_to(run_dir).as_posix()
            result = {"status": "interrupted", "recovered_at": utc_now(),
                      "artifact_dir": artifact_dir,
                      "artifact_sha256": _resume_artifact_hash(
                          run_dir, artifact_dir, artifact_hashes)}
            atomic_json(prompt.parent / "result.json", result)
            if not any(item["artifact_dir"] == result["artifact_dir"] for item in interrupted):
                interrupted.append(result)
    resume_context = build_resume_context(engine, state, artifact_hashes)
    scouts = [item for item in resume_context.get("workers", []) if "scouts" in str(
        item.get("artifact_dir", "")).replace("\\", "/").split("/")]
    _restore_pack_checkpoint(state, scouts)
    atomic_json(run_dir / "resume-context.json", resume_context)
    engine.store.event(state.run_id, "interrupted_recovered",
                       previous_status=state.status.value, workers=interrupted)
    if state.status is not RunStatus.REVIEWING:
        engine._transition(state, RunStatus.WORKING)
    state.message = "resumed from last durable checkpoint"
    engine.store.save_state(state)


def _resume_artifact_hash(run_dir: Path, artifact_dir: str,
                          artifact_hashes: dict[str, str] | None) -> str:
    if artifact_hashes is None:
        return artifact_sha256(run_dir / artifact_dir)
    try:
        return artifact_hashes[Path(artifact_dir).as_posix()]
    except KeyError as error:
        raise RuntimeError(f"missing precomputed resume artifact hash: {artifact_dir}") from error


def _resume_hash_paths(run_dir: Path, state: RunState) -> list[Path]:
    paths = {path.parent for pattern in (
        "rounds/**/prompt.txt", "rounds/**/launch.json",
        f"rounds/{state.round:03d}/**/interrupted.json",
        f"rounds/{state.round:03d}/**/result.json",
    ) for path in run_dir.glob(pattern)}
    try:
        previous = json.loads((run_dir / "resume-context.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        previous = {}
    for item in previous.get("workers", []) if isinstance(previous, dict) else []:
        artifact_dir = item.get("artifact_dir") if isinstance(item, dict) else None
        relative = Path(artifact_dir) if isinstance(artifact_dir, str) else None
        if relative is not None and not relative.is_absolute() and ".." not in relative.parts:
            paths.add(run_dir / relative)
    return sorted(paths, key=lambda path: path.as_posix())


def _redact_resume_artifacts(run_dir: Path,
                             cancel_path: Path | None = None) -> bool:
    return redact_artifacts(
        run_dir / "rounds", cancel_path=cancel_path)


def guard_other_run_writers(runs_dir: Path, current_run: str) -> None:
    if not runs_dir.exists():
        return
    for run_dir in runs_dir.iterdir():
        if run_dir.is_dir() and run_dir.name != current_run:
            _validated_unfinished_launches(run_dir, "rounds/**/writer/launch.json")

def _validated_unfinished_launches(
        run_dir: Path, pattern: str = "**/launch.json") -> list[tuple[Path, dict, int, int]]:
    launches: list[tuple[Path, dict, int, int]] = []
    for launch_path in run_dir.glob(pattern):
        if ((launch_path.parent / "outcome.json").exists() or
                (launch_path.parent / "interrupted.json").exists()):
            continue
        try:
            launch = json.loads(launch_path.read_text(encoding="utf-8"))
            if not isinstance(launch, dict):
                launch = {}
        except (OSError, json.JSONDecodeError):
            launch = {}
        worker_pid, worker_pid_valid = _pid_value(launch.get("worker_pid"))
        recorded_parent, parent_valid = _pid_value(launch.get("worker_ppid"))
        recorded_owner, owner_valid = _pid_value(launch.get("hound_pid"))
        if not worker_pid_valid:
            raise HoundResumeRetryError(
                f"cannot proceed with unverifiable unfinished worker PID: {worker_pid or 'invalid'}")
        alive = pid_alive(worker_pid)
        process = windows_process_table().get(worker_pid) if os.name == "nt" and alive else None
        recorded_creation = launch.get("worker_creation_time")
        if (process is not None and isinstance(recorded_creation, int) and
                recorded_creation > 0):
            try:
                process = windows_process_identity(worker_pid, process)
            except RuntimeError as error:
                raise HoundResumeRetryError(
                    f"cannot resume while live worker identity cannot be verified: PID={worker_pid}") from error
        identity_valid = (worker_pid_valid and parent_valid and owner_valid and
                          recorded_parent == recorded_owner and
                          worker_pid not in {recorded_parent, recorded_owner})
        if alive and not identity_valid:
            raise HoundResumeRetryError(
                f"cannot proceed with invalid live worker identity: PID={worker_pid} "
                f"PPID={recorded_parent or 'invalid'} hound_pid={recorded_owner or 'invalid'}")
        if os.name == "nt" and alive and process is None:
            raise HoundResumeRetryError(
                f"cannot resume while live worker identity cannot be verified: PID={worker_pid}")
        reused = bool(process and (
            process.parent_pid != recorded_parent or
            isinstance(recorded_creation, int) and recorded_creation > 0 and
            process.creation_time != recorded_creation))
        if alive and not reused:
            parent_pid = process.parent_pid if process else recorded_parent or "unknown"
            executable = process.executable if process else launch.get("worker_executable", "unknown")
            raise HoundResumeRetryError(
                f"cannot proceed while recorded worker is alive: PID={worker_pid} "
                f"PPID={parent_pid} executable={executable}")
        launches.append((launch_path, launch, worker_pid, recorded_parent))
    return launches


def _pid_value(value: Any) -> tuple[int, bool]:
    valid = isinstance(value, int) and not isinstance(value, bool) and value > 0
    if valid:
        return value, True
    try:
        candidate = int(value)
    except (TypeError, ValueError):
        candidate = 0
    return (candidate if candidate > 0 else 0), False
