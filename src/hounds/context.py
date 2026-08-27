from __future__ import annotations

import json
from typing import Any


def bounded_context(items: list[tuple[int, str, Any]], maximum_bytes: int) -> tuple[dict[str, Any], list[str]]:
    kept: dict[str, Any] = {}
    omitted: list[str] = []
    for _, name, value in sorted(items, key=lambda item: item[0]):
        candidate = {**kept, name: value}
        if len(json.dumps(candidate, ensure_ascii=False).encode()) <= maximum_bytes:
            kept[name] = value
        else:
            omitted.append(name)
    return kept, omitted
