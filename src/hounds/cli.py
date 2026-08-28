from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import tempfile
from pathlib import Path

from . import __version__
from .adapter import CodexAdapter
from .config import DEFAULT_CONFIG, load_config, make_budget, make_pack, split_command
from .engine import Engine
from .models import (HoundEnvironmentError, HoundLockError, HoundResumeRetryError,
                     RunContract, RunState, RunStatus, dump, validate_contract)
from .process import (cleanup_codex_homes, codex_environment, redact,
                      resolve_executable, run_process, trusted_python_executable)
from .store import RunStore

SKILL = """---
name: hounds
description: Use Hound for persistent verified Codex work that can call AgentFlow for bounded parallel read-only hunts when needed.
---

# Hound

- Do not use Hound for small or obvious changes.
- Start verified work with `hound run "objective" --verify-argv '["python","-m","pytest","-q"]'`.
- Hound keeps one Writer and uses AgentFlow only for a bounded parallel read-only hunt; direct Pack is a fallback.
- Hound isolates Codex worker config and pins workers to its trusted Python; never search application folders for runtimes.
- Inspect or continue durable work with `hound status`, `hound inspect <run-id>`, and `hound resume <run-id>`.
- Hound owns parallelism; never nest Codex native multi-agent work inside a Hound run.
- Do not enable dangerous sandbox access unless the user explicitly requests it.
"""

EXIT = {
    RunStatus.DONE: 0, RunStatus.BUDGET_EXHAUSTED: 2, RunStatus.BLOCKED: 3,
    RunStatus.CANCELLED: 130, RunStatus.FAILED_INTERNAL: 5,
}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="hound", description="Lightweight evidence-driven Codex orchestration")
    root.add_argument("--version", action="version", version=f"hound {__version__}")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create .hound/config.toml without overwriting")
    run = commands.add_parser("run", help="start a persistent run")
    run.add_argument("objective")
    run.add_argument("--verify", action="append", default=[], metavar="COMMAND")
    run.add_argument("--verify-argv", action="append", default=[], metavar="JSON_ARRAY")
    run.add_argument("--required-file", action="append", default=[])
    run.add_argument("--allowed-path", action="append", default=[])
    run.add_argument("--forbidden-path", action="append", default=[])
    run.add_argument("--pack", choices=("auto", "on", "off"))
    run.add_argument("--pack-backend", choices=("auto", "agentflow", "direct"),
                     help="auto prefers AgentFlow for a qualified parallel Pack and keeps direct as fallback")
    run.add_argument("--concurrency", type=int)
    run.add_argument("--max-rounds", type=int)
    run.add_argument("--max-idle-rounds", type=int)
    run.add_argument("--max-total-workers", type=int)
    run.add_argument("--max-wall-minutes", type=int)
    run.add_argument("--expected-exit-code", type=int, default=0)
    run.add_argument("--verify-timeout", type=float, default=900)
    resume = commands.add_parser("resume", help="resume from last durable checkpoint")
    resume.add_argument("run_id")
    status = commands.add_parser("status", help="show latest or selected run status")
    status.add_argument("run_id", nargs="?")
    inspect = commands.add_parser("inspect", help="print stored contract and state")
    inspect.add_argument("run_id")
    workers = commands.add_parser("workers", help="show stored worker outcomes")
    workers.add_argument("run_id")
    cancel = commands.add_parser("cancel", help="request cancellation")
    cancel.add_argument("run_id")
    commands.add_parser("doctor", help="check local capabilities without modifying them")
    install = commands.add_parser("install-skill", help="install user-wide Hound skill")
    install.add_argument("--force", action="store_true")
    install.add_argument("--project", action="store_true",
                         help="install only for the current project")
    uninstall = commands.add_parser(
        "uninstall-skill", help="remove only a Hound-installed user-wide skill")
    uninstall.add_argument("--project", action="store_true",
                           help="remove only from the current project")
    return root


def init(root: Path) -> int:
    target = root / ".hound" / "config.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        print(f"exists: {target}")
        return 0
    target.write_text(DEFAULT_CONFIG, encoding="utf-8", newline="\n")
    print(f"created: {target}")
    return 0


def adapter_from(config: dict) -> CodexAdapter:
    test_prefix = os.environ.get("HOUND_AGENT_PREFIX_JSON")
    prefix = json.loads(test_prefix) if test_prefix else None
    return CodexAdapter(config.get("codex", {}).get("executable", "codex"), prefix)


def build_contract(args: argparse.Namespace, root: Path, config: dict) -> RunContract:
    verify = [split_command(value) for value in args.verify]
    proposals: list[list[str]] = []
    for raw in args.verify_argv:
        value = json.loads(raw)
        if not isinstance(value, list) or not value or not all(isinstance(part, str) and part for part in value):
            raise ValueError("--verify-argv must be a non-empty JSON string array")
        verify.append(value)
    if not verify:
        suggestion = suggest_verification(root)
        if suggestion:
            verify.append(suggestion)
        else:
            proposals = propose_verification(root)
            if not proposals:
                raise ValueError("at least one machine verification command is required; use --verify-argv")
    budget = make_budget(config, max_rounds=args.max_rounds, max_idle_rounds=args.max_idle_rounds,
                         max_total_workers=args.max_total_workers, max_wall_minutes=args.max_wall_minutes)
    contract = RunContract(
        args.objective, str(root), verify, args.required_file, args.allowed_path,
        args.forbidden_path, args.expected_exit_code, args.verify_timeout, budget,
        make_pack(config, args.pack, args.concurrency, args.pack_backend),
        verification_proposals=proposals)
    validate_contract(contract)
    return contract


def suggest_verification(root: Path) -> list[str] | None:
    src = root / "src"
    if src.is_dir() and any(src.rglob("*.py")):
        return [sys.executable, "-m", "compileall", "-q", "src"]
    scripts = sorted(path.name for path in root.glob("*.py") if path.is_file())
    if scripts:
        return [sys.executable, "-m", "py_compile", *scripts]
    return None


def propose_verification(root: Path) -> list[list[str]]:
    proposals = (("pyproject.toml", [sys.executable, "-m", "pytest", "-q"]),
                 ("package.json", ["npm", "test"]),
                 ("Cargo.toml", ["cargo", "test"]),
                 ("go.mod", ["go", "test", "./..."]))
    return [command for marker, command in proposals if (root / marker).is_file()]


def proposal_message(commands: list[list[str]]) -> str:
    values = [json.dumps(command, ensure_ascii=False, separators=(",", ":"))
              for command in commands]
    options = " ".join(f"--verify-argv {value}" for value in values)
    exact_argv = json.dumps(
        [["--verify-argv", value] for value in values], ensure_ascii=False,
        separators=(",", ":"))
    return ("verification proposal requires explicit approval; no worker or verification command "
            "was run. Rerun the same objective and pass each JSON array as one argument: " +
            options + "; exact argv additions: " + exact_argv)


async def run_new(args: argparse.Namespace, root: Path) -> int:
    init(root)
    config = load_config(root)
    contract = build_contract(args, root, config)
    engine = Engine(root, adapter_from(config), config)
    if contract.verification_proposals:
        state = engine.new_run(contract)
        print(f"run_id: {state.run_id}", flush=True)
        state = engine._finish(
            state, RunStatus.BLOCKED, proposal_message(contract.verification_proposals))
        print(f"status: {state.status.value}\n{state.message}")
        return EXIT[state.status]
    await preflight(engine, contract)
    state = engine.new_run(contract)
    print(f"run_id: {state.run_id}", flush=True)
    state = await drive(engine, state, contract)
    print(f"status: {state.status.value}\n{state.message}")
    return EXIT.get(state.status, 5)


async def resume_run(run_id: str, root: Path) -> int:
    config, store = load_config(root), RunStore(root)
    state, contract = store.load_state(run_id), store.load_contract(run_id)
    if state.status == RunStatus.DONE:
        print(f"status: done\n{state.message}")
        return 0
    if state.status == RunStatus.BLOCKED and contract.verification_proposals:
        print(f"status: blocked\n{state.message}")
        return EXIT[RunStatus.BLOCKED]
    engine = Engine(root, adapter_from(config), config)
    await preflight(engine, contract)
    state = await drive(engine, state, contract, resume=True)
    print(f"status: {state.status.value}\n{state.message}")
    return EXIT.get(state.status, 5)


async def drive(engine: Engine, state: RunState, contract: RunContract,
                resume: bool = False) -> RunState:
    try:
        return await engine.run(state, contract, resume)
    except (HoundLockError, HoundResumeRetryError):
        raise
    except HoundEnvironmentError as error:
        if state.status is RunStatus.CREATED:
            raise
        return engine._finish(state, RunStatus.BLOCKED, redact(str(error))[:2000])
    except Exception as error:
        return engine._finish(
            state, RunStatus.FAILED_INTERNAL,
            redact(f"internal error: {type(error).__name__}: {error}")[:2000])


async def preflight(engine: Engine, contract: RunContract) -> None:
    if contract.verification_proposals:
        raise HoundEnvironmentError(proposal_message(contract.verification_proposals))
    if not engine.adapter.available():
        raise HoundEnvironmentError("Codex executable not found")
    for command in contract.verify:
        if not resolve_executable(command[0], engine.workspace):
            raise HoundEnvironmentError(f"verification executable not found: {command[0]}")
    if (contract.pack.mode != "off" and contract.pack.backend == "agentflow" and
            not engine.agentflow.available()):
        raise HoundEnvironmentError("AgentFlow Pack requested but executable was not found")
    with tempfile.TemporaryDirectory(prefix="hound-preflight-") as temporary:
        await engine.adapter.negotiate(engine.workspace, Path(temporary))


def show_status(run_id: str | None, root: Path) -> int:
    store = RunStore(root)
    run_id = run_id or next(iter(store.list_runs()), None)
    if not run_id:
        print("no runs")
        return 0
    state = store.load_state(run_id)
    print(json.dumps(dump(state), ensure_ascii=False, indent=2))
    return 0


def show_inspect(run_id: str, root: Path) -> int:
    store = RunStore(root)
    print(json.dumps({"state": dump(store.load_state(run_id)),
                      "contract": dump(store.load_contract(run_id))}, ensure_ascii=False, indent=2))
    return 0


def show_workers(run_id: str, root: Path) -> int:
    store = RunStore(root)
    state = store.load_state(run_id)
    workers = []
    for round_ in state.rounds:
        if not isinstance(round_, dict) or not isinstance(round_.get("number"), int):
            raise ValueError("invalid stored round record")
        number = round_["number"]
        workers.append({"role": "writer", "round": number, **round_.get("writer", {})})
        workers.extend({"role": "scout", "round": number, **scout}
                       for scout in round_.get("scouts", []) if isinstance(scout, dict))
        if round_.get("verifier"):
            workers.append({"role": "verifier", "round": number, **round_["verifier"]})
    for judge in sorted(store.run_dir(run_id).glob("rounds/*/judge/result.json")):
        workers.append({"role": "judge", "round": int(judge.parents[1].name),
                        **json.loads(judge.read_text(encoding="utf-8"))})
    print(json.dumps(workers, ensure_ascii=False, indent=2))
    return 0


def skill_path(root: Path, project: bool = False) -> Path:
    base = root if project else Path.home()
    return base / ".agents" / "skills" / "hounds" / "SKILL.md"


def doctor(root: Path) -> int:
    config = load_config(root)
    codex_config, agentflow_config = config.get("codex", {}), config.get("agentflow", {})
    codex = resolve_executable(codex_config.get("executable", "codex"), root)
    agentflow = resolve_executable(agentflow_config.get("executable", "agentflow"), root)
    write_error = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".hound-doctor-", dir=root) as handle:
            handle.write(b"hound")
            handle.flush()
            os.fsync(handle.fileno())
        workspace_writable = True
    except OSError as error:
        workspace_writable, write_error = False, str(error)
    lock = RunStore(root).workspace_lock_status()
    environment = codex_environment()
    trusted_python = trusted_python_executable()
    worker_python = resolve_executable("python", root, environment)
    user_skill, project_skill = skill_path(root), skill_path(root, project=True)
    details = {"python": platform.python_version(), "python_ok": sys.version_info >= (3, 11),
               "trusted_python": trusted_python, "worker_python": worker_python,
               "worker_python_pinned": bool(worker_python and os.path.normcase(worker_python) ==
                                             os.path.normcase(trusted_python)),
               "codex_home_isolated": environment.get("HOUND_CODEX_HOME_ISOLATED") == "1",
               "codex": codex, "workspace_writable": workspace_writable,
               "workspace_lock": lock, "workspace_lock_available": lock["available"],
               "git_repository": False,
               "skill_installed": user_skill.exists() or project_skill.exists(),
               "skill_user_installed": user_skill.exists(),
               "skill_project_installed": project_skill.exists(),
               "agentflow": agentflow, "agentflow_enabled": agentflow_config.get("enabled", True),
               "agentflow_pack_ready": False}
    if write_error:
        details["workspace_write_error"] = write_error
    with tempfile.TemporaryDirectory(prefix="hound-doctor-") as temporary:
        probe_root = Path(temporary)

        def probe(argv: list[str], name: str):
            try:
                return asyncio.run(run_process(
                    argv, root, probe_root / name, 15, environment))
            except (OSError, RuntimeError) as error:
                details[f"{name}_error"] = str(error)
                return None

        git = resolve_executable("git", root)
        if git:
            result = probe([git, "rev-parse", "--is-inside-work-tree"], "git")
            details["git_repository"] = bool(
                result and result.exit_code == 0 and result.stdout.strip() == "true")
        if codex:
            result = probe([codex, "--version"], "codex-version")
            if result:
                details["codex_version"] = result.stdout.strip() or result.stderr.strip()
            result = probe([codex, "exec", "--help"], "codex-help")
            help_text = result.stdout + result.stderr if result else ""
            details.update({"codex_exec": bool(result and result.exit_code == 0),
                            "json_output": "--json" in help_text,
                            "output_schema_option": "--output-schema" in help_text,
                            "output_last_message_option": "--output-last-message" in help_text,
                            "sandbox_option": "--sandbox" in help_text,
                            "ignore_user_config_option": "--ignore-user-config" in help_text,
                            "ephemeral_option": "--ephemeral" in help_text,
                            "skip_git_option": "--skip-git-repo-check" in help_text})
            root_result = probe([codex, "--help"], "codex-root-help")
            root_text = root_result.stdout + root_result.stderr if root_result else ""
            details["approval_policy_option"] = "--ask-for-approval" in root_text
        if agentflow:
            result = probe([agentflow, "run", "--help"], "agentflow-help")
            help_text = result.stdout + result.stderr if result else ""
            details["agentflow_pack_ready"] = bool(
                result and result.exit_code == 0 and "--runs-dir" in help_text)
    print(json.dumps(details, indent=2))
    required = ("python_ok", "worker_python_pinned", "codex_home_isolated", "workspace_writable",
                "workspace_lock_available", "codex_exec", "sandbox_option",
                "ignore_user_config_option", "approval_policy_option")
    return 0 if codex and all(details.get(name) for name in required) else 4


def install_skill(root: Path, force: bool, project: bool = False) -> int:
    target = skill_path(root, project)
    if target.exists():
        if target.read_text(encoding="utf-8") == SKILL:
            print(f"already installed: {target}")
            return 0
        if not force:
            print(f"refusing to overwrite: {target}; pass --force")
            return 4
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(SKILL, encoding="utf-8", newline="\n")
    print(f"installed: {target}")
    return 0


def uninstall_skill(root: Path, project: bool = False) -> int:
    target = skill_path(root, project)
    if not target.exists():
        print("not installed")
        return 0
    if target.read_text(encoding="utf-8") != SKILL:
        print(f"refusing to remove modified/unowned file: {target}")
        return 4
    target.unlink()
    try:
        target.parent.rmdir()
    except OSError:
        pass
    print(f"removed: {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        try:
            args = parser().parse_args(argv)
        except SystemExit as error:
            return 0 if error.code in (None, 0) else 4
        root = Path.cwd().resolve()
        try:
            if args.command == "init": return init(root)
            if args.command == "run": return asyncio.run(run_new(args, root))
            if args.command == "resume": return asyncio.run(resume_run(args.run_id, root))
            if args.command == "status": return show_status(args.run_id, root)
            if args.command == "inspect": return show_inspect(args.run_id, root)
            if args.command == "workers": return show_workers(args.run_id, root)
            if args.command == "cancel":
                path = RunStore(root).run_dir(args.run_id) / "cancel.requested"
                path.write_text("requested\n", encoding="ascii")
                print(f"cancel requested: {args.run_id}")
                return 0
            if args.command == "doctor": return doctor(root)
            if args.command == "install-skill":
                return install_skill(root, args.force, args.project)
            if args.command == "uninstall-skill": return uninstall_skill(root, args.project)
        except (ValueError, OSError, json.JSONDecodeError, HoundEnvironmentError) as error:
            print(f"hound: {error}", file=sys.stderr)
            return 4
        except KeyboardInterrupt:
            print("hound: interrupted", file=sys.stderr)
            return 130
        except Exception as error:
            print(f"hound internal error: {type(error).__name__}: {error}", file=sys.stderr)
            return 5
        return 5
    finally:
        cleanup_codex_homes()
