from __future__ import annotations

import math
import os
import shlex
import tomllib
from pathlib import Path

from .models import Budget, PackPolicy, RunContract, validate_contract

DEFAULT_CONFIG = """[run]
max_rounds = 20
max_idle_rounds = 3
max_total_workers = 12
max_wall_minutes = 120
pack = "auto"

[workers]
concurrency = 3
writer_timeout_seconds = 3600
scout_timeout_seconds = 900
judge_timeout_seconds = 300
verifier_timeout_seconds = 900

[pack]
# auto prefers AgentFlow for a qualified parallel Pack and falls back to direct supervision.
backend = "auto"
initial_workers = 3
maximum_workers = 5
minimum_runtime_seconds = 25
minimum_progress_events = 2
judge_interval_seconds = 40
cull_cooldown_seconds = 15
minimum_score_gap = 8
survivors = 1
semantic_judge = true

[agentflow]
enabled = true
executable = "agentflow"

[codex]
executable = "codex"
writer_model = ""
scout_model = ""
judge_model = ""
verifier_model = ""

[context]
maximum_prompt_bytes = 65536
maximum_trace_excerpt_bytes = 8192
retain_recent_rounds = 4
"""


def load_config(root: Path) -> dict:
    path = root / ".hound" / "config.toml"
    if not path.exists():
        config = tomllib.loads(DEFAULT_CONFIG)
        validate_config(config)
        return config
    with path.open("rb") as handle:
        config = tomllib.load(handle)
    validate_config(config)
    return config


def validate_config(config: dict) -> None:
    sections = ("run", "workers", "pack", "agentflow", "codex", "context")
    if not isinstance(config, dict) or any(
            name in config and not isinstance(config[name], dict) for name in sections):
        raise ValueError("configuration sections must be TOML tables")
    workers = config.get("workers", {})
    for name in ("concurrency", "writer_timeout_seconds", "scout_timeout_seconds",
                 "judge_timeout_seconds", "verifier_timeout_seconds"):
        value = workers.get(name)
        if value is not None and (not isinstance(value, (int, float)) or
                                  isinstance(value, bool) or not math.isfinite(value) or value <= 0):
            raise ValueError(f"workers.{name} must be positive")
    context = config.get("context", {})
    for name in ("maximum_prompt_bytes", "maximum_trace_excerpt_bytes", "retain_recent_rounds"):
        value = context.get(name)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value <= 0):
            raise ValueError(f"context.{name} must be a positive integer")
    agentflow = config.get("agentflow", {})
    if "enabled" in agentflow and not isinstance(agentflow["enabled"], bool):
        raise ValueError("agentflow.enabled must be boolean")
    for section in ("agentflow", "codex"):
        executable = config.get(section, {}).get("executable")
        if executable is not None and (not isinstance(executable, str) or not executable.strip()):
            raise ValueError(f"{section}.executable must be non-empty")
    for name in ("writer_model", "scout_model", "judge_model", "verifier_model"):
        value = config.get("codex", {}).get(name)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"codex.{name} must be a string")
        if isinstance(value, str) and value != value.strip():
            raise ValueError(f"codex.{name} must be empty or have no surrounding whitespace")
    validate_contract(RunContract("validate configuration", ".", [["verify"]],
                                  budget=make_budget(config), pack=make_pack(config)))


def make_budget(config: dict, **overrides: int | None) -> Budget:
    run = config.get("run", {})
    return Budget(**{
        name: overrides[name] if overrides.get(name) is not None else run.get(name, default)
        for name, default in {
            "max_rounds": 20, "max_idle_rounds": 3,
            "max_total_workers": 12, "max_wall_minutes": 120,
        }.items()
    })


def make_pack(config: dict, mode: str | None = None, concurrency: int | None = None,
              backend: str | None = None) -> PackPolicy:
    run, section = config.get("run", {}), config.get("pack", {})
    workers = config.get("workers", {})
    defaults = PackPolicy()
    values = {name: section.get(name, getattr(defaults, name)) for name in (
        "initial_workers", "maximum_workers", "minimum_runtime_seconds", "minimum_progress_events",
        "judge_interval_seconds", "cull_cooldown_seconds", "minimum_score_gap", "survivors",
        "semantic_judge",
    )}
    values["mode"] = mode if mode is not None else run.get("pack", "auto")
    values["backend"] = backend if backend is not None else section.get("backend", "auto")
    values["concurrency"] = concurrency if concurrency is not None else workers.get("concurrency", 3)
    return PackPolicy(**values)


def split_command(command: str) -> list[str]:
    if not command.strip():
        raise ValueError("verification command must be non-empty")
    if os.name != "nt":
        return shlex.split(command)
    import ctypes
    from ctypes import wintypes

    argc = ctypes.c_int()
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    argv = shell32.CommandLineToArgvW(command, ctypes.byref(argc))
    if not argv:
        raise ValueError("invalid verification command")
    try:
        return [argv[index] for index in range(argc.value)]
    finally:
        kernel32.LocalFree(argv)
