from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from .control import checkpoint, progress


class RecoveryError(Exception):
    """Expected operational failure that can be shown without a traceback."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    total = path.stat().st_size
    completed = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checkpoint()
            digest.update(block)
            completed += len(block)
            if completed % (16 * 1024 * 1024) == 0 or completed == total:
                progress("hash", completed, total)
    return digest.hexdigest()


def regular_file(path: Path) -> Path:
    path = path.resolve(strict=True)
    if not stat.S_ISREG(path.stat().st_mode):
        raise RecoveryError("Only regular image files are supported; raw devices are disabled.")
    return path


def image_identity(path: Path) -> dict:
    path = regular_file(path)
    before = path.stat()
    digest = sha256_file(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
        after.st_size, after.st_mtime_ns, after.st_ino
    ):
        raise RecoveryError("The image changed while it was being hashed. Use a detached copy.")
    return {"path": str(path), "size": after.st_size, "sha256": digest}


def check_identity(expected: dict) -> dict:
    if expected.get("kind") == "windows_volume":
        from .windows import check_volume
        return check_volume(expected)
    actual = image_identity(Path(expected["path"]))
    if (actual["size"], actual["sha256"]) != (expected["size"], expected["sha256"]):
        raise RecoveryError("The source image changed since scanning; start a new session.")
    return actual


def new_directory(path: Path) -> Path:
    # Only create the requested leaf. A fresh private directory avoids overwrites
    # and pre-existing symlinks inside paths derived from recovered metadata.
    parent = path.absolute().parent.resolve(strict=True)
    target = parent / path.name
    try:
        target.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise RecoveryError(f"Output must be a new directory: {target}") from exc
    return target


def write_json(path: Path, value: dict) -> None:
    # Callers use newly created output roots. Exclusive creation is intentional.
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def read_json(path: Path) -> dict:
    if not stat.S_ISREG(path.stat().st_mode):
        raise RecoveryError("JSON input must be a regular file.")
    if path.stat().st_size > 64 * 1024 * 1024:
        raise RecoveryError("JSON input exceeds the prototype's 64 MiB limit.")
    with path.open(encoding="utf-8") as stream:
        result = json.load(stream)
    if not isinstance(result, dict) or result.get("schema_version") != 1:
        raise RecoveryError("Unsupported report schema.")
    return result
