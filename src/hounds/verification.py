from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import threading
from pathlib import Path, PurePosixPath
from typing import Any

from .models import VerificationResult, dump
from .process import run_process
from .store import atomic_json, utc_now


GIT_BASELINE_SCHEMA = 2
GIT_VISIBLE_PATHSPEC = [".", ":(exclude).hound", ":(exclude).hound/**"]


async def verify_commands(commands: list[list[str]], workspace: Path, artifact_dir: Path,
                          timeout: float, expected_exit_code: int = 0,
                          cancel_path: Path | None = None) -> list[VerificationResult]:
    results: list[VerificationResult] = []
    artifact_dir.mkdir(parents=True, exist_ok=True)
    for index, command in enumerate(commands, 1):
        target = artifact_dir / f"{index:02d}"
        outcome = await run_process(command, workspace, target, timeout, cancel_path=cancel_path)
        stdout_path, stderr_path = target / "stdout.log", target / "stderr.log"
        stdout_sha256 = await _file_sha256_async(stdout_path, cancel_path)
        stderr_sha256 = (await _file_sha256_async(stderr_path, cancel_path)
                         if stdout_sha256 is not None else None)
        result = VerificationResult(
            command=command, exit_code=outcome.exit_code,
            started_at=outcome.started_at, finished_at=outcome.finished_at,
            stdout_artifact=str((target / "stdout.log").relative_to(artifact_dir.parent.parent.parent)),
            stderr_artifact=str((target / "stderr.log").relative_to(artifact_dir.parent.parent.parent)),
            stdout_sha256=stdout_sha256 or "",
            stderr_sha256=stderr_sha256 or "",
            passed=(outcome.exit_code == expected_exit_code and not outcome.timed_out and
                    not outcome.cancelled and stdout_sha256 is not None and
                    stderr_sha256 is not None),
            timed_out=outcome.timed_out,
        )
        atomic_json(target / "result.json", result)
        results.append(result)
        if not result.passed:
            break
    atomic_json(artifact_dir / "verification.json", [dump(result) for result in results])
    return results


def verification_signature(results: list[VerificationResult], artifact_dir: Path) -> str:
    material = []
    for result in results:
        material.append(str(result.exit_code))
        material.extend((result.stderr_sha256, result.stdout_sha256))
    return hashlib.sha256("\n".join(material).encode()).hexdigest()


class _HashCancelled(Exception):
    pass


def _hash_cancelled(cancel_path: Path | None,
                    stop_event: threading.Event | None) -> bool:
    return bool((stop_event is not None and stop_event.is_set()) or
                (cancel_path is not None and cancel_path.exists()))


def _file_sha256(path: Path, cancel_path: Path | None = None,
                 stop_event: threading.Event | None = None) -> str:
    digest = hashlib.sha256()
    if _hash_cancelled(cancel_path, stop_event):
        raise _HashCancelled
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            if _hash_cancelled(cancel_path, stop_event):
                raise _HashCancelled
            digest.update(chunk)
    return digest.hexdigest()


async def _file_sha256_async(path: Path,
                             cancel_path: Path | None = None) -> str | None:
    stop_event = threading.Event()
    try:
        return await asyncio.to_thread(_file_sha256, path, cancel_path, stop_event)
    except _HashCancelled:
        return None
    except asyncio.CancelledError:
        stop_event.set()
        raise


async def changed_paths(workspace: Path, artifact_dir: Path,
                        cancel_path: Path | None = None) -> list[str] | None:
    try:
        result = await run_process(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all",
             "--ignored=traditional", "--", *GIT_VISIBLE_PATHSPEC],
            workspace, artifact_dir, 10, cancel_path=cancel_path)
    except OSError:
        return None
    if result.exit_code != 0 or result.timed_out or result.cancelled:
        return None
    return parse_changed_paths((artifact_dir / "stdout.log").read_text(
        encoding="utf-8", errors="surrogateescape"))


async def capture_git_baseline(workspace: Path, baseline_path: Path,
                               cancel_path: Path | None = None,
                               include_ignored: bool = False) -> dict[str, Any]:
    """Atomically persist the initial Git HEAD and dirty-worktree identity."""
    workspace = workspace.resolve()
    snapshot = await _git_snapshot(
        workspace, baseline_path.parent / f"{baseline_path.stem}-git", cancel_path,
        include_ignored)
    baseline: dict[str, Any] = {
        "schema_version": GIT_BASELINE_SCHEMA,
        "kind": "git" if snapshot is not None else "unavailable",
        "workspace": str(workspace),
        "captured_at": utc_now(),
        "include_ignored": include_ignored,
    }
    if snapshot is not None:
        baseline.update(snapshot)
    atomic_json(baseline_path, baseline)
    return baseline


async def changed_paths_since_baseline(workspace: Path, baseline: dict[str, Any],
                                       artifact_dir: Path,
                                       cancel_path: Path | None = None) -> list[str] | None:
    """Return committed and worktree paths whose final state differs from baseline."""
    workspace = workspace.resolve()
    if (not isinstance(baseline, dict) or baseline.get("schema_version") != GIT_BASELINE_SCHEMA or
            baseline.get("kind") != "git" or
            not isinstance(baseline.get("dirty"), dict) or
            not isinstance(baseline.get("include_ignored"), bool)):
        return None
    try:
        if Path(str(baseline["workspace"])).resolve() != workspace:
            return None
    except (KeyError, OSError, TypeError, ValueError):
        return None
    baseline_head = baseline.get("head")
    if baseline_head is not None and (
            not isinstance(baseline_head, str) or not _valid_git_oid(baseline_head)):
        return None
    current = await _git_snapshot(
        workspace, artifact_dir / "current", cancel_path, baseline["include_ignored"])
    if current is None:
        return None
    committed = await _committed_paths(
        workspace, baseline_head, current["head"], artifact_dir / "committed", cancel_path)
    if committed is None:
        return None
    before, after = baseline["dirty"], current["dirty"]
    dirty = {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}
    return sorted(dirty | set(committed))


def changed_content_signature(workspace: Path, paths: list[str]) -> str | None:
    """Hash final identities for baseline-relative paths without storing file content."""
    return _changed_content_signature(workspace, paths)


def _changed_content_signature(workspace: Path, paths: list[str],
                               cancel_path: Path | None = None,
                               stop_event: threading.Event | None = None) -> str | None:
    if not paths:
        return ""
    material: dict[str, Any] = {}
    try:
        for raw in sorted(set(paths)):
            if _hash_cancelled(cancel_path, stop_event):
                raise _HashCancelled
            path = _git_path(raw)
            if path is None or not _visible_path(path):
                return None
            material[path] = _path_identity(workspace, path, cancel_path, stop_event)
    except OSError:
        return None
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


async def changed_content_signature_async(
        workspace: Path, paths: list[str],
        cancel_path: Path | None = None) -> str | None:
    """Hash content off-loop; return None when a run cancel marker stops the scan."""
    stop_event = threading.Event()
    try:
        return await asyncio.to_thread(
            _changed_content_signature, workspace, paths, cancel_path, stop_event)
    except _HashCancelled:
        return None
    except asyncio.CancelledError:
        stop_event.set()
        raise


async def _git_snapshot(workspace: Path, artifact_dir: Path,
                        cancel_path: Path | None,
                        include_ignored: bool = False) -> dict[str, Any] | None:
    status_dir, index_dir, head_dir = (
        artifact_dir / "status", artifact_dir / "index", artifact_dir / "head")
    try:
        argv = ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"]
        if include_ignored:
            argv.append("--ignored=traditional")
        argv.extend(["--", *GIT_VISIBLE_PATHSPEC])
        status = await run_process(
            argv, workspace, status_dir, 10, cancel_path=cancel_path)
    except OSError:
        return None
    if status.exit_code != 0 or status.timed_out or status.cancelled:
        return None
    entries = _parse_status_entries(_stdout(status_dir))
    if entries is None:
        return None
    try:
        index = await run_process(
            ["git", "ls-files", "--stage", "-z", "--", *GIT_VISIBLE_PATHSPEC],
            workspace, index_dir, 10, cancel_path=cancel_path)
        head = await run_process(["git", "rev-parse", "--verify", "HEAD"], workspace,
                                 head_dir, 10, cancel_path=cancel_path)
    except OSError:
        return None
    if (index.exit_code != 0 or index.timed_out or index.cancelled or
            head.timed_out or head.cancelled):
        return None
    staged = _parse_index_entries(_stdout(index_dir))
    if staged is None:
        return None
    identities = await _path_identities_async(workspace, list(entries), cancel_path)
    if identities is None:
        return None
    for path, entry in entries.items():
        entry["index"] = staged.get(path, [])
        entry["worktree"] = identities[path]
    head_value = head.stdout.strip() if head.exit_code == 0 else None
    if head_value is not None and not _valid_git_oid(head_value):
        return None
    return {"head": head_value or None, "dirty": entries}


async def _committed_paths(workspace: Path, before: str | None, after: str | None,
                           artifact_dir: Path,
                           cancel_path: Path | None) -> list[str] | None:
    if before == after:
        return []
    if after is None:
        return None
    argv = (["git", "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-z",
             "--relative", after, "--", *GIT_VISIBLE_PATHSPEC] if before is None else
            ["git", "diff", "--name-only", "-z", "--relative", before, after, "--",
             *GIT_VISIBLE_PATHSPEC])
    try:
        result = await run_process(argv, workspace, artifact_dir, 10, cancel_path=cancel_path)
    except OSError:
        return None
    if result.exit_code != 0 or result.timed_out or result.cancelled:
        return None
    return _parse_nul_paths(_stdout(artifact_dir))


def _stdout(artifact_dir: Path) -> str:
    return (artifact_dir / "stdout.log").read_text(
        encoding="utf-8", errors="surrogateescape")


def _parse_status_entries(output: str) -> dict[str, dict[str, Any]] | None:
    fields = output.split("\0")
    entries: dict[str, dict[str, Any]] = {}
    index = 0
    while index < len(fields) - 1:
        field = fields[index]
        index += 1
        if len(field) < 4 or field[2] != " ":
            return None
        status = field[:2]
        target = _git_path(field[3:])
        if target is None:
            return None
        if "R" in status or "C" in status:
            if index >= len(fields) - 1:
                return None
            source = _git_path(fields[index])
            index += 1
            if source is None:
                return None
            if _visible_path(target):
                entries[target] = {"status": status, "role": "target", "peer": source}
            if _visible_path(source):
                entries[source] = {"status": status, "role": "source", "peer": target}
        elif _visible_path(target):
            entries[target] = {"status": status, "role": "path"}
    return entries


def _parse_index_entries(output: str) -> dict[str, list[str]] | None:
    entries: dict[str, list[str]] = {}
    for field in output.split("\0")[:-1]:
        try:
            metadata, raw_path = field.split("\t", 1)
            mode, object_id, stage_number = metadata.split()
        except ValueError:
            return None
        path = _git_path(raw_path)
        if path is None:
            return None
        entries.setdefault(path, []).append(f"{mode}:{object_id}:{stage_number}")
    return entries


def _parse_nul_paths(output: str) -> list[str] | None:
    paths: list[str] = []
    for raw in output.split("\0")[:-1]:
        path = _git_path(raw)
        if path is None:
            return None
        if _visible_path(path) and path not in paths:
            paths.append(path)
    return paths


def _git_path(raw: str) -> str | None:
    path = raw.replace("\\", "/").rstrip("/")
    parsed = PurePosixPath(path)
    if not path or parsed.root or ".." in parsed.parts:
        return None
    return path


def _visible_path(path: str) -> bool:
    return path != ".hound" and not path.startswith(".hound/")


def _valid_git_oid(value: str) -> bool:
    return len(value) in {40, 64} and all(character in "0123456789abcdefABCDEF" for character in value)


def _path_identity(workspace: Path, relative: str,
                   cancel_path: Path | None = None,
                   stop_event: threading.Event | None = None) -> dict[str, Any]:
    if _hash_cancelled(cancel_path, stop_event):
        raise _HashCancelled
    path = workspace.joinpath(*PurePosixPath(relative).parts)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"kind": "missing"}
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        return {"kind": "symlink", "mode": mode,
                "sha256": hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()}
    if stat.S_ISREG(info.st_mode):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                if _hash_cancelled(cancel_path, stop_event):
                    raise _HashCancelled
                digest.update(chunk)
        return {"kind": "file", "mode": mode, "size": info.st_size,
                "sha256": digest.hexdigest()}
    if stat.S_ISDIR(info.st_mode):
        return {"kind": "directory", "mode": mode,
                "sha256": _directory_hash(path, cancel_path, stop_event)}
    return {"kind": "other", "mode": mode, "size": info.st_size}


def _directory_hash(root: Path, cancel_path: Path | None = None,
                    stop_event: threading.Event | None = None) -> str:
    digest = hashlib.sha256()
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        if _hash_cancelled(cancel_path, stop_event):
            raise _HashCancelled
        directories[:] = sorted(name for name in directories if name not in {".git", ".hound"})
        names = [*directories, *sorted(name for name in files if name not in {".git", ".hound"})]
        for name in names:
            if _hash_cancelled(cancel_path, stop_event):
                raise _HashCancelled
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            digest.update(f"{relative}\0{stat.S_IMODE(info.st_mode)}\0".encode())
            if stat.S_ISLNK(info.st_mode):
                digest.update(b"link\0" + os.fsencode(os.readlink(path)))
            elif stat.S_ISREG(info.st_mode):
                digest.update(b"file\0")
                with path.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        if _hash_cancelled(cancel_path, stop_event):
                            raise _HashCancelled
                        digest.update(chunk)
            else:
                digest.update(b"node\0")
    return digest.hexdigest()


def _path_identities(workspace: Path, paths: list[str],
                     cancel_path: Path | None = None,
                     stop_event: threading.Event | None = None) -> dict[str, dict[str, Any]]:
    identities: dict[str, dict[str, Any]] = {}
    for path in paths:
        if _hash_cancelled(cancel_path, stop_event):
            raise _HashCancelled
        identities[path] = _path_identity(workspace, path, cancel_path, stop_event)
    return identities


async def _path_identities_async(
        workspace: Path, paths: list[str],
        cancel_path: Path | None = None) -> dict[str, dict[str, Any]] | None:
    stop_event = threading.Event()
    try:
        return await asyncio.to_thread(
            _path_identities, workspace, paths, cancel_path, stop_event)
    except (OSError, _HashCancelled):
        return None
    except asyncio.CancelledError:
        stop_event.set()
        raise


def parse_changed_paths(output: str) -> list[str] | None:
    fields = output.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(fields) - 1:
        field = fields[index]
        index += 1
        if len(field) < 4 or field[2] != " ":
            return None
        status = field[:2]
        names = [field[3:]]
        if "R" in status or "C" in status:
            if index >= len(fields) - 1:
                return None
            names.append(fields[index])
            index += 1
        for raw in names:
            path = _git_path(raw)
            if path is None:
                return None
            if _visible_path(path) and path not in paths:
                paths.append(path)
    return paths


def matches_path(path: str, pattern: str) -> bool:
    path, pattern = path.replace("\\", "/"), pattern.replace("\\", "/")
    if pattern.endswith("/**"):
        root = pattern[:-3].rstrip("/")
        return path == root or path.startswith(root + "/")
    return PurePosixPath(path).match(pattern)


async def path_gate(workspace: Path, required: list[str], allowed: list[str], forbidden: list[str],
                    artifact_dir: Path, cancel_path: Path | None = None,
                    baseline: dict[str, Any] | None = None) -> list[str]:
    issues = [f"missing required file: {name}" for name in required if not (workspace / name).is_file()]
    if not allowed and not forbidden:
        return issues
    if baseline is not None and baseline.get("include_ignored") is not True:
        issues.append("could not inspect changed paths")
        return issues
    changes = (await changed_paths_since_baseline(
        workspace, baseline, artifact_dir, cancel_path) if baseline is not None else
        await changed_paths(workspace, artifact_dir, cancel_path))
    if changes is None:
        if allowed or forbidden:
            issues.append("could not inspect changed paths")
        return issues
    for path in changes:
        if any(matches_path(path, pattern) for pattern in forbidden):
            issues.append(f"forbidden path changed: {path}")
        if allowed and not any(matches_path(path, pattern) for pattern in allowed):
            issues.append(f"change outside allowed paths: {path}")
    return issues
