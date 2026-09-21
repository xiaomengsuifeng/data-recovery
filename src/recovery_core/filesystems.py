"""Read-only NTFS/exFAT identification and bounded exFAT allocation metadata.

exFAT layout follows Microsoft's exFAT File System Specification, sections
3, 4 and 7.1. No source handle in this module has write access.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .common import RecoveryError
from .control import checkpoint


def boot_format(boot: bytes) -> tuple[str, int]:
    if len(boot) < 512 or boot[510:512] != b"\x55\xaa":
        raise RecoveryError("无效的 NTFS / exFAT 文件系统引导扇区。")
    if boot[3:11] == b"NTFS    ":
        kind, sector = "ntfs", int.from_bytes(boot[11:13], "little")
    elif boot[3:11] == b"EXFAT   " and 9 <= boot[108] <= 12:
        kind, sector = "exfat", 1 << boot[108]
    else:
        raise RecoveryError("未识别到 NTFS / exFAT 引导信息；加密或其他文件系统不受支持。")
    if sector not in (512, 1024, 2048, 4096):
        raise RecoveryError("不支持此文件系统的扇区尺寸。")
    return kind, sector


def identify(image: Path, offset: int, sector_size: int) -> str:
    if type(offset) is not int or offset < 0 or type(sector_size) is not int or sector_size not in (512, 1024, 2048, 4096):
        raise RecoveryError("Invalid filesystem geometry.")
    with image.open("rb", buffering=0) as stream:
        stream.seek(offset * sector_size)
        kind, actual_sector = boot_format(stream.read(sector_size))
    if actual_sector != sector_size:
        raise RecoveryError("Filesystem sector size differs from --sector-size.")
    return kind


@dataclass(frozen=True)
class Exfat:
    origin: int
    sector_size: int
    sectors: int
    fat: int
    fat_sectors: int
    heap: int
    cluster_size: int
    cluster_count: int
    root: int

    @property
    def start(self):
        return self.origin + self.heap * self.sector_size

    def cluster_offset(self, cluster: int):
        if not 2 <= cluster < self.cluster_count + 2:
            raise RecoveryError("exFAT cluster is outside the cluster heap.")
        return self.start + (cluster - 2) * self.cluster_size


def exfat_geometry(image: Path, offset: int, sector_size: int, *, length: int | None = None) -> Exfat:
    if identify(image, offset, sector_size) != "exfat":
        raise RecoveryError("An exFAT volume is required.")
    origin = offset * sector_size
    with image.open("rb", buffering=0) as stream:
        stream.seek(origin)
        data = stream.read(12 * sector_size)
    if len(data) != 12 * sector_size:
        raise RecoveryError("Truncated exFAT boot region.")
    boot = data[:512]
    checksum = 0
    for index, value in enumerate(data[:11 * sector_size]):
        if index not in (106, 107, 112):
            checksum = (((checksum >> 1) | ((checksum & 1) << 31)) + value) & 0xffffffff
    if data[11 * sector_size:] != checksum.to_bytes(4, "little") * (sector_size // 4):
        raise RecoveryError("exFAT boot checksum failed; automatic layout guessing is disabled.")
    u32 = lambda at: int.from_bytes(boot[at:at + 4], "little")
    sectors = int.from_bytes(boot[72:80], "little")
    fat, fat_length, heap, count, root = (u32(at) for at in (80, 84, 88, 92, 96))
    if boot[110] != 1 or boot[106] & 1:
        raise RecoveryError("Transactional TexFAT volumes are not supported; use standard exFAT.")
    if (boot[:3] != b"\xeb\x76\x90" or any(boot[11:64]) or boot[104:106] != b"\x00\x01"
            or boot[109] > 25 - boot[108] or fat < 24 or not fat_length
            or heap < fat + fat_length or not 0 < count <= 0xfffffff5
            or not 2 <= root < count + 2 or fat_length * sector_size < (count + 2) * 4
            or heap + (count << boot[109]) > sectors):
        raise RecoveryError("Invalid or unsupported exFAT boot geometry.")
    if length is None:
        length = image.stat().st_size
    if origin + sectors * sector_size > length:
        raise RecoveryError("exFAT volume extends outside the source.")
    return Exfat(origin, sector_size, sectors, fat, fat_length, heap,
                 sector_size << boot[109], count, root)


def exfat_bitmap(image: Path, volume: Exfat) -> bytes:
    required = (volume.cluster_count + 7) // 8
    maximum = 64 * 1024 * 1024
    if required > maximum:
        raise RecoveryError("exFAT allocation bitmap exceeds the 64 MiB scanning limit.")
    with image.open("rb") as stream:
        def read(at, size):
            checkpoint()
            stream.seek(at)
            data = stream.read(size)
            if len(data) != size:
                raise RecoveryError("Truncated exFAT allocation metadata.")
            return data

        def chain(first, byte_limit):
            seen = set()
            current = first
            while current != 0xffffffff:
                if current in seen or len(seen) * volume.cluster_size >= byte_limit:
                    raise RecoveryError("exFAT metadata chain loops or exceeds the read limit.")
                seen.add(current)
                position = volume.cluster_offset(current)
                yield read(position, volume.cluster_size)
                current = int.from_bytes(read(volume.origin + volume.fat * volume.sector_size + current * 4, 4), "little")

        entries = []
        for block in chain(volume.root, maximum):
            stop = False
            for index in range(0, len(block), 32):
                entry = block[index:index + 32]
                if entry[0] == 0:
                    stop = True
                    break
                if entry[0] == 0x81:
                    entries.append(entry)
            if stop:
                break
        if len(entries) != 1 or entries[0][1] & 1:
            raise RecoveryError("exFAT must have one unambiguous active allocation bitmap.")
        entry = entries[0]
        first = int.from_bytes(entry[20:24], "little")
        size = int.from_bytes(entry[24:32], "little")
        if size != required:
            raise RecoveryError("Unexpected exFAT allocation bitmap length.")
        result = bytearray()
        for block in chain(first, ((required + volume.cluster_size - 1) // volume.cluster_size) * volume.cluster_size):
            result.extend(block[:required - len(result)])
            if len(result) == required:
                break
        if len(result) != required:
            raise RecoveryError("Incomplete exFAT allocation bitmap chain.")
        return bytes(result)
