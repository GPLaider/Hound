from __future__ import annotations

import asyncio
import json
import os
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import hounds.cli as cli_module
from hounds.adapter import CodexAdapter
from hounds.cli import SKILL, build_contract, doctor, drive, main, parser, show_workers
from hounds.config import DEFAULT_CONFIG, make_pack, split_command, validate_config
from hounds.engine import Engine
from hounds.models import (Budget, HoundResumeRetryError, PackPolicy, RunContract, RunStatus,
                           WorkerSpec, validate_contract)
from hounds.store import RunStore


def test_native_command_split():
    assert split_command('python -m pytest -q') == ["python", "-m", "pytest", "-q"]
    assert split_command('python "two words.py"') == ["python", "two words.py"]
    for command in ("", " \t\r\n"):
        try:
            split_command(command)
        except ValueError as error:
            assert "non-empty" in str(error)
        else:
            raise AssertionError("empty verification command was accepted")


def test_installed_skill_source_stays_in_sync():
    assert (Path(__file__).parents[1] / "skills/hounds/SKILL.md").read_text(encoding="utf-8") == SKILL


def test_skill_install_uninstall_scopes_and_ownership(tmp_path: Path, monkeypatch, capsys):
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    home.mkdir(); workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(workspace)
    user_skill = home / ".agents" / "skills" / "hounds" / "SKILL.md"
    project_skill = workspace / ".agents" / "skills" / "hounds" / "SKILL.md"

    assert main(["install-skill"]) == 0
    assert user_skill.read_text(encoding="utf-8") == SKILL and not project_skill.exists()
    assert main(["install-skill"]) == 0
    assert "already installed:" in capsys.readouterr().out

    assert main(["install-skill", "--project"]) == 0
    project_skill.write_text("custom\n", encoding="utf-8")
    assert main(["uninstall-skill", "--project"]) == 4
    assert main(["install-skill", "--project"]) == 4
    assert main(["install-skill", "--project", "--force"]) == 0
    assert main(["uninstall-skill", "--project"]) == 0 and not project_skill.exists()
    assert main(["uninstall-skill"]) == 0 and not user_skill.exists()


def test_python_src_gets_safe_default_verification(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    args = parser().parse_args(["run", "check it", "--pack", "off",
                                "--pack-backend", "agentflow"])
    contract = build_contract(args, tmp_path, tomllib.loads(DEFAULT_CONFIG))
    assert contract.verify == [[sys.executable, "-m", "compileall", "-q", "src"]]
    assert contract.verification_proposals == []
    assert contract.pack.backend == "agentflow"


def test_expected_exit_code_and_safe_top_level_python_suggestion(tmp_path: Path):
    (tmp_path / "check.py").write_text("value = 1\n", encoding="utf-8")
    args = parser().parse_args(["run", "check", "--expected-exit-code", "7"])
    contract = build_contract(args, tmp_path, tomllib.loads(DEFAULT_CONFIG))
    assert contract.expected_exit_code == 7
    assert contract.verify == [[sys.executable, "-m", "py_compile", "check.py"]]
    assert contract.verification_proposals == []


def test_marker_verification_proposals_are_durable_blocked_runs_without_workers(
        tmp_path: Path, monkeypatch, capsys):
    cases = (
        ("pyproject.toml", [sys.executable, "-m", "pytest", "-q"]),
        ("package.json", ["npm", "test"]),
        ("Cargo.toml", ["cargo", "test"]),
        ("go.mod", ["go", "test", "./..."]),
    )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("proposal-only run started a worker or preflight")

    monkeypatch.setattr(cli_module, "preflight", forbidden)
    monkeypatch.setattr(Engine, "run", forbidden)
    for index, (marker, expected) in enumerate(cases):
        root = tmp_path / str(index)
        root.mkdir()
        (root / marker).write_text("{}", encoding="utf-8")
        monkeypatch.chdir(root)

        assert main(["run", "review verification first"]) == 3
        store = RunStore(root)
        run_id, = store.list_runs()
        state, contract = store.load_state(run_id), store.load_contract(run_id)
        expected_json = json.dumps(expected, ensure_ascii=False, separators=(",", ":"))
        output = capsys.readouterr().out

        assert state.status is RunStatus.BLOCKED
        assert state.round == state.total_workers == 0
        assert contract.verify == []
        assert contract.verification_proposals == [expected]
        assert f"--verify-argv {expected_json}" in state.message
        exact_argv = json.dumps(
            [["--verify-argv", expected_json]], ensure_ascii=False, separators=(",", ":"))
        assert f"exact argv additions: {exact_argv}" in state.message
        assert "status: blocked" in output and run_id in output
        assert not (store.run_dir(run_id) / "rounds").exists()

        assert main(["resume", run_id]) == 3
        assert "status: blocked" in capsys.readouterr().out

        approved_args = parser().parse_args([
            "run", "reviewed verification", "--verify-argv", expected_json])
        approved = build_contract(approved_args, root, tomllib.loads(DEFAULT_CONFIG))
        assert approved.verify == [expected]
        assert approved.verification_proposals == []


def test_contract_requires_verification_and_valid_limits(tmp_path: Path):
    args = parser().parse_args(["run", "check it"])
    try:
        build_contract(args, tmp_path, tomllib.loads(DEFAULT_CONFIG))
    except ValueError as error:
        assert "machine verification" in str(error)
    else:
        raise AssertionError("unverified contract was accepted")
    args = parser().parse_args([
        "run", "check it", "--verify-argv", '["python","-V"]', "--concurrency", "0"])
    try:
        build_contract(args, tmp_path, tomllib.loads(DEFAULT_CONFIG))
    except ValueError as error:
        assert "concurrency" in str(error)
    else:
        raise AssertionError("invalid concurrency was accepted")


def test_config_rejects_non_integer_budget_values():
    for value in (True, 1.5):
        config = tomllib.loads(DEFAULT_CONFIG)
        config["run"]["max_rounds"] = value
        try:
            validate_config(config)
        except ValueError as error:
            assert "positive integer" in str(error)
        else:
            raise AssertionError(f"invalid budget value was accepted: {value!r}")


def test_pack_defaults_and_codex_model_validation():
    config = tomllib.loads(DEFAULT_CONFIG)
    policy = make_pack(config)
    assert policy.minimum_progress_events == 2
    assert policy.judge_interval_seconds == 40
    for name in ("writer_model", "scout_model", "judge_model", "verifier_model"):
        invalid = tomllib.loads(DEFAULT_CONFIG)
        invalid["codex"][name] = False
        try:
            validate_config(invalid)
        except ValueError as error:
            assert f"codex.{name} must be a string" in str(error)
        else:
            raise AssertionError(f"non-string codex.{name} was accepted")
        for value in ("   ", " model", "model "):
            invalid = tomllib.loads(DEFAULT_CONFIG)
            invalid["codex"][name] = value
            try:
                validate_config(invalid)
            except ValueError as error:
                assert f"codex.{name}" in str(error)
            else:
                raise AssertionError(f"untrimmed codex.{name} was accepted")


def test_cli_usage_errors_map_to_environment_exit_code(capsys):
    assert main([]) == 4
    assert main(["run", "task", "--max-rounds", "not-an-int"]) == 4
    assert main(["--help"]) == 0
    assert "usage:" in capsys.readouterr().out


def test_doctor_honors_config_and_probes_workspace(tmp_path: Path, monkeypatch, capsys):
    (tmp_path / ".hound").mkdir()
    (tmp_path / ".hound" / "config.toml").write_text(
        '[codex]\nexecutable = "custom-codex"\n'
        '[agentflow]\nenabled = false\nexecutable = "custom-agentflow"\n', encoding="utf-8")
    requested: list[str] = []
    home = tmp_path / "home"
    user_skill = home / ".agents" / "skills" / "hounds" / "SKILL.md"
    user_skill.parent.mkdir(parents=True)
    user_skill.write_text(SKILL, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    def resolve(name: str, _root: Path, *_args) -> str:
        requested.append(name)
        if name == "python":
            return cli_module.trusted_python_executable()
        return str(tmp_path / f"{name}.exe")

    async def run_process(argv, *_args, **_kwargs):
        if argv[1:] == ["rev-parse", "--is-inside-work-tree"]:
            stdout = "true\n"
        elif argv[1:] == ["exec", "--help"]:
            stdout = ("--json --sandbox --ephemeral --skip-git-repo-check "
                      "--ignore-user-config\n")
        elif argv[1:] == ["--help"]:
            stdout = "--ask-for-approval\n"
        elif argv[1:] == ["run", "--help"]:
            stdout = "--runs-dir\n"
        else:
            stdout = "codex 1.0\n"
        return SimpleNamespace(exit_code=0, stdout=stdout, stderr="")

    monkeypatch.setattr(cli_module, "resolve_executable", resolve)
    monkeypatch.setattr(cli_module, "run_process", run_process)
    assert doctor(tmp_path) == 0
    details = json.loads(capsys.readouterr().out)
    assert {"custom-codex", "custom-agentflow", "git"} <= set(requested)
    assert details["workspace_writable"] is True
    assert details["workspace_lock_available"] is True
    assert details["workspace_lock"]["held"] is False
    assert details["agentflow_enabled"] is False
    assert details["skill_installed"] is True
    assert details["skill_user_installed"] is True
    assert details["skill_project_installed"] is False
    assert not list(tmp_path.glob(".hound-doctor-*"))


def test_contract_rejects_cross_platform_workspace_escapes(tmp_path: Path):
    for path in ("../outside", "/etc/passwd", r"\Windows\win.ini", "D:relative"):
        contract = RunContract("safe paths", str(tmp_path), [[sys.executable, "-V"]],
                               required_files=[path])
        try:
            validate_contract(contract)
        except ValueError as error:
            assert "stay within" in str(error)
        else:
            raise AssertionError(f"workspace escape was accepted: {path}")


def test_production_codex_reads_prompt_from_stdin(tmp_path: Path):
    spec = WorkerSpec("writer-1", "writer", "work", "workspace-write", 5)
    argv = CodexAdapter().argv(spec, "sensitive prompt", tmp_path / "final.txt")
    assert argv[-1] == "-" and "sensitive prompt" not in argv
    assert "--ephemeral" in argv and "--skip-git-repo-check" in argv
    start = argv.index("--ask-for-approval")
    assert argv[start:start + 2] == ["--ask-for-approval", "never"]
    if os.name == "nt":
        assert 'windows.sandbox="elevated"' in argv
    assert "--approve-for-me" not in argv and "--ignore-user-config" in argv
    assert argv[argv.index("--output-schema") + 1].endswith("output-schema.json")


def test_started_run_commits_environment_failure_as_terminal(tmp_path: Path):
    contract = RunContract("x" * 400, str(tmp_path), [[sys.executable, "-V"]],
                           pack=PackPolicy(mode="off"))
    engine = Engine(tmp_path, CodexAdapter(prefix=[sys.executable, "unused"]),
                    {"context": {"maximum_prompt_bytes": 300}})
    state = engine.new_run(contract)
    result = asyncio.run(drive(engine, state, contract))
    assert result.status is RunStatus.BLOCKED
    assert engine.store.load_state(state.run_id).status is RunStatus.BLOCKED


def test_codex_argv_drops_unsupported_optional_flags(tmp_path: Path):
    adapter = CodexAdapter()
    adapter.capabilities = {"json": False, "ephemeral": False, "skip_git": False,
                            "color": False, "sandbox": True,
                            "output_last_message": False, "output_schema": False,
                            "model": False}
    spec = WorkerSpec("writer-1", "writer", "work", "workspace-write", 5, "ignored")
    argv = adapter.argv(spec, "prompt", tmp_path / "final.txt")
    assert argv[-1] == "-" and "--sandbox" in argv
    assert not {"--json", "--ephemeral", "--skip-git-repo-check",
                "--output-last-message", "--output-schema", "--model",
                "--ask-for-approval", "--ignore-user-config"} & set(argv)


def test_cli_fake_verification_loop(tmp_path: Path, monkeypatch):
    fake = Path(__file__).with_name("fake_agent.py")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOUND_AGENT_PREFIX_JSON", json.dumps([sys.executable, str(fake)]))
    monkeypatch.setenv("HOUND_FAKE_SCENARIO", "verification-loop")
    (tmp_path / "test_answer.py").write_text("from answer import answer\nassert answer() == 42\n", encoding="utf-8")
    verify = json.dumps([sys.executable, "test_answer.py"])
    assert main(["run", "make answer pass", "--pack", "off", "--max-rounds", "3",
                 "--verify-argv", verify]) == 0
    state = RunStore(tmp_path).load_state(RunStore(tmp_path).list_runs()[0])
    assert state.status.value == "done" and state.round == 2


def test_cli_resume_recovers_partial_worker(tmp_path: Path, monkeypatch):
    fake = Path(__file__).with_name("fake_agent.py")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOUND_AGENT_PREFIX_JSON", json.dumps([sys.executable, str(fake)]))
    monkeypatch.setenv("HOUND_FAKE_SCENARIO", "verification-loop")
    (tmp_path / "test_answer.py").write_text("from answer import answer\nassert answer() == 42\n", encoding="utf-8")
    contract = RunContract("make answer pass", str(tmp_path), [[sys.executable, "test_answer.py"]],
                           budget=Budget(max_rounds=3), pack=PackPolicy(mode="off"))
    engine = Engine(tmp_path, CodexAdapter(prefix=[sys.executable, str(fake)]), {})
    state = engine.new_run(contract)
    state.status = RunStatus.WORKING
    state.round = 1
    engine.store.save_state(state)
    partial = engine.store.run_dir(state.run_id) / "rounds" / "001" / "writer"
    partial.mkdir(parents=True)
    (partial / "prompt.txt").write_text("unfinished", encoding="utf-8")
    (partial / "stdout.log").write_text("partial output", encoding="utf-8")

    assert main(["resume", state.run_id]) == 0
    assert (partial / "stdout.log").read_text(encoding="utf-8") == "partial output"
    assert json.loads((partial / "result.json").read_text(encoding="utf-8"))["status"] == "interrupted"
    assert engine.store.load_state(state.run_id).status is RunStatus.DONE


def test_unexpected_engine_error_is_durable_and_exits_five(tmp_path: Path, monkeypatch):
    fake = Path(__file__).with_name("fake_agent.py")
    secret = "internal-secret-value"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOUND_AGENT_PREFIX_JSON", json.dumps([sys.executable, str(fake)]))
    monkeypatch.setenv("SERVICE_TOKEN", secret)

    async def explode(*_args, **_kwargs):
        raise RuntimeError("unexpected bug " + secret)

    monkeypatch.setattr(Engine, "run", explode)
    verify = json.dumps([sys.executable, "-V"])
    assert main(["run", "record internal failure", "--verify-argv", verify]) == 5
    store = RunStore(tmp_path)
    state = store.load_state(store.list_runs()[0])
    assert state.status is RunStatus.FAILED_INTERNAL
    assert "RuntimeError: unexpected bug" in state.message
    run_dir = store.run_dir(state.run_id)
    assert secret not in (json.dumps(state.message) + (run_dir / "summary.md").read_text() +
                          (run_dir / "events.jsonl").read_text())


def test_cli_terminal_exit_codes_two_three_and_130(tmp_path: Path, monkeypatch):
    fake = Path(__file__).with_name("fake_agent.py")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOUND_AGENT_PREFIX_JSON", json.dumps([sys.executable, str(fake)]))
    verify_failure = json.dumps([sys.executable, "-c", "raise SystemExit(1)"])
    monkeypatch.setenv("HOUND_FAKE_SCENARIO", "invalid-json")
    assert main(["run", "exhaust", "--pack", "off", "--max-rounds", "1",
                 "--verify-argv", verify_failure]) == 2
    monkeypatch.setenv("HOUND_FAKE_SCENARIO", "blocked")
    assert main(["run", "blocked", "--pack", "off",
                 "--verify-argv", json.dumps([sys.executable, "-V"])]) == 3

    engine = Engine(tmp_path, CodexAdapter(prefix=[sys.executable, str(fake)]), {})
    contract = RunContract("cancel", str(tmp_path), [[sys.executable, "-V"]],
                           pack=PackPolicy(mode="off"))
    state = engine.new_run(contract)
    (engine.store.run_dir(state.run_id) / "cancel.requested").touch()
    assert main(["resume", state.run_id]) == 130


def test_resume_preflight_failure_does_not_mutate_state(tmp_path: Path, monkeypatch):
    engine = Engine(tmp_path, CodexAdapter(prefix=[sys.executable, "unused"]), {})
    contract = RunContract("resume only when ready", str(tmp_path), [[sys.executable, "-V"]])
    state = engine.new_run(contract); state.status = RunStatus.WORKING; state.round = 2
    engine.store.save_state(state)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(CodexAdapter, "available", lambda _self: False)
    assert main(["resume", state.run_id]) == 4
    loaded = engine.store.load_state(state.run_id)
    assert loaded.status is RunStatus.WORKING and loaded.round == 2


def test_retryable_resume_error_exits_four_without_blocking_state(
        tmp_path: Path, monkeypatch, capsys):
    engine = Engine(tmp_path, CodexAdapter(prefix=[sys.executable, "unused"]), {})
    contract = RunContract("retry recovery", str(tmp_path), [[sys.executable, "-V"]])
    state = engine.new_run(contract); state.status = RunStatus.WORKING; state.round = 2
    engine.store.save_state(state)
    monkeypatch.chdir(tmp_path)

    async def ready(*_args, **_kwargs):
        return None

    async def retry(*_args, **_kwargs):
        raise HoundResumeRetryError("unfinished worker still alive; retry resume later")

    monkeypatch.setattr(cli_module, "preflight", ready)
    monkeypatch.setattr(Engine, "run", retry)
    assert main(["resume", state.run_id]) == 4
    loaded = engine.store.load_state(state.run_id)
    assert loaded.status is RunStatus.WORKING and loaded.round == 2
    assert not (engine.store.run_dir(state.run_id) / "summary.md").exists()
    assert "retry resume later" in capsys.readouterr().err


def test_missing_verification_executable_is_exit_four_before_run_creation(
        tmp_path: Path, monkeypatch):
    fake = Path(__file__).with_name("fake_agent.py")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOUND_AGENT_PREFIX_JSON", json.dumps([sys.executable, str(fake)]))
    verify = json.dumps(["definitely-missing-hound-verifier", "--check"])
    assert main(["run", "validate environment", "--verify-argv", verify]) == 4
    assert RunStore(tmp_path).list_runs() == []


def test_concurrent_resume_lock_is_exit_four_and_does_not_mutate_state(
        tmp_path: Path, monkeypatch):
    fake = Path(__file__).with_name("fake_agent.py")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOUND_AGENT_PREFIX_JSON", json.dumps([sys.executable, str(fake)]))
    engine = Engine(tmp_path, CodexAdapter(prefix=[sys.executable, str(fake)]), {})
    contract = RunContract("resume exactly once", str(tmp_path), [[sys.executable, "-V"]])
    state = engine.new_run(contract); state.status = RunStatus.WORKING; state.round = 1
    engine.store.save_state(state)
    with engine.store.workspace_lock():
        assert main(["resume", state.run_id]) == 4
    loaded = engine.store.load_state(state.run_id)
    assert loaded.status is RunStatus.WORKING and loaded.round == 1
    assert not (engine.store.run_dir(state.run_id) / "summary.md").exists()


def test_workers_includes_stored_judge(tmp_path: Path, capsys):
    engine = Engine(tmp_path, CodexAdapter(prefix=[sys.executable, "unused"]), {})
    contract = RunContract("inspect workers", str(tmp_path), [[sys.executable, "-V"]])
    state = engine.new_run(contract)
    state.rounds.append({"number": 1, "writer": {"status": "continue"}, "scouts": []})
    engine.store.save_state(state)
    judge = engine.store.run_dir(state.run_id) / "rounds" / "001" / "judge" / "result.json"
    judge.parent.mkdir(parents=True)
    judge.write_text('{"worker_id":"judge-1","status":"completed"}', encoding="utf-8")
    assert show_workers(state.run_id, tmp_path) == 0
    assert any(item.get("role") == "judge" for item in json.loads(capsys.readouterr().out))
