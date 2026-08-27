from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import HoundEnvironmentError, WorkerSpec
from .process import (ManagedProcess, ProcessOutcome, codex_environment, redact,
                      resolve_executable, run_process, trusted_python_executable,
                      worker_environment)
from .structured import matches_output_schema, normalize_result, output_schema
from .store import atomic_json


@runtime_checkable
class WorkerAdapter(Protocol):
    async def negotiate(self, workspace: Path, artifact_dir: Path) -> dict[str, bool]: ...

    async def run(self, spec: WorkerSpec, prompt: str, workspace: Path,
                  artifact_dir: Path, cancel_path: Path | None = None) -> tuple[ProcessOutcome, dict]: ...

    async def start(self, spec: WorkerSpec, prompt: str, workspace: Path,
                    artifact_dir: Path) -> tuple[ManagedProcess, Path]: ...

    def finish(self, spec: WorkerSpec, outcome: ProcessOutcome, final_path: Path,
               artifact_dir: Path) -> dict: ...

    def available(self) -> bool: ...


class CodexAdapter:
    def __init__(self, executable: str = "codex", prefix: list[str] | None = None):
        self.executable = executable
        self.prefix = prefix
        self.capabilities: dict[str, bool] | None = None
        self._strict_artifacts: set[Path] = set()

    def argv(self, spec: WorkerSpec, prompt: str, final_path: Path) -> list[str]:
        if self.prefix:
            return [*self.prefix, spec.role, prompt]
        supported = self.capabilities or {
            "json": True, "ephemeral": True, "skip_git": True, "color": True,
            "sandbox": True, "output_last_message": True, "output_schema": True,
            "model": True, "approval_policy": True, "ignore_user_config": True,
        }
        argv = [self.executable]
        if os.name == "nt":
            argv.extend(["-c", 'windows.sandbox="elevated"'])
        if supported.get("approval_policy"):
            argv.extend(["--ask-for-approval", "never"])
        argv.append("exec")
        if supported.get("ignore_user_config"): argv.append("--ignore-user-config")
        if supported["json"]: argv.append("--json")
        if supported["ephemeral"]: argv.append("--ephemeral")
        if supported["skip_git"]: argv.append("--skip-git-repo-check")
        if supported["color"]: argv.extend(["--color", "never"])
        if supported["sandbox"]: argv.extend(["--sandbox", spec.sandbox])
        if supported["output_last_message"]:
            argv.extend(["--output-last-message", str(final_path)])
        if supported.get("output_schema") and supported["output_last_message"]:
            argv.extend(["--output-schema", str(final_path.with_name("output-schema.json").resolve())])
        if spec.model and supported["model"]:
            argv.extend(["--model", spec.model])
        argv.append("-")
        return argv

    async def negotiate(self, workspace: Path, artifact_dir: Path) -> dict[str, bool]:
        if self.prefix:
            self.capabilities = {name: True for name in (
                "json", "ephemeral", "skip_git", "color", "sandbox",
                "output_last_message", "output_schema", "model", "approval_policy",
                "ignore_user_config")}
            return self.capabilities
        executable = resolve_executable(self.executable, workspace)
        if not executable:
            raise HoundEnvironmentError("Codex executable not found")
        environment = codex_environment()
        version = await run_process([executable, "--version"], workspace,
                                    artifact_dir / "version", 15, environment)
        root_help = await run_process([executable, "--help"], workspace,
                                      artifact_dir / "help", 15, environment)
        exec_help = await run_process([executable, "exec", "--help"], workspace,
                                      artifact_dir / "exec-help", 15, environment)
        if exec_help.exit_code != 0 or exec_help.timed_out:
            raise HoundEnvironmentError("Codex exec capability probe failed")
        text = exec_help.stdout + exec_help.stderr
        root_text = root_help.stdout + root_help.stderr
        self.capabilities = {
            "json": "--json" in text,
            "ephemeral": "--ephemeral" in text,
            "skip_git": "--skip-git-repo-check" in text,
            "color": "--color" in text,
            "sandbox": "--sandbox" in text,
            "output_last_message": "--output-last-message" in text,
            "output_schema": "--output-schema" in text,
            "model": "--model" in text,
            "approval_policy": "--ask-for-approval" in root_text,
            "ignore_user_config": "--ignore-user-config" in text,
        }
        trusted_python = trusted_python_executable()
        python_probe = await run_process(
            [trusted_python, "-I", "-c", "import sys;print(sys.executable)"],
            workspace, artifact_dir / "python", 15, environment)
        atomic_json(artifact_dir / "capabilities.json", {
            "executable": executable, "version_exit_code": version.exit_code,
            "help_exit_code": root_help.exit_code, "exec_help_exit_code": exec_help.exit_code,
            "trusted_python": trusted_python, "python_exit_code": python_probe.exit_code,
            "capabilities": self.capabilities,
        })
        if not self.capabilities["sandbox"]:
            raise HoundEnvironmentError("Codex exec lacks the sandbox option required for role isolation")
        required = ("approval_policy", "ignore_user_config")
        missing = [name for name in required if not self.capabilities[name]]
        if missing:
            raise HoundEnvironmentError(
                "Codex exec lacks required safe automation capability: " + ", ".join(missing))
        if python_probe.exit_code != 0 or python_probe.timed_out:
            raise HoundEnvironmentError("trusted Hound Python failed its isolated preflight")
        return self.capabilities

    async def run(self, spec: WorkerSpec, prompt: str, workspace: Path,
                  artifact_dir: Path, cancel_path: Path | None = None) -> tuple[ProcessOutcome, dict]:
        managed, final_path = await self.start(spec, prompt, workspace, artifact_dir)
        outcome = await managed.wait(spec.timeout_seconds, cancel_path)
        return outcome, self.finish(spec, outcome, final_path, artifact_dir)

    async def start(self, spec: WorkerSpec, prompt: str, workspace: Path,
                    artifact_dir: Path) -> tuple[ManagedProcess, Path]:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        final_path = artifact_dir / "final.txt"
        supported = self.capabilities or {"output_schema": True, "output_last_message": True}
        if (not self.prefix and supported.get("output_schema") and
                supported.get("output_last_message")):
            self._strict_artifacts.add(artifact_dir.resolve())
            atomic_json(artifact_dir / "output-schema.json", output_schema(spec.role))
        redacted = redact(prompt)
        (artifact_dir / "prompt.txt").write_text(redacted, encoding="utf-8", newline="\n")
        (artifact_dir / "prompt.sha256").write_text(hashlib.sha256(redacted.encode()).hexdigest(), encoding="ascii")
        managed = ManagedProcess(
            self.argv(spec, prompt, final_path), workspace, artifact_dir,
            env=worker_environment() if self.prefix else codex_environment(),
            input_text=None if self.prefix else prompt)
        await managed.start()
        return managed, final_path

    def finish(self, spec: WorkerSpec, outcome: ProcessOutcome, final_path: Path,
               artifact_dir: Path) -> dict:
        strict = (artifact_dir.resolve() in self._strict_artifacts or
                  not self.prefix and self.capabilities is not None and
                  self.capabilities.get("output_schema", False) and
                  self.capabilities.get("output_last_message", False))
        raw = final_path.read_text(encoding="utf-8", errors="replace") \
            if final_path.exists() else "" if strict else outcome.stdout
        final = redact(raw)
        if not final_path.exists() or final != raw:
            final_path.write_text(final, encoding="utf-8")
        if strict:
            try:
                value = json.loads(final)
            except json.JSONDecodeError:
                value = None
            valid = matches_output_schema(spec.role, value)
            result = normalize_result(
                spec.role, json.dumps(value, ensure_ascii=False) if valid else "")
            if not valid:
                result["parse_confidence"] = 0.1
        else:
            result = normalize_result(spec.role, final)
        result["structured_mode"] = "strict" if strict else "tolerant"
        result["exit_code"] = outcome.exit_code
        result["timed_out"] = outcome.timed_out
        result["cancelled"] = outcome.cancelled
        atomic_json(artifact_dir / "result.json", result)
        return result

    def available(self) -> bool:
        return bool(self.prefix or resolve_executable(self.executable, Path.cwd()))
