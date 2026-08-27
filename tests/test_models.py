from __future__ import annotations

import pytest

from hounds.adapter import CodexAdapter, WorkerAdapter
from hounds.models import (
    SCHEMA_VERSION,
    Budget,
    Evidence,
    PackPolicy,
    RoundState,
    RunContract,
    RunState,
    RunStatus,
    WorkerResult,
    WorkerState,
    WorkerStatus,
    transition,
    validate_contract,
)


def _state(status: RunStatus = RunStatus.CREATED) -> RunState:
    return RunState("run-1", status, "start", "start")


def test_contract_models_are_schema_versioned_and_adapter_is_explicit():
    evidence = Evidence("claim")
    worker = WorkerState("scout-1", "scout")
    result = WorkerResult("scout-1", "scout", WorkerStatus.COMPLETED, evidence=[evidence])
    round_state = RoundState(1, scouts=[result])

    assert evidence.schema_version == SCHEMA_VERSION
    assert worker.status is WorkerStatus.PENDING
    assert result.schema_version == round_state.schema_version == SCHEMA_VERSION
    assert {status.value for status in WorkerStatus} == {
        "pending", "starting", "running", "completed", "failed", "interrupted",
        "cancelled", "culled", "timed_out",
    }
    assert isinstance(CodexAdapter(), WorkerAdapter)
    assert not isinstance(object(), WorkerAdapter)


def test_pack_policy_defaults_and_validation():
    policy = PackPolicy()
    assert policy.minimum_progress_events == 2
    assert policy.judge_interval_seconds == 40

    contract = RunContract("objective", ".")
    contract.pack.minimum_progress_events = -1
    with pytest.raises(ValueError, match="minimum_progress_events"):
        validate_contract(contract)

    contract.pack.minimum_progress_events = 2
    contract.pack.judge_interval_seconds = float("nan")
    with pytest.raises(ValueError, match="judge_interval_seconds"):
        validate_contract(contract)

    contract.pack.judge_interval_seconds = 40
    contract.pack.initial_workers = contract.pack.maximum_workers = contract.pack.concurrency = 6
    with pytest.raises(ValueError, match="maximum_workers cannot exceed 5"):
        validate_contract(contract)


def test_verification_proposals_validate_without_shifting_positional_contract_fields():
    budget, policy = Budget(), PackPolicy(mode="off")
    contract = RunContract(
        "objective", ".", [], [], [], [], 0, 900, budget, policy, SCHEMA_VERSION)
    assert contract.budget is budget and contract.pack is policy
    assert contract.schema_version == SCHEMA_VERSION
    assert contract.verification_proposals == []

    contract.verification_proposals = [["npm", "test"]]
    validate_contract(contract)
    contract.verify = [["python", "-V"]]
    with pytest.raises(ValueError, match="mutually exclusive"):
        validate_contract(contract)
    contract.verify = []
    contract.verification_proposals = [[]]
    with pytest.raises(ValueError, match="verification proposals"):
        validate_contract(contract)


def test_run_status_transition_guard_allows_contract_path():
    state = _state()
    for status in (
            RunStatus.BASELINE, RunStatus.WORKING, RunStatus.VERIFYING,
            RunStatus.REVIEWING, RunStatus.DONE):
        assert transition(state, status) is state
    assert state.status is RunStatus.DONE


def test_run_status_transition_guard_rejects_skips_and_terminal_escape():
    state = _state()
    with pytest.raises(ValueError, match="created -> done"):
        transition(state, RunStatus.DONE)
    assert state.status is RunStatus.CREATED

    state.status = RunStatus.DONE
    with pytest.raises(ValueError, match="done -> working"):
        transition(state, RunStatus.WORKING)
    assert state.status is RunStatus.DONE
