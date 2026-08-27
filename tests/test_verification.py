from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import hounds.verification as verification
from hounds.process import run_process
from hounds.models import VerificationResult
from hounds.verification import (capture_git_baseline, changed_content_signature,
                                 changed_content_signature_async, changed_paths, matches_path,
                                 parse_changed_paths, path_gate, verification_signature)


def test_changed_paths_ignores_hound_artifacts():
    assert parse_changed_paths(
        " M src/app.py\0?? .hound/runs/one/state.json\0") == ["src/app.py"]
    assert matches_path("src/deep/app.py", "src/**")
    assert not matches_path("tests/app.py", "src/**")


def test_required_file_rejects_directory(tmp_path: Path):
    (tmp_path / "expected.txt").mkdir()
    issues = asyncio.run(path_gate(
        tmp_path, ["expected.txt"], [], [], tmp_path / "gate"))
    assert issues == ["missing required file: expected.txt"]


def test_changed_paths_handles_rename_and_git_failure(tmp_path: Path, monkeypatch):
    assert parse_changed_paths("R  new.py\0old.py\0") == ["new.py", "old.py"]
    async def failed(*_args, **_kwargs):
        return SimpleNamespace(exit_code=128, timed_out=False, cancelled=False)
    monkeypatch.setattr(verification, "run_process", failed)
    assert asyncio.run(changed_paths(tmp_path, tmp_path / "git")) is None
    assert asyncio.run(path_gate(tmp_path, [], ["src/**"], [], tmp_path / "git")) == [
        "could not inspect changed paths"]


def _git(root: Path, *args: str) -> None:
    command_root = root / ".hound" / "test-git"
    artifact = command_root / f"{len(list(command_root.glob('*'))):03d}"
    result = asyncio.run(run_process(["git", *args], root, artifact, 10))
    assert result.exit_code == 0, result.stderr


def _repository(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "hound@example.invalid")
    _git(root, "config", "user.name", "Hound Test")


def test_baseline_detects_forbidden_change_after_commit(tmp_path: Path):
    _repository(tmp_path)
    target = tmp_path / "protected.txt"
    target.write_text("before\n", encoding="utf-8")
    _git(tmp_path, "add", "protected.txt")
    _git(tmp_path, "commit", "-q", "-m", "baseline")
    baseline_path = tmp_path / ".hound" / "baseline.json"
    baseline = asyncio.run(capture_git_baseline(
        tmp_path, baseline_path, include_ignored=True))
    assert baseline["kind"] == "git"
    assert json.loads(baseline_path.read_text(encoding="utf-8"))["head"] == baseline["head"]

    target.write_text("after\n", encoding="utf-8")
    _git(tmp_path, "add", "protected.txt")
    _git(tmp_path, "commit", "-q", "-m", "forbidden")
    issues = asyncio.run(path_gate(
        tmp_path, [], [], ["protected.txt"], tmp_path / ".hound" / "gate",
        baseline=baseline))
    assert issues == ["forbidden path changed: protected.txt"]


def test_baseline_ignores_unchanged_initial_dirt_but_detects_later_change(tmp_path: Path):
    _repository(tmp_path)
    target = tmp_path / "dirty.txt"
    target.write_text("committed\n", encoding="utf-8")
    _git(tmp_path, "add", "dirty.txt")
    _git(tmp_path, "commit", "-q", "-m", "baseline")
    target.write_text("initial dirt\n", encoding="utf-8")
    baseline = asyncio.run(capture_git_baseline(
        tmp_path, tmp_path / ".hound" / "baseline.json", include_ignored=True))

    unchanged = asyncio.run(path_gate(
        tmp_path, [], [], ["dirty.txt"], tmp_path / ".hound" / "gate-unchanged",
        baseline=baseline))
    assert unchanged == []

    target.write_text("changed again\n", encoding="utf-8")
    changed = asyncio.run(path_gate(
        tmp_path, [], [], ["dirty.txt"], tmp_path / ".hound" / "gate-changed",
        baseline=baseline))
    assert changed == ["forbidden path changed: dirty.txt"]

    target.write_text("initial dirt\n", encoding="utf-8")
    _git(tmp_path, "add", "dirty.txt")
    _git(tmp_path, "commit", "-q", "-m", "commit initial dirt")
    committed = asyncio.run(path_gate(
        tmp_path, [], [], ["dirty.txt"], tmp_path / ".hound" / "gate-committed",
        baseline=baseline))
    assert committed == ["forbidden path changed: dirty.txt"]


def test_baseline_detects_changed_gitignored_path(tmp_path: Path):
    _repository(tmp_path)
    (tmp_path / ".gitignore").write_text("protected.txt\n", encoding="utf-8")
    ignored = tmp_path / "protected.txt"
    ignored.write_text("before\n", encoding="utf-8")
    _git(tmp_path, "add", ".gitignore")
    _git(tmp_path, "commit", "-q", "-m", "ignore protected path")
    baseline = asyncio.run(capture_git_baseline(
        tmp_path, tmp_path / ".hound" / "baseline.json", include_ignored=True))

    ignored.write_text("after\n", encoding="utf-8")
    issues = asyncio.run(path_gate(
        tmp_path, [], [], ["protected.txt"], tmp_path / ".hound" / "gate-ignored",
        baseline=baseline))
    assert issues == ["forbidden path changed: protected.txt"]


def test_baseline_detects_exact_leaf_inside_ignored_directory(tmp_path: Path):
    _repository(tmp_path)
    (tmp_path / ".gitignore").write_text("ignored_dir/\n", encoding="utf-8")
    ignored = tmp_path / "ignored_dir" / "secret.txt"
    ignored.parent.mkdir()
    ignored.write_text("before\n", encoding="utf-8")
    _git(tmp_path, "add", ".gitignore")
    _git(tmp_path, "commit", "-q", "-m", "ignore directory")
    baseline = asyncio.run(capture_git_baseline(
        tmp_path, tmp_path / ".hound" / "baseline.json", include_ignored=True))

    ignored.write_text("after\n", encoding="utf-8")
    forbidden = asyncio.run(path_gate(
        tmp_path, [], [], ["ignored_dir/secret.txt"],
        tmp_path / ".hound" / "gate-forbidden", baseline=baseline))
    allowed = asyncio.run(path_gate(
        tmp_path, [], ["ignored_dir/secret.txt"], [],
        tmp_path / ".hound" / "gate-allowed", baseline=baseline))
    assert forbidden == ["forbidden path changed: ignored_dir/secret.txt"]
    assert allowed == []


def test_git_snapshot_excludes_hound_artifacts_before_status_output(tmp_path: Path):
    _repository(tmp_path)
    (tmp_path / ".gitignore").write_text(".hound/\n", encoding="utf-8")
    _git(tmp_path, "add", ".gitignore")
    _git(tmp_path, "commit", "-q", "-m", "ignore hound artifacts")
    artifacts = tmp_path / ".hound" / "old-runs"
    artifacts.mkdir(parents=True)
    for index in range(300):
        (artifacts / f"artifact-{index}.json").write_text("{}", encoding="ascii")

    baseline_path = tmp_path / ".hound" / "baseline.json"
    baseline = asyncio.run(capture_git_baseline(
        tmp_path, baseline_path, include_ignored=True))
    status = tmp_path / ".hound" / "baseline-git" / "status" / "stdout.log"
    assert baseline["dirty"] == {} and status.read_text(encoding="utf-8") == ""


def test_verification_signature_distinguishes_same_exit_different_output(tmp_path: Path):
    signatures = []
    tail = "same tail\n" * 1000
    for name, output in (("first", "prefix A\n" + tail),
                         ("second", "prefix B\n" + tail)):
        root = tmp_path / name
        step = root / "01"
        step.mkdir(parents=True)
        (step / "stdout.log").write_text(output, encoding="utf-8")
        (step / "stderr.log").write_text("", encoding="utf-8")
        result = VerificationResult(
            ["check"], 1, "start", "finish", "stdout", "stderr", False,
            stdout_sha256=hashlib.sha256(output.encode()).hexdigest(),
            stderr_sha256=hashlib.sha256(b"").hexdigest())
        signatures.append(verification_signature([result], root))
    assert signatures[0] != signatures[1]


def test_changed_content_signature_distinguishes_same_path_rewrite(tmp_path: Path):
    target = tmp_path / "same.txt"
    target.write_text("first\n", encoding="utf-8")
    first = changed_content_signature(tmp_path, ["same.txt"])
    target.write_text("second\n", encoding="utf-8")
    assert first and first != changed_content_signature(tmp_path, ["same.txt"])


def test_async_content_signature_yields_and_stops_after_deadline(tmp_path: Path, monkeypatch):
    started, stopped = threading.Event(), threading.Event()

    def slow_signature(_workspace, _paths, _cancel_path=None, stop_event=None):
        started.set()
        while stop_event is not None and not stop_event.is_set():
            time.sleep(0.001)
        stopped.set()
        raise verification._HashCancelled

    monkeypatch.setattr(verification, "_changed_content_signature", slow_signature)

    async def scenario():
        heartbeat = asyncio.create_task(asyncio.sleep(0.01))
        try:
            async with asyncio.timeout(0.03):
                await changed_content_signature_async(tmp_path, ["."])
        except TimeoutError:
            pass
        else:
            raise AssertionError("content hashing ignored the outer deadline")
        await heartbeat

    asyncio.run(scenario())
    assert started.is_set() and stopped.wait(0.5)


def test_async_content_signature_honors_existing_cancel_marker(tmp_path: Path):
    cancel = tmp_path / "cancel.requested"
    cancel.touch()
    assert asyncio.run(changed_content_signature_async(tmp_path, ["."], cancel)) is None
