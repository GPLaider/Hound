from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import hounds.process as process_module
from hounds.process import (ManagedProcess, WindowsProcess, cleanup_codex_homes,
                            codex_environment, guarded_windows_target, guarded_windows_tree,
                            run_process, redact, redact_artifacts, resolve_executable,
                            trusted_python_executable, windows_process_table,
                            windows_taskkill_path, worker_environment)
from hounds.store import pid_alive


def test_timeout_preserves_partial_output(tmp_path: Path):
    async def check():
        owner_pid = os.getpid()
        code = "import time;print('partial',flush=True);time.sleep(30)"
        result = await run_process([sys.executable, "-c", code], tmp_path, tmp_path / "artifacts", 0.1)
        assert result.timed_out and not result.cancelled and "partial" in result.stdout
        assert "partial" in (tmp_path / "artifacts" / "stdout.log").read_text()
        termination = __import__("json").loads(
            (tmp_path / "artifacts" / "termination.json").read_text(encoding="utf-8"))
        assert termination["reason"] == "cancelled_for_policy"
        assert termination["hound_pid"] == owner_pid
        assert termination["target_ppid"] == owner_pid
        assert termination["target_pid"] != owner_pid
        if os.name == "nt":
            launch = __import__("json").loads(
                (tmp_path / "artifacts" / "launch.json").read_text(encoding="utf-8"))
            assert launch["windows_creation_flags"] == subprocess.CREATE_NO_WINDOW
            assert launch["worker_creation_time"] == termination["target_creation_time"] > 0
            assert termination["guarded_tree"][0]["pid"] == termination["target_pid"]
            assert owner_pid in windows_process_table()
            assert termination["target_pid"] not in windows_process_table()
    asyncio.run(check())


def test_short_named_secret_is_redacted():
    assert redact("value=abcde", {"API_KEY": "abcde"}) == "value=[REDACTED]"


def test_artifact_redaction_honors_cancel_without_replacing_source(tmp_path: Path):
    secret = "redaction-cancel-secret"
    artifact = tmp_path / "final.txt"
    artifact.write_text(secret, encoding="utf-8")
    cancel = tmp_path / "cancel.requested"
    cancel.touch()
    assert redact_artifacts(tmp_path, (secret,), cancel_path=cancel) is False
    assert artifact.read_text(encoding="utf-8") == secret
    cancel.unlink()
    assert redact_artifacts(tmp_path, (secret,)) is True
    assert artifact.read_text(encoding="utf-8") == "[REDACTED]"


def test_output_line_over_stream_limit_is_preserved(tmp_path: Path):
    async def check():
        result = await run_process([sys.executable, "-c", "print('x'*100000)"],
                                   tmp_path, tmp_path / "artifacts", 5)
        assert result.exit_code == 0 and len(result.stdout.strip()) == 100000
        assert len((tmp_path / "artifacts" / "trace.jsonl").read_text().strip()) == 100000
    asyncio.run(check())


def test_process_tree_cancellation(tmp_path: Path):
    async def check():
        fake = Path(__file__).with_name("fake_agent.py")
        env = os.environ.copy(); env["HOUND_FAKE_SCENARIO"] = "child-process"
        managed = ManagedProcess([sys.executable, str(fake), "writer", "x"], tmp_path,
                                 tmp_path / "artifacts", env)
        await managed.start()
        child_pid = None
        try:
            for _ in range(100):
                try:
                    child_pid = int((tmp_path / "child.pid").read_text())
                    break
                except (FileNotFoundError, ValueError):
                    await asyncio.sleep(0.02)
            assert child_pid is not None
        finally:
            await managed.terminate("test_cancel", 0.1)
        assert managed.process.returncode is not None
        assert not pid_alive(child_pid)
        assert pid_alive(os.getpid())
    asyncio.run(check())


def test_windows_guard_rejects_hound_itself():
    if os.name == "nt":
        try:
            guarded_windows_target(os.getpid())
        except RuntimeError as error:
            assert "Hound or its ancestor" in str(error)
        else:
            raise AssertionError("guard accepted Hound PID")


def test_windows_guard_rejects_pid_reuse_and_protected_descendants():
    owner = WindowsProcess(100, 50, "python.exe", 1, r"C:\Python\python.exe")
    expected = WindowsProcess(200, 100, "codex.exe", 2, r"C:\Codex\codex.exe")
    reused = WindowsProcess(200, 100, "codex.exe", 3, r"C:\Codex\codex.exe")
    table = {50: WindowsProcess(50, 0, "parent.exe"), 100: owner, 200: reused}
    try:
        guarded_windows_target(200, 100, table, expected)
    except RuntimeError as error:
        assert "PID reuse" in str(error)
    else:
        raise AssertionError("guard accepted a reused PID")
    table[200] = expected
    table[201] = WindowsProcess(201, 200, "explorer.exe")
    try:
        guarded_windows_tree(200, 100, table, expected)
    except RuntimeError as error:
        assert "protected worker descendant" in str(error)
    else:
        raise AssertionError("guard accepted Explorer inside the taskkill tree")


def test_worker_environment_pins_hound_python():
    env = worker_environment({"PATH": os.pathsep.join(("quoted", "quoted")),
                              "PYTHONHOME": "broken", "__PYVENV_LAUNCHER__": "broken"})
    trusted = trusted_python_executable()
    assert env["HOUND_PYTHON_EXECUTABLE"] == trusted
    assert resolve_executable(Path(trusted).name, Path.cwd(), env) == trusted
    if os.name == "nt":
        assert resolve_executable("python", Path.cwd(), env) == trusted
    assert "PYTHONHOME" not in env and "__PYVENV_LAUNCHER__" not in env


def test_codex_environment_isolates_global_config(tmp_path: Path):
    source = tmp_path / "codex-home"
    source.mkdir()
    (source / "auth.json").write_text(
        '{"tokens":{"access_token":"test-access-token"}}', encoding="utf-8")
    (source / "config.toml").write_text('model = "global"\n', encoding="utf-8")
    skill = source / "skills" / "lyriq-porting-safety"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Gate 0\n", encoding="utf-8")
    (skill / "references" / "gate.md").write_text("preserve slot A\n", encoding="utf-8")
    system_skill = source / "skills" / ".system" / "internal"
    system_skill.mkdir(parents=True)
    (system_skill / "SKILL.md").write_text("internal\n", encoding="utf-8")
    env = codex_environment({"CODEX_HOME": str(source), "PATH": os.environ["PATH"]})
    isolated = Path(env["CODEX_HOME"])
    assert isolated != source and env["HOUND_CODEX_HOME_ISOLATED"] == "1"
    assert (isolated / "auth.json").read_bytes() == (source / "auth.json").read_bytes()
    assert (isolated / "skills" / "lyriq-porting-safety" / "SKILL.md").read_text() == "# Gate 0\n"
    assert (isolated / "skills" / "lyriq-porting-safety" / "references" / "gate.md").is_file()
    assert not (isolated / "skills" / ".system").exists()
    assert not (isolated / "config.toml").exists()
    (isolated / "config.toml").write_text('[projects.test]\ntrust_level="trusted"\n', encoding="utf-8")
    assert (source / "config.toml").read_text(encoding="utf-8") == 'model = "global"\n'
    cleanup_codex_homes()
    assert not isolated.exists()


def test_stale_codex_home_cleanup_requires_marker_and_dead_owner(tmp_path: Path,
                                                                 monkeypatch):
    stale = tmp_path / "hound-codex-99999999-stale"
    active = tmp_path / f"hound-codex-{os.getpid()}-active"
    unowned = tmp_path / "hound-codex-99999998-unowned"
    for directory in (stale, active, unowned):
        directory.mkdir()
        (directory / "auth.json").write_text("secret", encoding="ascii")
    (stale / ".hound-owned").write_text(
        "hound-codex-home-v1\n99999999\n", encoding="ascii")
    (active / ".hound-owned").write_text(
        f"hound-codex-home-v1\n{os.getpid()}\n", encoding="ascii")
    monkeypatch.setattr(process_module, "pid_alive", lambda pid: pid == os.getpid())

    process_module._cleanup_stale_codex_homes(tmp_path)

    assert not stale.exists()
    assert active.exists() and unowned.exists()


def test_windows_refuses_protected_root_before_launch(tmp_path: Path):
    if os.name != "nt":
        return

    async def check():
        protected = tmp_path / "explorer.exe"
        protected.write_bytes(b"not an executable")
        managed = ManagedProcess([str(protected)], tmp_path, tmp_path / "artifacts")
        try:
            await managed.start()
        except PermissionError as error:
            assert "protected Windows executable" in str(error)
        else:
            raise AssertionError("managed a protected Windows executable")

    asyncio.run(check())


def test_stdin_defaults_closed_and_explicit_input_works(tmp_path: Path):
    async def check():
        closed = await run_process(
            [sys.executable, "-c", "import sys; print(len(sys.stdin.read()))"],
            tmp_path, tmp_path / "closed", 5)
        supplied = await run_process(
            [sys.executable, "-c", "import sys; print(sys.stdin.read())"],
            tmp_path, tmp_path / "supplied", 5, input_text="hound")
        assert closed.stdout.strip() == "0"
        assert supplied.stdout.strip() == "hound"
    asyncio.run(check())


def test_cancelled_wait_cleans_worker(tmp_path: Path):
    async def check():
        managed = ManagedProcess([sys.executable, "-c", "import time; time.sleep(30)"],
                                 tmp_path, tmp_path / "cancelled")
        await managed.start()
        worker_pid = managed.process.pid
        waiter = asyncio.create_task(managed.wait())
        await asyncio.sleep(0.05)
        waiter.cancel()
        try:
            await waiter
        except asyncio.CancelledError:
            pass
        assert not pid_alive(worker_pid) and pid_alive(os.getpid())
    asyncio.run(check())


def test_cancelled_wait_redacts_child_written_artifact(tmp_path: Path):
    async def check():
        secret = "cancel-secret-value"
        artifact = tmp_path / "cancel-redaction"
        env = os.environ.copy()
        env.update({"SERVICE_TOKEN": secret,
                    "HOUND_RAW_ARTIFACT": str(artifact / "final.txt")})
        code = ("import os,time;from pathlib import Path;"
                "Path(os.environ['HOUND_RAW_ARTIFACT']).write_text(os.environ['SERVICE_TOKEN']);"
                "time.sleep(30)")
        managed = ManagedProcess([sys.executable, "-c", code], tmp_path, artifact, env)
        await managed.start()
        for _ in range(100):
            if (artifact / "final.txt").exists():
                break
            await asyncio.sleep(0.01)
        waiter = asyncio.create_task(managed.wait())
        await asyncio.sleep(0)
        waiter.cancel()
        try:
            await waiter
        except asyncio.CancelledError:
            pass
        assert managed.process.returncode is not None
        stored = (artifact / "final.txt").read_text()
        assert secret not in stored and stored == "[REDACTED]"
    asyncio.run(check())


def test_memory_output_is_bounded_but_log_is_complete(tmp_path: Path):
    async def check():
        size = 2_000_000
        result = await run_process(
            [sys.executable, "-c", f"import sys;sys.stdout.write('x'*{size})"],
            tmp_path, tmp_path / "bounded", 5)
        assert len(result.stdout) <= 16 * 65536
        assert (tmp_path / "bounded" / "stdout.log").stat().st_size == size
    asyncio.run(check())


def test_streaming_logs_redact_secret_split_across_chunks(tmp_path: Path):
    async def check():
        secret = "boundary-secret-value"
        env = os.environ.copy(); env["SERVICE_TOKEN"] = secret
        code = ("import os,sys,time;s=os.environ['SERVICE_TOKEN'];"
                "sys.stdout.write('out='+s[:7]);sys.stdout.flush();time.sleep(.05);"
                "sys.stdout.write(s[7:]+'\\n');sys.stdout.flush();"
                "sys.stderr.write('err='+s[:9]);sys.stderr.flush();time.sleep(.05);"
                "sys.stderr.write(s[9:]+'\\n');sys.stderr.flush()")
        artifact = tmp_path / "redacted"
        result = await run_process([sys.executable, "-c", code], tmp_path, artifact, 5, env)
        stored = "\n".join([
            result.stdout, result.stderr,
            (artifact / "stdout.log").read_text(),
            (artifact / "stderr.log").read_text(),
            (artifact / "trace.jsonl").read_text(),
            (artifact / "outcome.json").read_text(),
        ])
        assert secret not in stored
        assert stored.count("[REDACTED]") >= 5
    asyncio.run(check())


def test_windows_taskkill_uses_system_directory():
    if os.name == "nt":
        path = Path(windows_taskkill_path())
        assert path.is_absolute() and path.name.lower() == "taskkill.exe"


def test_executable_resolution_never_implicitly_uses_workspace(tmp_path: Path):
    name = "shadow.exe" if os.name == "nt" else "shadow"
    shadow = tmp_path / name
    shadow.write_bytes(b"not an executable")
    if os.name != "nt":
        shadow.chmod(0o700)
    assert resolve_executable(name, tmp_path, {"PATH": ""}) is None
    explicit = f".{os.sep}{name}"
    assert resolve_executable(explicit, tmp_path, {"PATH": ""}) == str(shadow.resolve())


def test_launch_artifact_failure_still_cleans_worker(tmp_path: Path, monkeypatch):
    async def check():
        managed = ManagedProcess([sys.executable, "-c", "import time;time.sleep(30)"],
                                 tmp_path, tmp_path / "broken-launch")
        def fail(*_args, **_kwargs):
            raise OSError("artifact unavailable")
        monkeypatch.setattr(process_module, "atomic_json", fail)
        try:
            await managed.start()
        except (OSError, RuntimeError):
            pass
        else:
            raise AssertionError("launch artifact failure was ignored")
        assert managed.process is not None
        assert not pid_alive(managed.process.pid) and pid_alive(os.getpid())
    asyncio.run(check())


def test_terminate_collects_output_after_natural_exit(tmp_path: Path):
    async def check():
        managed = ManagedProcess([sys.executable, "-c", "print('complete')"],
                                 tmp_path, tmp_path / "natural")
        await managed.start()
        await managed.process.wait()
        await managed.terminate("cleanup")
        assert (tmp_path / "natural" / "stdout.log").read_text().strip() == "complete"
    asyncio.run(check())
