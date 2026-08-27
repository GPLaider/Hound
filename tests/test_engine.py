from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import hounds.engine as engine_module
import hounds.pack as pack_module
import hounds.process as process_module
import hounds.recovery as recovery_module
from hounds.adapter import CodexAdapter
from hounds.engine import (Engine, _evidence_tokens, _followed_scheduled_action,
                           _round_evidence_hash)
from hounds.models import (Budget, HoundEnvironmentError, HoundResumeRetryError,
                           PackPolicy, RoundState, RunContract, RunStatus, WorkerSpec, dump)
from hounds.policy import fingerprint, should_pack
from hounds.prompts import writer_prompt
from hounds.process import ProcessOutcome, run_process
from hounds.recovery import recover_interrupted_async
from hounds.verification import changed_content_signature

FAKE = Path(__file__).with_name("fake_agent.py")


def adapter() -> CodexAdapter:
    return CodexAdapter(prefix=[sys.executable, str(FAKE)])


def base_config() -> dict:
    return {"workers": {"writer_timeout_seconds": 5, "scout_timeout_seconds": 5,
                        "verifier_timeout_seconds": 5}, "context": {"maximum_prompt_bytes": 65536}}


def git(root: Path, *args: str) -> None:
    artifact = root / ".hound" / "test-git" / str(time.time_ns())
    result = asyncio.run(run_process(["git", *args], root, artifact, 10))
    assert result.exit_code == 0, result.stderr


def non_git_identity(engine: Engine, run_id: str) -> str | None:
    (engine.store.run_dir(run_id) / "baseline.json").write_text(
        '{"schema_version":2,"kind":"unavailable"}', encoding="utf-8")
    return changed_content_signature(engine.workspace, ["."])


def test_verification_loop_reaches_done(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOUND_FAKE_SCENARIO", "verification-loop")
    (tmp_path / "test_answer.py").write_text("from answer import answer\nassert answer() == 42\n", encoding="utf-8")
    contract = RunContract("make answer pass", str(tmp_path),
                           [[sys.executable, "test_answer.py"]], budget=Budget(max_rounds=3),
                           pack=PackPolicy(mode="off"))
    engine = Engine(tmp_path, adapter(), base_config())
    state = engine.new_run(contract)
    result = asyncio.run(engine.run(state, contract))
    assert result.status is RunStatus.DONE
    assert result.round == 2
    assert result.rounds[0]["verification"][0]["passed"] is False
    assert result.rounds[1]["verification"][0]["passed"] is True
    assert result.rounds[1]["verifier"]["verdict"] == "accept"
    assert len(result.rounds[1]["writer"]["artifact_sha256"]) == 64
    assert result.rounds[1]["writer"]["artifact_dir"].endswith("rounds/002/writer")
    assert len(result.rounds[1]["verifier"]["artifact_sha256"]) == 64
    assert len(result.rounds[1]["verification"][0]["stdout_sha256"]) == 64
    final = engine.store.run_dir(result.run_id) / "final"
    verification = json.loads((final / "verification.json").read_text())
    assert verification["passed"] is True and verification["round"] == 2
    assert (engine.store.run_dir(result.run_id) / "rounds" / "002" /
            "verifier" / "outcome.json").exists()
    assert not (final / "outcome.json").exists()


def test_engine_path_gate_catches_writer_commit(tmp_path: Path, monkeypatch):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "hound@example.invalid")
    git(tmp_path, "config", "user.name", "Hound Test")
    (tmp_path / "protected.txt").write_text("before\n", encoding="utf-8")
    git(tmp_path, "add", "protected.txt")
    git(tmp_path, "commit", "-q", "-m", "baseline")
    monkeypatch.setenv("HOUND_FAKE_SCENARIO", "commit-forbidden")
    contract = RunContract("do not touch protected", str(tmp_path), [[sys.executable, "-V"]],
                           forbidden_paths=["protected.txt"],
                           budget=Budget(max_rounds=1), pack=PackPolicy(mode="off"))
    engine = Engine(tmp_path, adapter(), base_config())
    result = asyncio.run(engine.run(engine.new_run(contract), contract))
    assert result.status is RunStatus.BUDGET_EXHAUSTED
    assert result.rounds[0]["path_issues"] == ["forbidden path changed: protected.txt"]
    assert result.rounds[0]["git_diff"] == ["protected.txt"]


def test_full_worker_prompt_obeys_byte_cap_before_launch(tmp_path: Path):
    config = base_config(); config["context"]["maximum_prompt_bytes"] = 300
    contract = RunContract("x" * 400, str(tmp_path), [[sys.executable, "-V"]],
                           pack=PackPolicy(mode="off"))
    engine = Engine(tmp_path, adapter(), config)
    state = engine.new_run(contract)
    try:
        asyncio.run(engine.run(state, contract))
    except HoundEnvironmentError as error:
        assert "configured maximum" in str(error)
    else:
        raise AssertionError("oversized full prompt reached a worker")
    assert state.total_workers == 0


def test_pack_culls_low_workers_and_keeps_artifacts(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOUND_FAKE_SCENARIO", "pack")
    policy = PackPolicy(mode="on", backend="direct", initial_workers=3, minimum_runtime_seconds=0.1,
                        minimum_progress_events=2, judge_interval_seconds=0.25,
                        cull_cooldown_seconds=0.02, minimum_score_gap=5, survivors=1,
                        semantic_judge=False)
    contract = RunContract("find bug", str(tmp_path), budget=Budget(max_total_workers=6), pack=policy)
    engine = Engine(tmp_path, adapter(), base_config())
    state = engine.new_run(contract); state.round = 1; state.status = RunStatus.WORKING
    started = time.monotonic()
    results = asyncio.run(engine._pack(state, contract, engine.store.run_dir(state.run_id) / "rounds" / "001", {}, {}))
    assert time.monotonic() - started >= policy.minimum_runtime_seconds
    assert [item["worker_id"] for item in results if item["status"] == "culled"] == [
        "scout-1-2", "scout-1-3"]
    good = next(item for item in results if item.get("summary") == "good")
    assert good["status"] == "completed" and good["retained"] is True
    decisions = (engine.store.run_dir(state.run_id) / "cull-decisions.jsonl").read_text().splitlines()
    parsed_decisions = [json.loads(line) for line in decisions]
    assert [item["worker_id"] for item in parsed_decisions] == ["scout-1-3", "scout-1-2"]
    assert all("score" in item and item["reason"] for item in parsed_decisions)
    culled = [item for item in results if item["status"] == "culled"]
    assert all(item["artifact_dir"] and len(item["artifact_sha256"]) == 64 and
               "partial_excerpt" in item and item["cull_reason"]
               for item in culled)
    for item in results:
        artifact = (engine.store.run_dir(state.run_id) / "rounds" / "001" / "scouts" /
                    item["worker_id"])
        assert (artifact / "stdout.log").exists()
        assert json.loads((artifact / "result.json").read_text())["status"] == item["status"]
    ranking = json.loads((engine.store.run_dir(state.run_id) / "rounds" / "001" /
                          "pack-ranking.json").read_text())
    assert ranking["ranking"][0]["worker_id"] == good["worker_id"]
    synthesis = json.loads((engine.store.run_dir(state.run_id) / "rounds" / "001" /
                            "synthesis.json").read_text())
    partials = [item for item in synthesis["scouts"] if item["status"] == "culled"]
    assert all(item["artifact_dir"] and item["cull_reason"] for item in partials)
    digest = engine.pack.digest([{"number": 1, "scouts": culled,
                                  "pack_ranking": ranking["ranking"]}])
    assert any(item.get("partial_excerpt") is not None for item in digest["items"])
    assert "pack_completed" in (engine.store.run_dir(state.run_id) / "events.jsonl").read_text()


def test_completed_scout_counts_as_survivor_and_stalled_worker_is_culled(
        tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOUND_FAKE_SCENARIO", "completed-good-stalled")
    policy = PackPolicy(
        mode="on", backend="direct", initial_workers=2, maximum_workers=2,
        concurrency=2, minimum_runtime_seconds=0.15, minimum_progress_events=0,
        judge_interval_seconds=0.15, cull_cooldown_seconds=0,
        minimum_score_gap=0, survivors=1, semantic_judge=False)
    contract = RunContract(
        "keep completed evidence", str(tmp_path), budget=Budget(max_total_workers=4), pack=policy)
    config = base_config(); config["workers"]["scout_timeout_seconds"] = 1
    engine = Engine(tmp_path, adapter(), config)
    state = engine.new_run(contract); state.round = 1; state.status = RunStatus.WORKING
    results = asyncio.run(engine._pack(
        state, contract, engine.store.run_dir(state.run_id) / "rounds" / "001", {}, {}))
    assert next(item for item in results if item["status"] == "completed")["retained"] is True
    assert [item["worker_id"] for item in results if item["status"] == "culled"] == ["scout-1-2"]


def test_higher_quality_partial_outweighs_lower_scoring_completed_worker(
        tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOUND_FAKE_SCENARIO", "completed-low-stalled-high")
    monkeypatch.setattr(pack_module, "unique_evidence", lambda *_args: False)
    policy = PackPolicy(
        mode="on", backend="direct", initial_workers=2, maximum_workers=2,
        concurrency=2, minimum_runtime_seconds=0.15, minimum_progress_events=0,
        judge_interval_seconds=0.15, cull_cooldown_seconds=0,
        minimum_score_gap=0, survivors=1, semantic_judge=False)
    contract = RunContract(
        "prefer stronger evidence", str(tmp_path), budget=Budget(max_total_workers=4), pack=policy)
    config = base_config(); config["workers"]["scout_timeout_seconds"] = 1
    engine = Engine(tmp_path, adapter(), config)
    state = engine.new_run(contract); state.round = 1; state.status = RunStatus.WORKING
    results = asyncio.run(engine._pack(
        state, contract, engine.store.run_dir(state.run_id) / "rounds" / "001", {}, {}))
    completed = next(item for item in results if item["status"] == "completed")
    partial = next(item for item in results if item["status"] != "completed")
    assert completed["score"] < partial["score"]
    assert partial["retained"] is True and completed["retained"] is False


def test_unique_low_scorer_does_not_bypass_gap_for_replacement(tmp_path: Path, monkeypatch):
    policy = PackPolicy(
        mode="on", backend="direct", initial_workers=3, maximum_workers=3, concurrency=3,
        minimum_runtime_seconds=0, minimum_progress_events=0, judge_interval_seconds=0,
        cull_cooldown_seconds=0, minimum_score_gap=8, survivors=1,
        semantic_judge=False)
    contract = RunContract(
        "respect the actual victim gap", str(tmp_path),
        budget=Budget(max_total_workers=5), pack=policy)
    engine = Engine(tmp_path, adapter(), base_config())
    state = engine.new_run(contract); state.round = 1; state.status = RunStatus.WORKING
    terminated: list[str] = []

    class FakeProcess:
        progress_events = 1
        started_monotonic = time.monotonic()

        def __init__(self, worker_id: str):
            self.worker_id = worker_id
            self.process = SimpleNamespace(pid=10000 + int(worker_id[-1]), returncode=None)

        async def wait(self, *_args):
            self.process.returncode = 0
            return ProcessOutcome(["worker"], 0, "", "", "start", "finish")

        async def terminate(self, reason: str):
            if self.process.returncode is None:
                terminated.append(reason)
                self.process.returncode = 1
                return True
            return False

    async def start(spec, _prompt, _workspace, artifact):
        artifact.mkdir(parents=True, exist_ok=True)
        final = artifact / "final.txt"
        final.write_text(spec.worker_id, encoding="utf-8")
        return FakeProcess(spec.worker_id), final

    def finish(*_args):
        return {"parse_confidence": 1.0, "evidence": []}

    scores = {"scout-1-1": 10, "scout-1-2": 85, "scout-1-3": 90}
    monkeypatch.setattr(engine.adapter, "start", start)
    monkeypatch.setattr(engine.adapter, "finish", finish)
    monkeypatch.setattr(pack_module, "deterministic_score", lambda text, *_args: next(
        score for worker_id, score in scores.items() if worker_id in text))
    monkeypatch.setattr(
        pack_module, "unique_evidence", lambda text, _others: "scout-1-1" in text)
    results = asyncio.run(engine._pack_direct(
        state, contract, engine.store.run_dir(state.run_id) / "rounds" / "001", {}, {}))
    assert len(results) == 3 and terminated == []
    assert not (engine.store.run_dir(state.run_id) / "cull-decisions.jsonl").exists()


def test_direct_scout_budget_is_durable_before_launch(tmp_path: Path, monkeypatch):
    policy = PackPolicy(mode="on", backend="direct", initial_workers=1, maximum_workers=1,
                        concurrency=1, survivors=1)
    contract = RunContract(
        "reserve before launch", str(tmp_path), budget=Budget(max_total_workers=3), pack=policy)
    engine = Engine(tmp_path, adapter(), base_config())
    state = engine.new_run(contract); state.round = 1; state.status = RunStatus.WORKING

    async def fail_after_reservation(*_args):
        assert engine.store.load_state(state.run_id).total_workers == 1
        raise RuntimeError("simulated launch crash window")

    monkeypatch.setattr(engine.adapter, "start", fail_after_reservation)
    try:
        asyncio.run(engine._pack_direct(
            state, contract, engine.store.run_dir(state.run_id) / "rounds" / "001", {}, {}))
    except RuntimeError as error:
        assert "launch crash" in str(error)
    else:
        raise AssertionError("simulated launch failure was ignored")
    assert state.total_workers == 1


def test_naturally_completed_scout_is_not_recorded_as_culled(tmp_path: Path, monkeypatch):
    policy = PackPolicy(
        mode="on", backend="direct", initial_workers=2, maximum_workers=2, concurrency=2,
        minimum_runtime_seconds=0, minimum_progress_events=0, judge_interval_seconds=0,
        cull_cooldown_seconds=0, minimum_score_gap=1, survivors=1,
        semantic_judge=False)
    contract = RunContract(
        "rank a cull race honestly", str(tmp_path), budget=Budget(max_total_workers=4), pack=policy)
    engine = Engine(tmp_path, adapter(), base_config())
    state = engine.new_run(contract); state.round = 1; state.status = RunStatus.WORKING

    class RacingProcess:
        progress_events = 1
        started_monotonic = time.monotonic()

        def __init__(self, worker_id: str):
            self.worker_id = worker_id
            self.process = SimpleNamespace(pid=11000 + int(worker_id[-1]), returncode=None)

        async def terminate(self, _reason: str):
            if self.worker_id.endswith("1"):
                self.process.returncode = 0
                return False
            self.process.returncode = 1
            return True

        async def wait(self, *_args):
            self.process.returncode = 0 if self.process.returncode is None else self.process.returncode
            return ProcessOutcome(
                ["worker"], self.process.returncode, "", "", "start", "finish")

    async def start(spec, _prompt, _workspace, artifact):
        artifact.mkdir(parents=True, exist_ok=True)
        final = artifact / "final.txt"; final.write_text(spec.worker_id, encoding="utf-8")
        return RacingProcess(spec.worker_id), final

    monkeypatch.setattr(engine.adapter, "start", start)
    monkeypatch.setattr(engine.adapter, "finish", lambda *_args: {
        "parse_confidence": 1.0, "evidence": []})
    monkeypatch.setattr(pack_module, "deterministic_score", lambda text, *_args: (
        10 if "scout-1-1" in text else 90))
    monkeypatch.setattr(pack_module, "unique_evidence", lambda *_args: False)
    results = asyncio.run(engine._pack_direct(
        state, contract, engine.store.run_dir(state.run_id) / "rounds" / "001", {}, {}))
    low = next(item for item in results if item["worker_id"] == "scout-1-1")
    assert low["status"] == "completed"
    assert not (engine.store.run_dir(state.run_id) / "cull-decisions.jsonl").exists()


def test_direct_pack_synthesis_reaches_next_single_writer(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOUND_FAKE_SCENARIO", "direct-loop")
    (tmp_path / "test_answer.py").write_text(
        "from answer import answer\nassert answer() == 42\n", encoding="utf-8")
    policy = PackPolicy(mode="on", backend="direct", initial_workers=3, concurrency=3,
                        minimum_runtime_seconds=0.1, minimum_progress_events=2,
                        judge_interval_seconds=0.25, cull_cooldown_seconds=0.02,
                        minimum_score_gap=5, survivors=1, semantic_judge=False)
    contract = RunContract("make answer pass", str(tmp_path),
                           [[sys.executable, "test_answer.py"]],
                           budget=Budget(max_rounds=3, max_total_workers=7), pack=policy)
    engine = Engine(tmp_path, adapter(), base_config())
    result = asyncio.run(engine.run(engine.new_run(contract), contract))
    assert result.status is RunStatus.DONE and result.round == 2
    prompt = (tmp_path / ".second-writer-prompt").read_text(encoding="utf-8")
    assert "pack_digest" in prompt and "root cause" in prompt and "retained" in prompt
    assert result.rounds[0]["pack_ranking"][0]["retained"] is True
    assert result.rounds[0]["next_fingerprint"] == fingerprint(
        "writer", contract.objective, "fix failed test", [], contract.verify)


def test_resume_checkpoint(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOUND_FAKE_SCENARIO", "single-writer")
    contract = RunContract("serialize", str(tmp_path), pack=PackPolicy(mode="off"))
    config = base_config(); config["context"]["maximum_trace_excerpt_bytes"] = 512
    engine = Engine(tmp_path, adapter(), config)
    state = engine.new_run(contract); state.round = 1
    run_dir = engine.store.run_dir(state.run_id)
    partial = run_dir / "rounds" / "001" / "writer" / "stdout.log"
    partial.parent.mkdir(parents=True, exist_ok=True); partial.write_text("partial", encoding="utf-8")
    (partial.parent / "trace.jsonl").write_text("x" * 2000 + "\nROOT_CAUSE=parser\n", encoding="utf-8")
    (partial.parent / "prompt.txt").write_text("unfinished", encoding="utf-8")
    (partial.parent.parent / "writer-intent.json").write_text(json.dumps({
        "schema_version": 1, "round": 1, "evidence_hash": "a" * 64,
        "scheduled_fingerprint": "", "git_diff": [], "git_identity": "",
    }), encoding="utf-8")
    (run_dir / "cull-decisions.jsonl").write_text(json.dumps({
        "worker_id": "scout-1-2", "score": 1, "reason": "duplicate evidence",
        "timestamp": "2026-01-01T00:00:00+00:00", "order": 1}) + "\n", encoding="utf-8")
    state.status = RunStatus.FAILED_INTERNAL; engine.store.save_state(state)
    engine._recover_interrupted(state)
    assert partial.read_text() == "partial" and state.status is RunStatus.WORKING
    assert json.loads((partial.parent / "result.json").read_text())["status"] == "interrupted"
    engine._recover_interrupted(state)
    assert state.rounds == [] and engine.store.fingerprints(state.run_id) == []
    resumed = Engine(tmp_path, adapter(), config)
    loaded = resumed.store.load_state(state.run_id); loaded.round += 1
    context, _ = resumed._context(loaded, contract)
    prompt = writer_prompt(contract.objective, context)
    assert any(item["artifact_dir"] == str(partial.parent.relative_to(run_dir))
               for item in context["resume_recovery"]["workers"])
    assert all(len(item["artifact_sha256"]) == 64
               for item in context["resume_recovery"]["workers"])
    assert "ROOT_CAUSE=parser" in prompt and "scout-1-2" in prompt
    assert len(json.dumps(context["resume_recovery"], ensure_ascii=False).encode()) <= resumed.trace_limit
    assert "omitted" in context["resume_recovery"]


def test_resume_rehydrates_partial_pack_evidence_after_prepack_checkpoint(tmp_path: Path):
    engine = Engine(tmp_path, adapter(), base_config())
    contract = RunContract("keep completed Scout evidence", str(tmp_path),
                           pack=PackPolicy(mode="on"))
    state = engine.new_run(contract)
    state.status = RunStatus.WORKING; state.round = 1; state.idle_rounds = 1
    writer = {"status": "continue", "summary": "stalled", "action_taken": "inspect",
              "evidence": [], "changed_files": [], "commands_run": [],
              "verification_observed": [], "next_action": "try evidence",
              "parallel_request": {"needed": False}, "blocker": None}
    evidence_hash = _round_evidence_hash(state, writer, [], [])
    recovery_module.save_round_checkpoint(engine, state, dump(RoundState(
        number=1, writer=writer, idle=True, evidence_hash=evidence_hash)), None)
    scout = engine.store.run_dir(state.run_id) / "rounds" / "001" / "scouts" / "scout-1-1"
    scout.mkdir(parents=True)
    (scout / "prompt.txt").write_text("unfinished Scout", encoding="utf-8")
    (scout / "trace.jsonl").write_text(
        '{"type":"evidence","claim":"new root cause","source":"trace.log"}\n',
        encoding="utf-8")
    engine.store.save_state(state)

    engine._recover_interrupted(state)
    assert state.rounds[-1]["pack_evidence"] is True
    assert "new root cause" in state.rounds[-1]["scouts"][0]["partial_excerpt"]
    assert state.rounds[-1]["evidence_hash"] != evidence_hash and state.idle_rounds == 0


def test_recovery_redacts_agentflow_run_artifacts_after_crash(tmp_path: Path, monkeypatch):
    secret = "hound-resume-secret-value"
    monkeypatch.setenv("HOUND_TEST_SECRET", secret)
    engine = Engine(tmp_path, adapter(), base_config())
    contract = RunContract("redact interrupted evidence", str(tmp_path),
                           pack=PackPolicy(mode="off"))
    state = engine.new_run(contract); state.status = RunStatus.WORKING; state.round = 1
    run_dir = engine.store.run_dir(state.run_id)
    leaked = run_dir / "rounds" / "001" / "agentflow" / "runs" / "one" / "run.json"
    leaked.parent.mkdir(parents=True)
    leaked.write_text(json.dumps({"stderr": secret}), encoding="utf-8")
    direct = run_dir / "rounds" / "001" / "writer" / "final.txt"
    direct.parent.mkdir(parents=True)
    direct.write_text(secret, encoding="utf-8")
    engine.store.save_state(state)

    engine._recover_interrupted(state)

    text = leaked.read_text(encoding="utf-8")
    assert secret not in text and "[REDACTED]" in text
    assert direct.read_text(encoding="utf-8") == "[REDACTED]"


def test_async_recovery_cancelled_hash_commits_no_checkpoint(tmp_path: Path, monkeypatch):
    engine = Engine(tmp_path, adapter(), base_config())
    contract = RunContract("cancel recovery hashing", str(tmp_path),
                           pack=PackPolicy(mode="off"))
    state = engine.new_run(contract); state.status = RunStatus.WORKING; state.round = 1
    run_dir = engine.store.run_dir(state.run_id)
    artifact = run_dir / "rounds" / "001" / "writer"
    artifact.mkdir(parents=True)
    (artifact / "prompt.txt").write_text("unfinished", encoding="utf-8")
    engine.store.save_state(state)

    async def cancelled_hash(*_args, **_kwargs):
        return None

    monkeypatch.setattr(recovery_module, "artifact_sha256_async", cancelled_hash)
    recovered = asyncio.run(recover_interrupted_async(
        engine, state, run_dir / "cancel.requested"))

    assert recovered is False
    assert not (artifact / "result.json").exists()
    assert not (run_dir / "resume-context.json").exists()
    assert state.status is RunStatus.WORKING and state.message == ""


def test_async_recovery_redaction_yields_and_stops_after_deadline(
        tmp_path: Path, monkeypatch):
    engine = Engine(tmp_path, adapter(), base_config())
    contract = RunContract("bound recovery redaction", str(tmp_path),
                           pack=PackPolicy(mode="off"))
    state = engine.new_run(contract); state.status = RunStatus.WORKING; state.round = 1
    run_dir = engine.store.run_dir(state.run_id)
    engine.store.save_state(state)
    started, stopped = threading.Event(), threading.Event()

    def slow_redaction(_root, _values=None, _names=None,
                       _cancel_path=None, stop_event=None):
        started.set()
        while stop_event is not None and not stop_event.is_set():
            time.sleep(0.001)
        stopped.set()
        return False

    monkeypatch.setattr(process_module, "redact_artifacts", slow_redaction)

    async def scenario():
        try:
            async with asyncio.timeout(0.03):
                await recover_interrupted_async(
                    engine, state, run_dir / "cancel.requested")
        except TimeoutError:
            return
        raise AssertionError("recovery redaction ignored the outer deadline")

    asyncio.run(scenario())
    assert started.is_set() and stopped.wait(0.5)
    assert not (run_dir / "resume-context.json").exists()


def test_repeated_crash_recovery_keeps_earlier_partial_evidence(tmp_path: Path):
    engine = Engine(tmp_path, adapter(), base_config())
    contract = RunContract("survive repeated crashes", str(tmp_path), [[sys.executable, "-V"]])
    state = engine.new_run(contract); state.status = RunStatus.WORKING; state.round = 1
    first = engine.store.run_dir(state.run_id) / "rounds" / "001" / "writer"
    first.mkdir(parents=True)
    (first / "prompt.txt").write_text("first", encoding="utf-8")
    (first / "stdout.log").write_text("EVIDENCE_A", encoding="utf-8")
    engine.store.save_state(state)
    engine._recover_interrupted(state)

    state.round = 2; state.status = RunStatus.WORKING
    second = engine.store.run_dir(state.run_id) / "rounds" / "002" / "writer"
    second.mkdir(parents=True)
    (second / "prompt.txt").write_text("second", encoding="utf-8")
    (second / "stdout.log").write_text("EVIDENCE_B", encoding="utf-8")
    engine.store.save_state(state)
    engine._recover_interrupted(state)
    context = json.loads((engine.store.run_dir(state.run_id) /
                          "resume-context.json").read_text())
    artifacts = {item["artifact_dir"]: item for item in context["workers"]}
    assert str(first.relative_to(engine.store.run_dir(state.run_id))) in artifacts
    assert str(second.relative_to(engine.store.run_dir(state.run_id))) in artifacts
    assert {item.get("partial_excerpt") for item in artifacts.values()} >= {
        "EVIDENCE_A", "EVIDENCE_B"}


def test_crash_checkpoint_restores_round_and_fingerprint_evidence(tmp_path: Path, monkeypatch):
    contract = RunContract(
        "preserve failed strategy", str(tmp_path),
        [[sys.executable, "-c", "raise SystemExit(1)"]],
        budget=Budget(max_total_workers=6), pack=PackPolicy(mode="on"))
    engine = Engine(tmp_path, adapter(), base_config())
    state = engine.new_run(contract); state.status = RunStatus.WORKING
    engine.store.save_state(state)

    async def writer(*_args):
        return {"status": "continue", "summary": "failed strategy", "action_taken": "try one",
                "evidence": [{"claim": "same failure"}], "changed_files": [],
                "commands_run": [], "verification_observed": [], "next_action": "try two",
                "parallel_request": {"needed": False}, "blocker": None}

    async def crash(*_args):
        raise RuntimeError("simulated crash after checkpoint")

    monkeypatch.setattr(engine, "_writer", writer)
    monkeypatch.setattr(engine, "_pack", crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        asyncio.run(engine._round(state, contract))
    assert state.rounds == []

    resumed = Engine(tmp_path, adapter(), base_config())
    loaded = resumed.store.load_state(state.run_id)
    resumed._recover_interrupted(loaded)
    assert [item["number"] for item in loaded.rounds] == [1]
    record = resumed.store.fingerprints(state.run_id)[-1]
    round_state = loaded.rounds[-1]
    assert record["evidence_hash"] == _round_evidence_hash(
        loaded, {}, [], [], git_diff=round_state["git_diff"],
        git_identity=round_state["git_identity"])


def test_writer_final_resumes_same_round_before_verification_checkpoint(
        tmp_path: Path, monkeypatch):
    contract = RunContract(
        "preserve completed Writer action", str(tmp_path), [[sys.executable, "-V"]],
        budget=Budget(max_rounds=1, max_total_workers=2), pack=PackPolicy(mode="off"))
    engine = Engine(tmp_path, adapter(), base_config())
    state = engine.new_run(contract); state.status = RunStatus.WORKING
    engine.store.save_state(state)
    real_verify = engine_module.verify_commands

    async def crash_verification(*_args, **_kwargs):
        raise RuntimeError("crash before verification checkpoint")

    monkeypatch.setattr(engine_module, "verify_commands", crash_verification)
    with pytest.raises(RuntimeError, match="before verification checkpoint"):
        asyncio.run(engine._round(state, contract))
    run_dir = engine.store.run_dir(state.run_id)
    assert (run_dir / "rounds" / "001" / "writer-intent.json").exists()
    writer_dir = run_dir / "rounds" / "001" / "writer"
    assert (writer_dir / "final.txt").exists()
    (writer_dir / "result.json").unlink()
    (writer_dir / "outcome.json").unlink()
    assert state.rounds == []

    resumed = Engine(tmp_path, adapter(), base_config())
    loaded = resumed.store.load_state(state.run_id)
    calls = {"writer": 0, "verifier": 0}

    async def no_writer(*_args):
        calls["writer"] += 1
        raise AssertionError("completed Writer ran again")

    async def verifier(review_state, *_args):
        calls["verifier"] += 1; review_state.total_workers += 1
        resumed._transition(review_state, RunStatus.REVIEWING)
        return {"verdict": "accept", "verified_claims": ["machine proof"],
                "unverified_claims": [], "blocking_issues": [],
                "nonblocking_issues": [], "required_next_action": None}

    monkeypatch.setattr(engine_module, "verify_commands", real_verify)
    monkeypatch.setattr(resumed, "_writer", no_writer)
    monkeypatch.setattr(resumed, "_verifier", verifier)
    result = asyncio.run(resumed.run(loaded, contract, resume=True))
    assert result.status is RunStatus.DONE and result.round == 1
    assert calls == {"writer": 0, "verifier": 1}
    assert result.rounds[-1]["verification"][0]["passed"] is True
    assert any(item["fingerprint"] == result.rounds[-1]["fingerprint"] and
               item["status"] == "passed" for item in resumed.store.fingerprints(state.run_id))


def test_malformed_round_checkpoint_does_not_poison_saved_state(tmp_path: Path):
    engine = Engine(tmp_path, adapter(), base_config())
    state = engine.new_run(RunContract("reject corrupt checkpoint", str(tmp_path)))
    state.status = RunStatus.WORKING; state.round = 1
    engine.store.save_state(state)
    checkpoint = engine.store.run_dir(state.run_id) / "rounds" / "001" / "checkpoint.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(json.dumps({
        "schema_version": 1, "round": {"number": 1, "writer": []},
        "run": {}, "fingerprint": None}), encoding="utf-8")

    with pytest.raises(ValueError, match="stored round writer"):
        engine._recover_interrupted(state)
    assert state.rounds == []
    assert engine.store.load_state(state.run_id).rounds == []


def test_resume_review_retries_only_verifier_at_round_budget(tmp_path: Path, monkeypatch):
    contract = RunContract(
        "resume final review", str(tmp_path), [[sys.executable, "-V"]],
        budget=Budget(max_rounds=1, max_total_workers=3), pack=PackPolicy(mode="off"))
    engine = Engine(tmp_path, adapter(), base_config())
    state = engine.new_run(contract)
    state.status = RunStatus.VERIFYING; state.round = 1; state.total_workers = 2
    state.rounds = [{"number": 1, "status": "verified",
                     "writer": {"status": "candidate_done"},
                     "verification": [{"passed": True}], "path_issues": [],
                     "git_diff": [],
                     "git_identity": non_git_identity(engine, state.run_id)}]
    engine.store.save_state(state)
    calls = {"verifier": 0}

    async def no_writer(*_args):
        raise AssertionError("completed Writer round ran again")

    async def verifier(*_args):
        calls["verifier"] += 1
        return {"verdict": "accept", "verified_claims": ["stored machine proof"],
                "unverified_claims": [], "blocking_issues": [], "nonblocking_issues": [],
                "required_next_action": None}

    monkeypatch.setattr(engine, "_writer", no_writer)
    monkeypatch.setattr(engine, "_verifier", verifier)
    result = asyncio.run(engine.run(state, contract, resume=True))
    assert result.status is RunStatus.DONE and result.round == 1
    assert calls["verifier"] == 1
    proof = json.loads((engine.store.run_dir(state.run_id) / "final" /
                        "verification.json").read_text())
    assert proof["round"] == 1 and proof["passed"] is True


def test_resume_review_reuses_durable_verifier_without_worker_budget(tmp_path: Path, monkeypatch):
    contract = RunContract(
        "finish durable review", str(tmp_path), [[sys.executable, "-V"]],
        budget=Budget(max_rounds=1, max_total_workers=2), pack=PackPolicy(mode="off"))
    engine = Engine(tmp_path, adapter(), base_config())
    state = engine.new_run(contract)
    state.status = RunStatus.REVIEWING; state.round = 1; state.total_workers = 2
    state.rounds = [{"number": 1, "status": "verified",
                     "writer": {"status": "candidate_done"},
                     "verification": [{"passed": True}], "path_issues": [],
                     "git_diff": [],
                     "git_identity": non_git_identity(engine, state.run_id),
                     "verifier": {"verdict": "accept", "verified_claims": ["proof"],
                                  "unverified_claims": [], "blocking_issues": [],
                                  "nonblocking_issues": [], "required_next_action": None}}]
    final = engine.store.run_dir(state.run_id) / "final"; final.mkdir(parents=True)
    (final / "verification.json").write_text(
        '{"round":1,"passed":true}', encoding="utf-8")
    engine.store.save_state(state)

    async def no_worker(*_args):
        raise AssertionError("durable final review spawned a worker")

    monkeypatch.setattr(engine, "_writer", no_worker)
    monkeypatch.setattr(engine, "_verifier", no_worker)
    result = asyncio.run(engine.run(state, contract, resume=True))
    assert result.status is RunStatus.DONE and result.total_workers == 2
    assert json.loads((final / "verifier.json").read_text())["round"] == 1


def test_resume_review_recovers_completed_attempt_before_final_copy(tmp_path: Path, monkeypatch):
    contract = RunContract(
        "recover completed review", str(tmp_path), [[sys.executable, "-V"]],
        budget=Budget(max_rounds=1, max_total_workers=2), pack=PackPolicy(mode="off"))
    engine = Engine(tmp_path, adapter(), base_config())
    state = engine.new_run(contract)
    state.status = RunStatus.REVIEWING; state.round = 1; state.total_workers = 2
    state.rounds = [{"number": 1, "status": "verified",
                     "writer": {"status": "candidate_done"},
                     "verification": [{"passed": True}], "path_issues": [],
                     "git_diff": [],
                     "git_identity": non_git_identity(engine, state.run_id)}]
    attempt = engine.store.run_dir(state.run_id) / "rounds" / "001" / "verifier"
    attempt.mkdir(parents=True)
    (attempt / "result.json").write_text(json.dumps({
        "verdict": "accept", "verified_claims": ["durable proof"],
        "unverified_claims": [], "blocking_issues": [], "nonblocking_issues": [],
        "required_next_action": None}), encoding="utf-8")
    (attempt / "outcome.json").write_text(json.dumps({
        "exit_code": 0, "timed_out": False, "cancelled": False}), encoding="utf-8")
    engine.store.save_state(state)

    async def no_worker(*_args):
        raise AssertionError("completed verifier attempt ran again")

    monkeypatch.setattr(engine, "_writer", no_worker)
    monkeypatch.setattr(engine, "_verifier", no_worker)
    result = asyncio.run(engine.run(state, contract, resume=True))
    assert result.status is RunStatus.DONE and result.total_workers == 2
    final = engine.store.run_dir(state.run_id) / "final" / "verifier.json"
    stored = json.loads(final.read_text())
    assert stored["verified_claims"] == ["durable proof"]
    assert stored["artifact_dir"].endswith("rounds/001/verifier")
    assert len(stored["artifact_sha256"]) == 64


def test_repeated_verifier_revise_is_evidence_and_triggers_pack(tmp_path: Path):
    contract = RunContract("repair final review", str(tmp_path), [[sys.executable, "-V"]])
    engine = Engine(tmp_path, adapter(), base_config())
    state = engine.new_run(contract); state.status = RunStatus.REVIEWING; state.round = 1
    state.rounds = [{"number": 1, "status": "verified",
                     "writer": {"status": "candidate_done", "next_action": "repair issue"},
                     "verification": [{"passed": True}], "path_issues": [],
                     "fingerprint": "writer-action", "git_diff": [], "git_identity": ""}]
    verifier = {"verdict": "revise", "verified_claims": ["tests pass"],
                "unverified_claims": ["edge case"], "blocking_issues": ["missing edge case"],
                "nonblocking_issues": [], "required_next_action": "add edge-case proof"}

    engine._review_result(state, verifier)
    context, _ = engine._context(state, contract)
    assert state.status is RunStatus.WORKING and state.repeated_verifier_failures == 1
    assert context["last_verifier"]["required_next_action"] == "add edge-case proof"
    assert state.rounds[-1]["evidence_hash"]
    assert state.rounds[-1]["scheduled_action"] == "add edge-case proof"
    assert state.rounds[-1]["next_fingerprint"] == fingerprint(
        "writer", contract.objective, "add edge-case proof", [], contract.verify)
    assert any(item["fingerprint"] == "writer-action" and item["status"] == "failed"
               for item in engine.store.fingerprints(state.run_id))

    state.status = RunStatus.REVIEWING
    engine._review_result(state, verifier)
    assert state.repeated_verifier_failures == 1

    state.round = 2; state.status = RunStatus.REVIEWING
    state.rounds.append({"number": 2, "status": "verified",
                         "writer": {"status": "candidate_done"},
                         "verification": [{"passed": True}], "path_issues": [],
                         "fingerprint": "second-action", "git_diff": [],
                         "git_identity": ""})
    engine._review_result(state, verifier)
    assert state.repeated_verifier_failures == 2
    assert should_pack(PackPolicy(mode="auto"), state, {})


def test_repeated_final_revise_runs_pack_before_third_candidate(tmp_path: Path, monkeypatch):
    contract = RunContract(
        "finish after independent review", str(tmp_path), [[sys.executable, "-V"]],
        budget=Budget(max_rounds=4, max_total_workers=12),
        pack=PackPolicy(mode="auto", initial_workers=2, maximum_workers=2,
                        concurrency=2, survivors=1))
    engine = Engine(tmp_path, adapter(), base_config())
    calls = {"writer": 0, "verifier": 0, "pack": 0}

    async def writer(state, _contract, _round_dir, context):
        calls["writer"] += 1
        if state.round > 1:
            assert context["last_verifier"]["required_next_action"] == "repair edge case"
        return {"status": "candidate_done", "summary": "candidate",
                "action_taken": f"candidate variant {state.round}",
                "evidence": [{"claim": f"candidate proof {state.round}"}],
                "changed_files": [], "commands_run": [], "verification_observed": [],
                "next_action": "done", "parallel_request": {"needed": False},
                "blocker": None}

    async def verifier(state, *_args):
        calls["verifier"] += 1
        engine._transition(state, RunStatus.REVIEWING)
        if calls["verifier"] == 3:
            return {"verdict": "accept", "verified_claims": ["edge case fixed"],
                    "unverified_claims": [], "blocking_issues": [],
                    "nonblocking_issues": [], "required_next_action": None}
        return {"verdict": "revise", "verified_claims": ["machine proof"],
                "unverified_claims": ["edge case"],
                "blocking_issues": ["edge case remains"], "nonblocking_issues": [],
                "required_next_action": "repair edge case"}

    async def pack(*_args):
        calls["pack"] += 1
        return [{"worker_id": "scout-3-1", "status": "completed",
                 "evidence": [{"claim": "independent edge-case proof"}]}]

    monkeypatch.setattr(engine, "_writer", writer)
    monkeypatch.setattr(engine, "_verifier", verifier)
    monkeypatch.setattr(engine, "_pack", pack)
    result = asyncio.run(engine.run(engine.new_run(contract), contract))
    assert result.status is RunStatus.DONE and result.round == 3
    assert calls == {"writer": 3, "verifier": 3, "pack": 1}


def test_empty_pack_stops_repeated_final_revise(tmp_path: Path, monkeypatch):
    contract = RunContract(
        "stop repeated review", str(tmp_path), [[sys.executable, "-V"]],
        budget=Budget(max_rounds=4, max_total_workers=12),
        pack=PackPolicy(mode="auto", initial_workers=2, maximum_workers=2,
                        concurrency=2, survivors=1))
    engine = Engine(tmp_path, adapter(), base_config())
    calls = {"writer": 0, "verifier": 0, "pack": 0}

    async def writer(state, *_args):
        calls["writer"] += 1
        return {"status": "candidate_done", "summary": "candidate",
                "action_taken": f"candidate variant {state.round}",
                "evidence": [{"claim": f"candidate proof {state.round}"}],
                "changed_files": [], "commands_run": [], "verification_observed": [],
                "next_action": "done", "parallel_request": {"needed": False},
                "blocker": None}

    async def verifier(state, *_args):
        calls["verifier"] += 1
        engine._transition(state, RunStatus.REVIEWING)
        return {"verdict": "revise", "verified_claims": ["machine proof"],
                "unverified_claims": ["edge case"],
                "blocking_issues": ["edge case remains"], "nonblocking_issues": [],
                "required_next_action": "repair edge case"}

    async def pack(*_args):
        calls["pack"] += 1
        return []

    monkeypatch.setattr(engine, "_writer", writer)
    monkeypatch.setattr(engine, "_verifier", verifier)
    monkeypatch.setattr(engine, "_pack", pack)
    result = asyncio.run(engine.run(engine.new_run(contract), contract))
    assert result.status is RunStatus.BLOCKED and result.round == 3
    assert calls == {"writer": 3, "verifier": 3, "pack": 1}
    assert result.message == "idle budget exhausted without new evidence"


def test_resume_review_rejects_stale_workspace_proof(tmp_path: Path, monkeypatch):
    (tmp_path / "answer.py").write_text("value = 1\n", encoding="utf-8")
    contract = RunContract(
        "reject stale proof", str(tmp_path), [[sys.executable, "-V"]],
        budget=Budget(max_rounds=1, max_total_workers=2), pack=PackPolicy(mode="off"))
    engine = Engine(tmp_path, adapter(), base_config())
    state = engine.new_run(contract)
    state.status = RunStatus.REVIEWING; state.round = 1; state.total_workers = 2
    state.rounds = [{"number": 1, "status": "verified",
                     "writer": {"status": "candidate_done"},
                     "verification": [{"passed": True}], "path_issues": [],
                     "git_diff": [],
                     "git_identity": non_git_identity(engine, state.run_id),
                     "verifier": {"verdict": "accept", "verified_claims": ["old proof"],
                                  "unverified_claims": [], "blocking_issues": [],
                                  "nonblocking_issues": [], "required_next_action": None}}]
    engine.store.save_state(state)
    (tmp_path / "answer.py").write_text("value = 2\n", encoding="utf-8")

    async def no_worker(*_args):
        raise AssertionError("stale completed round spawned a worker past its round budget")

    monkeypatch.setattr(engine, "_writer", no_worker)
    monkeypatch.setattr(engine, "_verifier", no_worker)
    result = asyncio.run(engine.run(state, contract, resume=True))
    assert result.status is RunStatus.BUDGET_EXHAUSTED
    assert "review_checkpoint_stale" in (
        engine.store.run_dir(state.run_id) / "events.jsonl").read_text()


def test_resume_review_is_bounded_by_remaining_wall_budget(tmp_path: Path, monkeypatch):
    contract = RunContract(
        "bound resumed review", str(tmp_path), [[sys.executable, "-V"]],
        budget=Budget(max_rounds=1, max_total_workers=3, max_wall_minutes=1),
        pack=PackPolicy(mode="off"))
    engine = Engine(tmp_path, adapter(), base_config())
    state = engine.new_run(contract)
    state.status = RunStatus.REVIEWING; state.round = 1; state.total_workers = 2
    state.active_seconds = 59.95
    state.rounds = [{"number": 1, "status": "verified",
                     "writer": {"status": "candidate_done"},
                     "verification": [{"passed": True}], "path_issues": [],
                     "git_diff": [],
                     "git_identity": non_git_identity(engine, state.run_id)}]
    proof = engine.store.run_dir(state.run_id) / "final" / "verification.json"
    proof.parent.mkdir(parents=True); proof.write_text(
        '{"round":1,"passed":true}', encoding="utf-8")
    engine.store.save_state(state)

    async def slow_verifier(*_args):
        await asyncio.sleep(1)
        raise AssertionError("review exceeded wall budget")

    monkeypatch.setattr(engine, "_verifier", slow_verifier)
    started = time.monotonic()
    result = asyncio.run(engine.run(state, contract, resume=True))
    assert result.status is RunStatus.BUDGET_EXHAUSTED
    assert time.monotonic() - started < 0.5


def test_resume_refuses_recorded_live_worker_before_artifact_mutation(
        tmp_path: Path, monkeypatch):
    secret = "live-worker-artifact-must-stay-untouched"
    monkeypatch.setenv("RECOVERY_TOKEN", secret)
    contract = RunContract("resume safely", str(tmp_path), [[sys.executable, "-V"]],
                           pack=PackPolicy(mode="off"))
    engine = Engine(tmp_path, adapter(), base_config())
    state = engine.new_run(contract)
    state.status = RunStatus.WORKING
    engine.store.save_state(state)
    artifact = engine.store.run_dir(state.run_id) / "rounds" / "001" / "writer"
    artifact.mkdir(parents=True)
    (artifact / "launch.json").write_text(json.dumps({"worker_pid": os.getpid()}), encoding="utf-8")
    (artifact / "stdout.log").write_text(secret, encoding="utf-8")
    try:
        engine._recover_interrupted(state)
    except RuntimeError as error:
        assert f"PID={os.getpid()}" in str(error)
    else:
        raise AssertionError("resume accepted a recorded live worker")
    assert (artifact / "stdout.log").read_text(encoding="utf-8") == secret


def test_resume_rejects_malformed_identity_for_live_pid(tmp_path: Path):
    contract = RunContract("resume fail closed", str(tmp_path), pack=PackPolicy(mode="off"))
    engine = Engine(tmp_path, adapter(), base_config())
    state = engine.new_run(contract); state.status = RunStatus.WORKING
    engine.store.save_state(state)
    artifact = engine.store.run_dir(state.run_id) / "rounds" / "001" / "writer"
    artifact.mkdir(parents=True)
    (artifact / "launch.json").write_text(json.dumps({
        "worker_pid": os.getpid(), "worker_ppid": str(os.getppid()),
        "hound_pid": os.getppid(),
    }), encoding="utf-8")
    try:
        engine._recover_interrupted(state)
    except HoundEnvironmentError as error:
        assert "invalid live worker identity" in str(error) and f"PID={os.getpid()}" in str(error)
    else:
        raise AssertionError("resume accepted malformed identity for a live PID")


def test_resume_rejects_unverifiable_unfinished_worker_pid(tmp_path: Path):
    engine = Engine(tmp_path, adapter(), base_config())
    state = engine.new_run(RunContract("resume fail closed", str(tmp_path),
                                       pack=PackPolicy(mode="off")))
    state.status = RunStatus.WORKING; engine.store.save_state(state)
    artifact = engine.store.run_dir(state.run_id) / "rounds" / "001" / "writer"
    artifact.mkdir(parents=True)
    (artifact / "launch.json").write_text("{}", encoding="utf-8")
    with pytest.raises(HoundResumeRetryError, match="unverifiable unfinished worker PID"):
        engine._recover_interrupted(state)
    assert not (artifact / "interrupted.json").exists()


def test_resume_hydrates_windows_identity_before_creation_time_comparison(
        tmp_path: Path, monkeypatch):
    artifact = tmp_path / "rounds" / "001" / "writer"
    artifact.mkdir(parents=True)
    (artifact / "launch.json").write_text(json.dumps({
        "worker_pid": 12345, "worker_ppid": 6789, "hound_pid": 6789,
        "worker_creation_time": 111,
    }), encoding="utf-8")
    monkeypatch.setattr(recovery_module.os, "name", "nt")
    monkeypatch.setattr(recovery_module, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(recovery_module, "windows_process_table", lambda: {
        12345: process_module.WindowsProcess(12345, 6789, "other.exe")})
    monkeypatch.setattr(recovery_module, "windows_process_identity", lambda pid, process:
                        process_module.WindowsProcess(pid, process.parent_pid,
                                                      process.executable, 111))

    with pytest.raises(HoundResumeRetryError, match="recorded worker is alive"):
        recovery_module._validated_unfinished_launches(tmp_path)
    assert not (artifact / "interrupted.json").exists()


@pytest.mark.parametrize("resume", [False, True])
def test_run_or_resume_refuses_other_live_orphan_writer(
        tmp_path: Path, monkeypatch, resume: bool):
    contract = RunContract("keep one Writer", str(tmp_path), [[sys.executable, "-V"]],
                           pack=PackPolicy(mode="off"))
    engine = Engine(tmp_path, adapter(), base_config())
    old = engine.new_run(contract)
    artifact = engine.store.run_dir(old.run_id) / "rounds" / "001" / "writer"
    artifact.mkdir(parents=True)
    worker_pid, owner_pid = 987654, 456789
    (artifact / "launch.json").write_text(json.dumps({
        "worker_pid": worker_pid, "worker_ppid": owner_pid,
        "hound_pid": owner_pid, "worker_executable": "codex.exe",
    }), encoding="utf-8")
    monkeypatch.setattr(recovery_module, "pid_alive", lambda pid: pid == worker_pid)
    monkeypatch.setattr(recovery_module, "windows_process_table", lambda: {
        worker_pid: process_module.WindowsProcess(worker_pid, owner_pid, "codex.exe")})
    state = engine.new_run(contract)
    if resume:
        state.status = RunStatus.WORKING; engine.store.save_state(state)

    async def no_writer(*_args):
        raise AssertionError("new Writer overlapped a live orphan")

    monkeypatch.setattr(engine, "_writer", no_writer)
    with pytest.raises(HoundResumeRetryError, match="recorded worker is alive"):
        asyncio.run(engine.run(state, contract, resume=resume))
    assert state.status is (RunStatus.WORKING if resume else RunStatus.CREATED)
    assert state.total_workers == 0
    assert not (engine.store.run_dir(state.run_id) / "summary.md").exists()


def test_resume_reapplies_durable_round_terminal_reason(tmp_path: Path, monkeypatch):
    contract = RunContract(
        "stop after durable idle", str(tmp_path), [[sys.executable, "-V"]],
        budget=Budget(max_rounds=3, max_idle_rounds=1), pack=PackPolicy(mode="off"))
    engine = Engine(tmp_path, adapter(), base_config())
    state = engine.new_run(contract)
    state.status = RunStatus.WORKING; state.round = 1; state.idle_rounds = 1
    state.rounds = [{"number": 1, "writer": {"status": "continue"},
                     "pack_evidence": False, "next_fingerprint": "next"}]
    engine.store.save_state(state)

    async def no_writer(*_args):
        raise AssertionError("resume bypassed the durable terminal decision")

    monkeypatch.setattr(engine, "_writer", no_writer)
    result = asyncio.run(engine.run(state, contract, resume=True))
    assert result.status is RunStatus.BLOCKED and result.round == 1
    assert result.message == "idle budget exhausted without new evidence"


def test_round_budget_exhaustion(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOUND_FAKE_SCENARIO", "good")
    contract = RunContract("no verify means no proof", str(tmp_path), budget=Budget(max_rounds=1),
                           pack=PackPolicy(mode="on"))
    engine = Engine(tmp_path, adapter(), base_config())

    async def useless_pack(*_args, **_kwargs):
        raise AssertionError("Pack started without a remaining Writer round")

    monkeypatch.setattr(engine, "_pack", useless_pack)
    result = asyncio.run(engine.run(engine.new_run(contract), contract))
    assert result.status is RunStatus.BUDGET_EXHAUSTED


def test_pack_missions_deduplicate_requested_defaults(tmp_path: Path):
    policy = PackPolicy(initial_workers=3, maximum_workers=3, concurrency=3)
    contract = RunContract("hunt", str(tmp_path), budget=Budget(max_total_workers=8), pack=policy)
    engine = Engine(tmp_path, adapter(), base_config())
    state = engine.new_run(contract)
    writer = {"parallel_request": {"missions": ["Reproduce the failure and extract exact logs"]}}
    missions = engine._pack_missions(state, contract, writer)
    assert len(missions) == 3
    assert sum(mission.casefold() == "reproduce the failure and extract exact logs"
               for mission in missions) == 1


def test_pack_receives_current_machine_failure_context(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOUND_FAKE_SCENARIO", "invalid-json")
    contract = RunContract(
        "inspect current failure", str(tmp_path),
        [[sys.executable, "-c", "raise SystemExit(7)"]],
        budget=Budget(max_total_workers=6), pack=PackPolicy(mode="on"))
    engine = Engine(tmp_path, adapter(), base_config())
    state = engine.new_run(contract); state.status = RunStatus.WORKING
    captured = {}

    async def pack(_state, _contract, _round_dir, context, _writer):
        captured.update(context)
        return []

    monkeypatch.setattr(engine, "_pack", pack)
    asyncio.run(engine._round(state, contract))
    assert captured["current_verification"][0]["exit_code"] == 7
    assert captured["current_verification"][0]["passed"] is False
    assert captured["current_path_issues"] == []


def test_pack_digest_keeps_hypotheses_for_next_writer(tmp_path: Path):
    engine = Engine(tmp_path, adapter(), base_config())
    digest = engine.pack.digest([{"number": 1, "scouts": [{
        "worker_id": "scout-1-1", "status": "completed", "retained": True,
        "hypotheses": [{"claim": "configuration race", "confidence": 0.8}],
    }]}])
    assert digest["items"][0]["hypotheses"] == [
        {"claim": "configuration race", "confidence": 0.8}]


def test_pack_digest_deduplicates_scout_evidence_and_keeps_retained(tmp_path: Path):
    engine = Engine(tmp_path, adapter(), base_config())
    evidence = [{"claim": "same fact", "source": "trace.jsonl"}]
    scouts = [
        {"worker_id": "low", "status": "completed", "score": 20, "evidence": evidence},
        {"worker_id": "kept", "status": "completed", "score": 90,
         "retained": True, "evidence": evidence},
        {"worker_id": "unique", "status": "culled", "score": 10,
         "evidence": [{"claim": "unique fact"}]},
    ]
    digest = engine.pack.digest([{"number": 1, "scouts": scouts}])
    assert [item["worker_id"] for item in digest["items"]] == ["kept", "unique"]
    assert digest["omitted"] == []


def test_pack_digest_records_internally_omitted_scouts(tmp_path: Path):
    config = base_config(); config["context"]["maximum_prompt_bytes"] = 2048
    engine = Engine(tmp_path, adapter(), config)
    scouts = [{"worker_id": f"scout-{index}", "status": "completed", "score": index,
               "summary": f"unique-{index}-" + "x" * 500}
              for index in range(10)]
    digest = engine.pack.digest([{"number": 1, "scouts": scouts}])
    assert digest["items"] and digest["omitted"]


def test_context_keeps_bounded_old_and_partial_evidence(tmp_path: Path):
    config = base_config(); config["context"]["retain_recent_rounds"] = 1
    engine = Engine(tmp_path, adapter(), config)
    contract = RunContract("remember evidence", str(tmp_path), [[sys.executable, "-V"]])
    state = engine.new_run(contract)
    state.rounds = [
        {"number": 1, "writer": {"evidence": [{"claim": "old root cause"}]}},
        {"number": 2, "scouts": [{
            "partial_excerpt": '{"type":"evidence","claim":"live partial fact"}\n'
                                  '{"type":"progress"}'}]},
    ]
    context, _ = engine._context(state, contract)
    assert any("old root cause" in item["evidence"] for item in context["older_evidence"])
    assert any("live partial fact" in token for token in _evidence_tokens(state.rounds))


def test_context_prioritizes_latest_evidence_and_records_nested_omissions(tmp_path: Path):
    config = base_config(); config["context"].update(
        {"maximum_prompt_bytes": 4096, "retain_recent_rounds": 1})
    engine = Engine(tmp_path, adapter(), config)
    contract = RunContract(
        "retain the newest proof", str(tmp_path), [[sys.executable, "-V"]],
        expected_exit_code=7, verify_timeout_seconds=19)
    state = engine.new_run(contract)
    state.rounds = [
        {"number": index, "writer": {"evidence": [
            {"claim": f"old-{index}-" + "x" * 400}]}}
        for index in range(1, 12)
    ] + [{"number": 12, "writer": {"status": "continue", "summary": "new",
                                     "artifact_dir": "rounds/012/writer",
                                     "artifact_sha256": "a" * 64,
                                     "next_action": "verify", "evidence": [
                                         {"claim": "LATEST-CONCRETE-EVIDENCE",
                                          "artifact": "rounds/012/writer/stdout.log"}]}}]

    context, omitted = engine._context(state, contract)
    assert any("LATEST-CONCRETE-EVIDENCE".casefold() in token
               for token in context["latest_evidence"] if isinstance(token, str))
    assert {"artifact_dir": "rounds/012/writer", "artifact_sha256": "a" * 64} \
        in context["latest_evidence"]
    assert any("rounds/012/writer/stdout.log" in token
               for token in context["latest_evidence"] if isinstance(token, str))
    assert any(name.startswith("older_evidence.") for name in omitted)
    assert context["acceptance"]["expected_exit_code"] == 7
    assert context["acceptance"]["verify_timeout_seconds"] == 19


def test_writer_timeout_is_not_user_cancellation(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOUND_FAKE_SCENARIO", "writer-timeout")
    config = base_config(); config["workers"]["writer_timeout_seconds"] = 0.1
    contract = RunContract("continue after timeout", str(tmp_path), [[sys.executable, "-V"]],
                           budget=Budget(max_rounds=1), pack=PackPolicy(mode="off"))
    engine = Engine(tmp_path, adapter(), config)
    result = asyncio.run(engine.run(engine.new_run(contract), contract))
    assert result.status is RunStatus.BUDGET_EXHAUSTED
    writer = result.rounds[0]["writer"]
    assert writer["timed_out"] is True and writer["cancelled"] is False
    stored = json.loads((engine.store.run_dir(result.run_id) / "rounds" / "001" /
                         "writer" / "result.json").read_text())
    assert stored["status"] == writer["status"] and stored["timed_out"] is True
    assert not (engine.store.run_dir(result.run_id) / "cancel.requested").exists()


def test_zero_exit_timeout_or_cancel_is_never_semantic_success(tmp_path: Path):
    for flag in ("timed_out", "cancelled"):
        workspace = tmp_path / flag; workspace.mkdir()

        class InterruptedAdapter:
            async def run(self, spec, *_args, **_kwargs):
                outcome = ProcessOutcome(
                    ["worker"], 0, "", "", "start", "finish",
                    **{flag: True})
                values = {
                    "writer": {"status": "candidate_done", "summary": "claimed done"},
                    "verifier": {"verdict": "accept", "verified_claims": ["claim"],
                                 "unverified_claims": [], "blocking_issues": [],
                                 "nonblocking_issues": [], "required_next_action": None},
                    "judge": {"ranking": [{"worker_id": "scout-1-1",
                                             "semantic_score": 99}]},
                }
                return outcome, values[spec.role]

        engine = Engine(workspace, InterruptedAdapter(), base_config())
        contract = RunContract("fail closed", str(workspace), [[sys.executable, "-V"]])
        state = engine.new_run(contract); state.status = RunStatus.WORKING; state.round = 1
        round_dir = engine.store.run_dir(state.run_id) / "rounds" / "001"
        writer = asyncio.run(engine._writer(state, contract, round_dir, {}))
        verifier = asyncio.run(engine._verifier(state, contract, round_dir, {}))
        artifact = round_dir / "scouts" / "scout-1-1"; artifact.mkdir(parents=True)
        ranked = [{"spec": WorkerSpec("scout-1-1", "scout", "inspect", "read-only", 5),
                   "artifact": artifact, "final": artifact / "final.txt", "score": 10,
                   "process": SimpleNamespace(progress_events=1,
                                              started_monotonic=time.monotonic())}]
        semantic = asyncio.run(engine.pack.judge(state, contract, round_dir, ranked))
        assert writer["status"] == "continue"
        assert verifier["verdict"] == "revise" and verifier["blocking_issues"]
        assert semantic == {}


def test_cancel_marker_wins_timeout_and_exhausted_budget(tmp_path: Path, monkeypatch):
    contract = RunContract(
        "cancel wins", str(tmp_path), [[sys.executable, "-V"]],
        budget=Budget(max_wall_minutes=1), pack=PackPolicy(mode="off"))
    engine = Engine(tmp_path, adapter(), base_config())
    state = engine.new_run(contract); state.active_seconds = 59.95
    baseline = engine.store.run_dir(state.run_id) / "baseline.json"
    baseline.write_text('{"schema_version":2,"kind":"unavailable"}', encoding="utf-8")

    async def cancelled_round(current, *_args):
        (engine.store.run_dir(current.run_id) / "cancel.requested").touch()
        await asyncio.sleep(1)

    monkeypatch.setattr(engine, "_round", cancelled_round)
    result = asyncio.run(engine.run(state, contract))
    assert result.status is RunStatus.CANCELLED

    other = engine.new_run(contract); other.active_seconds = 60
    (engine.store.run_dir(other.run_id) / "cancel.requested").touch()
    assert asyncio.run(engine.run(other, contract, resume=True)).status is RunStatus.CANCELLED


def test_resume_cannot_reset_wall_budget(tmp_path: Path, monkeypatch):
    contract = RunContract("bounded", str(tmp_path), [[sys.executable, "-V"]],
                           budget=Budget(max_wall_minutes=1), pack=PackPolicy(mode="off"))
    engine = Engine(tmp_path, adapter(), base_config())
    state = engine.new_run(contract)
    state.active_seconds = 60
    engine.store.save_state(state)

    async def no_baseline(*_args, **_kwargs):
        raise AssertionError("baseline probe started outside the wall budget")

    monkeypatch.setattr(engine_module, "capture_git_baseline", no_baseline)
    result = asyncio.run(engine.run(state, contract, resume=True))
    assert result.status is RunStatus.BUDGET_EXHAUSTED and result.total_workers == 0


def test_content_hashing_is_bounded_by_remaining_wall_budget(tmp_path: Path, monkeypatch):
    contract = RunContract("bound content hashing", str(tmp_path),
                           budget=Budget(max_wall_minutes=1), pack=PackPolicy(mode="off"))
    engine = Engine(tmp_path, adapter(), base_config())
    state = engine.new_run(contract); state.status = RunStatus.WORKING
    state.active_seconds = 59.95
    run_dir = engine.store.run_dir(state.run_id)
    (run_dir / "baseline.json").write_text(
        '{"schema_version":2,"kind":"unavailable"}', encoding="utf-8")
    engine.store.save_state(state)

    async def slow_identity(*_args, **_kwargs):
        await asyncio.sleep(1)
        raise AssertionError("content hashing exceeded the wall budget")

    monkeypatch.setattr(engine_module, "_content_identity", slow_identity)
    started = time.monotonic()
    result = asyncio.run(engine.run(state, contract))
    assert result.status is RunStatus.BUDGET_EXHAUSTED and result.total_workers == 0
    assert time.monotonic() - started < 0.5


def test_resume_hashing_is_bounded_before_active_checkpoint(tmp_path: Path, monkeypatch):
    contract = RunContract("bound recovery hashing", str(tmp_path),
                           budget=Budget(max_wall_minutes=1), pack=PackPolicy(mode="off"))
    engine = Engine(tmp_path, adapter(), base_config())
    state = engine.new_run(contract); state.status = RunStatus.WORKING; state.round = 1
    state.active_seconds = 59.95
    run_dir = engine.store.run_dir(state.run_id)
    artifact = run_dir / "rounds" / "001" / "writer"
    artifact.mkdir(parents=True)
    (artifact / "prompt.txt").write_text("unfinished", encoding="utf-8")
    engine.store.save_state(state)

    async def slow_hash(*_args, **_kwargs):
        await asyncio.sleep(1)
        raise AssertionError("resume hashing exceeded the wall budget")

    monkeypatch.setattr(recovery_module, "artifact_sha256_async", slow_hash)
    started = time.monotonic()
    result = asyncio.run(engine.run(state, contract, resume=True))
    assert result.status is RunStatus.BUDGET_EXHAUSTED and result.total_workers == 0
    assert not (artifact / "result.json").exists()
    assert result.active_seconds >= 59.95 and result.active_started_at == ""
    assert time.monotonic() - started < 0.5


def test_crash_marker_counts_elapsed_time_fail_closed(tmp_path: Path):
    contract = RunContract("bounded crash", str(tmp_path), [[sys.executable, "-V"]],
                           budget=Budget(max_wall_minutes=1), pack=PackPolicy(mode="off"))
    engine = Engine(tmp_path, adapter(), base_config())
    state = engine.new_run(contract)
    state.active_started_at = (datetime.now(UTC) - timedelta(seconds=70)).isoformat()
    engine.store.save_state(state)
    result = asyncio.run(engine.run(state, contract, resume=True))
    assert result.status is RunStatus.BUDGET_EXHAUSTED and result.total_workers == 0
    assert result.active_seconds >= 70 and result.active_started_at == ""


def test_explicit_writer_blocker_stops_immediately(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOUND_FAKE_SCENARIO", "blocked")
    contract = RunContract("needs credential", str(tmp_path), [[sys.executable, "-V"]],
                           budget=Budget(max_rounds=5), pack=PackPolicy(mode="off"))
    engine = Engine(tmp_path, adapter(), base_config())
    result = asyncio.run(engine.run(engine.new_run(contract), contract))
    assert result.status is RunStatus.BLOCKED and result.round == 1
    assert result.message == "missing external credential"


def test_writer_blocker_with_new_safe_action_continues(tmp_path: Path, monkeypatch):
    contract = RunContract(
        "try the available fallback", str(tmp_path), [[sys.executable, "-V"]],
        budget=Budget(max_rounds=2), pack=PackPolicy(mode="off"))
    engine = Engine(tmp_path, adapter(), base_config())

    async def writer(state, *_args):
        if state.round == 1:
            return {"status": "blocked", "summary": "primary route unavailable",
                    "action_taken": "check primary route", "evidence": [],
                    "changed_files": [], "commands_run": [], "verification_observed": [],
                    "next_action": "try safe fallback", "parallel_request": {"needed": False},
                    "blocker": "primary route unavailable"}
        return {"status": "candidate_done", "summary": "fallback worked",
                "action_taken": "try safe fallback", "evidence": [{"claim": "fallback passed"}],
                "changed_files": [], "commands_run": [], "verification_observed": [],
                "next_action": "done", "parallel_request": {"needed": False}, "blocker": None}

    async def verifier(state, *_args):
        engine._transition(state, RunStatus.REVIEWING)
        return {"verdict": "accept", "verified_claims": ["machine gate"],
                "unverified_claims": [], "blocking_issues": [], "nonblocking_issues": [],
                "required_next_action": None}

    monkeypatch.setattr(engine, "_writer", writer)
    monkeypatch.setattr(engine, "_verifier", verifier)
    result = asyncio.run(engine.run(engine.new_run(contract), contract))
    assert result.status is RunStatus.DONE and result.round == 2


def test_direct_pack_reports_cleanup_failure(tmp_path: Path, monkeypatch):
    contract = RunContract("clean up", str(tmp_path), [[sys.executable, "-V"]])
    engine = Engine(tmp_path, adapter(), base_config())
    state = engine.new_run(contract)
    class BrokenCleanup:
        argv = ["worker"]
        process = SimpleNamespace(pid=12345, returncode=None)
        async def terminate(self, _reason):
            self.process.returncode = 1
            raise RuntimeError("artifact failed")
    async def inner(_state, _contract, _round_dir, _context, _writer, running):
        running.append({"process": BrokenCleanup()})
        (engine.store.run_dir(state.run_id) / "cancel.requested").touch()
        return []
    monkeypatch.setattr(engine.pack, "_direct_run", inner)
    try:
        asyncio.run(engine._pack_direct(
            state, contract, engine.store.run_dir(state.run_id) / "rounds" / "001", {}, {}))
    except RuntimeError as error:
        assert "artifact failed" in str(error)
        assert state.status is not RunStatus.CANCELLED
    else:
        raise AssertionError("Pack cleanup failure was hidden")


def test_direct_pack_cancel_wins_before_cull_and_labels_cleanup(tmp_path: Path, monkeypatch):
    policy = PackPolicy(
        mode="on", backend="direct", initial_workers=2, maximum_workers=2,
        concurrency=2, survivors=1, minimum_runtime_seconds=0,
        minimum_progress_events=2, judge_interval_seconds=0,
        cull_cooldown_seconds=0, minimum_score_gap=0, semantic_judge=False)
    contract = RunContract(
        "cancel before cull", str(tmp_path), budget=Budget(max_total_workers=4), pack=policy)
    engine = Engine(tmp_path, adapter(), base_config())
    state = engine.new_run(contract); state.round = 1; state.status = RunStatus.WORKING
    reasons: list[str] = []

    class FakeProcess:
        argv = ["worker"]
        progress_events = 2
        started_monotonic = time.monotonic()

        def __init__(self, pid: int):
            self.process = SimpleNamespace(pid=pid, returncode=None)

        async def terminate(self, reason: str):
            reasons.append(reason)
            self.process.returncode = 1
            return True

    async def start(spec, _prompt, _workspace, artifact):
        artifact.mkdir(parents=True, exist_ok=True)
        return FakeProcess(10000 + len(reasons) + int(spec.worker_id[-1])), artifact / "final.txt"

    cancel_path = engine.store.run_dir(state.run_id) / "cancel.requested"

    def cancel_during_selection(*_args):
        cancel_path.touch()
        return False

    monkeypatch.setattr(engine.adapter, "start", start)
    monkeypatch.setattr(pack_module, "unique_evidence", cancel_during_selection)
    result = asyncio.run(engine._pack_direct(
        state, contract, engine.store.run_dir(state.run_id) / "rounds" / "001", {}, {}))
    assert result == [] and state.status is RunStatus.CANCELLED
    assert reasons and all(reason == "cancelled_by_user" for reason in reasons)
    assert not (engine.store.run_dir(state.run_id) / "cull-decisions.jsonl").exists()


def test_completion_state_is_written_only_after_artifacts(tmp_path: Path, monkeypatch):
    contract = RunContract("finish safely", str(tmp_path), [[sys.executable, "-V"]])
    engine = Engine(tmp_path, adapter(), base_config())
    state = engine.new_run(contract); state.status = RunStatus.WORKING
    engine.store.save_state(state)
    original = engine.store.event

    def fail_finished(run_id, event_type, **data):
        if event_type == "run_finished":
            raise OSError("event disk failed")
        return original(run_id, event_type, **data)

    monkeypatch.setattr(engine.store, "event", fail_finished)
    try:
        engine._finish(state, RunStatus.DONE, "done")
    except OSError:
        pass
    else:
        raise AssertionError("completion artifact failure was ignored")
    assert state.status is RunStatus.WORKING
    assert engine.store.load_state(state.run_id).status is RunStatus.WORKING


def test_empty_pack_evidence_does_not_defeat_idle_block(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOUND_FAKE_SCENARIO", "invalid-json")
    contract = RunContract(
        "stop an evidence-free loop", str(tmp_path),
        [[sys.executable, "-c", "raise SystemExit(1)"]],
        budget=Budget(max_rounds=3, max_idle_rounds=1, max_total_workers=6),
        pack=PackPolicy(mode="on"))
    engine = Engine(tmp_path, adapter(), base_config())

    async def empty_pack(*_args, **_kwargs):
        return [{"worker_id": "scout-1-1", "status": "failed",
                 "evidence": [{"claim": "   ", "source": ""}, {}],
                 "unique_contribution": "unsupported self-claim"}]

    monkeypatch.setattr(engine, "_pack", empty_pack)
    result = asyncio.run(engine.run(engine.new_run(contract), contract))
    assert result.status is RunStatus.BLOCKED and result.round == 1
    assert result.rounds[0]["pack_evidence"] is False
    assert result.message == "idle budget exhausted without new evidence"


def test_failed_fingerprint_is_refused_before_third_writer(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOUND_FAKE_SCENARIO", "invalid-json")
    contract = RunContract(
        "do not repeat", str(tmp_path),
        [[sys.executable, "-c", "raise SystemExit(1)"]],
        budget=Budget(max_rounds=4, max_idle_rounds=4), pack=PackPolicy(mode="off"))
    engine = Engine(tmp_path, adapter(), base_config())
    result = asyncio.run(engine.run(engine.new_run(contract), contract))
    assert result.status is RunStatus.BLOCKED and result.round == 3
    assert result.total_workers == 2
    assert result.rounds[-1]["writer"]["refused_fingerprint"]
    assert "action_refused" in (engine.store.run_dir(result.run_id) /
                                "events.jsonl").read_text()


def test_empty_pack_does_not_change_failed_fingerprint_evidence(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOUND_FAKE_SCENARIO", "invalid-json")
    contract = RunContract(
        "do not repeat through an empty Pack", str(tmp_path),
        [[sys.executable, "-c", "raise SystemExit(1)"]],
        budget=Budget(max_rounds=4, max_idle_rounds=4, max_total_workers=6),
        pack=PackPolicy(mode="on"))
    engine = Engine(tmp_path, adapter(), base_config())

    async def empty_pack(*_args, **_kwargs):
        return [{"worker_id": "empty", "status": "failed", "evidence": [],
                 "unique_contribution": ""}]

    monkeypatch.setattr(engine, "_pack", empty_pack)
    result = asyncio.run(engine.run(engine.new_run(contract), contract))
    assert result.status is RunStatus.BLOCKED and result.round == 3
    assert result.total_workers == 2 and result.rounds[-1]["writer"]["refused_fingerprint"]


def test_scheduled_fingerprint_requires_matching_action_echo():
    previous = {"writer": {"next_action": "Run the focused regression"}}
    assert _followed_scheduled_action(
        previous, {"action_taken": " run  the focused regression ", "parse_confidence": 1})
    assert not _followed_scheduled_action(
        previous, {"action_taken": "edit unrelated docs", "parse_confidence": 1})
    assert _followed_scheduled_action(previous, {"action_taken": "", "parse_confidence": 0.1})


def test_useful_pack_evidence_resets_idle_counter(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOUND_FAKE_SCENARIO", "invalid-json")
    contract = RunContract(
        "use new evidence", str(tmp_path),
        [[sys.executable, "-c", "raise SystemExit(1)"]],
        budget=Budget(max_rounds=3, max_idle_rounds=2, max_total_workers=6),
        pack=PackPolicy(mode="on"))
    engine = Engine(tmp_path, adapter(), base_config())

    async def useful_pack(*_args, **_kwargs):
        return [{"worker_id": "scout-1-1", "status": "completed",
                 "evidence": [{"claim": "new root cause"}],
                 "unique_contribution": "reproduction"}]

    monkeypatch.setattr(engine, "_pack", useful_pack)
    state = engine.new_run(contract); state.idle_rounds = 1; state.status = RunStatus.WORKING
    asyncio.run(engine._round(state, contract))
    assert state.idle_rounds == 0 and state.status is not RunStatus.BLOCKED


def test_duplicate_pack_evidence_does_not_postpone_idle_stop(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOUND_FAKE_SCENARIO", "invalid-json")
    contract = RunContract(
        "stop duplicate evidence", str(tmp_path),
        [[sys.executable, "-c", "raise SystemExit(1)"]],
        budget=Budget(max_rounds=4, max_idle_rounds=1, max_total_workers=6),
        pack=PackPolicy(mode="on"))
    engine = Engine(tmp_path, adapter(), base_config())

    async def duplicate_pack(*_args, **_kwargs):
        return [{"worker_id": "duplicate", "status": "completed",
                 "evidence": [{"claim": " Same   Fact ", "source": "log.txt"}],
                 "unique_contribution": "claimed unique"}]

    monkeypatch.setattr(engine, "_pack", duplicate_pack)
    result = asyncio.run(engine.run(engine.new_run(contract), contract))
    assert result.status is RunStatus.BLOCKED and result.round == 2
    assert result.rounds[0]["pack_evidence"] is True
    assert result.rounds[1]["pack_evidence"] is False


def test_duplicate_writer_evidence_is_an_idle_round(tmp_path: Path, monkeypatch):
    contract = RunContract(
        "stop duplicate writer evidence", str(tmp_path),
        [[sys.executable, "-c", "raise SystemExit(1)"]],
        budget=Budget(max_rounds=4, max_idle_rounds=1), pack=PackPolicy(mode="off"))
    engine = Engine(tmp_path, adapter(), base_config())

    async def duplicate_writer(*_args, **_kwargs):
        (tmp_path / "claimed.py").write_text("same\n", encoding="utf-8")
        return {"status": "continue", "summary": "same", "action_taken": "repeat",
                "evidence": [{"claim": "same fact", "source": "same.log"}],
                "changed_files": ["claimed.py"], "commands_run": [],
                "verification_observed": [], "next_action": "repeat",
                "parallel_request": {"needed": False, "reason": "", "missions": []}}

    monkeypatch.setattr(engine, "_writer", duplicate_writer)
    result = asyncio.run(engine.run(engine.new_run(contract), contract))
    assert result.status is RunStatus.BLOCKED and result.round == 2
    assert result.rounds[0]["idle"] is False and result.rounds[1]["idle"] is True


def test_no_file_change_is_idle_even_with_new_evidence(tmp_path: Path, monkeypatch):
    contract = RunContract(
        "continue a read-only diagnosis", str(tmp_path),
        [[sys.executable, "-c", "raise SystemExit(1)"]],
        budget=Budget(max_rounds=2, max_idle_rounds=1), pack=PackPolicy(mode="off"))
    engine = Engine(tmp_path, adapter(), base_config())

    async def investigating_writer(state, *_args, **_kwargs):
        return {"status": "continue", "summary": "new finding", "action_taken": "inspect",
                "evidence": [{"claim": f"fact {state.round}"}], "changed_files": [],
                "commands_run": [], "verification_observed": [],
                "next_action": f"inspect next lead {state.round}",
                "parallel_request": {"needed": False, "reason": "", "missions": []}}

    monkeypatch.setattr(engine, "_writer", investigating_writer)
    result = asyncio.run(engine.run(engine.new_run(contract), contract))
    assert result.status is RunStatus.BLOCKED and result.round == 1
    assert result.rounds[0]["idle"] is True


def test_same_verification_is_idle_even_with_changes(tmp_path: Path, monkeypatch):
    contract = RunContract(
        "stop repeating a verification result", str(tmp_path),
        [[sys.executable, "-c", "raise SystemExit(1)"]],
        budget=Budget(max_rounds=3, max_idle_rounds=1), pack=PackPolicy(mode="off"))
    engine = Engine(tmp_path, adapter(), base_config())

    async def changing_writer(state, *_args, **_kwargs):
        path = tmp_path / "progress.txt"
        path.write_text(f"round {state.round}\n", encoding="utf-8")
        return {"status": "continue", "summary": "changed approach",
                "action_taken": f"change {state.round}",
                "evidence": [{"claim": f"fact {state.round}"}],
                "changed_files": ["progress.txt"], "commands_run": [],
                "verification_observed": [], "next_action": f"try {state.round}",
                "parallel_request": {"needed": False, "reason": "", "missions": []}}

    monkeypatch.setattr(engine, "_writer", changing_writer)
    result = asyncio.run(engine.run(engine.new_run(contract), contract))
    assert result.status is RunStatus.BLOCKED and result.round == 2
    assert result.rounds[0]["idle"] is False and result.rounds[1]["idle"] is True


def test_same_next_action_is_idle_even_with_new_evidence(tmp_path: Path, monkeypatch):
    contract = RunContract(
        "stop repeating a proposal", str(tmp_path),
        [[sys.executable, "-c",
          "from pathlib import Path; print(Path('progress.txt').read_text()); raise SystemExit(1)"]],
        budget=Budget(max_rounds=3, max_idle_rounds=1), pack=PackPolicy(mode="off"))
    engine = Engine(tmp_path, adapter(), base_config())

    async def repeating_writer(state, *_args, **_kwargs):
        (tmp_path / "progress.txt").write_text(f"round {state.round}\n", encoding="utf-8")
        return {"status": "continue", "summary": "new fact", "action_taken": "inspect",
                "evidence": [{"claim": f"new fact {state.round}"}],
                "changed_files": ["progress.txt"],
                "commands_run": [], "verification_observed": [],
                "next_action": "repeat the same action",
                "parallel_request": {"needed": False, "reason": "", "missions": []}}

    monkeypatch.setattr(engine, "_writer", repeating_writer)
    result = asyncio.run(engine.run(engine.new_run(contract), contract))
    assert result.status is RunStatus.BLOCKED and result.round == 2
    assert result.rounds[0]["idle"] is False and result.rounds[1]["idle"] is True


def test_evidence_hash_includes_verification_signature_and_git_diff():
    state = SimpleNamespace(rounds=[{
        "verification": [{"command": ["check"], "exit_code": 1,
                          "passed": False, "timed_out": False}],
        "verification_signature": "output-a", "git_diff": [], "git_identity": "",
    }])
    initial = _evidence_tokens(state.rounds)
    state.rounds[0]["verification_signature"] = "output-b"
    changed_output = _evidence_tokens(state.rounds)
    state.rounds[0]["git_diff"] = ["src/new.py"]
    changed_path = _evidence_tokens(state.rounds)
    state.rounds[0]["git_identity"] = "content-a"
    assert initial != changed_output != changed_path != _evidence_tokens(state.rounds)


def test_evidence_tokens_include_commands_observations_and_scout_hypotheses():
    tokens = _evidence_tokens([{
        "writer": {"commands_run": [{"argv": ["python", "-V"], "exit_code": 0,
                                      "summary": "version checked"}],
                   "verification_observed": ["reproduced failure"]},
        "scouts": [{"hypotheses": [{"statement": "configuration race",
                                      "supports": ["trace"], "contradicts": [],
                                      "next_test": "repeat under load"}]}],
    }])
    assert any(token.startswith("command:") for token in tokens)
    assert "verification_observed:reproduced failure" in tokens
    assert any(token.startswith("hypothesis:") and "repeat under load" in token
               for token in tokens)


def test_auto_pack_checks_alternative_before_accepting_writer_blocker(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOUND_FAKE_SCENARIO", "blocked")
    contract = RunContract("seek an alternative", str(tmp_path), [[sys.executable, "-V"]],
                           budget=Budget(max_rounds=3, max_total_workers=6),
                           pack=PackPolicy(mode="auto"))
    engine = Engine(tmp_path, adapter(), base_config())

    async def useful_pack(*_args, **_kwargs):
        return [{"worker_id": "scout-1-1", "status": "completed",
                 "evidence": [{"claim": "safe alternative"}],
                 "unique_contribution": "alternative"}]

    monkeypatch.setattr(engine, "_pack", useful_pack)
    state = engine.new_run(contract); state.status = RunStatus.WORKING
    asyncio.run(engine._round(state, contract))
    assert state.status is not RunStatus.BLOCKED
    assert state.rounds[0]["pack_evidence"] is True


def test_pack_cancel_marker_wins_over_pack_state(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOUND_FAKE_SCENARIO", "invalid-json")
    contract = RunContract(
        "cancel a Pack", str(tmp_path), [[sys.executable, "-c", "raise SystemExit(1)"]],
        budget=Budget(max_rounds=3, max_idle_rounds=1, max_total_workers=6),
        pack=PackPolicy(mode="on"))
    engine = Engine(tmp_path, adapter(), base_config())

    async def cancelled_pack(state, *_args, **_kwargs):
        (engine.store.run_dir(state.run_id) / "cancel.requested").touch()
        state.status = RunStatus.SYNTHESIZING
        return []

    monkeypatch.setattr(engine, "_pack", cancelled_pack)
    result = asyncio.run(engine.run(engine.new_run(contract), contract))
    assert result.status is RunStatus.CANCELLED
    assert result.message == "cancelled during Pack execution"


def test_cancel_wins_at_each_content_identity_boundary(tmp_path: Path, monkeypatch):
    for cancel_on, expected_calls in ((1, (0, 0)), (2, (1, 0)), (3, (1, 1))):
        workspace = tmp_path / str(cancel_on); workspace.mkdir()
        contract = RunContract(
            "honor cancellation", str(workspace), budget=Budget(max_total_workers=6),
            pack=PackPolicy(mode="on"))
        engine = Engine(workspace, adapter(), base_config())
        state = engine.new_run(contract); state.status = RunStatus.WORKING
        calls = {"identity": 0, "writer": 0, "pack": 0}

        async def identity(*_args):
            calls["identity"] += 1
            if calls["identity"] == cancel_on:
                (engine.store.run_dir(state.run_id) / "cancel.requested").touch()
            return "identity"

        async def writer(*_args):
            calls["writer"] += 1
            return {"status": "continue", "summary": "", "evidence": [],
                    "changed_files": [], "commands_run": [], "verification_observed": [],
                    "next_action": "inspect", "parallel_request": {"needed": False}}

        async def pack(*_args):
            calls["pack"] += 1
            return []

        monkeypatch.setattr(engine_module, "_content_identity", identity)
        monkeypatch.setattr(engine, "_writer", writer)
        monkeypatch.setattr(engine, "_pack", pack)
        asyncio.run(engine._round(state, contract))
        assert state.status is RunStatus.CANCELLED and state.rounds == []
        assert (calls["writer"], calls["pack"]) == expected_calls
