from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

import hounds.agentflow as agentflow_module
from hounds.adapter import CodexAdapter
from hounds.agentflow import AgentFlowPack, AgentFlowPackError
from hounds.engine import Engine
from hounds.models import Budget, PackPolicy, RunContract, RunStatus
from hounds.recovery import recover_interrupted_async


FAKE_AGENTFLOW = Path(__file__).with_name("fake_agentflow.py")
PARALLEL_REQUEST = {"parallel_request": {
    "needed": True, "missions": ["inspect logs", "trace code"]}}
CULLING_POLICY = {"minimum_runtime_seconds": 25, "minimum_progress_events": 2,
                  "judge_interval_seconds": 40, "cooldown_seconds": 15,
                  "minimum_score_gap": 8, "survivors": 1}


def agentflow() -> AgentFlowPack:
    pack = AgentFlowPack(prefix=[sys.executable, str(FAKE_AGENTFLOW)])
    pack._native_member_culling = lambda: True
    return pack


def test_native_member_culling_follows_platform_safety_boundary():
    assert AgentFlowPack()._native_member_culling() is (os.name != "nt")


def test_agentflow_pack_builds_readonly_parallel_pipeline(tmp_path: Path):
    round_dir = tmp_path / "rounds" / "001"
    results = asyncio.run(agentflow().run(
        "find the root cause", ["inspect logs", "trace code"], {}, tmp_path,
        round_dir, concurrency=2, timeout=5, culling_policy=CULLING_POLICY))
    pipeline = json.loads((round_dir / "agentflow" / "pipeline.json").read_text(encoding="utf-8"))
    assert pipeline["concurrency"] == 2 and len(pipeline["nodes"]) == 2
    scouts, controller = pipeline["nodes"]
    assert "as" not in scouts["fanout"] and len(scouts["fanout"]["values"]) == 2
    assert controller["schedule"]["until_fanout_settles_from"] == "scouts"
    assert controller["schedule"]["actuation"] == "output_json"
    assert controller["schedule"]["every_seconds"] == 40
    assert all(node["tools"] == "read_only" for node in pipeline["nodes"])
    assert scouts["timeout_seconds"] > 5 and controller["timeout_seconds"] == 5
    safe_args = (["--ignore-user-config", "--ephemeral", "-c", 'windows.sandbox="elevated"']
                 if os.name == "nt" else ["--ignore-user-config", "--ephemeral"])
    assert all(node["extra_args"] == safe_args for node in pipeline["nodes"])
    mapping = json.loads((round_dir / "agentflow" / "worker-map.json").read_text())
    assert mapping == {"scout-1-1": "scouts_0", "scout-1-2": "scouts_1"}
    assert all(result["backend"] == "agentflow" and result["status"] == "completed"
               for result in results)
    assert [result["summary"] for result in results] == ["evidence 1", "evidence 2"]
    assert all((round_dir / "scouts" / result["worker_id"] / "result.json").exists()
               for result in results)
    for result in results:
        artifact = round_dir / "scouts" / result["worker_id"]
        prompt = (artifact / "prompt.txt").read_bytes()
        assert (artifact / "prompt.sha256").read_text() == hashlib.sha256(prompt).hexdigest()


def test_agentflow_can_disable_native_member_culling(tmp_path: Path):
    pack = AgentFlowPack(prefix=[sys.executable, str(FAKE_AGENTFLOW)])
    pack._native_member_culling = lambda: False
    round_dir = tmp_path / "rounds" / "001"
    results = asyncio.run(pack.run(
        "hunt", ["one", "two"], {}, tmp_path, round_dir, 2, 5,
        culling_policy=CULLING_POLICY))
    pipeline = json.loads((round_dir / "agentflow" / "pipeline.json").read_text())
    audit = json.loads((round_dir / "agentflow" / "controller-audit.json").read_text())
    assert len(pipeline["nodes"]) == 1 and audit["enabled"] is False
    assert all(result["status"] == "completed" for result in results)


def test_engine_auto_pack_invokes_agentflow_for_distinct_requested_missions(tmp_path: Path):
    fake_codex = Path(__file__).with_name("fake_agent.py")
    config = {"agentflow": {"enabled": True}, "workers": {"scout_timeout_seconds": 5}}
    engine = Engine(tmp_path, CodexAdapter(prefix=[sys.executable, str(fake_codex)]), config)
    engine.agentflow = agentflow()
    contract = RunContract("hunt", str(tmp_path), budget=Budget(max_total_workers=5),
                           pack=PackPolicy(mode="on", backend="auto", initial_workers=2,
                                           concurrency=2, survivors=1))
    state = engine.new_run(contract); state.status = RunStatus.WORKING
    state.round = 1
    round_dir = engine.store.run_dir(state.run_id) / "rounds" / "001"
    results = asyncio.run(engine._pack(state, contract, round_dir, {}, PARALLEL_REQUEST))
    assert len(results) == 2 and all(result["backend"] == "agentflow" for result in results)
    assert sum(bool(result["retained"]) for result in results) == 1
    ranking = json.loads((round_dir / "pack-ranking.json").read_text(encoding="utf-8"))
    assert ranking["backend"] == "agentflow"


def test_engine_auto_pack_prefers_agentflow_for_parallel_hunt(tmp_path: Path):
    fake_codex = Path(__file__).with_name("fake_agent.py")
    engine = Engine(tmp_path, CodexAdapter(prefix=[sys.executable, str(fake_codex)]),
                    {"agentflow": {"enabled": True}})
    engine.agentflow = agentflow()

    contract = RunContract("hunt", str(tmp_path), budget=Budget(max_total_workers=5),
                           pack=PackPolicy(mode="on", backend="auto", initial_workers=2,
                                           concurrency=2))
    state = engine.new_run(contract); state.status = RunStatus.WORKING
    results = asyncio.run(engine._pack(
        state, contract, engine.store.run_dir(state.run_id) / "rounds" / "001", {}, {}))
    assert len(results) == 2 and all(result["backend"] == "agentflow" for result in results)
    events = [json.loads(line) for line in
              (engine.store.run_dir(state.run_id) / "events.jsonl").read_text().splitlines()]
    selection = next(event for event in events if event["type"] == "pack_backend_selected")
    assert selection["selected_backend"] == "agentflow"
    assert selection["qualified_parallel_pack"] is True


def test_agentflow_imports_only_applied_controller_cull(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOUND_FAKE_AGENTFLOW_CULL", "1")
    round_dir = tmp_path / "rounds" / "001"
    results = asyncio.run(agentflow().run(
        "hunt", ["audit", "compare"], {}, tmp_path, round_dir, 2, 5,
        culling_policy=CULLING_POLICY))
    assert results[0]["status"] == "culled"
    assert results[0]["agentflow_node_id"] == "scouts_0"
    assert "duplicates" in results[0]["cull_reason"]
    assert "scout-1-1" in (tmp_path / "cull-decisions.jsonl").read_text()
    recovered = asyncio.run(agentflow().recover(round_dir, 8192))
    assert recovered[0]["status"] == "culled"
    assert len((tmp_path / "cull-decisions.jsonl").read_text().splitlines()) == 1

    record = {"id": "outer", "status": "cancelled", "nodes": {"scouts_0": {
        "status": "cancelled", "exit_code": None, "stdout_lines": [],
        "stderr_lines": [], "trace_events": []}}}
    outer = asyncio.run(agentflow()._results(
        record, ["audit"], round_dir, 8192,
        worker_map={"scout-1-1": "scouts_0"}))
    assert outer[0]["status"] == "cancelled"


def test_agentflow_controller_audit_rejects_rerun(tmp_path: Path):
    run_dir = tmp_path / "native" / "artifacts" / "scout_controller"
    run_dir.mkdir(parents=True)
    (run_dir / "periodic-actions-tick-2.json").write_text(json.dumps({
        "analysis": "bad", "actions": [
            {"kind": "rerun", "node_ids": ["scouts_0"], "reason": "bad"}]}))
    _, violations = agentflow()._controller_audit(
        tmp_path, {"id": "native"}, {"scouts_0", "scouts_1"}, CULLING_POLICY)
    assert any("non-cancel" in item for item in violations)


def test_agentflow_timeout_recovers_completed_scout(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOUND_FAKE_AGENTFLOW_PARTIAL", "1")
    round_dir = tmp_path / "rounds" / "001"
    results = asyncio.run(agentflow().run(
        "hunt", ["fast", "stalled"], {}, tmp_path, round_dir,
        concurrency=2, timeout=0.1))
    assert [result["status"] for result in results] == ["completed", "interrupted"]
    assert all(result["agentflow_pack_complete"] is False for result in results)
    interrupted = results[1]
    assert interrupted["artifact_dir"].endswith("scouts/scout-1-2")
    assert len(interrupted["artifact_sha256"]) == 64
    assert len(interrupted["partial_excerpt"].encode()) <= 8192
    assert '"type": "evidence"' in interrupted["partial_excerpt"]
    if os.name == "nt":
        termination = json.loads(
            (round_dir / "agentflow" / "launcher" / "termination.json").read_text(encoding="utf-8"))
        assert termination["target_ppid"] == termination["hound_pid"]
        assert termination["method"] == "taskkill_pid_tree" and termination["taskkill_exit_code"] == 0


def test_agentflow_cancellation_redacts_durable_runs(tmp_path: Path, monkeypatch):
    secret = "agentflow-secret-value"
    monkeypatch.setenv("SERVICE_TOKEN", secret)
    round_dir = tmp_path / "rounds" / "001"
    raw = round_dir / "agentflow" / "runs" / "run-1" / "run.json"

    async def cancelled(*_args, **_kwargs):
        raw.parent.mkdir(parents=True)
        raw.write_text(json.dumps({"output": secret}), encoding="utf-8")
        raise asyncio.CancelledError

    monkeypatch.setattr(agentflow_module, "run_process", cancelled)
    try:
        asyncio.run(agentflow().run("hunt", ["one", "two"], {}, tmp_path, round_dir, 2, 5))
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("AgentFlow cancellation was swallowed")
    stored = raw.read_text(encoding="utf-8")
    assert secret not in stored and "[REDACTED]" in stored


def test_resume_imports_completed_native_agentflow_nodes(tmp_path: Path):
    fake_codex = Path(__file__).with_name("fake_agent.py")
    engine = Engine(tmp_path, CodexAdapter(prefix=[sys.executable, str(fake_codex)]), {})
    state = engine.new_run(RunContract("recover scouts", str(tmp_path)))
    state.status = RunStatus.SCOUTING; state.round = 1
    engine.store.save_state(state)
    round_dir = engine.store.run_dir(state.run_id) / "rounds" / "001"
    root = round_dir / "agentflow"
    (root / "runs" / "native").mkdir(parents=True)
    (root / "missions.json").write_text(
        json.dumps({"missions": ["inspect logs", "trace code"]}), encoding="utf-8")
    nodes = {}
    for index in (1, 2):
        worker_id = f"scout-1-{index}"
        nodes[worker_id] = {
            "status": "completed", "exit_code": 0,
            "final_response": json.dumps({
                "summary": f"recovered {index}",
                "evidence": [{"claim": f"fact {index}", "source": "native run",
                              "artifact": f"node-{index}", "confidence": 0.8}],
                "hypotheses": [],
                "recommended_next_action": "use proof", "unique_contribution": "proof",
                "confidence": 0.8}),
            "stdout_lines": [f"fact {index}"], "stderr_lines": [], "trace_events": [],
        }
    (root / "runs" / "native" / "run.json").write_text(json.dumps({
        "id": "native", "status": "completed", "nodes": nodes}), encoding="utf-8")

    assert asyncio.run(recover_interrupted_async(engine, state)) is True
    context = json.loads((engine.store.run_dir(state.run_id) /
                          "resume-context.json").read_text(encoding="utf-8"))
    assert [item["status"] for item in context["workers"]] == ["completed", "completed"]
    assert all((round_dir / "scouts" / f"scout-1-{index}" / "result.json").exists()
               for index in (1, 2))
    assert "agentflow_pack_recovered" in (
        engine.store.run_dir(state.run_id) / "events.jsonl").read_text(encoding="utf-8")


def test_agentflow_exit_zero_with_failed_record_is_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOUND_FAKE_AGENTFLOW_STATUS_FAILED", "1")
    try:
        asyncio.run(agentflow().run("hunt", ["one", "two"], {}, tmp_path,
                                    tmp_path / "rounds" / "001", 2, 5))
    except AgentFlowPackError as error:
        assert "without completed Scout evidence" in str(error)
    else:
        raise AssertionError("failed AgentFlow record was accepted")


def test_agentflow_completed_node_rejects_jsonl_event_as_scout_result(tmp_path: Path):
    round_dir = tmp_path / "rounds" / "001"
    record = {"id": "native", "status": "completed", "nodes": {"scout-1-1": {
        "status": "completed", "exit_code": 0,
        "output": json.dumps({"type": "item.started", "item": {"id": "item_1"}}),
        "stdout_lines": [], "stderr_lines": [], "trace_events": [],
    }}}

    result = asyncio.run(agentflow()._results(record, ["audit"], round_dir, 8192))[0]

    assert result["status"] == "failed" and result["parse_confidence"] == 0.1


def test_auto_pack_falls_back_to_direct(tmp_path: Path, monkeypatch):
    fake_codex = Path(__file__).with_name("fake_agent.py")
    monkeypatch.setenv("HOUND_FAKE_AGENTFLOW_ERROR", "1")
    monkeypatch.setenv("HOUND_FAKE_SCENARIO", "pack")
    config = {"agentflow": {"enabled": True},
              "workers": {"scout_timeout_seconds": 5},
              "codex": {}}
    engine = Engine(tmp_path, CodexAdapter(prefix=[sys.executable, str(fake_codex)]), config)
    engine.agentflow = agentflow()
    contract = RunContract("hunt", str(tmp_path), budget=Budget(max_total_workers=6),
                           pack=PackPolicy(mode="on", backend="auto", initial_workers=3,
                                           concurrency=3, minimum_runtime_seconds=0.1,
                                           minimum_progress_events=2, judge_interval_seconds=0.25,
                                           cull_cooldown_seconds=0.01, minimum_score_gap=5,
                                           survivors=1, semantic_judge=False))
    state = engine.new_run(contract); state.status = RunStatus.WORKING
    state.round = 1
    results = asyncio.run(engine._pack(
        state, contract, engine.store.run_dir(state.run_id) / "rounds" / "001", {}, PARALLEL_REQUEST))
    assert results and all(result["backend"] == "direct" for result in results)
    events = (engine.store.run_dir(state.run_id) / "events.jsonl").read_text(encoding="utf-8")
    assert "agentflow_pack_fallback" in events


def test_persistent_loop_delegates_needed_pack_to_agentflow(tmp_path: Path, monkeypatch):
    fake_codex = Path(__file__).with_name("fake_agent.py")
    monkeypatch.setenv("HOUND_FAKE_SCENARIO", "agentflow-loop")
    (tmp_path / "test_answer.py").write_text(
        "from answer import answer\nassert answer() == 42\n", encoding="utf-8")
    config = {"agentflow": {"enabled": True},
              "workers": {"writer_timeout_seconds": 5, "scout_timeout_seconds": 5,
                          "verifier_timeout_seconds": 5},
              "context": {"maximum_prompt_bytes": 65536}}
    engine = Engine(tmp_path, CodexAdapter(prefix=[sys.executable, str(fake_codex)]), config)
    engine.agentflow = agentflow()
    contract = RunContract("make answer pass", str(tmp_path), [[sys.executable, "test_answer.py"]],
                           budget=Budget(max_rounds=3, max_total_workers=6),
                           pack=PackPolicy(mode="auto", backend="auto", initial_workers=2,
                                           concurrency=2, survivors=1))
    result = asyncio.run(engine.run(engine.new_run(contract), contract))
    assert result.status is RunStatus.DONE and result.round == 2
    assert len(result.rounds[0]["scouts"]) == 2
    assert all(scout["backend"] == "agentflow" for scout in result.rounds[0]["scouts"])
