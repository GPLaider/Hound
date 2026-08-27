from __future__ import annotations

import asyncio
import codecs
import ctypes
import locale
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .store import atomic_json, pid_alive, utc_now


_SECRET_NAMES = ("TOKEN", "KEY", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")
_TERMINATION_REASONS = {
    "cancelled_by_user", "culled_by_supervisor", "cancelled_for_policy",
    "cancelled_for_budget", "cancelled_for_run_shutdown",
}
_WINDOWS_PROTECTED_EXECUTABLES = frozenset({
    "chatgpt.exe", "csrss.exe", "dwm.exe", "explorer.exe", "lsass.exe",
    "runtimebroker.exe", "searchhost.exe", "services.exe", "shellexperiencehost.exe",
    "sihost.exe", "smss.exe", "startmenuexperiencehost.exe", "winlogon.exe",
})
_CODEX_HOMES: dict[str, tempfile.TemporaryDirectory] = {}
_CODEX_HOME_LOCK = threading.Lock()
_CODEX_HOME_NAME = re.compile(r"^hound-codex-(\d+)-[A-Za-z0-9_-]+$")
_CODEX_HOME_MARKER = ".hound-owned"


def _termination_reason(reason: str) -> str:
    normalized = {"timed_out": "cancelled_for_policy",
                  "runner_interrupted": "cancelled_for_run_shutdown",
                  "start_failed": "cancelled_for_run_shutdown"}.get(reason, reason)
    return normalized if normalized in _TERMINATION_REASONS else "cancelled_for_run_shutdown"


def secret_values(environment: dict[str, str] | None = None) -> tuple[str, ...]:
    environment = environment if environment is not None else os.environ
    return tuple(sorted({value for name, value in environment.items()
                         if value and len(value) >= 3 and
                         any(part in name.upper() for part in _SECRET_NAMES)},
                        key=len, reverse=True))


def _redact_values(text: str, values: tuple[str, ...]) -> str:
    for value in values:
        text = text.replace(value, "[REDACTED]")
    return text


def redact(text: str, environment: dict[str, str] | None = None) -> str:
    return _redact_values(text, secret_values(environment))


def trusted_python_executable() -> str:
    """Return the interpreter already proven healthy by running Hound itself."""
    for value in (getattr(sys, "_base_executable", ""), sys.executable):
        if value and Path(value).is_file():
            return str(Path(value).resolve())
    raise RuntimeError("Hound cannot identify its running Python interpreter")


def worker_environment(environment: dict[str, str] | None = None) -> dict[str, str]:
    """Pin workers to Hound's Python instead of scavenging application runtimes."""
    env = dict(os.environ if environment is None else environment)
    trusted = trusted_python_executable()
    python_dir = str(Path(trusted).parent)
    scripts_dir = str(Path(python_dir) / "Scripts")
    entries = [python_dir]
    if Path(scripts_dir).is_dir():
        entries.append(scripts_dir)
    entries.extend(part.strip('"') for part in env.get("PATH", "").split(os.pathsep) if part)
    unique: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        key = os.path.normcase(os.path.abspath(entry))
        if key not in seen:
            seen.add(key)
            unique.append(entry)
    env["PATH"] = os.pathsep.join(unique)
    env["HOUND_PYTHON_EXECUTABLE"] = trusted
    env["HOUND_MANAGED_WORKER"] = "1"
    env.pop("PYTHONHOME", None)
    env.pop("__PYVENV_LAUNCHER__", None)
    return env


def codex_environment(environment: dict[str, str] | None = None) -> dict[str, str]:
    """Keep Codex runtime state out of the user's global config directory."""
    _cleanup_stale_codex_homes()
    env = worker_environment(environment)
    if env.get("HOUND_CODEX_HOME_ISOLATED") == "1":
        return env
    source = Path(env.get("CODEX_HOME", Path.home() / ".codex")).resolve()
    key = os.path.normcase(str(source))
    with _CODEX_HOME_LOCK:
        temporary = _CODEX_HOMES.get(key)
        if temporary is None:
            temporary = tempfile.TemporaryDirectory(prefix=f"hound-codex-{os.getpid()}-")
            isolated = Path(temporary.name)
            (isolated / _CODEX_HOME_MARKER).write_text(
                f"hound-codex-home-v1\n{os.getpid()}\n", encoding="ascii")
            auth = source / "auth.json"
            if auth.is_file():
                shutil.copy2(auth, isolated / "auth.json")
            skills = source / "skills"
            if skills.is_dir():
                shutil.copytree(
                    skills, isolated / "skills",
                    ignore=shutil.ignore_patterns(".system", "__pycache__", "*.pyc"))
            _CODEX_HOMES[key] = temporary
        env["CODEX_HOME"] = temporary.name
    env["HOUND_CODEX_HOME_ISOLATED"] = "1"
    return env


def _cleanup_stale_codex_homes(root: Path | None = None) -> None:
    root = (root or Path(tempfile.gettempdir())).resolve()
    for candidate in root.glob("hound-codex-*"):
        match = _CODEX_HOME_NAME.fullmatch(candidate.name)
        if not match or candidate.is_symlink() or candidate.resolve().parent != root:
            continue
        owner_pid = int(match.group(1))
        try:
            marker = (candidate / _CODEX_HOME_MARKER).read_text(encoding="ascii")
        except OSError:
            continue
        if marker != f"hound-codex-home-v1\n{owner_pid}\n" or pid_alive(owner_pid):
            continue
        try:
            shutil.rmtree(candidate)
        except FileNotFoundError:
            pass
        except OSError as error:
            raise RuntimeError(f"failed to remove stale isolated Codex home: {candidate}") from error


def cleanup_codex_homes() -> None:
    with _CODEX_HOME_LOCK:
        temporary_directories = list(_CODEX_HOMES.values())
        _CODEX_HOMES.clear()
    for temporary in temporary_directories:
        path = Path(temporary.name)
        temporary.cleanup()
        if path.exists():
            raise RuntimeError(f"failed to remove isolated Codex home: {path}")


def redact_artifacts(root: Path, values: tuple[str, ...] | None = None,
                     names: set[str] | None = None,
                     cancel_path: Path | None = None,
                     stop_event: threading.Event | None = None) -> bool:
    values = secret_values() if values is None else values
    cancelled = lambda: bool(
        (stop_event is not None and stop_event.is_set()) or
        (cancel_path is not None and cancel_path.exists()))
    if cancelled():
        return False
    if not values:
        return True
    for path in sorted(root.glob("**/*")):
        if cancelled():
            return False
        if (not path.is_file() or names is not None and path.name not in names or
                path.suffix.lower() not in {".json", ".jsonl", ".log", ".md", ".txt"}):
            continue
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        try:
            redactor = _StreamingRedactor(values)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as target, \
                    path.open("r", encoding="utf-8", errors="replace", newline="") as source:
                while chunk := source.read(65536):
                    if cancelled():
                        return False
                    target.write(redactor.feed(chunk))
                target.write(redactor.feed("", final=True))
                target.flush()
                os.fsync(target.fileno())
            if cancelled():
                return False
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return True


async def redact_artifacts_async(
        root: Path, values: tuple[str, ...] | None = None,
        names: set[str] | None = None,
        cancel_path: Path | None = None) -> bool:
    stop_event = threading.Event()
    try:
        return await asyncio.to_thread(
            redact_artifacts, root, values, names, cancel_path, stop_event)
    except asyncio.CancelledError:
        stop_event.set()
        raise


class _StreamingRedactor:
    def __init__(self, values: tuple[str, ...]):
        self.values = values
        self.pattern = re.compile("|".join(re.escape(value) for value in values)) if values else None
        self.pending = ""

    def feed(self, text: str, final: bool = False) -> str:
        if self.pattern is None:
            return text
        data = self.pending + text
        output: list[str] = []
        cursor = 0
        for match in self.pattern.finditer(data):
            output.extend((data[cursor:match.start()], "[REDACTED]"))
            cursor = match.end()
        trailing = data[cursor:]
        held = 0
        if not final:
            for value in self.values:
                for length in range(min(len(value) - 1, len(trailing)), held, -1):
                    if trailing.endswith(value[:length]):
                        held = length
                        break
        output.append(trailing[:-held] if held else trailing)
        self.pending = trailing[-held:] if held else ""
        return "".join(output)


@dataclass(slots=True)
class ProcessOutcome:
    argv: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    started_at: str
    finished_at: str
    timed_out: bool = False
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class WindowsProcess:
    pid: int
    parent_pid: int
    executable: str
    creation_time: int = 0
    image_path: str = ""


def windows_process_table() -> dict[int, WindowsProcess]:
    """Snapshot PID/PPID without a shell or console-control API."""
    if os.name != "nt":
        return {}
    from ctypes import wintypes

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    table: dict[int, WindowsProcess] = {}
    entry = ProcessEntry32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            process = WindowsProcess(int(entry.th32ProcessID), int(entry.th32ParentProcessID), entry.szExeFile)
            table[process.pid] = process
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return table


def windows_process_identity(pid: int, process: WindowsProcess | None = None) -> WindowsProcess:
    """Bind a PID to its kernel creation time and image path to detect PID reuse."""
    if os.name != "nt":
        return process or WindowsProcess(pid, 0, "")
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        raise RuntimeError(f"process identity unavailable for PID={pid}: {ctypes.WinError(ctypes.get_last_error())}")
    created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
    buffer, length = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
    try:
        if not kernel32.GetProcessTimes(
                handle, ctypes.byref(created), ctypes.byref(exited),
                ctypes.byref(kernel), ctypes.byref(user)):
            raise RuntimeError(
                f"process creation time unavailable for PID={pid}: {ctypes.WinError(ctypes.get_last_error())}")
        image_path = buffer.value if kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(length)) else ""
    finally:
        kernel32.CloseHandle(handle)
    base = process or WindowsProcess(pid, 0, Path(image_path).name)
    creation_time = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
    return WindowsProcess(base.pid, base.parent_pid, base.executable,
                          creation_time, image_path)


def windows_process_subtree(target_pid: int,
                            process_table: dict[int, WindowsProcess]) -> list[WindowsProcess]:
    children: dict[int, list[WindowsProcess]] = {}
    for process in process_table.values():
        children.setdefault(process.parent_pid, []).append(process)
    result: list[WindowsProcess] = []
    pending = [target_pid]
    seen: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        process = process_table.get(pid)
        if process:
            result.append(process)
            pending.extend(child.pid for child in children.get(pid, ()))
    return result


def guarded_windows_target(target_pid: int, owner_pid: int | None = None,
                           process_table: dict[int, WindowsProcess] | None = None,
                           expected: WindowsProcess | None = None) -> WindowsProcess:
    """Accept only the live direct worker child owned by this Hound process."""
    owner_pid = owner_pid or os.getpid()
    table = process_table if process_table is not None else windows_process_table()
    target = table.get(target_pid)
    owner = table.get(owner_pid)
    if not target or not owner:
        raise RuntimeError(f"termination guard: missing target PID={target_pid} or owner PID={owner_pid}")
    ancestors: set[int] = set()
    cursor = owner
    while cursor.parent_pid and cursor.parent_pid not in ancestors:
        ancestors.add(cursor.parent_pid)
        cursor = table.get(cursor.parent_pid, WindowsProcess(cursor.parent_pid, 0, ""))
    if target.pid == owner_pid or target.pid in ancestors:
        raise RuntimeError(f"termination guard: target PID={target.pid} is Hound or its ancestor")
    if target.parent_pid != owner_pid:
        raise RuntimeError(
            f"termination guard: target PID={target.pid} PPID={target.parent_pid} is not direct worker child of Hound PID={owner_pid}")
    if target.executable.casefold() in _WINDOWS_PROTECTED_EXECUTABLES:
        raise RuntimeError(
            f"termination guard: protected target PID={target.pid} executable={target.executable}")
    if expected is not None:
        if (target.pid != expected.pid or target.parent_pid != expected.parent_pid or
                target.executable.casefold() != expected.executable.casefold()):
            raise RuntimeError(f"termination guard: worker identity changed for PID={target.pid}")
        if not target.creation_time or not expected.creation_time:
            raise RuntimeError(f"termination guard: missing creation identity for PID={target.pid}")
        if target.creation_time != expected.creation_time:
            raise RuntimeError(f"termination guard: PID reuse detected for PID={target.pid}")
        if (target.image_path and expected.image_path and
                os.path.normcase(target.image_path) != os.path.normcase(expected.image_path)):
            raise RuntimeError(f"termination guard: image path changed for PID={target.pid}")
    return target


def guarded_windows_tree(target_pid: int, owner_pid: int,
                         process_table: dict[int, WindowsProcess],
                         expected: WindowsProcess | None = None) -> tuple[WindowsProcess, list[WindowsProcess]]:
    target = guarded_windows_target(target_pid, owner_pid, process_table, expected)
    tree = windows_process_subtree(target_pid, process_table)
    protected = [process for process in tree[1:]
                 if process.executable.casefold() in _WINDOWS_PROTECTED_EXECUTABLES]
    if protected:
        detail = ", ".join(f"PID={item.pid} executable={item.executable}" for item in protected)
        raise RuntimeError(f"termination guard: protected worker descendant(s): {detail}")
    owner_ancestors: set[int] = set()
    cursor = process_table.get(owner_pid)
    while cursor and cursor.parent_pid and cursor.parent_pid not in owner_ancestors:
        owner_ancestors.add(cursor.parent_pid)
        cursor = process_table.get(cursor.parent_pid)
    overlap = owner_ancestors.intersection(item.pid for item in tree)
    if overlap:
        raise RuntimeError(
            "termination guard: worker tree overlaps Hound ancestor(s): " +
            ", ".join(str(pid) for pid in sorted(overlap)))
    return target, tree


def suppress_windows_error_dialogs() -> int:
    """Make loader/crash failures non-interactive for Hound and inherited workers."""
    if os.name != "nt":
        return 0
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetErrorMode.restype = wintypes.UINT
    kernel32.SetErrorMode.argtypes = [wintypes.UINT]
    kernel32.SetErrorMode.restype = wintypes.UINT
    mode = int(kernel32.GetErrorMode()) | 0x0001 | 0x0002 | 0x8000
    kernel32.SetErrorMode(mode)
    return mode


def windows_taskkill_path() -> str:
    from ctypes import wintypes

    buffer = ctypes.create_unicode_buffer(32768)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    kernel32.GetSystemDirectoryW.restype = wintypes.UINT
    length = kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    return str(Path(buffer.value) / "taskkill.exe")


def resolve_executable(command: str, cwd: Path, env: dict[str, str] | None = None) -> str | None:
    separators = tuple(separator for separator in (os.sep, os.altsep) if separator)
    if Path(command).is_absolute() or any(separator in command for separator in separators):
        candidate = Path(command)
        candidate = candidate if candidate.is_absolute() else cwd / candidate
        return str(candidate.resolve()) if candidate.is_file() else None
    names = [command]
    if os.name == "nt" and not Path(command).suffix:
        names = [command + suffix.lower() for suffix in (".EXE", ".COM")]
    for directory in os.get_exec_path(env):
        if not directory:
            continue  # Never use the platform's implicit current-directory search.
        for name in names:
            base = Path(directory.strip('"'))
            candidate = (base if base.is_absolute() else cwd / base) / name
            if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return str(candidate.resolve())
    return None


class ManagedProcess:
    def __init__(self, argv: list[str], cwd: Path, artifact_dir: Path,
                 env: dict[str, str] | None = None, input_text: str | None = None):
        self.argv, self.cwd, self.artifact_dir = argv, cwd, artifact_dir
        self.env, self.input_text = env, input_text
        self.process: asyncio.subprocess.Process | None = None
        self.stdout_lines: deque[str] = deque(maxlen=16)
        self.stderr_lines: deque[str] = deque(maxlen=16)
        self._pump_tasks: list[asyncio.Task] = []
        self.progress_events = 0
        self.started_at = ""
        self.started_monotonic = 0.0
        self.cancelled = False
        self._secret_values: tuple[str, ...] = ()
        self._windows_launch_identity: WindowsProcess | None = None

    async def start(self) -> None:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        environment = self.env if self.env is not None else os.environ.copy()
        self._secret_values = secret_values(environment)
        executable = resolve_executable(self.argv[0], self.cwd, environment)
        if not executable:
            raise FileNotFoundError(
                f"executable not found outside implicit current-directory search: {self.argv[0]}")
        self.argv = [executable, *self.argv[1:]]
        if (os.name == "nt" and
                Path(executable).name.casefold() in _WINDOWS_PROTECTED_EXECUTABLES):
            raise PermissionError(f"refusing to manage protected Windows executable: {executable}")
        kwargs: dict = {"cwd": self.cwd, "env": environment,
                        "stdin": asyncio.subprocess.DEVNULL,
                        "stdout": asyncio.subprocess.PIPE, "stderr": asyncio.subprocess.PIPE}
        if self.input_text is not None:
            kwargs["stdin"] = asyncio.subprocess.PIPE
        windows_error_mode = 0
        if os.name == "nt":
            windows_error_mode = suppress_windows_error_dialogs()
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        self.started_at = utc_now()
        self.started_monotonic = time.monotonic()
        self.process = await asyncio.create_subprocess_exec(*self.argv, **kwargs)
        try:
            self._pump_tasks = [
                asyncio.create_task(self._pump(self.process.stdout, "stdout", self.stdout_lines)),
                asyncio.create_task(self._pump(self.process.stderr, "stderr", self.stderr_lines)),
            ]
            launch = {"started_at": self.started_at, "hound_pid": os.getpid(),
                      "worker_pid": self.process.pid, "worker_ppid": os.getpid(),
                      "worker_executable": Path(self.argv[0]).name}
            if os.name == "nt":
                snapshot = windows_process_table().get(self.process.pid)
                if snapshot:
                    snapshot = windows_process_identity(self.process.pid, snapshot)
                    self._windows_launch_identity = snapshot
                    launch.update({"worker_ppid": snapshot.parent_pid,
                                   "worker_executable": snapshot.executable,
                                   "worker_creation_time": snapshot.creation_time,
                                   "worker_image_path": snapshot.image_path})
                launch.update({"windows_creation_flags": subprocess.CREATE_NO_WINDOW,
                               "windows_error_mode": windows_error_mode})
            atomic_json(self.artifact_dir / "launch.json", launch)
            if self.input_text is not None and self.process.stdin:
                try:
                    self.process.stdin.write(self.input_text.encode())
                    await self.process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                self.process.stdin.close()
        except BaseException:
            cleanup = asyncio.create_task(self.terminate("start_failed"))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
            finally:
                await asyncio.gather(*self._pump_tasks, return_exceptions=True)
            raise

    async def _pump(self, stream: asyncio.StreamReader | None, name: str, target: deque[str]) -> None:
        if stream is None:
            return
        path = self.artifact_dir / f"{name}.log"
        trace_path = self.artifact_dir / "trace.jsonl"
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        redactor = _StreamingRedactor(self._secret_values)
        line_has_content = False
        trace = trace_path.open("a", encoding="utf-8", newline="\n") if name == "stdout" else None
        try:
            handle = path.open("a", encoding="utf-8", newline="\n")
        except BaseException:
            if trace:
                trace.close()
            raise
        try:
            while chunk := await stream.read(65536):
                text = redactor.feed(decoder.decode(chunk))
                if not text:
                    continue
                target.append(text)
                handle.write(text)
                handle.flush()
                if trace:
                    trace.write(text)
                    trace.flush()
                parts = text.split("\n")
                for line in parts[:-1]:
                    if line_has_content or line.strip():
                        self.progress_events += 1
                    line_has_content = False
                line_has_content = line_has_content or bool(parts[-1].strip())
            tail = redactor.feed(decoder.decode(b"", final=True), final=True)
            if tail:
                target.append(tail)
                handle.write(tail)
                if trace:
                    trace.write(tail)
                line_has_content = line_has_content or bool(tail.strip())
            if line_has_content:
                self.progress_events += 1
        finally:
            handle.close()
            if trace:
                trace.close()

    async def wait(self, timeout: float | None = None, cancel_path: Path | None = None) -> ProcessOutcome:
        assert self.process is not None
        timed_out = False
        deadline = self.started_monotonic + timeout if timeout is not None else None
        try:
            while self.process.returncode is None:
                if cancel_path and cancel_path.exists():
                    await self.terminate("cancelled_by_user")
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    timed_out = True
                    await self.terminate("timed_out")
                    break
                await asyncio.sleep(0.05)
            await self.process.wait()
            await asyncio.gather(*self._pump_tasks)
        except BaseException:
            reason = ("cancelled_by_user" if cancel_path and cancel_path.exists()
                      else "runner_interrupted")
            cleanup = asyncio.create_task(self.terminate(reason))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
            finally:
                await asyncio.gather(*self._pump_tasks, return_exceptions=True)
            raise
        redact_artifacts(self.artifact_dir, self._secret_values, {"final.txt"})
        safe_argv = [_redact_values(part, self._secret_values) for part in self.argv]
        outcome = ProcessOutcome(safe_argv, self.process.returncode, "".join(self.stdout_lines),
                                 "".join(self.stderr_lines), self.started_at, utc_now(), timed_out,
                                 self.cancelled)
        atomic_json(self.artifact_dir / "outcome.json", outcome)
        return outcome

    async def terminate(self, reason: str = "cancelled", grace: float = 2) -> bool:
        try:
            return await self._terminate(reason, grace)
        finally:
            if self.process is not None and self.process.returncode is not None:
                redact_artifacts(self.artifact_dir, self._secret_values, {"final.txt"})

    async def _terminate(self, reason: str, grace: float) -> bool:
        if not self.process:
            return False
        if self.process.returncode is not None:
            await asyncio.gather(*self._pump_tasks)
            return False
        self.cancelled = reason not in {"timed_out", "start_failed"}
        if os.name == "nt":
            table = windows_process_table()
            if os.getpid() not in table:
                raise RuntimeError(f"termination guard: missing owner PID={os.getpid()}")
            if self.process.pid not in table:
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=0.5)
                except TimeoutError as error:
                    raise RuntimeError(
                        f"termination guard: missing target PID={self.process.pid} while worker remains live") from error
                await asyncio.gather(*self._pump_tasks)
                atomic_json(self.artifact_dir / "termination.json", {
                    "timestamp": utc_now(), "reason": _termination_reason(reason),
                    "hound_pid": os.getpid(), "target_pid": self.process.pid,
                    "method": "already_exited_before_taskkill",
                    "worker_exit_code": self.process.returncode, "finished_at": utc_now()})
                return False
            table[self.process.pid] = windows_process_identity(
                self.process.pid, table[self.process.pid])
            try:
                if self._windows_launch_identity is None:
                    raise RuntimeError(
                        f"termination guard: missing launch identity for PID={self.process.pid}")
                target, tree = guarded_windows_tree(
                    self.process.pid, os.getpid(), table, self._windows_launch_identity)
            except RuntimeError as error:
                refused_tree = windows_process_subtree(self.process.pid, table)
                try:
                    atomic_json(self.artifact_dir / "termination.json", {
                        "timestamp": utc_now(), "reason": _termination_reason(reason),
                        "hound_pid": os.getpid(), "target_pid": self.process.pid,
                        "method": "refused_by_guard", "guard_error": str(error),
                        "guarded_tree": [
                            {"pid": item.pid, "parent_pid": item.parent_pid,
                             "executable": item.executable} for item in refused_tree],
                    })
                except OSError:
                    pass
                raise
            record = {"timestamp": utc_now(), "reason": _termination_reason(reason),
                      "hound_pid": os.getpid(),
                      "target_pid": target.pid, "target_ppid": target.parent_pid,
                      "target_executable": target.executable,
                      "target_creation_time": target.creation_time,
                      "target_image_path": target.image_path,
                      "guarded_tree": [{"pid": item.pid, "parent_pid": item.parent_pid,
                                        "executable": item.executable} for item in tree],
                      "method": "taskkill_pid_tree"}
            try:
                atomic_json(self.artifact_dir / "termination.json", record)
            except OSError:
                pass
            result = await asyncio.create_subprocess_exec(
                windows_taskkill_path(), "/PID", str(target.pid), "/T", "/F",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                stdout, stderr = await asyncio.wait_for(result.communicate(), timeout=10)
            except TimeoutError:
                result.kill()
                await result.wait()
                raise RuntimeError(f"taskkill hung for worker PID={target.pid}")
            try:
                await asyncio.wait_for(self.process.wait(), timeout=10 if result.returncode == 0 else 0.5)
            except TimeoutError:
                encoding = locale.getpreferredencoding(False)
                record.update({"taskkill_exit_code": result.returncode,
                               "taskkill_stdout": stdout.decode(encoding, errors="replace")[-4000:],
                               "taskkill_stderr": stderr.decode(encoding, errors="replace")[-4000:],
                               "finished_at": utc_now()})
                try:
                    atomic_json(self.artifact_dir / "termination.json", record)
                except OSError:
                    pass
                raise RuntimeError(f"taskkill failed to stop worker PID={target.pid}: exit {result.returncode}")
            await asyncio.gather(*self._pump_tasks)
            encoding = locale.getpreferredencoding(False)
            record.update({"taskkill_exit_code": result.returncode,
                           "taskkill_stdout": stdout.decode(encoding, errors="replace")[-4000:],
                           "taskkill_stderr": stderr.decode(encoding, errors="replace")[-4000:],
                           "worker_exit_code": self.process.returncode,
                           "finished_at": utc_now()})
            try:
                atomic_json(self.artifact_dir / "termination.json", record)
            except OSError as error:
                raise RuntimeError(
                    f"worker PID={target.pid} stopped but termination artifact failed: {error}") from error
            return result.returncode == 0
        else:
            record = {"timestamp": utc_now(), "reason": _termination_reason(reason),
                      "hound_pid": os.getpid(), "target_pid": self.process.pid,
                      "method": "posix_process_group"}
            atomic_json(self.artifact_dir / "termination.json", record)
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                await asyncio.wait_for(self.process.wait(), timeout=grace)
                await asyncio.gather(*self._pump_tasks)
                record.update({"signal": "SIGTERM", "worker_exit_code": self.process.returncode,
                               "finished_at": utc_now()})
                atomic_json(self.artifact_dir / "termination.json", record)
                return True
            except (OSError, TimeoutError, ProcessLookupError):
                sent = False
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    sent = True
                except ProcessLookupError:
                    pass
            await self.process.wait()
            await asyncio.gather(*self._pump_tasks)
            record.update({"signal": "SIGKILL", "worker_exit_code": self.process.returncode,
                           "finished_at": utc_now()})
            atomic_json(self.artifact_dir / "termination.json", record)
            return sent


async def run_process(argv: list[str], cwd: Path, artifact_dir: Path,
                      timeout: float | None = None, env: dict[str, str] | None = None,
                      cancel_path: Path | None = None, input_text: str | None = None) -> ProcessOutcome:
    managed = ManagedProcess(argv, cwd, artifact_dir, env, input_text)
    await managed.start()
    return await managed.wait(timeout, cancel_path)
