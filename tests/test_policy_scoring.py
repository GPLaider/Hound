from __future__ import annotations

from hounds.models import PackPolicy, RunState, RunStatus
from hounds.policy import fingerprint, should_pack
from hounds.scoring import deterministic_score, merge_semantic, unique_evidence
from hounds.store import utc_now


def test_fingerprint_stable_and_sensitive():
    one = fingerprint("writer", " x  y ", "act", ["b", "a"], [["pytest"]])
    assert one == fingerprint("writer", "x y", "act", ["a", "b"], [["pytest"]])
    assert one != fingerprint("writer", "x y", "different", ["a", "b"], [["pytest"]])


def test_pack_policy_and_scoring():
    now = utc_now()
    state = RunState("r", RunStatus.WORKING, now, now, idle_rounds=2)
    assert should_pack(PackPolicy(mode="auto"), state, {})
    assert not should_pack(PackPolicy(mode="off"), state, {"parallel_request": {"needed": True}})
    good = "evidence verified src/core.py exit_code root cause hypothesis"
    assert deterministic_score(good, 3, "trace core") > deterministic_score("noise\nnoise", 1)
    assert merge_semantic(80, 50) == 68
    assert merge_semantic(80, 1000) == 88
    assert merge_semantic(80, -100) == 48
    assert unique_evidence("src/unique.py", ["src/common.py"])
    assert unique_evidence(
        '{"type":"evidence","claim":"only live trace"}\n{"type":"progress"}',
        ['{"type":"progress"}'])
