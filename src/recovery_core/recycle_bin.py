"""Strict, read-only parsing of Windows Recycle Bin metadata.

Format reference:
https://github.com/libyal/dtformats/blob/main/documentation/Windows%20Recycle.Bin%20file%20formats.asciidoc

A parsed record is an untrusted metadata claim, not proof that a recovered
payload came from its original path. Pair keys must also be scoped to the same
source image and volume by the caller. No file or device is opened here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
import struct


class InvalidRecycleMetadata(ValueError):
    """The record is unsupported, incomplete, or structurally ambiguous."""


@dataclass(frozen=True, slots=True)
class RecycleMetadata:
    """Values claimed by a structurally valid $I record.

    ``deleted_at`` is UTC ISO 8601, retaining FILETIME's seven decimal places.
    It describes the recycle event, not necessarily when the bin was emptied.
    """

    original_path: str
    original_size: int
    deleted_at: str
    version: int


_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
_TICKS_PER_SECOND = 10_000_000
_MAX_PATH_UNITS = 32_768  # Includes the required terminating UTF-16 NUL.
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_DRIVE_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_SID = re.compile(r"S-1-(?:0|[1-9][0-9]*)(?:-(?:0|[1-9][0-9]*)){1,15}", re.ASCII | re.IGNORECASE)
_RECYCLED_NAME = re.compile(r"\$[IR][A-Z0-9]{6}(?:\.[^\\/:*?\"<>|]+)?", re.ASCII | re.IGNORECASE)


def _filetime_string(value: int) -> str:
    seconds, fraction = divmod(value, _TICKS_PER_SECOND)
    try:
        moment = _FILETIME_EPOCH + timedelta(seconds=seconds)
    except (OverflowError, ValueError) as exc:
        raise InvalidRecycleMetadata("Deletion FILETIME is outside the supported calendar range") from exc
    return f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}T{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}.{fraction:07d}Z"


def _check_original_path(path: str) -> None:
    """Check path syntax without normalizing or granting it export authority."""
    if not path or any(ord(character) < 32 for character in path):
        raise InvalidRecycleMetadata("Original path is empty or contains control characters")
    canonical = path.replace("/", "\\")
    if _DRIVE_ABSOLUTE.match(canonical):
        parts = canonical[3:].split("\\")
    elif canonical.startswith("\\\\") and not canonical.startswith(("\\\\?\\", "\\\\.\\")):
        parts = canonical[2:].split("\\")
        if len(parts) < 3:  # Server, share, and an item within the share.
            raise InvalidRecycleMetadata("Original UNC path does not identify an item")
    else:
        raise InvalidRecycleMetadata("Original path is not an absolute Windows item path")
    if any(not part or part in (".", "..") or ":" in part for part in parts):
        raise InvalidRecycleMetadata("Original path contains an ambiguous component")
    if any(character in '*?"<>|' for character in canonical):
        raise InvalidRecycleMetadata("Original path contains invalid Windows characters")


def parse_dollar_i(data: bytes) -> RecycleMetadata:
    """Parse one complete version 1 or 2 $I record, rejecting truncation.

    Version 1 must contain the full 260-code-unit path field (544 bytes total).
    Version 2 must have exactly its declared number of UTF-16 code units,
    including one final NUL. Trailing data, invalid surrogates, unsupported
    versions, and the anomalous 543-byte legacy variant are rejected.

    File size is the format's unsigned 64-bit value; it is not used to allocate
    memory and must be independently compared with the recovered payload.
    """
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")
    if len(data) < 24:
        raise InvalidRecycleMetadata("Truncated $I header")
    version, original_size, deleted_time = struct.unpack_from("<QQQ", data)
    if version == 1:
        if len(data) != 544:
            raise InvalidRecycleMetadata("Version 1 requires exactly 544 bytes")
        encoded_path = data[24:]
    elif version == 2:
        if len(data) < 28:
            raise InvalidRecycleMetadata("Truncated version 2 path length")
        code_units = struct.unpack_from("<I", data, 24)[0]
        if not 2 <= code_units <= _MAX_PATH_UNITS:
            raise InvalidRecycleMetadata("Version 2 path length is outside the supported range")
        if len(data) != 28 + code_units * 2:
            raise InvalidRecycleMetadata("Version 2 data does not match its declared path length")
        encoded_path = data[28:]
    else:
        raise InvalidRecycleMetadata(f"Unsupported $I version: {version}")

    try:
        decoded = encoded_path.decode("utf-16-le", errors="strict")
    except UnicodeDecodeError as exc:
        raise InvalidRecycleMetadata("Original path is not valid UTF-16LE") from exc
    original_path, separator, padding = decoded.partition("\0")
    if not separator:
        raise InvalidRecycleMetadata("Original path is missing its NUL terminator")
    if version == 1 and any(character != "\0" for character in padding):
        raise InvalidRecycleMetadata("Version 1 contains nonzero data after the path")
    if version == 2 and padding:
        raise InvalidRecycleMetadata("Version 2 must have exactly one final NUL terminator")
    _check_original_path(original_path)
    return RecycleMetadata(original_path, original_size, _filetime_string(deleted_time), version)


def pair_key(path: str) -> tuple[str, str] | None:
    """Return a conservative (recycle-directory, suffix) candidate key.

    Accepts TSK paths relative to the volume root, optionally root-prefixed, and
    ordinary drive-absolute Windows paths. Only direct children of
    ``$Recycle.Bin/<SID>`` are recognized. A matching key is a candidate link;
    callers must reject nonunique matches and scope matches to one volume.

    ASCII case is normalized. Non-ASCII characters remain literal: Python's
    Unicode casefold is not the source volume's NTFS $UpCase table and could
    conflate distinct names. This can conservatively miss Unicode variants.
    """
    if not isinstance(path, str) or not path or any(ord(character) < 32 for character in path):
        return None
    normalized = path.replace("\\", "/")
    if normalized.startswith("//"):
        return None  # UNC and device namespaces do not establish volume identity.
    volume = ""
    if _DRIVE_ABSOLUTE.match(normalized):
        volume = normalized[:2].translate(_ASCII_LOWER)
        normalized = normalized[3:]
    elif normalized.startswith("/"):
        normalized = normalized[1:]
    if ":" in normalized:
        return None  # Reject drive-relative paths and alternate data streams.
    parts = normalized.split("/")
    if len(parts) != 3 or any(not part or part.endswith((" ", ".")) for part in parts):
        return None
    recycle_dir, sid, filename = parts
    if recycle_dir.translate(_ASCII_LOWER) != "$recycle.bin" or not _SID.fullmatch(sid):
        return None
    sid_numbers = sid.split("-")[2:]
    if len(sid_numbers[0]) > 15 or any(len(value) > 10 for value in sid_numbers[1:]):
        return None  # Bound conversion even for adversarial multi-thousand-digit SIDs.
    if int(sid_numbers[0]) > (1 << 48) - 1 or any(int(value) > (1 << 32) - 1 for value in sid_numbers[1:]):
        return None
    if not _RECYCLED_NAME.fullmatch(filename):
        return None
    parent = f"{volume}/$recycle.bin/{sid.translate(_ASCII_LOWER)}"
    return parent, filename[2:].translate(_ASCII_LOWER)
