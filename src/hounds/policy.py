from __future__ import annotations

import hashlib
import json

from .models import PackPolicy, RunState


def fingerprint(role: str, objective: str, action: str, scope: list[str], verification: list[list[str]]) -> str:
    normalized = json.dumps({"role": role, "objective": " ".join(objective.split()),
                             "action": " ".join(action.split()), "scope": sorted(scope),
                             "verification": verification}, sort_keys=True)
    return hashlib.sha256(normalized.encode()).hexdigest()


def should_pack(policy: PackPolicy, state: RunState, writer: dict) -> bool:
    if policy.mode == "off":
        return False
    if policy.mode == "on":
        return True
    request = writer.get("parallel_request")
    requested = request.get("needed") is True if isinstance(request, dict) else False
    return (requested or writer.get("status") == "blocked" or state.idle_rounds >= 2 or
            state.repeated_verification_failures >= 2 or
            state.repeated_verifier_failures >= 2)


def parallel_missions(writer: dict) -> list[str]:
    request = writer.get("parallel_request")
    missions = request.get("missions") if isinstance(request, dict) else None
    if not isinstance(missions, list):
        return []
    normalized = [" ".join(mission.split()) for mission in missions
                  if isinstance(mission, str) and mission.strip()]
    return list({mission.casefold(): mission for mission in normalized}.values())


def terminal(status: str) -> bool:
    return status in {"done", "blocked", "budget_exhausted", "cancelled", "failed_internal"}
