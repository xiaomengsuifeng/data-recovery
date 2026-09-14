"""Bounded read-only discovery of NTFS volumes in raw MBR/GPT images."""
from __future__ import annotations

from pathlib import Path
import struct
import zlib

from .common import RecoveryError, regular_file


def inspect_image(path: Path) -> list[dict]:
    path = regular_file(path)
    length = path.stat().st_size
    found = []
    seen = set()
    with path.open("rb") as source:
        def read(offset, size):
            if offset < 0 or offset + size > length:
                return b""
            source.seek(offset)
            return source.read(size)

        def probe(lba, sector_size, label, partition_sectors=None):
            key = lba * sector_size
            boot = read(key, 512)
            if len(boot) != 512 or boot[3:11] != b"NTFS    " or boot[510:512] != b"\x55\xaa":
                return
            bps = int.from_bytes(boot[11:13], "little")
            sectors = int.from_bytes(boot[40:48], "little")
            if bps not in (512, 1024, 2048, 4096) or key % bps or not sectors:
                return
            size = sectors * bps
            if key + size > length or (partition_sectors and size > partition_sectors * sector_size):
                return
            if key not in seen:
                seen.add(key)
                found.append(dict(label=label, offset=key // bps, sector_size=bps, size=size))

        probe(0, 512, "NTFS 分区镜像")
        if found:
            return found
        mbr = read(0, 512)
        if len(mbr) != 512 or mbr[510:512] != b"\x55\xaa":
            raise RecoveryError("未识别到 NTFS 分区或有效分区表。请选择 raw 镜像。")
        for sector in (512, 4096):
            header = read(sector, sector)
            if header[:8] == b"EFI PART":
                header_size = int.from_bytes(header[12:16], "little")
                if not 92 <= header_size <= sector:
                    continue
                checked = bytearray(header[:header_size])
                crc = int.from_bytes(checked[16:20], "little")
                checked[16:20] = b"\0" * 4
                if zlib.crc32(checked) != crc:
                    raise RecoveryError("GPT 分区表校验失败，未自动猜测分区位置。")
                entry_lba, count, entry_size, array_crc = struct.unpack_from("<QIII", header, 72)
                if not 1 <= count <= 4096 or not 128 <= entry_size <= 4096 or count * entry_size > 16 * 1024 * 1024:
                    raise RecoveryError("GPT 分区数量或长度超出支持范围。")
                entries = read(entry_lba * sector, count * entry_size)
                if len(entries) != count * entry_size or zlib.crc32(entries) != array_crc:
                    raise RecoveryError("GPT 分区条目校验失败。")
                for index in range(count):
                    entry = entries[index * entry_size:(index + 1) * entry_size]
                    if entry[:16] == bytes(16):
                        continue
                    first, last = struct.unpack_from("<QQ", entry, 32)
                    if last >= first:
                        probe(first, sector, f"GPT 分区 {index + 1}", last - first + 1)
                break
            for index in range(4):
                entry = mbr[446 + 16 * index:462 + 16 * index]
                kind, first, size = entry[4], *struct.unpack_from("<II", entry, 8)
                if not kind or not size:
                    continue
                if kind in (5, 15, 133):
                    extended, current, visited = first, first, set()
                    while current not in visited and len(visited) < 128:
                        visited.add(current)
                        ebr = read(current * sector, 512)
                        if len(ebr) != 512 or ebr[510:512] != b"\x55\xaa":
                            break
                        data = ebr[446:462]
                        relative, logical_size = struct.unpack_from("<II", data, 8)
                        if data[4] and relative + current + logical_size <= extended + size:
                            probe(current + relative, sector, f"逻辑分区 {len(visited)}", logical_size)
                        link = ebr[462:478]
                        relative_next = int.from_bytes(link[8:12], "little")
                        if link[4] not in (5, 15, 133) or not relative_next or relative_next >= size:
                            break
                        current = extended + relative_next
                elif kind != 238:
                    probe(first, sector, f"MBR 分区 {index + 1}", size)
    if not found:
        raise RecoveryError("此镜像中未找到受支持的 NTFS 分区。第一版支持 NTFS。")
    return sorted(found, key=lambda item: item["offset"] * item["sector_size"])
