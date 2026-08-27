from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path

import hounds.store as store_module
from hounds.models import RunState, RunStatus
from hounds.store import (FileLock, RunStore, append_jsonl, artifact_sha256,
                          artifact_sha256_async, atomic_json, pid_alive, utc_now)


def test_atomic_json_and_event_append(tmp_path: Path):
    target = tmp_path / "state.json"
    atomic_json(target, {"ok": True})
    assert json.loads(target.read_text()) == {"ok": True}
    events = tmp_path / "events.jsonl"
    append_jsonl(events, {"one": 1})
    append_jsonl(events, {"two": 2})
    assert len(events.read_text().splitlines()) == 2


def test_artifact_hash_covers_prefix_not_only_bounded_tail(tmp_path: Path):
    artifact = tmp_path / "worker"; artifact.mkdir()
    trace = artifact / "trace.jsonl"
    trace.write_text("prefix-a\n" + "same-tail" * 100, encoding="utf-8")
    first = artifact_sha256(artifact)
    trace.write_text("prefix-b\n" + "same-tail" * 100, encoding="utf-8")
    assert artifact_sha256(artifact) != first


def test_async_artifact_hash_yields_and_stops_after_deadline(tmp_path: Path, monkeypatch):
    started, stopped = threading.Event(), threading.Event()

    def slow_hash(_root, _cancel_path=None, stop_event=None):
        started.set()
        while stop_event is not None and not stop_event.is_set():
            time.sleep(0.001)
        stopped.set()
        raise store_module._HashCancelled

    monkeypatch.setattr(store_module, "_artifact_sha256", slow_hash)

    async def scenario():
        heartbeat = asyncio.create_task(asyncio.sleep(0.01))
        try:
            async with asyncio.timeout(0.03):
                await artifact_sha256_async(tmp_path)
        except TimeoutError:
            pass
        else:
            raise AssertionError("artifact hashing ignored the outer deadline")
        await heartbeat

    asyncio.run(scenario())
    assert started.is_set() and stopped.wait(0.5)


def test_async_artifact_hash_honors_existing_cancel_marker(tmp_path: Path):
    cancel = tmp_path / "cancel.requested"
    cancel.touch()
    assert asyncio.run(artifact_sha256_async(tmp_path, cancel)) is None


def test_stale_lock_recovered(tmp_path: Path):
    path = tmp_path / "active.lock"
    path.write_text(json.dumps({"pid": 99999999}), encoding="utf-8")
    with FileLock(path):
        assert path.exists()
        try:
            FileLock(path).acquire()
        except RuntimeError:
            pass
        else:
            raise AssertionError("OS lock allowed a second owner")
    assert json.loads(path.read_text())["pid"] == os.getpid()
    with FileLock(path):
        pass


def test_pid_alive_is_read_only_for_current_process():
    assert pid_alive(os.getpid())


def test_workspace_lock_is_exclusive(tmp_path: Path):
    first, second = RunStore(tmp_path), RunStore(tmp_path)
    with first.workspace_lock():
        try:
            with second.workspace_lock():
                pass
        except RuntimeError:
            pass
        else:
            raise AssertionError("workspace lock allowed concurrent runs")


def test_workspace_lock_status_is_non_mutating_and_reports_owner(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-state"))
    store = RunStore(tmp_path / "workspace")
    initial = store.workspace_lock_status()
    assert initial["available"] is True and initial["held"] is False
    assert initial["owner"] is None and not Path(initial["path"]).exists()
    with store.workspace_lock():
        held = store.workspace_lock_status()
        assert held["available"] is False and held["held"] is True
        # Windows byte-range locks can also block the metadata read itself.
        assert held["owner"] is None or held["owner"]["pid"] == os.getpid()
    released = store.workspace_lock_status()
    assert released["available"] is True and released["held"] is False
    assert released["stale"] is True and released["owner"]["pid"] == os.getpid()


def test_future_state_fields_are_ignored(tmp_path: Path):
    store = RunStore(tmp_path)
    run = store.run_dir("r1")
    run.mkdir(parents=True)
    (run / "state.json").write_text(json.dumps({"run_id": "r1", "status": "created",
        "created_at": utc_now(), "updated_at": utc_now(), "future": 9}), encoding="utf-8")
    assert store.load_state("r1").status is RunStatus.CREATED


def test_state_directory_identity_is_validated(tmp_path: Path):
    store = RunStore(tmp_path)
    run = store.run_dir("r1")
    run.mkdir(parents=True)
    (run / "state.json").write_text(json.dumps({"run_id": "other", "status": "created",
        "created_at": utc_now(), "updated_at": utc_now()}), encoding="utf-8")
    try:
        store.load_state("r1")
    except ValueError as error:
        assert "does not match" in str(error)
    else:
        raise AssertionError("mismatched state identity was accepted")


def test_fingerprints_ignore_only_torn_tail(tmp_path: Path):
    store = RunStore(tmp_path)
    run = store.run_dir("r1")
    run.mkdir(parents=True)
    path = run / "fingerprints.jsonl"
    path.write_text('{"fingerprint":"ok"}\n{"finger', encoding="utf-8")
    assert store.fingerprints("r1") == [{"fingerprint": "ok"}]
    path.write_text('{bad}\n{"fingerprint":"ok"}\n', encoding="utf-8")
    try:
        store.fingerprints("r1")
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("middle JSONL corruption was ignored")
    path.write_text('{"fingerprint":"ok"}\n{bad}\n', encoding="utf-8")
    try:
        store.fingerprints("r1")
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("complete corrupt tail was ignored")
