"""Read historical nonresident file evidence from an NTFS 1.1 $LogFile.

This is a bounded, read-only evidence reader, not NTFS crash replay. A complete
transaction chain and an older MFT generation associate names, sizes and runs;
they cannot establish that the surviving data is the original file content.
Only the affected image is an input. See docs/milestones/07-ntfs-log-recovery.md
for format references, validation evidence and deliberately unsupported cases.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import hashlib
import io
from pathlib import Path
import re
import struct

from .carving import allocation_bitmap, geometry, READ_BLOCK
from .common import RecoveryError, regular_file
from .control import checkpoint, progress

PAGE = 4096
RECORD = 1024
MAX_METADATA_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_HASH_BYTES = 1024 * 1024 * 1024
MAX_RECORDS = 100000
PATH_EVIDENCE = "historical_ntfs_log_association_unverified"


class InvalidLog(ValueError):
    pass


def u16(data, offset):
    return struct.unpack_from("<H", data, offset)[0]


def u32(data, offset):
    return struct.unpack_from("<I", data, offset)[0]


def u64(data, offset):
    return struct.unpack_from("<Q", data, offset)[0]


def fixup(data: bytes, signature: bytes) -> bytes:
    """Validate every sector trailer before restoring the in-memory copy."""
    if len(data) < 512 or len(data) % 512 or data[:4] != signature:
        raise InvalidLog("Invalid NTFS record signature or size.")
    start, count = u16(data, 4), u16(data, 6)
    if start < 8 or start % 2 or count != len(data) // 512 + 1 or start + count * 2 > 510:
        raise InvalidLog("Invalid NTFS update sequence array.")
    marker = data[start:start + 2]
    result = bytearray(data)
    for index in range(1, count):
        end = index * 512
        if data[end - 2:end] != marker:
            raise InvalidLog("Torn NTFS record (sector trailer mismatch).")
        result[end - 2:end] = data[start + index * 2:start + index * 2 + 2]
    return bytes(result)


def attributes(record: bytes) -> list[tuple[int, bytes]]:
    if (len(record) < 56 or record[:4] != b"FILE" or u32(record, 28) != RECORD
            or u64(record, 32) or not u16(record, 16)):
        raise InvalidLog("Unsupported MFT record or extension record.")
    start, used = u16(record, 20), u32(record, 24)
    if start < 48 or start % 8 or not start + 8 <= used <= min(RECORD, len(record)):
        raise InvalidLog("MFT attribute bounds are invalid.")
    result = []
    while start + 8 <= used:
        kind = u32(record, start)
        if kind == 0xffffffff:
            return result
        length = u32(record, start + 4)
        if length < 24 or length % 8 or start + length > used:
            break
        attr = record[start:start + length]
        if attr[8] not in (0, 1) or (attr[8] and length < 64):
            break
        if attr[9] and (u16(attr, 10) < 16 or u16(attr, 10) + attr[9] * 2 > length):
            break
        if not attr[8] and (u16(attr, 20) < 24 or u16(attr, 20) + u32(attr, 16) > length):
            break
        result.append((start, attr))
        start += length
    raise InvalidLog("Malformed MFT attribute list.")


def names(attrs: list[tuple[int, bytes]]) -> frozenset[tuple[int, str]]:
    result = set()
    for _, attr in attrs:
        if u32(attr, 0) != 0x30:
            continue
        if attr[8] or attr[9]:
            raise InvalidLog("Unsupported file name attribute.")
        start = u16(attr, 20)
        value = attr[start:start + u32(attr, 16)]
        if len(value) < 66 or value[65] not in (0, 1, 2, 3) or len(value) != 66 + value[64] * 2:
            raise InvalidLog("Invalid file name value.")
        if value[65] == 2:  # A DOS alias does not establish a second full name.
            continue
        try:
            name = value[66:].decode("utf-16le", errors="strict")
        except UnicodeError as exc:
            raise InvalidLog("Invalid file name encoding.") from exc
        if not name or any(ord(c) < 32 or c in '/\\:' for c in name) or name in (".", ".."):
            # The root's '.' name is handled separately using its file reference.
            continue
        result.add((u64(value, 0), name))
    return frozenset(result)


def data_attribute(attrs):
    if any(u32(a, 0) == 0x20 for _, a in attrs):
        raise InvalidLog("Attribute-list reconstruction is unsupported.")
    found = [(off, a) for off, a in attrs if u32(a, 0) == 0x80 and not a[9]]
    if len(found) != 1:
        raise InvalidLog("No unique unnamed data attribute.")
    return found[0]


def runs(attr: bytes, cluster_count: int, *, check_highest=True) -> list[tuple[int, int, int]]:
    """Decode VCN, LCN and count, including signed relative LCN deltas."""
    if len(attr) < 64 or attr[8] != 1 or attr[9] or u16(attr, 12) or u16(attr, 34) or u64(attr, 16):
        raise InvalidLog("Sparse, compressed, encrypted or extension data is unsupported.")
    position = u16(attr, 32)
    if not 64 <= position < len(attr):
        raise InvalidLog("Invalid mapping-pairs offset.")
    vcn = lcn = 0
    result = []
    while position < len(attr):
        header = attr[position]
        position += 1
        if not header:
            if check_highest and u64(attr, 24) != (vcn - 1) % (1 << 64):
                raise InvalidLog("Mapping-pairs length differs from highest VCN.")
            return result
        size_bytes, delta_bytes = header & 15, header >> 4
        if not 1 <= size_bytes <= 8 or not 1 <= delta_bytes <= 8 or position + size_bytes + delta_bytes > len(attr):
            break
        count = int.from_bytes(attr[position:position + size_bytes], "little")
        position += size_bytes
        lcn += int.from_bytes(attr[position:position + delta_bytes], "little", signed=True)
        position += delta_bytes
        if not count or not 0 < lcn < cluster_count or count > cluster_count - lcn or vcn + count > cluster_count:
            break
        if any(lcn < old_lcn + old_count and old_lcn < lcn + count for _, old_lcn, old_count in result):
            break
        result.append((vcn, lcn, count))
        vcn += count
    raise InvalidLog("Invalid or overlapping NTFS mapping pairs.")


def restart(log: bytes) -> tuple[int, int]:
    copies = []
    for offset in (0, PAGE):
        try:
            page = fixup(log[offset:offset + PAGE], b"RSTR")
            area = u16(page, 24)
            if (u32(page, 16) != PAGE or u32(page, 20) != PAGE
                    or (u16(page, 28), u16(page, 26)) != (1, 1) or not 32 <= area <= PAGE - 160):
                continue
            bits = 64 - u32(page, area + 16)
            client_offset = area + u16(page, area + 22)
            if (not 16 <= bits <= 32 or u64(page, area + 24) != len(log)
                    or (1 << bits) * 8 < len(log) or u16(page, area + 36) != 48
                    or u16(page, area + 38) != 64 or u16(page, area + 8) != 1
                    or not area + 40 <= client_offset <= PAGE - 96
                    or u32(page, client_offset + 28) != 8
                    or page[client_offset + 32:client_offset + 40] != "NTFS".encode("utf-16le")):
                continue
            copies.append((u64(page, area), bits, u16(page, client_offset + 20)))
        except (InvalidLog, struct.error):
            continue
    if not copies or len({c[1:] for c in copies}) != 1:
        raise RecoveryError("日志恢复仅支持具有有效重启页的 NTFS 日志 1.1（4 KiB 日志页）。")
    _, bits, client = max(copies)
    return bits, client


@dataclass
class LogRecord:
    lsn: int
    previous: int
    transaction: int
    epoch: int
    redo: int
    undo: int
    record_offset: int
    attr_offset: int
    cluster_offset: int
    vcn: int
    lcns: tuple[int, ...]
    data: bytes
    old: bytes
    redo_length: int
    closed: bool = False


def log_records(log: bytes) -> tuple[list[LogRecord], dict]:
    if len(log) > MAX_METADATA_BYTES or len(log) < 5 * PAGE or len(log) % PAGE:
        raise RecoveryError("日志大小不受支持（上限 64 MiB，必须包含完整日志页）。")
    bits, client_id = restart(log)
    mask = (1 << bits) - 1
    pages, rejected = {}, 0
    # Log 1.1 has two restart pages and two tail copies. A tail copy is not a
    # second historical record and must not supply a stale continuation page.
    for base in range(4 * PAGE, len(log), PAGE):
        checkpoint()
        raw = log[base:base + PAGE]
        if raw[:4] != b"RCRD":
            continue
        try:
            page = fixup(raw, b"RCRD")
            if u16(page, 4) != 40 or not 64 <= u16(page, 24) <= PAGE:
                raise InvalidLog("Unsupported log page layout.")
            pages[base] = page
        except InvalidLog:
            rejected += 1
    records = {}
    for base, page in pages.items():
        checkpoint()
        for position in range(64, PAGE - 47, 8):
            lsn, previous, _, length, client, kind, tx, flags = struct.unpack_from("<QQQIIIIH", page, position)
            if not lsn or ((lsn & mask) << 3) != base + position or kind != 1 or client != client_id:
                continue
            if u64(page, 8) >> bits != lsn >> bits:
                rejected += 1
                continue
            if not 40 <= length <= 65536 or flags & ~7 or not tx:
                rejected += 1
                continue
            payload = bytearray(page[position + 48:min(PAGE, position + 48 + length)])
            last_base, last_position = base, position + 48 + len(payload)
            while len(payload) < length and flags & 1:
                next_base = last_base + PAGE
                if next_base == len(log):
                    next_base = 4 * PAGE
                following = pages.get(next_base)
                # Refuse continuation across overwritten log cycles.
                if (following is None or u64(following, 8) < lsn
                        or u64(following, 8) >> bits != (lsn >> bits) + (next_base < base)):
                    break
                take = min(length - len(payload), PAGE - 64)
                payload.extend(following[64:64 + take])
                last_base, last_position = next_base, 64 + take
            last_page = pages[last_base]
            if (len(payload) != length or last_position > u16(last_page, 24)
                    or u64(last_page, 32) < lsn or u64(page, 8) < lsn):
                rejected += 1
                continue
            redo, undo, ro, rl, uo, ul, _, count, off, aoff, coff, _, vcn = struct.unpack_from("<12HQ", payload)
            minimum = 32 + 8 * max(1, count)
            if count > 16 or minimum > length:
                rejected += 1
                continue
            # ZeroEndOfFileRecord carries a byte COUNT, not that many payload
            # bytes. Zero-length operands likewise need no dereference.
            if any(size and op != 37 and (start < minimum or start + size > length)
                   for op, start, size in ((redo, ro, rl), (undo, uo, ul))):
                rejected += 1
                continue
            records[lsn] = LogRecord(lsn, previous, tx, lsn >> bits, redo, undo, off, aoff, coff, vcn,
                                    struct.unpack_from("<" + "Q" * count, payload, 32),
                                    bytes(payload[ro:ro + rl]) if redo != 37 else b"",
                                    bytes(payload[uo:uo + ul]) if undo != 37 else b"", rl)
            if len(records) > MAX_RECORDS:
                raise RecoveryError("日志记录超过 100,000 条，扫描未完成。")
    # Only closed, complete backward chains without rollback operations may
    # contribute metadata. Transaction slots alone are reused and insufficient.
    walked = 0
    for end in records.values():
        if end.redo != 27:
            continue
        chain, current = [], end
        while current is not None and current.transaction == end.transaction:
            walked += 1
            if walked > MAX_RECORDS * 4:
                raise RecoveryError("日志事务链检查超过预算，扫描未完成。")
            if current.redo == 1 or (current.undo == 1 and current.redo not in (24, 27)):
                break
            chain.append(current)
            if current.previous == 0:
                for row in chain:
                    row.closed = True
                break
            if current.previous >= current.lsn:
                break
            current = records.get(current.previous)
    return sorted(records.values(), key=lambda r: r.lsn), {
        "valid_pages": len(pages), "rejected_records_or_pages": rejected,
        "log_records": len(records), "closed_chain_records": sum(r.closed for r in records.values()),
    }


@dataclass
class History:
    number: int
    sequence: int
    directory: bool
    first_lsn: int
    last_lsn: int
    attrs: list[tuple[int, bytes]]
    file_names: frozenset
    invalid: str | None = None
    ended: bool = False
    evidence_lsns: list[int] = field(default_factory=list)

    def update(self, row: LogRecord, cluster_count: int):
        self.last_lsn = row.lsn
        if not row.closed:
            raise InvalidLog("Metadata transaction is incomplete or rolled back.")
        if row.redo == 3:
            self.ended = True
            return
        if row.redo == 0:
            return
        if self.ended:
            raise InvalidLog("Missing initialization after record deallocation.")
        found = next(((i, a) for i, (off, a) in enumerate(self.attrs) if off == row.record_offset), None)
        if self.directory:
            # Child index entries do not change this directory's own name.
            if row.redo in (12, 13, 17, 19, 33):
                return
            if row.redo == 7 and found and u32(found[1], 0) == 0x10:
                return
            raise InvalidLog("Directory name history is ambiguous.")
        if row.redo == 37:
            end = max(off + len(a) for off, a in self.attrs) + 8
            if row.record_offset < end or row.record_offset + row.redo_length > RECORD:
                raise InvalidLog("Zero range overlaps file metadata.")
            return
        if row.redo == 5:
            attr = row.data
            if len(attr) < 24 or u32(attr, 4) != len(attr) or len(attr) % 8:
                raise InvalidLog("Invalid created attribute.")
            end = self.attrs[-1][0] + len(self.attrs[-1][1])
            if row.record_offset not in [off for off, _ in self.attrs] + [end]:
                raise InvalidLog("Created attribute does not align with historical metadata.")
            self.attrs = [(off + len(attr) if off >= row.record_offset else off, a) for off, a in self.attrs]
            self.attrs.append((row.record_offset, attr))
            self.attrs.sort()
        elif found is None:
            raise InvalidLog("Log operation does not match the historical attribute layout.")
        else:
            index, attr = found
            if row.redo == 6:
                if row.old != attr:
                    raise InvalidLog("Deleted attribute differs from its historical value.")
                self.attrs.pop(index)
                self.attrs = [(off - len(attr) if off > row.record_offset else off, a) for off, a in self.attrs]
            elif row.redo in (7, 9, 11):
                changed = bytearray(attr)
                if row.redo == 11:
                    if attr[8] != 1 or len(row.data) != 24 or len(row.old) != 24:
                        raise InvalidLog("Unsupported attribute size operation.")
                    if row.old != struct.pack("<QQQ", u64(attr, 40), u64(attr, 56), u64(attr, 48)):
                        raise InvalidLog("Size update differs from the historical attribute.")
                    allocated, initialized, size = struct.unpack("<QQQ", row.data)
                    struct.pack_into("<QQQ", changed, 40, allocated, size, initialized)
                else:
                    start, stop = row.attr_offset, row.attr_offset + len(row.data)
                    if len(row.data) != len(row.old) or stop > len(attr) or start < (64 if row.redo == 9 else 24):
                        raise InvalidLog("Resized or out-of-bounds attribute update is unsupported.")
                    if row.redo == 9 and (attr[8] != 1 or start < u16(attr, 32) or attr[start:stop] != row.old):
                        raise InvalidLog("Mapping update differs from the historical run list.")
                    if row.redo == 7 and attr[8]:
                        raise InvalidLog("Resident update targets a nonresident attribute.")
                    changed[start:stop] = row.data
                    if row.redo == 9:
                        mapping = runs(changed, cluster_count, check_highest=False)
                        struct.pack_into("<Q", changed, 24, (sum(n for _, _, n in mapping) - 1) % (1 << 64))
                self.attrs[index] = (row.record_offset, bytes(changed))
            else:
                raise InvalidLog("Unsupported historical MFT operation.")
        if self.attrs[-1][0] + len(self.attrs[-1][1]) + 8 > RECORD:
            raise InvalidLog("Historical attributes exceed the file record.")
        current_names = names(self.attrs)
        if current_names != self.file_names:
            raise InvalidLog("Renamed or multiply linked historical file is unsupported.")
        if row.redo in (5, 6, 9, 11):
            self.evidence_lsns.append(row.lsn)


def histories(mft: bytes, rows: list[LogRecord], volume):
    record0 = fixup(mft[:RECORD], b"FILE")
    mft_runs = runs(data_attribute(attributes(record0))[1], volume.cluster_count)
    current = {}
    for number in range(len(mft) // RECORD):
        checkpoint()
        try:
            record = fixup(mft[number * RECORD:(number + 1) * RECORD], b"FILE")
            attrs = attributes(record)
            if u32(record, 44) != number:
                continue
            current[number] = (u16(record, 16), bool(u16(record, 22) & 1), bool(u16(record, 22) & 2), names(attrs))
        except InvalidLog:
            continue
    active, all_histories, epoch = {}, [], None
    for row in rows:
        checkpoint()
        if row.epoch != epoch:
            # Retained pages from another log cycle cannot fill a missing
            # generation initialization in the next cycle.
            active, epoch = {}, row.epoch
        if not row.lcns or row.cluster_offset * 512 >= volume.cluster_size:
            continue
        if any(not any(v <= row.vcn + i < v + count and lcn + row.vcn + i - v == actual
                       for v, lcn, count in mft_runs) for i, actual in enumerate(row.lcns)):
            continue
        address = row.vcn * volume.cluster_size + row.cluster_offset * 512
        if address % RECORD or address + RECORD > len(mft):
            continue
        number = address // RECORD
        if row.redo == 2:
            try:
                if not row.closed or row.record_offset or u32(row.data, 44) != number:
                    raise InvalidLog("Unconfirmed file initialization.")
                attrs = attributes(row.data)
                state = History(number, u16(row.data, 16), bool(u16(row.data, 22) & 2),
                                row.lsn, row.lsn, attrs, names(attrs), evidence_lsns=[row.lsn])
                if not u16(row.data, 22) & 1:
                    raise InvalidLog("File initialization has no allocated flag.")
                all_histories.append(state)
                active[number] = state
            except (InvalidLog, struct.error):
                active.pop(number, None)
        elif number in active:
            state = active[number]
            if not state.invalid:
                try:
                    state.update(row, volume.cluster_count)
                except (InvalidLog, struct.error, IndexError) as exc:
                    state.invalid = str(exc)
    return all_histories, current


def original_path(state: History, all_histories, current) -> str | None:
    choices = defaultdict(set)
    for number, (seq, allocated, directory, file_names) in current.items():
        if allocated and directory:
            choices[(number, seq)].update(file_names)
    for parent in all_histories:
        if parent.directory and not parent.invalid:
            choices[(parent.number, parent.sequence)].update(parent.file_names)
    if len(state.file_names) != 1 or 5 not in current:
        return None
    reference, name = next(iter(state.file_names))
    path, seen = [name], set()
    for _ in range(64):
        key = (reference & ((1 << 48) - 1), reference >> 48)
        if key == (5, current[5][0]) and current[5][1:3] == (True, True):
            return "/" + "/".join(reversed(path))
        if key in seen or len(choices[key]) != 1:
            return None
        seen.add(key)
        reference, name = next(iter(choices[key]))
        path.append(name)
    return None


def available_ranges(mapping, size: int, initialized: int, volume, bitmap):
    groups, group = [], []
    for vcn, lcn, count in mapping:
        for index in range(count):
            if index % 4096 == 0:
                checkpoint()
            logical = (vcn + index) * volume.cluster_size
            length = min(volume.cluster_size, min(size, initialized) - logical)
            if length <= 0:
                break
            cluster = lcn + index
            if bitmap[cluster // 8] & (1 << (cluster % 8)):
                if group:
                    groups.append(group)
                    group = []
                continue
            start = volume.start + cluster * volume.cluster_size
            if group and group[-1]["image_offset"] + group[-1]["length"] == start:
                group[-1]["length"] += length
            else:
                group.append({"file_offset": logical, "image_offset": start, "length": length})
    if group:
        groups.append(group)
    return groups


def copy_ranges(stream, extents, output=None) -> str:
    digest = hashlib.sha256()
    for extent in extents:
        stream.seek(extent["image_offset"])
        remaining = extent["length"]
        while remaining:
            checkpoint()
            data = stream.read(min(remaining, READ_BLOCK))
            if not data:
                raise RecoveryError("日志指向的数据区读取不完整。")
            digest.update(data)
            if output is not None:
                output.write(data)
            remaining -= len(data)
    return digest.hexdigest()


def scan_log(image: Path, offset: int, sector_size: int, *, backend, max_candidates: int = 10000):
    image = regular_file(image)
    volume = geometry(image, offset, sector_size)
    with image.open("rb") as stream:
        stream.seek(volume.start + 64)
        record_code = stream.read(1)
    if sector_size != 512 or record_code != b"\xf6" or volume.cluster_size < RECORD:
        raise RecoveryError("日志恢复目前仅支持 512 字节扇区、1 KiB 文件记录的 NTFS 镜像。")
    progress("ntfs_log", message="读取旧文件记录与 NTFS 日志")
    metadata = []
    for number in (0, 2):
        with io.BytesIO() as output:
            backend.extract_log_metadata(image, offset, sector_size, number, output, MAX_METADATA_BYTES)
            metadata.append(output.getvalue())
    mft, log = metadata
    if not RECORD <= len(mft) <= MAX_METADATA_BYTES or len(mft) % RECORD:
        raise RecoveryError("MFT 超过上限或文件记录被截断。")
    rows, report = log_records(log)
    states, current = histories(mft, rows, volume)
    bitmap = allocation_bitmap(image, offset, sector_size, volume, backend)
    candidates, diagnostics, budget = [], [], MAX_HASH_BYTES
    # Multiple initializations with the same file reference are ambiguous.
    counts = defaultdict(int)
    for state in states:
        counts[(state.number, state.sequence)] += 1
    with image.open("rb") as stream:
        for state in states:
            checkpoint()
            now = current.get(state.number)
            if (state.directory or not now or now[0] <= state.sequence
                    or (now[0] == state.sequence + 1 and not now[1])):
                continue  # Live generation or a deleted generation available to normal TSK.
            diagnostic = {"record": state.number, "sequence": state.sequence, "initialization_lsn": state.first_lsn,
                          "historical_names": sorted(n for _, n in state.file_names)}
            diagnostics.append(diagnostic)
            try:
                if state.invalid or counts[(state.number, state.sequence)] != 1:
                    raise InvalidLog(state.invalid or "Ambiguous historical generation.")
                _, attr = data_attribute(state.attrs)
                if not attr[8]:
                    raise InvalidLog("Resident data is not recovered from initialization records; it may omit later user writes.")
                mapping = runs(attr, volume.cluster_count)
                allocated, size, initialized = struct.unpack_from("<QQQ", attr, 40)
                if not 0 < initialized <= size <= MAX_FILE_BYTES or sum(n for _, _, n in mapping) * volume.cluster_size != allocated or size > allocated:
                    raise InvalidLog("Historical data size, allocation or initialized length is unsupported.")
                groups = available_ranges(mapping, size, initialized, volume, bitmap)
                path = original_path(state, states, current)
                diagnostic.update(original_size=size, initialized_size=initialized,
                                  available_bytes=sum(e["length"] for g in groups for e in g))
                for extents in groups:
                    length = sum(e["length"] for e in extents)
                    start = extents[0]["file_offset"]
                    if len(candidates) >= max_candidates or length > budget:
                        raise RecoveryError("日志恢复超过候选数或 1 GiB 内容读取预算，扫描未完成。")
                    budget -= length
                    fragment = start != 0 or length != size
                    suffix = Path(next(iter(state.file_names))[1]).suffix if len(state.file_names) == 1 else ".bin"
                    generated = f"/Historical/NTFS/record-{state.number}-seq-{state.sequence}-{state.first_lsn}{suffix}"
                    info = {"record": state.number, "sequence": state.sequence,
                            "initialization_lsn": state.first_lsn, "evidence_lsns": state.evidence_lsns,
                            "original_size": size, "file_offset": start, "extents": extents,
                            "sha256": copy_ranges(stream, extents), "allocation": "unallocated_clusters"}
                    candidates.append({"inode": None, "kind": "file", "size": length,
                                       "observed_path": generated, "original_path": path,
                                       "path_evidence": PATH_EVIDENCE if path else "historical_name_only",
                                       "recovery_method": "ntfs_log", "content_status": "fragment" if fragment else "unverified",
                                       "ntfs_log": info,
                                       "warnings": ["旧日志关联了文件记录、长度和数据位置；后续日志可能缺失，内容仍需独立验证。",
                                                    "只导出当前未分配的数据簇；未分配不代表未被覆盖过。"]
                                                   + ([f"不完整片段：原文件 {size} 字节，本片段从字节 {start} 开始，共 {length} 字节。"] if fragment else [])})
                diagnostic["status"] = "candidate" if groups else "no_unallocated_data"
            except InvalidLog as exc:
                diagnostic.update(status="unsupported_or_incomplete_evidence", reason=str(exc))
    report.update(status="completed", candidate_count=len(candidates),
                  fragment_count=sum(c["content_status"] == "fragment" for c in candidates),
                  historical_generations=len(states), diagnostics=diagnostics, hashed_bytes=MAX_HASH_BYTES - budget)
    return candidates, report


def validate_candidate(candidate: dict):
    info = candidate.get("ntfs_log")
    if (not isinstance(info, dict) or candidate.get("inode") is not None or candidate.get("kind") != "file"
            or candidate.get("content_status") not in ("fragment", "unverified")
            or candidate.get("path_evidence") not in (PATH_EVIDENCE, "historical_name_only")
            or type(candidate.get("size")) is not int or not 0 < candidate["size"] <= MAX_FILE_BYTES):
        raise RecoveryError("Invalid historical NTFS candidate.")
    for key in ("record", "sequence", "initialization_lsn", "original_size", "file_offset"):
        if type(info.get(key)) is not int or not 0 <= info[key] < 1 << 63:
            raise RecoveryError("Invalid historical NTFS file reference or range.")
    if (not 0 < info["sequence"] < 65535 or not 0 < info["original_size"] <= MAX_FILE_BYTES
            or info["file_offset"] + candidate["size"] > info["original_size"]
            or not isinstance(info.get("sha256"), str) or not re.fullmatch("[0-9a-f]{64}", info["sha256"])
            or not isinstance(info.get("extents"), list) or not 1 <= len(info["extents"]) <= RECORD
            or not isinstance(info.get("evidence_lsns"), list) or not 1 <= len(info["evidence_lsns"]) <= MAX_RECORDS
            or info.get("allocation") != "unallocated_clusters"):
        raise RecoveryError("Invalid historical NTFS evidence descriptor.")
    logical = info["file_offset"]
    for extent in info["extents"]:
        if (not isinstance(extent, dict) or any(type(extent.get(k)) is not int for k in ("file_offset", "image_offset", "length"))
                or extent["file_offset"] != logical or not 0 <= extent["image_offset"] < 1 << 63 or not 0 < extent["length"] <= MAX_FILE_BYTES):
            raise RecoveryError("Invalid historical NTFS extent.")
        logical += extent["length"]
    fragment = info["file_offset"] != 0 or candidate["size"] != info["original_size"]
    if logical != info["file_offset"] + candidate["size"] or fragment != (candidate["content_status"] == "fragment"):
        raise RecoveryError("Historical NTFS fragment must not be presented as a complete file.")


def extract_log(image, offset, sector_size, candidate, output, *, backend):
    validate_candidate(candidate)
    # Re-derive all associations, names, ranges and hashes from the image. A
    # saved session is editable and cannot authorize arbitrary source ranges.
    derived, _ = scan_log(image, offset, sector_size, backend=backend, max_candidates=MAX_RECORDS)
    descriptor = {k: v for k, v in candidate.items() if k != "id"}
    if descriptor not in derived:
        raise RecoveryError("旧日志候选与镜像证据不一致，请重新扫描。")
    with image.open("rb") as stream:
        digest = copy_ranges(stream, candidate["ntfs_log"]["extents"], output)
    if digest != candidate["ntfs_log"]["sha256"]:
        raise RecoveryError("日志候选的字节在保存过程中变化，结果不完整。")
