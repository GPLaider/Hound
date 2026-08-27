from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .models import RunState, dump
from .policy import fingerprint
from .scoring import evidence_signals
from .verification import changed_content_signature, changed_content_signature_async


def evidence_tokens(rounds: list[dict]) -> set[str]:
    tokens: set[str] = set()

    def add_evidence(value: Any) -> None:
        if not isinstance(value, list):
            return
        for item in value:
            if not isinstance(item, dict):
                continue
            fields = {name: normalized_text(item.get(name)) for name in (
                "claim", "source", "artifact", "file", "fact", "observed", "command")}
            fields = {name: text for name, text in fields.items() if text}
            if fields.get("claim"):
                tokens.add("evidence:" + json.dumps(fields, sort_keys=True))

    for round_ in rounds:
        if not isinstance(round_, dict):
            continue
        writer = round_.get("writer", {})
        if isinstance(writer, dict):
            add_evidence(writer.get("evidence", []))
            for command in writer.get("commands_run", []):
                if (isinstance(command, dict) and isinstance(command.get("argv"), list) and
                        all(isinstance(part, str) for part in command["argv"])):
                    tokens.add("command:" + json.dumps({
                        "argv": command["argv"], "exit_code": command.get("exit_code"),
                        "summary": normalized_text(command.get("summary")),
                    }, sort_keys=True))
            for observed in writer.get("verification_observed", []):
                if isinstance(observed, str) and (text := normalized_text(observed)):
                    tokens.add("verification_observed:" + text)
                elif isinstance(observed, dict):
                    tokens.add("verification_observed:" + json.dumps(
                        observed, ensure_ascii=False, sort_keys=True))
        for result in round_.get("verification", []):
            if not isinstance(result, dict):
                continue
            command = result.get("command", [])
            if not isinstance(command, list) or any(not isinstance(part, str) for part in command):
                continue
            tokens.add("verification:" + json.dumps({
                "command": command, "exit_code": result.get("exit_code"),
                "passed": result.get("passed"), "timed_out": result.get("timed_out", False),
            }, sort_keys=True))
        if signature := normalized_text(round_.get("verification_signature")):
            tokens.add("verification_signature:" + signature)
        for path in round_.get("git_diff", []):
            if text := normalized_text(path):
                tokens.add("git_diff:" + text)
        if identity := normalized_text(round_.get("git_identity")):
            tokens.add("git_identity:" + identity)
        for issue in round_.get("path_issues", []):
            if text := normalized_text(issue):
                tokens.add("path_issue:" + text)
        for scout in round_.get("scouts", []):
            if isinstance(scout, dict):
                add_evidence(scout.get("evidence", []))
                for hypothesis in scout.get("hypotheses", []):
                    if isinstance(hypothesis, dict) and normalized_text(hypothesis.get("statement")):
                        tokens.add("hypothesis:" + json.dumps({name: hypothesis.get(name) for name in (
                            "statement", "supports", "contradicts", "next_test")},
                            ensure_ascii=False, sort_keys=True))
                excerpt = scout.get("partial_excerpt", "")
                if isinstance(excerpt, str):
                    tokens.update("scout_partial:" + item for item in evidence_signals(excerpt))
        verifier = round_.get("verifier", {})
        if isinstance(verifier, dict) and verifier:
            material = {name: verifier.get(name) for name in (
                "verdict", "verified_claims", "unverified_claims", "blocking_issues",
                "nonblocking_issues", "required_next_action", "artifact_dir",
                "artifact_sha256") if name in verifier}
            tokens.add("verifier:" + json.dumps(
                material, ensure_ascii=False, sort_keys=True))
    return tokens


def round_evidence_hash(state: RunState, writer: dict, verification: list,
                        issues: list[str], scouts: list[dict] | None = None,
                        verification_signature: str = "",
                        git_diff: list[str] | None = None,
                        git_identity: str = "") -> str:
    current = {"writer": writer, "verification": [dump(item) for item in verification],
               "verification_signature": verification_signature,
               "path_issues": issues, "scouts": scouts or [], "git_diff": git_diff or [],
               "git_identity": git_identity}
    return hashlib.sha256(json.dumps(
        sorted(evidence_tokens([*state.rounds, current])), ensure_ascii=False).encode()).hexdigest()


def executed_action(writer: dict) -> str:
    action = writer.get("action_taken")
    if isinstance(action, str) and action.strip():
        return " ".join(action.split())
    commands = [item.get("argv", []) for item in writer.get("commands_run", [])
                if isinstance(item, dict)]
    return json.dumps({"summary": writer.get("summary", ""), "commands": commands,
                       "changed_files": writer.get("changed_files", [])},
                      ensure_ascii=False, sort_keys=True)


def followed_scheduled_action(previous_round: dict, writer: dict) -> bool:
    planned = (previous_round.get("scheduled_action") or
               previous_round.get("writer", {}).get("next_action", ""))
    actual = writer.get("action_taken", "")
    if isinstance(planned, str) and planned.strip() and isinstance(actual, str) and actual.strip():
        return " ".join(planned.split()).casefold() == " ".join(actual.split()).casefold()
    return not actual and writer.get("parse_confidence") == 0.1


def writer_fingerprints(state: RunState, objective: str, verification: list[list[str]],
                        writer: dict, scheduled: str = "") -> tuple[str, str]:
    followed = bool(
        scheduled and state.rounds and followed_scheduled_action(state.rounds[-1], writer))
    executed = scheduled if followed else fingerprint(
        "writer", objective, executed_action(writer), writer.get("changed_files", []),
        verification)
    next_action = writer.get("next_action", "")
    normalized = normalized_text(next_action)
    next_fingerprint = (fingerprint("writer", objective, next_action, [], verification)
                        if normalized not in {"", "done", "complete", "completed", "none", "n/a"}
                        else "")
    return executed, next_fingerprint


def content_identity(workspace: Path, paths: list[str] | None) -> str | None:
    return changed_content_signature(workspace, paths if paths is not None else ["."])


async def content_identity_async(workspace: Path, paths: list[str] | None,
                                 cancel_path: Path | None = None) -> str | None:
    return await changed_content_signature_async(
        workspace, paths if paths is not None else ["."], cancel_path)


def normalized_text(value: Any) -> str:
    return " ".join(value.split()).casefold() if isinstance(value, str) else ""
