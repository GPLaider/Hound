from __future__ import annotations

import os

from hounds.adapter import CodexAdapter, redact
from hounds.context import bounded_context
from hounds.models import WorkerSpec
from hounds.process import ProcessOutcome
from hounds.structured import (last_json_object, matches_output_schema, normalize_result,
                               output_schema)


def test_last_valid_json_and_fallback():
    assert last_json_object('bad {"a":1} tail {"b":2}')["b"] == 2
    assert last_json_object('{"outer":{"nested":true}}')["outer"]["nested"] is True
    assert normalize_result("writer", "not-json")["parse_confidence"] == 0.1
    malformed = normalize_result("verifier", '{"verdict":"accept"}')
    assert malformed["verdict"] == "revise"
    assert malformed["blocking_issues"] == ["malformed verifier result"]
    writer = normalize_result(
        "writer", '{"status":"candidate_done","summary":7,"changed_files":"x"}')
    assert writer["summary"] == "" and writer["changed_files"] == []


def test_writer_result_validates_evidence_commands_and_parallel_request():
    result = normalize_result("writer", '''{
      "status":"continue", "summary":"ok", "action_taken":"inspect", "next_action":"test", "blocker":null,
      "evidence":[{"claim":"seen", "source":"test"}, {"source":"missing claim"}, 3],
      "changed_files":["src/a.py", 4],
      "commands_run":[
        {"argv":["python", "-V"], "exit_code":0, "summary":"ok"},
        {"argv":"python -V", "exit_code":0, "summary":"bad"}
      ],
      "verification_observed":["passed", 5],
      "parallel_request":{"needed":true, "reason":"two leads", "missions":["a", "b"]}
    }''')
    assert result["evidence"] == [{"claim": "seen", "source": "test"}]
    assert result["action_taken"] == "inspect"
    assert result["changed_files"] == ["src/a.py"]
    assert len(result["commands_run"]) == 1
    assert result["verification_observed"] == ["passed"]
    assert result["parallel_request"]["missions"] == ["a", "b"]

    malformed = normalize_result(
        "writer", '{"parallel_request":{"needed":"yes","reason":4,"missions":"a"}}')
    assert malformed["parallel_request"] == {"needed": False, "reason": "", "missions": []}


def test_scout_judge_and_verifier_result_validation():
    scout = normalize_result("scout", '''{
      "summary":"lead", "evidence":[{"claim":"fact"}, {"claim":8}],
      "hypotheses":[
        {"statement":"race", "supports":["log"], "contradicts":[], "next_test":"repeat"},
        {"statement":"bad", "supports":"log", "contradicts":[], "next_test":"repeat"}
      ], "recommended_next_action":"repeat", "unique_contribution":"timing", "confidence":2
    }''')
    assert scout["evidence"] == [{"claim": "fact"}]
    assert len(scout["hypotheses"]) == 1
    assert scout["confidence"] == 0.0

    judge = normalize_result("judge", '''{
      "ranking":[
        {"worker_id":"s1", "semantic_score":88, "retain":true,
         "unique_evidence":true, "brief_reason":"best"},
        {"worker_id":"s2", "semantic_score":101}
      ], "kill_order":["s2", 4], "merge_notes":"merge", "confidence":0.8
    }''')
    assert [item["worker_id"] for item in judge["ranking"]] == ["s1"]
    assert judge["kill_order"] == ["s2"]

    verifier = normalize_result("verifier", '''{
      "verdict":"accept", "verified_claims":["ok", 7], "unverified_claims":[],
      "blocking_issues":[], "nonblocking_issues":[], "required_next_action":null
    }''')
    assert verifier["verdict"] == "revise"
    assert verifier["verified_claims"] == ["ok"]
    assert verifier["blocking_issues"] == ["malformed verifier result"]


def test_context_cap_and_redaction(monkeypatch):
    context, omitted = bounded_context([(1, "must", "ok"), (9, "huge", "x" * 1000)], 50)
    assert context == {"must": "ok"} and omitted == ["huge"]
    monkeypatch.setenv("SERVICE_TOKEN", "super-secret-value")
    assert "super-secret-value" not in redact("token=super-secret-value")


def test_final_and_result_artifacts_are_redacted(tmp_path, monkeypatch):
    secret = "final-secret-value"
    monkeypatch.setenv("SERVICE_TOKEN", secret)
    artifact = tmp_path / "worker"; artifact.mkdir()
    final = artifact / "final.txt"
    final.write_text(
        '{"status":"continue","summary":"' + secret + '","evidence":[]}', encoding="utf-8")
    outcome = ProcessOutcome(["worker"], 0, "", "", "start", "finish")
    result = CodexAdapter().finish(
        WorkerSpec("writer-1", "writer", "work", "workspace-write", 5),
        outcome, final, artifact)
    stored = final.read_text() + (artifact / "result.json").read_text()
    assert secret not in stored and result["summary"] == "[REDACTED]"


def test_role_output_schemas_are_closed_and_complete():
    for role in ("writer", "scout", "judge", "verifier"):
        schema = output_schema(role)
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
    assert not matches_output_schema("writer", {"status": "candidate_done"})
    evidence = output_schema("scout")["properties"]["evidence"]["items"]
    assert "confidence" in evidence["properties"]
    assert matches_output_schema("scout", {
        "summary": "ok", "evidence": [{"claim": "fact", "source": "log",
                                          "artifact": "trace", "confidence": 0.8}],
        "hypotheses": [], "recommended_next_action": "inspect",
        "unique_contribution": "fact", "confidence": 0.8})


def test_strict_result_never_treats_jsonl_stdout_as_final(tmp_path):
    artifact = tmp_path / "worker"; artifact.mkdir()
    spec = WorkerSpec("writer-1", "writer", "work", "workspace-write", 5)
    outcome = ProcessOutcome(
        ["worker"], 0, '{"type":"event","status":"candidate_done"}\n', "",
        "start", "finish")
    adapter = CodexAdapter()
    adapter.capabilities = {"output_schema": True, "output_last_message": True}
    missing = adapter.finish(spec, outcome, artifact / "final.txt", artifact)
    assert missing["status"] == "continue" and missing["parse_confidence"] == 0.1
    assert missing["structured_mode"] == "strict"

    (artifact / "final.txt").write_text(
        '{"status":"candidate_done","summary":"ok","action_taken":"work",'
        '"evidence":[],"changed_files":[],"commands_run":[],"verification_observed":[],'
        '"next_action":"","parallel_request":{"needed":false,"reason":"","missions":[]},'
        '"blocker":null}', encoding="utf-8")
    valid = adapter.finish(spec, outcome, artifact / "final.txt", artifact)
    assert valid["status"] == "candidate_done" and valid["structured_mode"] == "strict"

    (artifact / "output-schema.json").unlink(missing_ok=True)
    (artifact / "final.txt").write_text('{"status":"candidate_done"} trailing', encoding="utf-8")
    trailing = adapter.finish(spec, outcome, artifact / "final.txt", artifact)
    assert trailing["status"] == "continue" and trailing["parse_confidence"] == 0.1
