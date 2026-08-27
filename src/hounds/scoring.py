from __future__ import annotations

import json
import re
from typing import Iterable

from .structured import last_json_object


def normalized_trace(text: str) -> dict[str, int]:
    """Extract stable signals from version-varying JSONL while raw trace stays on disk."""
    found = {"events": 0, "commands": 0, "exit_codes": 0, "stderr": 0,
             "finals": 0, "evidence": 0, "unknown": 0}
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        found["events"] += 1
        event_type = str(event.get("type", "")).casefold()
        keys = {str(key).casefold() for key in event}
        recognized = False
        if keys & {"command", "argv", "cmd"} or "command" in event_type:
            found["commands"] += 1; recognized = True
        if keys & {"exit_code", "returncode", "status_code"}:
            found["exit_codes"] += 1; recognized = True
        if "stderr" in keys or "stderr" in event_type:
            found["stderr"] += 1; recognized = True
        if (keys & {"final_response", "output", "summary"} or
                event_type in {"final", "result", "agent_message"}):
            found["finals"] += 1; recognized = True
        if keys & {"evidence", "claim", "artifact"} or "evidence" in event_type:
            found["evidence"] += 1; recognized = True
        if not recognized:
            found["unknown"] += 1
    return found


def signals(text: str) -> dict[str, int]:
    paths = set(re.findall(r"(?:[\w.-]+[/\\])+[\w.-]+", text))
    errors = len(re.findall(r"\b(?:error|failed|exception|traceback)\b", text, re.I))
    evidence = len(re.findall(r"\b(?:evidence|verified|exit[_ ]?code|reproduced|claim)\b", text, re.I))
    hypotheses = len(re.findall(r"\b(?:hypothesis|likely|contradict|root cause)\b", text, re.I))
    repeats = max(0, len(text.splitlines()) - len(set(text.splitlines())))
    return {"paths": len(paths), "errors": errors, "evidence": evidence,
            "hypotheses": hypotheses, "repeats": repeats}


def deterministic_score(text: str, progress_events: int, mission: str = "") -> float:
    found = signals(text)
    trace = normalized_trace(text)
    score = 15.0
    score += min(30, found["evidence"] * 6 + found["paths"] * 3 + trace["evidence"] * 3)
    score += min(25, 5 + sum(word.lower() in text.lower() for word in mission.split()[:12]) * 2)
    score += min(15, found["hypotheses"] * 4)
    score += min(10, progress_events * 2 + trace["commands"] + trace["exit_codes"])
    score += 5 if len(text) < 12000 else 1
    score -= min(30, found["errors"] * 5)
    score -= min(20, found["repeats"] * 2)
    if not text.strip():
        score -= 30
    return round(max(0, min(100, score)), 2)


def merge_semantic(deterministic: float, semantic: float | None) -> float:
    deterministic = max(0, min(100, deterministic))
    semantic = None if semantic is None else max(0, min(100, semantic))
    return round(deterministic if semantic is None else deterministic * 0.6 + semantic * 0.4, 2)


def unique_evidence(worker_text: str, other_texts: Iterable[str]) -> bool:
    mine = evidence_signals(worker_text)
    others: set[str] = set()
    for text in other_texts:
        others.update(evidence_signals(text))
    return bool(mine - others)


def evidence_signals(text: str) -> set[str]:
    values = {path.casefold() for path in re.findall(r"(?:[\w.-]+[/\\])+[\w.-]+", text)}
    documents = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            documents.append(event)
    final = last_json_object(text)
    if isinstance(final, dict) and final not in documents:
        documents.append(final)
    pending = list(documents)
    while pending:
        value = pending.pop()
        if isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, dict):
            for name in ("claim", "source", "artifact", "statement", "next_test"):
                item = value.get(name)
                if isinstance(item, str) and item.strip():
                    values.add(" ".join(item.casefold().split()))
            pending.extend(value.get(name, []) for name in (
                "evidence", "hypotheses", "supports", "contradicts"))
    return values
