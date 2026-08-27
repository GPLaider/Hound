from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from .evidence import content_identity_async, round_evidence_hash
from .models import RunContract, RunState, RunStatus
from .policy import fingerprint
from .store import artifact_sha256_async, atomic_json
from .structured import normalize_result
from .verification import changed_paths_since_baseline

if TYPE_CHECKING:
    from .engine import Engine


def verifier_failure(message: str) -> dict:
    return {"verdict": "revise", "verified_claims": [], "unverified_claims": [],
            "blocking_issues": [message], "nonblocking_issues": [],
            "required_next_action": "repair verifier invocation"}


def _verifier_signature(verifier: dict) -> str:
    material = {name: verifier.get(name) for name in (
        "verdict", "verified_claims", "unverified_claims", "blocking_issues",
        "nonblocking_issues", "required_next_action")}
    return hashlib.sha256(json.dumps(
        material, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


async def resume_review(engine: Engine, state: RunState, contract: RunContract) -> None:
    run_dir = engine.store.run_dir(state.run_id)
    cancel_path = run_dir / "cancel.requested"
    round_dir = run_dir / "rounds" / f"{state.round:03d}"
    last = state.rounds[-1] if state.rounds else {}
    verification = last.get("verification", []) if isinstance(last, dict) else []
    checkpoint = run_dir / "final" / "verification.json"
    try:
        proof = json.loads(checkpoint.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        proof = {}
    round_ready = (
        last.get("number") == state.round and last.get("status") == "verified" and
        last.get("writer", {}).get("status") == "candidate_done" and
        len(verification) == len(contract.verify) and verification and
        all(isinstance(item, dict) and item.get("passed") is True for item in verification) and
        last.get("path_issues") == [])
    if not round_ready:
        if state.status is RunStatus.REVIEWING:
            engine._transition(state, RunStatus.WORKING)
            engine.store.save_state(state)
        return
    saved_review = last.get("verifier", {})
    if (state.status is RunStatus.WORKING and isinstance(saved_review, dict) and
            last.get("verifier_signature") == _verifier_signature(saved_review)):
        return
    current_paths = await changed_paths_since_baseline(
        engine.workspace, engine.baseline, round_dir / "review-freshness", cancel_path)
    if engine._cancelled(state, cancel_path, "cancelled during final review freshness check"):
        return
    current_identity = await content_identity_async(
        engine.workspace, current_paths, cancel_path)
    if engine._cancelled(state, cancel_path, "cancelled during final review content check"):
        return
    paths_match = (current_paths == last.get("git_diff", [])
                   if engine.baseline.get("kind") == "git" else True)
    if (current_identity is None or not paths_match or
            current_identity != last.get("git_identity", "")):
        engine.store.event(
            state.run_id, "review_checkpoint_stale",
            previous_paths=last.get("git_diff", []), current_paths=current_paths,
            previous_identity=last.get("git_identity", ""), current_identity=current_identity)
        engine._transition(state, RunStatus.WORKING)
        engine.store.save_state(state)
        return
    if not (isinstance(proof, dict) and proof.get("round") == state.round and
            proof.get("passed") is True):
        atomic_json(checkpoint, {
            "round": state.round, "passed": True, "machine": verification,
            "path_issues": [], "git_diff": last.get("git_diff", []),
        })
    if state.status is RunStatus.WORKING:
        engine._transition(state, RunStatus.VERIFYING)
    engine._transition(state, RunStatus.REVIEWING)
    engine.store.save_state(state)
    if engine._cancelled(state, cancel_path, "cancelled before final review resume"):
        return
    saved = last.get("verifier")
    verifier = ({**saved, "round": state.round} if
                isinstance(saved, dict) and saved.get("verdict") in {"accept", "revise"} and
                isinstance(saved.get("blocking_issues"), list) else {})
    verifier_path = run_dir / "final" / "verifier.json"
    if not verifier:
        try:
            loaded = json.loads(verifier_path.read_text(encoding="utf-8"))
            verifier = (loaded if isinstance(loaded, dict) and
                        loaded.get("round") == state.round else {})
        except (OSError, json.JSONDecodeError):
            verifier = {}
    if not verifier:
        attempts = []
        for result_path in round_dir.glob("**/verifier/result.json"):
            try:
                attempts.append((result_path.stat().st_mtime_ns, result_path))
            except OSError:
                continue
        for _, result_path in sorted(
                attempts, key=lambda item: (item[0], str(item[1])), reverse=True):
            outcome_path = result_path.with_name("outcome.json")
            if not outcome_path.exists():
                continue
            try:
                stored_result = json.loads(result_path.read_text(encoding="utf-8"))
                stored_outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                verifier = verifier_failure("durable verifier artifact is malformed")
                break
            if not isinstance(stored_result, dict) or not isinstance(stored_outcome, dict):
                verifier = verifier_failure("durable verifier artifact is malformed")
                break
            if (stored_outcome.get("exit_code") == 0 and
                    stored_outcome.get("timed_out") is False and
                    stored_outcome.get("cancelled") is False):
                verifier = normalize_result(
                    "verifier", json.dumps(stored_result, ensure_ascii=False))
            else:
                verifier = verifier_failure(
                    "durable verifier attempt did not complete successfully")
            verifier["round"] = state.round
            verifier["artifact_dir"] = result_path.parent.relative_to(run_dir).as_posix()
            artifact_hash = await artifact_sha256_async(
                result_path.parent, cancel_path)
            if artifact_hash is None:
                engine._cancelled(
                    state, cancel_path, "cancelled while recovering final review artifact")
                return
            verifier["artifact_sha256"] = artifact_hash
            engine.store.event(
                state.run_id, "verifier_attempt_recovered",
                artifact_dir=verifier["artifact_dir"])
            break
    if not verifier:
        if state.total_workers >= contract.budget.max_total_workers:
            engine._finish(state, RunStatus.BUDGET_EXHAUSTED,
                           "worker budget exhausted before final Verifier resume")
            return
        attempt = 1
        while (round_dir / f"review-resume-{attempt}").exists():
            attempt += 1
        verifier = await engine._verifier(
            state, contract, round_dir / f"review-resume-{attempt}", last)
    verifier = normalize_result("verifier", json.dumps(verifier, ensure_ascii=False))
    atomic_json(verifier_path, {**verifier, "round": state.round})
    last["verifier"] = verifier
    engine.store.save_state(state)
    if not engine._cancelled(state, cancel_path, "cancelled during final review resume"):
        engine._review_result(state, verifier)


def review_result(engine: Engine, state: RunState, verifier: dict) -> None:
    if verifier.get("verdict") == "accept" and verifier.get("blocking_issues") == []:
        state.repeated_verifier_failures = 0
        state.last_verifier_signature = ""
        engine._finish(state, RunStatus.DONE, "acceptance and verification passed")
        return
    signature = _verifier_signature(verifier)
    current = state.rounds[-1] if state.rounds and state.rounds[-1].get(
        "number") == state.round else None
    if current is not None and current.get("verifier_signature") == signature:
        engine._transition(state, RunStatus.WORKING)
        engine.store.save_state(state)
        return
    state.repeated_verifier_failures = (
        state.repeated_verifier_failures + 1
        if signature == state.last_verifier_signature else 1)
    state.last_verifier_signature = signature
    if current is not None:
        current["verifier"] = verifier
        current["verifier_signature"] = signature
        required = verifier.get("required_next_action")
        if isinstance(required, str) and required.strip():
            required = " ".join(required.split())
            contract = engine.store.load_contract(state.run_id)
            current["scheduled_action"] = required
            current["next_fingerprint"] = fingerprint(
                "writer", contract.objective, required, [], contract.verify)
        evidence_hash = round_evidence_hash(state, {}, [], [])
        current["evidence_hash"] = evidence_hash
        action = current.get("fingerprint")
        record = {"fingerprint": action, "status": "failed",
                  "evidence_hash": evidence_hash}
        if action and not any(all(item.get(name) == record[name] for name in record)
                              for item in engine.store.fingerprints(state.run_id)):
            engine.store.fingerprint(state.run_id, action, "failed", evidence_hash)
    engine._transition(state, RunStatus.WORKING)
    engine.store.event(
        state.run_id, "final_review_revise", result=verifier,
        verifier_signature=signature,
        repeated_verifier_failures=state.repeated_verifier_failures)
    engine.store.save_state(state)
