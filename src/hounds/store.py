from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .models import (HoundLockError, RunContract, RunState, dump, load_dataclass,
                     validate_contract, validate_state)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(dump(value), handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(dump(value), ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
    lines = [line for line in raw.splitlines() if line]
    values: list[dict] = []
    for index, line in enumerate(lines):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1 and not raw.endswith("\n"):
                break
            raise
        if not isinstance(value, dict):
            raise ValueError(f"JSONL record is not an object: {path}")
        values.append(value)
    return values


class _HashCancelled(Exception):
    pass


def _hash_cancelled(cancel_path: Path | None,
                    stop_event: threading.Event | None) -> bool:
    return bool((stop_event is not None and stop_event.is_set()) or
                (cancel_path is not None and cancel_path.exists()))


def _artifact_sha256(root: Path, cancel_path: Path | None = None,
                     stop_event: threading.Event | None = None) -> str:
    """Hash the complete canonical worker trace, including missing-file identities."""
    digest = hashlib.sha256()
    for name in ("stdout.log", "stderr.log", "trace.jsonl", "final.txt"):
        if _hash_cancelled(cancel_path, stop_event):
            raise _HashCancelled
        path = root / name
        digest.update(name.encode() + b"\0")
        if not path.is_file():
            digest.update(b"missing\0")
            continue
        digest.update(b"file\0")
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                if _hash_cancelled(cancel_path, stop_event):
                    raise _HashCancelled
                digest.update(chunk)
    return digest.hexdigest()


def artifact_sha256(root: Path) -> str:
    return _artifact_sha256(root)


async def artifact_sha256_async(root: Path, cancel_path: Path | None = None) -> str | None:
    """Hash off-loop; return None when a run cancel marker stops the scan."""
    stop_event = threading.Event()
    try:
        return await asyncio.to_thread(_artifact_sha256, root, cancel_path, stop_event)
    except _HashCancelled:
        return None
    except asyncio.CancelledError:
        stop_event.set()
        raise


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() != 87  # Unknown/access-denied is live; invalid PID is dead.
        code = wintypes.DWORD()
        try:
            return not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class FileLock:
    def __init__(self, path: Path):
        self.path, self.owned, self._handle = path, False, None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.fdopen(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600), "r+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            raise HoundLockError(f"lock already held: {self.path}") from error
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps({"pid": os.getpid(), "created_at": utc_now()}).encode())
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            handle.close()  # Closing also releases the platform lock.
            raise
        self._handle, self.owned = handle, True

    def release(self) -> None:
        if not self.owned or self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle, self.owned = None, False

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class RunStore:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.root = self.workspace / ".hound"
        self.runs = self.root / "runs"

    def run_dir(self, run_id: str) -> Path:
        if not run_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in run_id):
            raise ValueError("invalid run id")
        return self.runs / run_id

    def create(self, state: RunState, contract: RunContract) -> Path:
        path = self.run_dir(state.run_id)
        path.mkdir(parents=True, exist_ok=False)
        atomic_json(path / "state.json", state)
        atomic_json(path / "contract.json", contract)
        append_jsonl(path / "events.jsonl", {"timestamp": utc_now(), "type": "run_created"})
        return path

    def save_state(self, state: RunState) -> None:
        state.updated_at = utc_now()
        atomic_json(self.run_dir(state.run_id) / "state.json", state)

    def load_state(self, run_id: str) -> RunState:
        data = json.loads((self.run_dir(run_id) / "state.json").read_text(encoding="utf-8"))
        state = load_dataclass(RunState, data)
        validate_state(state)
        if state.run_id != run_id:
            raise ValueError("state run_id does not match its directory")
        return state

    def load_contract(self, run_id: str) -> RunContract:
        data = json.loads((self.run_dir(run_id) / "contract.json").read_text(encoding="utf-8"))
        contract = load_dataclass(RunContract, data)
        validate_contract(contract)
        return contract

    def event(self, run_id: str, event_type: str, **data: Any) -> None:
        append_jsonl(self.run_dir(run_id) / "events.jsonl", {
            "timestamp": utc_now(), "type": event_type, **data,
        })

    def fingerprint(self, run_id: str, value: str, status: str, evidence_hash: str) -> None:
        append_jsonl(self.run_dir(run_id) / "fingerprints.jsonl", {
            "timestamp": utc_now(), "fingerprint": value,
            "status": status, "evidence_hash": evidence_hash,
        })

    def fingerprints(self, run_id: str) -> list[dict]:
        return read_jsonl(self.run_dir(run_id) / "fingerprints.jsonl")

    def list_runs(self) -> list[str]:
        if not self.runs.exists():
            return []
        def created(path: Path) -> str:
            try:
                return str(json.loads((path / "state.json").read_text(encoding="utf-8"))["created_at"])
            except (OSError, KeyError, TypeError, json.JSONDecodeError):
                return ""
        return [path.name for path in sorted(
            (path for path in self.runs.iterdir() if path.is_dir()), key=created, reverse=True)]

    def workspace_lock_status(self) -> dict[str, Any]:
        """Return a non-mutating snapshot; it never reclaims or kills an owner."""
        path = self._workspace_lock_path()
        if not path.exists():
            return {"path": str(path), "available": True, "held": False,
                    "stale": False, "owner": None}
        try:
            owner = json.loads(path.read_text(encoding="utf-8"))
            owner = owner if isinstance(owner, dict) else None
        except (OSError, json.JSONDecodeError):
            owner = None
        try:
            handle = os.fdopen(os.open(path, os.O_RDWR), "r+b")
        except FileNotFoundError:
            return {"path": str(path), "available": True, "held": False,
                    "stale": False, "owner": None}
        except OSError as error:
            return {"path": str(path), "available": False, "held": None,
                    "stale": None, "owner": owner, "error": str(error)}
        acquired = False
        try:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                return {"path": str(path), "available": False, "held": True,
                        "stale": False, "owner": owner}
            return {"path": str(path), "available": True, "held": False,
                    "stale": owner is not None, "owner": owner}
        finally:
            if acquired:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _workspace_lock_path(self) -> Path:
        canonical = os.path.normcase(os.path.realpath(self.workspace))
        identity = hashlib.sha256(os.fsencode(canonical)).hexdigest()
        state_root = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_RUNTIME_DIR")
                          or Path.home() / ".cache")
        return state_root / "Hound" / "locks" / f"{identity}.lock"

    @contextmanager
    def workspace_lock(self) -> Iterator[None]:
        lock = FileLock(self._workspace_lock_path())
        with lock:
            yield
