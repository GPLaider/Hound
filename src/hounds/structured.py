from __future__ import annotations

import json
import math
from typing import Any


def output_schema(role: str) -> dict[str, Any]:
    """Small strict schemas for Codex versions that support --output-schema."""
    string = {"type": "string"}
    strings = {"type": "array", "items": string}

    def object_(properties: dict[str, Any]) -> dict[str, Any]:
        return {"type": "object", "properties": properties,
                "required": list(properties), "additionalProperties": False}

    evidence_item = {
        "type": "object",
        "properties": {"claim": string, "source": string, "artifact": string,
                       "confidence": {"type": "number", "minimum": 0, "maximum": 1}},
        "required": ["claim", "source", "artifact", "confidence"],
        "additionalProperties": False,
    }
    evidence = {"type": "array", "items": evidence_item}
    schemas = {
        "writer": object_({
            "status": {"type": "string", "enum": ["continue", "candidate_done", "blocked"]},
            "summary": string, "action_taken": string, "evidence": evidence,
            "changed_files": strings,
            "commands_run": {"type": "array", "items": object_({
                "argv": strings, "exit_code": {"type": ["integer", "null"]},
                "summary": string})},
            "verification_observed": strings, "next_action": string,
            "parallel_request": object_({"needed": {"type": "boolean"},
                                         "reason": string, "missions": strings}),
            "blocker": {"type": ["string", "null"]},
        }),
        "scout": object_({
            "summary": string, "evidence": evidence,
            "hypotheses": {"type": "array", "items": object_({
                "statement": string, "supports": strings, "contradicts": strings,
                "next_test": string})},
            "recommended_next_action": string, "unique_contribution": string,
            "confidence": {"type": "number"},
        }),
        "judge": object_({
            "ranking": {"type": "array", "items": object_({
                "worker_id": string, "semantic_score": {"type": "number"},
                "retain": {"type": "boolean"}, "unique_evidence": {"type": "boolean"},
                "brief_reason": string})},
            "kill_order": strings, "merge_notes": string, "confidence": {"type": "number"},
        }),
        "verifier": object_({
            "verdict": {"type": "string", "enum": ["accept", "revise"]},
            "verified_claims": strings, "unverified_claims": strings,
            "blocking_issues": strings, "nonblocking_issues": strings,
            "required_next_action": {"type": ["string", "null"]},
        }),
    }
    return schemas[role]


def matches_output_schema(role: str, value: Any) -> bool:
    def matches(item: Any, schema: dict[str, Any]) -> bool:
        expected = schema.get("type")
        types = expected if isinstance(expected, list) else [expected]
        kind = ("null" if item is None else "boolean" if isinstance(item, bool) else
                "object" if isinstance(item, dict) else "array" if isinstance(item, list) else
                "string" if isinstance(item, str) else "integer" if isinstance(item, int) else
                "number" if isinstance(item, float) and math.isfinite(item) else "invalid")
        if not (kind in types or kind == "integer" and "number" in types) or (
                "enum" in schema and item not in schema["enum"]):
            return False
        if kind in {"integer", "number"} and (
                item < schema.get("minimum", item) or item > schema.get("maximum", item)):
            return False
        if kind == "array":
            return all(matches(child, schema["items"]) for child in item)
        if kind != "object":
            return True
        properties = schema.get("properties", {})
        if any(name not in item for name in schema.get("required", [])):
            return False
        if schema.get("additionalProperties") is False and set(item) - set(properties):
            return False
        return all(matches(item[name], child) for name, child in properties.items() if name in item)

    return matches(value, output_schema(role))


def last_json_object(text: str) -> dict[str, Any] | None:
    """Return the last decodable JSON object without trusting markdown fences."""
    decoder = json.JSONDecoder()
    found: tuple[int, int, dict[str, Any]] | None = None
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidate = (index + end, -index, value)
            if found is None or candidate[:2] > found[:2]:
                found = candidate
    return found[2] if found else None


def _number(value: Any, minimum: float, maximum: float) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and
            math.isfinite(value) and minimum <= value <= maximum)


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _findings(value: Any) -> tuple[list[str | dict[str, Any]], bool]:
    if not isinstance(value, list):
        return [], False
    findings = [item for item in value if isinstance(item, (str, dict))]
    return findings, len(findings) == len(value)


def _evidence(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    accepted: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if not isinstance(item.get("claim"), str) or not item["claim"].strip():
            continue
        string_fields = ("source", "artifact", "file", "fact", "observed", "command")
        if any(name in item and not isinstance(item[name], str) for name in string_fields):
            continue
        if "confidence" in item and not _number(item["confidence"], 0, 1):
            continue
        accepted.append(item)
    return accepted


def _commands(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    accepted: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        argv = item.get("argv")
        exit_code = item.get("exit_code")
        if (not isinstance(argv, list) or not argv or
                any(not isinstance(part, str) or not part for part in argv)):
            continue
        if exit_code is not None and (not isinstance(exit_code, int) or isinstance(exit_code, bool)):
            continue
        if not isinstance(item.get("summary"), str):
            continue
        accepted.append(item)
    return accepted


def _parallel_request(value: Any) -> dict[str, Any]:
    fallback = {"needed": False, "reason": "", "missions": []}
    if not isinstance(value, dict) or not isinstance(value.get("needed"), bool):
        return fallback
    reason = value.get("reason", "")
    if not isinstance(reason, str):
        return fallback
    missions = value.get("missions", [])
    if not isinstance(missions, list) or any(
            not isinstance(mission, str) or not mission.strip() for mission in missions):
        return fallback
    normalized = dict(value)
    normalized["reason"] = reason
    normalized["missions"] = missions
    return normalized


def normalize_result(role: str, text: str) -> dict[str, Any]:
    value = last_json_object(text)
    if value is None:
        value = {"status": "continue", "summary": text[-2000:], "evidence": [],
                 "next_action": "Inspect the preserved raw output and continue.",
                 "parse_confidence": 0.1}
    value.setdefault("summary", "")
    value.setdefault("evidence", [])
    value.setdefault("parse_confidence", 1.0)
    if not _number(value["parse_confidence"], 0, 1):
        value["parse_confidence"] = 0.1
    if role == "writer":
        if value.get("status") not in {"continue", "candidate_done", "blocked"}:
            value["status"] = "continue"
        for name in ("summary", "action_taken", "next_action"):
            if not isinstance(value.get(name), str):
                value[name] = ""
        if value.get("blocker") is not None and not isinstance(value.get("blocker"), str):
            value["blocker"] = None
        value["evidence"] = _evidence(value.get("evidence"))
        value["changed_files"] = _strings(value.get("changed_files"))
        value["commands_run"] = _commands(value.get("commands_run"))
        observed, _ = _findings(value.get("verification_observed", []))
        value["verification_observed"] = observed
        value["parallel_request"] = _parallel_request(value.get("parallel_request"))
    if role == "scout":
        if not isinstance(value.get("summary"), str):
            value["summary"] = ""
        value["evidence"] = _evidence(value.get("evidence"))
        hypotheses: list[dict[str, Any]] = []
        if isinstance(value.get("hypotheses"), list):
            for hypothesis in value["hypotheses"]:
                if not isinstance(hypothesis, dict):
                    continue
                if (not isinstance(hypothesis.get("statement"), str) or
                        not hypothesis["statement"].strip()):
                    continue
                if any(not isinstance(hypothesis.get(name), list) or
                       any(not isinstance(item, str) for item in hypothesis[name])
                       for name in ("supports", "contradicts")):
                    continue
                if not isinstance(hypothesis.get("next_test"), str):
                    continue
                hypotheses.append(hypothesis)
        value["hypotheses"] = hypotheses
        for name in ("recommended_next_action", "unique_contribution"):
            if not isinstance(value.get(name), str):
                value[name] = ""
        if not _number(value.get("confidence"), 0, 1):
            value["confidence"] = 0.0
    if role == "judge":
        ranking: list[dict[str, Any]] = []
        if isinstance(value.get("ranking"), list):
            for item in value["ranking"]:
                if (not isinstance(item, dict) or
                        not isinstance(item.get("worker_id"), str) or
                        not item["worker_id"].strip() or
                        not _number(item.get("semantic_score"), 0, 100)):
                    continue
                normalized = dict(item)
                normalized["retain"] = item.get("retain", False) if isinstance(
                    item.get("retain", False), bool) else False
                normalized["unique_evidence"] = item.get("unique_evidence", False) if isinstance(
                    item.get("unique_evidence", False), bool) else False
                if not isinstance(normalized.get("brief_reason", ""), str):
                    normalized["brief_reason"] = ""
                ranking.append(normalized)
        value["ranking"] = ranking
        value["kill_order"] = _strings(value.get("kill_order"))
        if not isinstance(value.get("merge_notes"), str):
            value["merge_notes"] = ""
        if not _number(value.get("confidence"), 0, 1):
            value["confidence"] = 0.0
    if role == "verifier":
        lists = ("verified_claims", "unverified_claims", "blocking_issues", "nonblocking_issues")
        malformed = value.get("verdict") not in {"accept", "revise"}
        for name in lists:
            value[name], valid = _findings(value.get(name))
            malformed = malformed or not valid
        if value.get("required_next_action") is not None and not isinstance(
                value.get("required_next_action"), str):
            value["required_next_action"] = "repair malformed verifier output"
            malformed = True
        if malformed:
            value["verdict"] = "revise"
            if "malformed verifier result" not in value["blocking_issues"]:
                value["blocking_issues"].append("malformed verifier result")
    return value
