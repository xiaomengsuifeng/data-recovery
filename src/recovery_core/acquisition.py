"""Read-only, restartable acquisition with bounded child reads and bad ranges.

The source is never opened for writing. A private destination owns image.img
and acquisition.json; data is fsynced before publishing a successful range.
Missing sectors are explicit zero-filled holes, never called recovered data.
"""
from __future__ import annotations

import hashlib
import io
import math
import os
from pathlib import Path
import re
import shutil
import sys

from .common import RecoveryError, new_directory, read_json, regular_file, sha256_file
from .control import checkpoint, progress
from .journal import atomic_json, exclusive_job
from .tsk import run_bounded
from .windows import VolumeSource, DiskSource, volume_identity, disk_identity, check_volume, check_disk, ensure_safe_locations

MAX_RANGES = 200000


class ReadFailure(OSError):
    pass


def read_chunk(path: str, offset: int, size: int, timeout: float) -> bytes:
    output = io.BytesIO()
    executable = Path(sys.executable)
    # pythonw.exe deliberately has no stdout; use its console sibling hidden.
    if executable.name.lower() == "pythonw.exe":
        executable = executable.with_name("python.exe")
    try:
        run_bounded([str(executable), "-B", "-m", "recovery_core.device_reader", path, str(offset), str(size)],
                    output, limit=size, timeout=timeout)
    except RecoveryError as exc:
        if "SOURCE_UNAVAILABLE" in str(exc):
            raise RecoveryError("源设备不可访问、已断开或需要管理员权限；采集进度已保留。") from exc
        raise ReadFailure(str(exc)) from exc
    data = output.getvalue()
    if len(data) != size:
        raise ReadFailure("Source returned an incomplete read.")
    return data


def _identity(source):
    if isinstance(source, VolumeSource):
        return volume_identity(source.mount)
    if isinstance(source, DiskSource):
        return disk_identity(source.number)
    path = regular_file(Path(source))
    stat = path.stat()
    # Avoid reading a failing file twice before acquisition. On resume, verify
    # the file identity/mtime and all completed destination-range hashes.
    return dict(kind="acquisition_file", path=str(path), size=stat.st_size,
                inode=stat.st_ino, device=stat.st_dev, mtime_ns=stat.st_mtime_ns, sector_size=512)


def _check(expected):
    if expected.get("kind") == "windows_volume":
        check_volume(expected)
    elif expected.get("kind") == "windows_disk":
        check_disk(expected)
    elif expected.get("kind") == "acquisition_file":
        if _identity(Path(expected["path"])) != expected:
            raise RecoveryError("源文件已变化，拒绝把不同数据拼入同一镜像。")
    else:
        raise RecoveryError("Unsupported acquisition source identity.")


def _options(block_bytes, retries, timeout):
    if (type(block_bytes) is not int or not 512 <= block_bytes <= 64 * 1024 * 1024 or block_bytes % 512
            or type(retries) is not int or not 0 <= retries <= 10
            or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 300):
        raise RecoveryError("Invalid acquisition block size, retry count or timeout.")


def acquire(source, output: Path, *, block_bytes=16 * 1024 * 1024, retries=1, timeout=30, reader=None):
    _options(block_bytes, retries, timeout)
    identity = _identity(source)
    if not 0 < identity["size"] < 1 << 63 or block_bytes % identity["sector_size"]:
        raise RecoveryError("Source is empty or the acquisition block is not sector-aligned.")
    ensure_safe_locations(identity, output)
    if shutil.disk_usage(output.absolute().parent).free < identity["size"] + 16 * 1024 * 1024:
        raise RecoveryError("目标磁盘空间不足以保存完整镜像和采集记录。")
    directory = new_directory(output)
    image = directory / "image.img"
    with image.open("xb") as stream:
        stream.truncate(identity["size"])
        stream.flush()
        os.fsync(stream.fileno())
    stat = image.stat()
    state = dict(schema_version=1, kind="acquisition", acquisition_version=1, status="running",
                 source=identity, image=str(image), image_identity=dict(inode=stat.st_ino, device=stat.st_dev),
                 options=dict(block_bytes=block_bytes, retries=retries, timeout=timeout),
                 ranges=[dict(offset=0, size=identity["size"], status="pending", block_bytes=block_bytes, attempts=0)],
                 limitations=["Unreadable ranges contain zero placeholders, not recovered data.",
                              "Live device identity is checked; live filesystem contents can still change."])
    atomic_json(directory / "acquisition.json", state)
    return _run(directory, state, reader or read_chunk)


def _validate(state, directory):
    if (state.get("kind") != "acquisition" or state.get("acquisition_version") != 1
            or not isinstance(state.get("source"), dict) or not isinstance(state.get("options"), dict)
            or set(state["options"]) != {"block_bytes", "retries", "timeout"}
            or not isinstance(state.get("image_identity"), dict)):
        raise RecoveryError("Invalid acquisition progress file.")
    _options(**state["options"])
    image = directory / "image.img"
    if image.is_symlink() or not image.is_file() or str(image.resolve()) != state.get("image"):
        raise RecoveryError("采集镜像路径已改变，拒绝写入。")
    stat = image.stat()
    if stat.st_size != state["source"].get("size") or state["image_identity"] != dict(inode=stat.st_ino, device=stat.st_dev):
        raise RecoveryError("采集镜像身份或尺寸已改变，拒绝写入。")
    if state["source"].get("kind") == "acquisition_file" and os.path.samefile(image, state["source"]["path"]):
        raise RecoveryError("Source and destination refer to the same file.")
    ranges, at = state.get("ranges"), 0
    if not isinstance(ranges, list) or not 1 <= len(ranges) <= MAX_RANGES:
        raise RecoveryError("Invalid acquisition range map.")
    sector = state["source"].get("sector_size")
    if type(sector) is not int or sector not in (512, 1024, 2048, 4096):
        raise RecoveryError("Invalid acquisition sector size.")
    for item in ranges:
        if (not isinstance(item, dict) or type(item.get("offset")) is not int or item["offset"] != at
                or type(item.get("size")) is not int or item["size"] <= 0
                or item.get("status") not in ("good", "bad", "pending")):
            raise RecoveryError("Overlapping or incomplete acquisition range map.")
        if item["status"] == "good":
            if not isinstance(item.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
                raise RecoveryError("Invalid completed acquisition range hash.")
        elif (type(item.get("attempts")) is not int or not 0 <= item["attempts"] <= 11
              or type(item.get("block_bytes")) is not int or not sector <= item["block_bytes"] <= state["options"]["block_bytes"]
              or item["block_bytes"] % sector):
            raise RecoveryError("Invalid acquisition retry range.")
        at += item["size"]
    if at != stat.st_size:
        raise RecoveryError("Acquisition ranges do not cover the source size.")


def resume_acquisition(directory: Path, *, retry_bad=True, reader=None):
    directory = directory.resolve(strict=True)
    with exclusive_job(directory):
        state = read_json(directory / "acquisition.json")
        _validate(state, directory)
        _check(state["source"])
        ensure_safe_locations(state["source"], directory)
        with (directory / "image.img").open("rb") as stream:
            for item in state["ranges"]:
                if item["status"] == "good":
                    progress("acquisition_verify", item["offset"], state["source"]["size"], "核对已采集数据")
                    stream.seek(item["offset"])
                    digest, left = hashlib.sha256(), item["size"]
                    while left:
                        checkpoint()
                        block = stream.read(min(1024 * 1024, left))
                        if not block:
                            raise RecoveryError("Truncated acquisition image.")
                        digest.update(block)
                        left -= len(block)
                    if digest.hexdigest() != item["sha256"]:
                        raise RecoveryError("已采集数据校验失败，拒绝在被修改的镜像上继续。")
                elif item["status"] == "bad" and retry_bad:
                    item.update(status="pending", attempts=0)
        return _run_locked(directory, state, reader or read_chunk)


def _run(directory, state, reader):
    with exclusive_job(directory):
        return _run_locked(directory, state, reader)


def _run_locked(directory, state, reader):
    path, image = directory / "acquisition.json", directory / "image.img"
    _validate(state, directory)
    state["status"] = "running"
    state.pop("error", None)
    state.pop("image_sha256", None)
    options, source = state["options"], state["source"]
    ranges, sector = state["ranges"], source["sector_size"]
    checked_at = -128 * 1024 * 1024
    def save():
        for status in ("good", "bad", "pending"):
            state[status + "_bytes"] = sum(r["size"] for r in ranges if r["status"] == status)
        atomic_json(path, state)
    try:
        with image.open("r+b", buffering=0) as stream:
            def write_range(at, data):
                stream.seek(at)
                pending = memoryview(data)
                while pending:
                    checkpoint()
                    written = stream.write(pending)
                    if not written:
                        raise OSError("Destination did not accept acquisition bytes.")
                    pending = pending[written:]
                os.fsync(stream.fileno())

            while True:
                checkpoint()
                # Finish the coarse pass over healthy areas before returning
                # to progressively smaller reads around failures.
                index = max((i for i, r in enumerate(ranges) if r["status"] == "pending"),
                            key=lambda i: ranges[i]["block_bytes"], default=None)
                if index is None:
                    break
                if len(ranges) > MAX_RANGES - 2:
                    raise RecoveryError("采集坏区记录达到上限，进度已保留；请按分区缩小采集范围。")
                item = ranges[index]
                at, amount = item["offset"], min(item["size"], item["block_bytes"])
                progress("acquisition", sum(r["size"] for r in ranges if r["status"] != "pending"), source["size"],
                         f"只读采集，位置 {at:,} 字节；坏区 {state.get('bad_bytes', 0):,} 字节")
                if at - checked_at >= 128 * 1024 * 1024:
                    _check(source)
                    checked_at = at
                remainder = [] if amount == item["size"] else [dict(item, offset=at + amount, size=item["size"] - amount)]
                try:
                    data = reader(source["path"], at, amount, options["timeout"])
                    if not isinstance(data, bytes) or len(data) != amount:
                        raise ReadFailure("Incomplete source read.")
                except OSError as exc:
                    _check(source)  # A disconnected/replaced device is not just a bad sector.
                    if isinstance(exc, (PermissionError, FileNotFoundError)):
                        raise RecoveryError("Source is no longer accessible; acquisition paused.") from exc
                    if item["block_bytes"] > sector:
                        smaller = max(sector, min(item["block_bytes"] // 2, 65536 if item["block_bytes"] > 65536 else 4096 if item["block_bytes"] > 4096 else sector))
                        smaller = max(sector, smaller // sector * sector)
                        replacement = dict(item, size=amount, block_bytes=smaller, attempts=0)
                    else:
                        attempts = item["attempts"] + 1
                        replacement = dict(item, size=amount, attempts=attempts, error=str(exc)[:1024],
                                           status="bad" if attempts > options["retries"] else "pending")
                        if replacement["status"] == "bad":
                            write_range(at, bytes(amount))
                    ranges[index:index + 1] = [replacement, *remainder]
                    save()
                    continue
                write_range(at, data)
                ranges[index:index + 1] = [dict(offset=at, size=amount, status="good", sha256=hashlib.sha256(data).hexdigest()), *remainder]
                save()
            _check(source)
        state["image_sha256"] = sha256_file(image)
        _check(source)
        state["status"] = "completed_with_errors" if any(r["status"] == "bad" for r in ranges) else "completed"
    except KeyboardInterrupt:
        state["status"] = "cancelled"
    except (RecoveryError, OSError) as exc:
        state["status"] = "failed"
        state["error"] = str(exc)
    save()
    return state
