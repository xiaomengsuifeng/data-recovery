"""Conservative PNG carving from contiguous, unallocated NTFS image clusters.

Only the affected image and its current $Bitmap are inputs. PNG chunk framing
and CRCs establish a byte range, never an original filename or content match.
This is not a PNG decoder, fragmented-file reconstruction, or log recovery.
"""
from __future__ import annotations

import hashlib
import io
import re
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

from .common import RecoveryError
from .control import checkpoint, progress
from .filesystems import identify, exfat_geometry, exfat_bitmap
from .journal import partial, save_partial

SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_PNG_BYTES = 256 * 1024 * 1024
MAX_BITMAP_BYTES = 64 * 1024 * 1024
MAX_CHUNKS = 100000
MAX_SIGNATURE_CHECKS = 100000
SCAN_BLOCK = 1024 * 1024
READ_BLOCK = 64 * 1024


@dataclass(frozen=True)
class Geometry:
    start: int
    cluster_size: int
    cluster_count: int

    @property
    def end(self):
        return self.start + self.cluster_size * self.cluster_count


def geometry(image: Path, offset: int, sector_size: int) -> Geometry:
    if (type(offset) is not int or offset < 0 or type(sector_size) is not int
            or sector_size not in (512, 1024, 2048, 4096)):
        raise RecoveryError("Invalid PNG carving source geometry.")
    if identify(image, offset, sector_size) == "exfat":
        value = exfat_geometry(image, offset, sector_size)
        return Geometry(value.start, value.cluster_size, value.cluster_count)
    with image.open("rb") as stream:
        stream.seek(offset * sector_size)
        boot = stream.read(512)
    if (len(boot) != 512 or boot[3:11] != b"NTFS    " or boot[510:512] != b"\x55\xaa"
            or int.from_bytes(boot[11:13], "little") != sector_size):
        raise RecoveryError("PNG carving requires a valid NTFS image boot sector.")
    sectors_per_cluster = boot[13]
    if sectors_per_cluster not in (1, 2, 4, 8, 16, 32, 64, 128):
        raise RecoveryError("Unsupported NTFS cluster size for PNG carving.")
    sectors = int.from_bytes(boot[40:48], "little")
    result = Geometry(offset * sector_size, sector_size * sectors_per_cluster,
                      sectors // sectors_per_cluster)
    if not result.cluster_count or offset * sector_size + sectors * sector_size > image.stat().st_size:
        raise RecoveryError("NTFS volume is empty or extends outside the image; PNG carving refused.")
    return result


def allocation_bitmap(image: Path, offset: int, sector_size: int, volume: Geometry, backend) -> bytes:
    if identify(image, offset, sector_size) == "exfat":
        return exfat_bitmap(image, exfat_geometry(image, offset, sector_size))
    # NTFS $Bitmap is MFT record 6, with one low-bit-first bit per cluster.
    # Its data attribute is rounded to an eight-byte boundary by Windows.
    required = (volume.cluster_count + 7) // 8
    rounded = (required + 7) // 8 * 8
    if rounded > MAX_BITMAP_BYTES:
        raise RecoveryError("NTFS allocation bitmap exceeds the 64 MiB PNG carving limit.")
    with io.BytesIO() as output:
        backend.extract_bitmap(image, offset, sector_size, output, rounded)
        bitmap = output.getvalue()
    if not required <= len(bitmap) <= rounded:
        raise RecoveryError("NTFS allocation bitmap has an unexpected length; PNG carving refused.")
    return bitmap[:required]


def free_runs(bitmap: bytes, volume: Geometry):
    """Yield physical byte ranges; never join across an allocated cluster."""
    start = None
    for byte_index, value in enumerate(bitmap):
        if byte_index % 4096 == 0:
            checkpoint()
        first = byte_index * 8
        count = min(8, volume.cluster_count - first)
        if value == 0:
            if start is None:
                start = first
            continue
        if value == 255:
            if start is not None:
                yield volume.start + start * volume.cluster_size, volume.start + first * volume.cluster_size
                start = None
            continue
        for bit in range(count):
            cluster = first + bit
            if value & (1 << bit):
                if start is not None:
                    yield volume.start + start * volume.cluster_size, volume.start + cluster * volume.cluster_size
                    start = None
            elif start is None:
                start = cluster
    if start is not None:
        yield volume.start + start * volume.cluster_size, volume.end


def _unallocated(bitmap: bytes, volume: Geometry, start: int, end: int) -> bool:
    if not volume.start <= start < end <= volume.end:
        return False
    first, last = (start - volume.start) // volume.cluster_size, (end - 1 - volume.start) // volume.cluster_size
    for cluster in range(first, last + 1):
        if cluster % 4096 == 0:
            checkpoint()
        if bitmap[cluster // 8] & (1 << (cluster % 8)):
            return False
    return True


class InvalidPng(ValueError):
    pass


def inspect_png(stream, start: int, end: int, *, budget: list[int] | None = None) -> dict:
    """Check critical chunk order, IHDR fields, all CRCs and an exact IEND.

    The hard byte/chunk limits also apply when reopening a saved session.
    No decompression is performed; chunk validity alone is not image integrity.
    """
    end = min(end, start + MAX_PNG_BYTES)
    stream.seek(start)
    position = start
    digest = hashlib.sha256()

    def read(size):
        nonlocal position
        checkpoint()
        if size < 0 or position + size > end:
            raise InvalidPng("PNG is truncated or exceeds the supported byte range.")
        if budget is not None:
            if budget[0] < size:
                raise RecoveryError("PNG signature validation byte budget exceeded; scan incomplete.")
            budget[0] -= size
        data = stream.read(size)
        if len(data) != size:
            raise RecoveryError("Source image became unreadable during PNG validation.")
        position += size
        digest.update(data)
        return data

    if read(8) != SIGNATURE:
        raise InvalidPng("Missing PNG signature.")
    header = None
    palette = False
    idat_seen = idat_ended = False
    data_bytes = 0
    for index in range(MAX_CHUNKS):
        size, kind = struct.unpack(">I4s", read(8))
        if size > 0x7fffffff or not re.fullmatch(b"[A-Za-z]{4}", kind) or kind[2] & 32:
            raise InvalidPng("Unsupported PNG chunk type or length.")
        if (index == 0 and (kind != b"IHDR" or size != 13)) or (index and kind == b"IHDR"):
            raise InvalidPng("PNG must begin with one IHDR chunk.")
        if kind in (b"acTL", b"fcTL", b"fdAT"):
            raise InvalidPng("Animated PNG carving is not supported.")
        if not kind[0] & 32 and kind not in (b"IHDR", b"PLTE", b"IDAT", b"IEND"):
            raise InvalidPng("Unknown critical PNG chunk.")
        if kind == b"PLTE":
            if palette or idat_seen or header[3] in (0, 4) or not 3 <= size <= 768 or size % 3:
                raise InvalidPng("Invalid PNG palette.")
            if header[3] == 3 and size // 3 > 1 << header[2]:
                raise InvalidPng("PNG palette exceeds indexed bit depth.")
            palette = True
        if kind == b"IDAT":
            if idat_ended or (header[3] == 3 and not palette):
                raise InvalidPng("Invalid PNG image-data order.")
            idat_seen = True
            data_bytes += size
        elif idat_seen:
            idat_ended = True
        if kind == b"IEND" and (size or not idat_seen or not data_bytes):
            raise InvalidPng("PNG lacks image data or has an invalid IEND.")
        if position + size + 4 > end:
            raise InvalidPng("PNG chunk extends beyond its unallocated range or size limit.")
        crc = zlib.crc32(kind)
        remaining = size
        while remaining:
            data = read(min(READ_BLOCK, remaining))
            crc = zlib.crc32(data, crc)
            if kind == b"IHDR":
                header = struct.unpack(">IIBBBBB", data)
                width, height, depth, colour, compression, filtering, interlace = header
                depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
                if (not 0 < width <= 0x7fffffff or not 0 < height <= 0x7fffffff
                        or depth not in depths.get(colour, ()) or compression or filtering or interlace not in (0, 1)):
                    raise InvalidPng("Unsupported PNG IHDR fields.")
            remaining -= len(data)
        if int.from_bytes(read(4), "big") != crc:
            raise InvalidPng("PNG chunk CRC mismatch.")
        if kind == b"IEND":
            return {"size": position - start, "sha256": digest.hexdigest()}
    raise InvalidPng("PNG exceeds the supported chunk count.")


def observed_path(start: int) -> str:
    return f"/Carved/PNG/png_{start:016x}.png"


def scan_png(image: Path, offset: int, sector_size: int, *, backend, max_candidates: int):
    volume = geometry(image, offset, sector_size)
    progress("carving", message="读取文件系统未分配空间信息")
    bitmap = allocation_bitmap(image, offset, sector_size, volume, backend)
    candidates = []
    scanned_bytes = attempts = 0
    # Bound repeated parsing of overlapping false signatures to two volume
    # reads, in addition to the single pass searching unallocated clusters.
    budget = [2 * (volume.end - volume.start)]
    saved = partial()
    cursor = volume.start
    accepted_end = volume.start
    if saved is not None:
        candidates, cursor, accepted_end = saved["candidates"], saved["cursor"], saved["accepted_end"]
        scanned_bytes, attempts, budget[0] = saved["scanned_bytes"], saved["attempts"], saved["budget"]
        if (not isinstance(candidates, list) or len(candidates) > max_candidates
                or any(type(n) is not int for n in (cursor, accepted_end, scanned_bytes, attempts, budget[0]))
                or not volume.start <= cursor <= volume.end or not volume.start <= accepted_end <= volume.end
                or not 0 <= attempts <= MAX_SIGNATURE_CHECKS or not 0 <= scanned_bytes <= volume.end - volume.start
                or not 0 <= budget[0] <= 2 * (volume.end - volume.start)):
            raise RecoveryError("Invalid PNG scan checkpoint.")
        for item in candidates:
            validate_candidate(item)
    with image.open("rb") as stream:
        for start, end in free_runs(bitmap, volume):
            if cursor >= end:
                continue
            position, tail = max(start, cursor), b""
            accepted_end = max(accepted_end, start)
            if position > start:
                stream.seek(max(start, position - 7))
                tail = stream.read(min(7, position - start))
            while position < end:
                progress("carving", position - volume.start, volume.end - volume.start, "查找 PNG 内容")
                stream.seek(position)
                block = stream.read(min(SCAN_BLOCK, end - position))
                if not block:
                    raise RecoveryError("Source image became unreadable during PNG scanning.")
                scanned_bytes += len(block)
                data = tail + block
                base = position - len(tail)
                search = 0
                while (found := data.find(SIGNATURE, search)) != -1:
                    checkpoint()
                    search = found + len(SIGNATURE)
                    absolute = base + found
                    if absolute < accepted_end:
                        continue
                    attempts += 1
                    if attempts > MAX_SIGNATURE_CHECKS:
                        raise RecoveryError("PNG signature count limit exceeded; scan incomplete.")
                    try:
                        info = inspect_png(stream, absolute, end, budget=budget)
                    except InvalidPng:
                        continue
                    if len(candidates) >= max_candidates:
                        raise RecoveryError("PNG carving exceeds --max-candidates; increase it explicitly.")
                    candidates.append({
                        "inode": None, "kind": "file", "size": info["size"],
                        "observed_path": observed_path(absolute), "original_path": None,
                        "path_evidence": "content_only", "recovery_method": "png_carving",
                        "carving": {"format": "png", "image_offset": absolute,
                                    "sha256": info["sha256"], "allocation": "unallocated_clusters",
                                    "validation": "chunk_structure_and_crc"},
                        "warnings": ["按 PNG 内容生成名称，原文件名与目录未知。",
                                     "块结构与 CRC 通过，尚未证明与删除前内容一致；可能与其他候选重复，或是文件内嵌图片。"],
                    })
                    accepted_end = absolute + info["size"]
                tail = data[-7:]
                position += len(block)
                save_partial(dict(candidates=candidates, cursor=position, accepted_end=accepted_end,
                                  scanned_bytes=scanned_bytes, attempts=attempts, budget=budget[0]))
    progress("carving", volume.end - volume.start, volume.end - volume.start, "PNG 内容扫描完成")
    return candidates, {"status": "completed", "format": "png", "scope": identify(image, offset, sector_size) + "_unallocated_clusters",
                        "candidate_count": len(candidates), "scanned_bytes": scanned_bytes,
                        "signature_checks": attempts, "max_file_bytes": MAX_PNG_BYTES}


def validate_candidate(candidate: dict) -> None:
    info = candidate.get("carving")
    if not isinstance(info, dict) or info.get("format") != "png":
        raise RecoveryError("Invalid PNG carving descriptor.")
    start = info.get("image_offset")
    if (type(start) is not int or not 0 <= start < 1 << 63
            or type(candidate.get("size")) is not int or not 58 <= candidate["size"] <= MAX_PNG_BYTES
            or candidate.get("inode") is not None or candidate.get("original_path") is not None
            or candidate.get("kind") != "file" or candidate.get("path_evidence") != "content_only"
            or candidate.get("observed_path") != observed_path(start)
            or not isinstance(info.get("sha256"), str) or not re.fullmatch("[0-9a-f]{64}", info["sha256"])
            or info.get("allocation") != "unallocated_clusters" or info.get("validation") != "chunk_structure_and_crc"):
        raise RecoveryError("Invalid or unsupported PNG carving candidate.")


def extract_png(image: Path, offset: int, sector_size: int, candidate: dict, output, *, backend) -> None:
    validate_candidate(candidate)
    volume = geometry(image, offset, sector_size)
    start = candidate["carving"]["image_offset"]
    end = start + candidate["size"]
    bitmap = allocation_bitmap(image, offset, sector_size, volume, backend)
    if not _unallocated(bitmap, volume, start, end):
        raise RecoveryError("PNG byte range is outside unallocated NTFS clusters; rescan the image.")
    with image.open("rb") as stream:
        try:
            info = inspect_png(stream, start, end)
        except InvalidPng as exc:
            raise RecoveryError(f"PNG candidate no longer validates: {exc}") from exc
        if info["size"] != candidate["size"] or info["sha256"] != candidate["carving"]["sha256"]:
            raise RecoveryError("PNG candidate differs from its scan; rescan the image.")
        stream.seek(start)
        remaining = candidate["size"]
        copied_digest = hashlib.sha256()
        while remaining:
            checkpoint()
            block = stream.read(min(READ_BLOCK, remaining))
            if not block:
                raise RecoveryError("Source image became unreadable during PNG extraction.")
            output.write(block)
            copied_digest.update(block)
            remaining -= len(block)
        if copied_digest.hexdigest() != info["sha256"]:
            raise RecoveryError("PNG bytes changed during extraction; the saved result is partial.")
