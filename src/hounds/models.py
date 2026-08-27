from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, fields
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, TypeVar

SCHEMA_VERSION = 1


class HoundEnvironmentError(RuntimeError):
    pass


class HoundLockError(HoundEnvironmentError):
    pass


class HoundResumeRetryError(HoundEnvironmentError):
    pass


class RunStatus(StrEnum):
    CREATED = "created"
    BASELINE = "baseline"
    WORKING = "working"
    SCOUTING = "scouting"
    CULLING = "culling"
    SYNTHESIZING = "synthesizing"
    VERIFYING = "verifying"
    REVIEWING = "reviewing"
    DONE = "done"
    BLOCKED = "blocked"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED = "cancelled"
    FAILED_INTERNAL = "failed_internal"


class WorkerStatus(StrEnum):
    PENDING = "pending"
    STARTING = "starting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    CULLED = "culled"
    TIMED_OUT = "timed_out"


@dataclass(slots=True)
class Budget:
    max_rounds: int = 20
    max_idle_rounds: int = 3
    max_total_workers: int = 12
    max_wall_minutes: int = 120
    schema_version: int = SCHEMA_VERSION


@dataclass(slots=True)
class PackPolicy:
    mode: str = "auto"
    backend: str = "auto"
    initial_workers: int = 3
    maximum_workers: int = 5
    concurrency: int = 3
    minimum_runtime_seconds: float = 25
    minimum_progress_events: int = 2
    judge_interval_seconds: float = 40
    cull_cooldown_seconds: float = 15
    minimum_score_gap: float = 8
    survivors: int = 1
    semantic_judge: bool = True
    schema_version: int = SCHEMA_VERSION


@dataclass(slots=True)
class RunContract:
    objective: str
    workspace: str
    verify: list[list[str]] = field(default_factory=list)
    required_files: list[str] = field(default_factory=list)
    allowed_paths: list[str] = field(default_factory=list)
    forbidden_paths: list[str] = field(default_factory=list)
    expected_exit_code: int = 0
    verify_timeout_seconds: float = 900
    budget: Budget = field(default_factory=Budget)
    pack: PackPolicy = field(default_factory=PackPolicy)
    schema_version: int = SCHEMA_VERSION
    verification_proposals: list[list[str]] = field(default_factory=list)


def validate_contract(contract: RunContract) -> None:
    errors: list[str] = []
    if contract.schema_version != SCHEMA_VERSION:
        errors.append(f"unsupported schema_version: {contract.schema_version}")
    if contract.budget.schema_version != SCHEMA_VERSION:
        errors.append(f"unsupported budget schema_version: {contract.budget.schema_version}")
    if contract.pack.schema_version != SCHEMA_VERSION:
        errors.append(f"unsupported pack schema_version: {contract.pack.schema_version}")
    if not isinstance(contract.objective, str) or not contract.objective.strip():
        errors.append("objective must be non-empty")
    if not isinstance(contract.workspace, str) or not contract.workspace.strip():
        errors.append("workspace must be non-empty")
    for name in ("max_rounds", "max_idle_rounds", "max_total_workers", "max_wall_minutes"):
        value = getattr(contract.budget, name)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            errors.append(f"{name} must be a positive integer")
    policy = contract.pack
    if policy.mode not in {"auto", "on", "off"}:
        errors.append("pack mode must be auto, on, or off")
    if policy.backend not in {"auto", "agentflow", "direct"}:
        errors.append("pack backend must be auto, agentflow, or direct")
    for name in ("initial_workers", "maximum_workers", "concurrency", "survivors"):
        value = getattr(policy, name)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            errors.append(f"{name} must be a positive integer")
    if (not isinstance(policy.minimum_progress_events, int) or
            isinstance(policy.minimum_progress_events, bool) or
            policy.minimum_progress_events < 0):
        errors.append("minimum_progress_events must be a non-negative integer")
    if (isinstance(policy.initial_workers, int) and isinstance(policy.maximum_workers, int)
            and policy.initial_workers > policy.maximum_workers):
        errors.append("initial_workers cannot exceed maximum_workers")
    if (isinstance(policy.survivors, int) and isinstance(policy.maximum_workers, int)
            and policy.survivors > policy.maximum_workers):
        errors.append("survivors cannot exceed maximum_workers")
    if (isinstance(policy.concurrency, int) and isinstance(policy.maximum_workers, int)
            and policy.concurrency > policy.maximum_workers):
        errors.append("concurrency cannot exceed maximum_workers")
    if isinstance(policy.maximum_workers, int) and policy.maximum_workers > 5:
        errors.append("maximum_workers cannot exceed 5")
    if (isinstance(policy.survivors, int) and isinstance(policy.initial_workers, int)
            and policy.survivors > policy.initial_workers):
        errors.append("survivors cannot exceed initial_workers")
    for name in ("minimum_runtime_seconds", "judge_interval_seconds",
                 "cull_cooldown_seconds", "minimum_score_gap"):
        value = getattr(policy, name)
        if (not isinstance(value, (int, float)) or isinstance(value, bool) or
                not math.isfinite(value) or value < 0):
            errors.append(f"{name} must be a non-negative number")
    if not isinstance(policy.semantic_judge, bool):
        errors.append("semantic_judge must be boolean")
    if (not isinstance(contract.verify_timeout_seconds, (int, float)) or
            isinstance(contract.verify_timeout_seconds, bool) or
            not math.isfinite(contract.verify_timeout_seconds) or contract.verify_timeout_seconds <= 0):
        errors.append("verify_timeout_seconds must be positive")
    if not isinstance(contract.expected_exit_code, int) or isinstance(contract.expected_exit_code, bool):
        errors.append("expected_exit_code must be an integer")
    if not isinstance(contract.verify, list) or any(
            not isinstance(command, list) or not command or
            any(not isinstance(part, str) or not part for part in command)
            for command in contract.verify):
        errors.append("verify commands must be non-empty string arrays")
    if not isinstance(contract.verification_proposals, list) or any(
            not isinstance(command, list) or not command or
            any(not isinstance(part, str) or not part for part in command)
            for command in contract.verification_proposals):
        errors.append("verification proposals must be non-empty string arrays")
    if contract.verify and contract.verification_proposals:
        errors.append("verify commands and verification proposals are mutually exclusive")
    for name in ("required_files", "allowed_paths", "forbidden_paths"):
        values = getattr(contract, name)
        if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
            errors.append(f"{name} must be a string array")
            continue
        if any((windows := PureWindowsPath(value)).drive or windows.root or
               (posix := PurePosixPath(value)).root or ".." in windows.parts or
               ".." in posix.parts or "\0" in value for value in values):
            errors.append(f"{name} entries must stay within the workspace")
    if errors:
        raise ValueError("; ".join(errors))


@dataclass(slots=True)
class WorkerSpec:
    worker_id: str
    role: str
    mission: str
    sandbox: str
    timeout_seconds: float
    model: str = ""
    schema_version: int = SCHEMA_VERSION


@dataclass(slots=True)
class Evidence:
    claim: str
    source: str = ""
    artifact: str = ""
    confidence: float | None = None
    schema_version: int = SCHEMA_VERSION


@dataclass(slots=True)
class WorkerState:
    worker_id: str
    role: str
    status: WorkerStatus = WorkerStatus.PENDING
    pid: int | None = None
    started_at: str = ""
    finished_at: str = ""
    schema_version: int = SCHEMA_VERSION


@dataclass(slots=True)
class WorkerResult:
    worker_id: str
    role: str
    status: WorkerStatus
    summary: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    exit_code: int | None = None
    artifact_dir: str = ""
    schema_version: int = SCHEMA_VERSION


@dataclass(slots=True)
class VerificationResult:
    command: list[str]
    exit_code: int | None
    started_at: str
    finished_at: str
    stdout_artifact: str
    stderr_artifact: str
    passed: bool
    stdout_sha256: str = ""
    stderr_sha256: str = ""
    timed_out: bool = False
    schema_version: int = SCHEMA_VERSION


@dataclass(slots=True)
class CullDecision:
    worker_id: str
    score: float
    reason: str
    timestamp: str
    order: int
    schema_version: int = SCHEMA_VERSION


@dataclass(slots=True)
class RunState:
    run_id: str
    status: RunStatus
    created_at: str
    updated_at: str
    round: int = 0
    idle_rounds: int = 0
    total_workers: int = 0
    repeated_verification_failures: int = 0
    repeated_verifier_failures: int = 0
    active_seconds: float = 0.0
    active_started_at: str = ""
    last_verification_signature: str = ""
    last_verifier_signature: str = ""
    rounds: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""
    schema_version: int = SCHEMA_VERSION


@dataclass(slots=True)
class RoundState:
    number: int
    status: str = "continue"
    writer: dict[str, Any] = field(default_factory=dict)
    scouts: list[dict[str, Any]] = field(default_factory=list)
    verification: list[VerificationResult] = field(default_factory=list)
    verification_signature: str = ""
    path_issues: list[str] = field(default_factory=list)
    pack_evidence: bool = False
    idle: bool = False
    fingerprint: str = ""
    next_fingerprint: str = ""
    scheduled_action: str = ""
    evidence_hash: str = ""
    git_diff: list[str] = field(default_factory=list)
    git_identity: str = ""
    pack_ranking: list[dict[str, Any]] = field(default_factory=list)
    verifier: dict[str, Any] = field(default_factory=dict)
    verifier_signature: str = ""
    schema_version: int = SCHEMA_VERSION


_TERMINAL_RUN_STATUSES = frozenset({
    RunStatus.DONE,
    RunStatus.BLOCKED,
    RunStatus.BUDGET_EXHAUSTED,
    RunStatus.CANCELLED,
    RunStatus.FAILED_INTERNAL,
})
_TERMINAL_EXITS = frozenset({
    RunStatus.BLOCKED,
    RunStatus.BUDGET_EXHAUSTED,
    RunStatus.CANCELLED,
    RunStatus.FAILED_INTERNAL,
})
RUN_STATUS_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.CREATED: frozenset({RunStatus.BASELINE, *_TERMINAL_EXITS}),
    RunStatus.BASELINE: frozenset({RunStatus.WORKING, *_TERMINAL_EXITS}),
    RunStatus.WORKING: frozenset({
        RunStatus.WORKING, RunStatus.SCOUTING, RunStatus.VERIFYING,
        *_TERMINAL_EXITS,
    }),
    RunStatus.SCOUTING: frozenset({
        RunStatus.WORKING, RunStatus.CULLING, RunStatus.SYNTHESIZING, *_TERMINAL_EXITS,
    }),
    RunStatus.CULLING: frozenset({RunStatus.WORKING, RunStatus.SYNTHESIZING,
                                  *_TERMINAL_EXITS}),
    RunStatus.SYNTHESIZING: frozenset({RunStatus.WORKING, *_TERMINAL_EXITS}),
    RunStatus.VERIFYING: frozenset({
        RunStatus.WORKING, RunStatus.SCOUTING, RunStatus.REVIEWING,
        *_TERMINAL_EXITS,
    }),
    RunStatus.REVIEWING: frozenset({RunStatus.DONE, RunStatus.WORKING, *_TERMINAL_EXITS}),
    RunStatus.DONE: frozenset(),
    RunStatus.BLOCKED: frozenset(),
    RunStatus.BUDGET_EXHAUSTED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
    RunStatus.FAILED_INTERNAL: frozenset({RunStatus.WORKING}),
}


def transition(state: RunState, new: RunStatus) -> RunState:
    """Apply an engine-owned run-state transition, rejecting invalid edges."""
    if not isinstance(new, RunStatus):
        raise ValueError(f"invalid run status: {new!r}")
    allowed = RUN_STATUS_TRANSITIONS.get(state.status, frozenset())
    if new not in allowed:
        raise ValueError(f"invalid run status transition: {state.status.value} -> {new.value}")
    state.status = new
    return state


def validate_state(state: RunState) -> None:
    if state.schema_version != SCHEMA_VERSION:
        raise ValueError(f"unsupported state schema_version: {state.schema_version}")
    if not state.run_id or not isinstance(state.run_id, str):
        raise ValueError("state run_id must be non-empty")
    if not isinstance(state.status, RunStatus):
        raise ValueError("state status is invalid")
    for name in ("round", "idle_rounds", "total_workers", "repeated_verification_failures",
                 "repeated_verifier_failures"):
        value = getattr(state, name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"state {name} must be a non-negative integer")
    if (not isinstance(state.active_seconds, (int, float)) or
            isinstance(state.active_seconds, bool) or not math.isfinite(state.active_seconds) or
            state.active_seconds < 0):
        raise ValueError("state active_seconds must be a non-negative number")
    if (not isinstance(state.rounds, list) or not isinstance(state.message, str) or
            not isinstance(state.active_started_at, str) or
            not isinstance(state.last_verification_signature, str) or
            not isinstance(state.last_verifier_signature, str)):
        raise ValueError("state rounds/message have invalid types")
    previous = 0
    for round_ in state.rounds:
        if not isinstance(round_, dict):
            raise ValueError("stored round must be an object")
        number = round_.get("number")
        if not isinstance(number, int) or isinstance(number, bool) or number <= previous:
            raise ValueError("stored round numbers must be strictly increasing positive integers")
        previous = number
        if "writer" in round_ and not isinstance(round_["writer"], dict):
            raise ValueError("stored round writer must be an object")
        if "scouts" in round_ and (not isinstance(round_["scouts"], list) or
                any(not isinstance(item, dict) for item in round_["scouts"])):
            raise ValueError("stored round scouts must be object arrays")
        if "verification" in round_ and (not isinstance(round_["verification"], list) or
                any(not isinstance(item, dict) for item in round_["verification"])):
            raise ValueError("stored round verification must be object arrays")
        if "path_issues" in round_ and (not isinstance(round_["path_issues"], list) or
                any(not isinstance(item, str) for item in round_["path_issues"])):
            raise ValueError("stored round path_issues must be string arrays")
        if "verifier" in round_ and not isinstance(round_["verifier"], dict):
            raise ValueError("stored round verifier must be an object")
        if "evidence_hash" in round_ and not isinstance(round_["evidence_hash"], str):
            raise ValueError("stored round evidence_hash must be a string")
        for name in ("fingerprint", "next_fingerprint", "scheduled_action",
                     "verifier_signature"):
            if name in round_ and not isinstance(round_[name], str):
                raise ValueError(f"stored round {name} must be a string")


T = TypeVar("T")


def dump(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if isinstance(value, StrEnum):
        return value.value
    return value


def load_dataclass(cls: type[T], data: dict[str, Any]) -> T:
    """Ignore future fields while preserving nested current models."""
    if not isinstance(data, dict):
        raise ValueError(f"{cls.__name__} must be a JSON object")
    accepted = {f.name for f in fields(cls)}
    values = {k: v for k, v in data.items() if k in accepted}
    if cls is RunContract:
        values["budget"] = load_dataclass(Budget, values.get("budget", {}))
        values["pack"] = load_dataclass(PackPolicy, values.get("pack", {}))
    if cls is RunState and "status" in values:
        values["status"] = RunStatus(values["status"])
    if cls in {WorkerState, WorkerResult} and "status" in values:
        values["status"] = WorkerStatus(values["status"])
    if cls is WorkerResult:
        values["evidence"] = [load_dataclass(Evidence, item)
                              for item in values.get("evidence", [])]
    return cls(**values)
