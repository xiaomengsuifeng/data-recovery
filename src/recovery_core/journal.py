"""Durable, bounded progress files and exclusive ownership of resumable work."""
from __future__ import annotations

import copy
import json
import os
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .common import RecoveryError

_stage = ContextVar("scan_stage", default=None)


def atomic_json(path: Path, value: dict):
    data = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(data) > 64 * 1024 * 1024:
        raise RecoveryError("Progress file exceeds the 64 MiB limit.")
    if path.is_symlink():
        raise RecoveryError("Progress file must not be a symbolic link.")
    fd, name = tempfile.mkstemp(prefix=".progress-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def exclusive_job(directory: Path):
    path = directory / ".job-lock"
    if path.is_symlink():
        raise RecoveryError("Invalid job lock.")
    with path.open("a+b") as stream:
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RecoveryError("此任务已在另一个窗口运行，请等待其结束。") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def partial() -> dict | None:
    context = _stage.get()
    return copy.deepcopy(context[0]["stages"].get(context[1], {}).get("partial")) if context else None


def save_partial(value: dict):
    context = _stage.get()
    if context:
        state, name, path = context
        state["stages"][name] = {"partial": copy.deepcopy(value)}
        atomic_json(path, state)


def run_stage(state: dict, name: str, path: Path | None, function):
    if name in state["stages"] and "result" in state["stages"][name]:
        return copy.deepcopy(state["stages"][name]["result"])
    token = _stage.set((state, name, path) if path else None)
    try:
        value = function()
    finally:
        _stage.reset(token)
    state["stages"][name] = {"result": copy.deepcopy(value)}
    if path:
        atomic_json(path, state)
    return value
