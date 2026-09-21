"""JPEG marker scanning and bounded, entropy-checked baseline reconstruction.

ITU-T T.81 Annexes B/C/F define the marker framing and sequential Huffman
syntax checked here. Validation does not establish provenance or original pixels.
"""
from __future__ import annotations

import hashlib
import io
import math
import re
from pathlib import Path

from .common import RecoveryError
from .control import checkpoint, progress
from .carving import geometry, allocation_bitmap, free_runs, _unallocated
from .journal import partial, save_partial

SIGNATURE = b"\xff\xd8\xff"
MAX_BYTES = 64 * 1024 * 1024
MAX_PIXELS = 40_000_000
BLOCK = 1024 * 1024
MAX_CHECKS = 100000
REASSEMBLY_BYTES = 8 * 1024 * 1024
TAIL_CLUSTERS = 128
HEAD_CLUSTERS = 8


class InvalidJpeg(ValueError):
    pass


class IncompleteJpeg(InvalidJpeg):
    pass


def _huffman(counts, symbols):
    table, code, at = {}, 0, 0
    for width, count in enumerate(counts, 1):
        # The all-one code is reserved for padding (T.81 C.2).
        if code + count >= (1 << width) and count:
            raise InvalidJpeg("Oversubscribed JPEG Huffman table.")
        for _ in range(count):
            table[width, code] = symbols[at]
            code += 1
            at += 1
        code <<= 1
    if not table:
        raise InvalidJpeg("Empty JPEG Huffman table.")
    return table


def _entropy(data, components, selectors, tables, width, height, interval):
    """Consume exactly the expected sequential DCT blocks, without rendering."""
    at = bits = buffer = 0
    def take(count):
        nonlocal at, bits, buffer
        while bits < count:
            if at >= len(data):
                raise InvalidJpeg("Truncated JPEG entropy data.")
            value = data[at]
            at += 1
            if value == 255:
                if at == len(data) or data[at] != 0:
                    raise InvalidJpeg("Unexpected marker in JPEG entropy data.")
                at += 1
            buffer = (buffer << 8) | value
            bits += 8
        bits -= count
        value = (buffer >> bits) & ((1 << count) - 1)
        buffer &= (1 << bits) - 1
        return value

    def symbol(table):
        code = 0
        for length in range(1, 17):
            code = (code << 1) | take(1)
            if (length, code) in table:
                return table[length, code]
        raise InvalidJpeg("Invalid JPEG Huffman code.")

    def align():
        nonlocal bits, buffer
        if buffer != (1 << bits) - 1:
            raise InvalidJpeg("Invalid JPEG entropy padding.")
        bits = buffer = 0

    max_h = max(c[0] for c in components.values())
    max_v = max(c[1] for c in components.values())
    if len(selectors) == 1:
        h, v, _ = components[selectors[0][0]]
        count = math.ceil(width * h / (8 * max_h)) * math.ceil(height * v / (8 * max_v))
        blocks = [(selectors[0], 1)]
    else:
        count = math.ceil(width / (8 * max_h)) * math.ceil(height / (8 * max_v))
        blocks = [(s, components[s[0]][0] * components[s[0]][1]) for s in selectors]
    restart = 0
    for mcu in range(count):
        if mcu % 256 == 0:
            checkpoint()
        if interval and mcu and mcu % interval == 0:
            align()
            while at + 2 < len(data) and data[at:at + 2] == b"\xff\xff":
                at += 1
            if data[at:at + 2] != bytes((255, 0xd0 + restart)):
                raise InvalidJpeg("Missing or out-of-order JPEG restart marker.")
            at += 2
            restart = (restart + 1) % 8
        for (component, dc, ac), amount in blocks:
            for _ in range(amount):
                size = symbol(tables[0, dc])
                if size > 11:
                    raise InvalidJpeg("Invalid baseline JPEG DC coefficient size.")
                take(size)
                coefficient = 1
                while coefficient < 64:
                    value = symbol(tables[1, ac])
                    run, size = value >> 4, value & 15
                    if not size:
                        if run == 0:
                            break
                        if run != 15:
                            raise InvalidJpeg("Invalid baseline JPEG zero run.")
                        coefficient += 16
                    else:
                        coefficient += run
                        if size > 10 or coefficient >= 64:
                            raise InvalidJpeg("JPEG AC coefficient exceeds its block.")
                        take(size)
                        coefficient += 1
                    if coefficient > 64:
                        raise InvalidJpeg("JPEG zero run exceeds its block.")
    align()
    if at != len(data):
        raise InvalidJpeg("Extra entropy bytes after the expected JPEG blocks.")


def inspect_jpeg(data: bytes, *, baseline_only=False) -> dict:
    if not data.startswith(b"\xff\xd8"):
        raise InvalidJpeg("Missing JPEG SOI.")
    at, segments, scans = 2, 0, 0
    components, tables, quantizers, seen = {}, {}, set(), set()
    successive_bits = {}
    frame = None
    interval = 0
    width = height = 0
    while at < min(len(data), MAX_BYTES):
        checkpoint()
        if data[at] != 255:
            raise InvalidJpeg("JPEG marker prefix missing.")
        while at < len(data) and data[at] == 255:
            at += 1
        if at == len(data):
            raise IncompleteJpeg("Truncated JPEG marker.")
        marker = data[at]
        at += 1
        if marker == 0xd9:
            if not scans or not components or seen != set(components):
                raise InvalidJpeg("JPEG ended without complete component scans.")
            return dict(size=at, sha256=hashlib.sha256(data[:at]).hexdigest(), width=width,
                        height=height, validation="baseline_entropy" if frame == 0xc0 else "progressive_markers")
        if marker in (0, 0xd8, 1) or 0xd0 <= marker <= 0xd7:
            raise InvalidJpeg("Unexpected JPEG standalone marker.")
        if at + 2 > len(data):
            raise IncompleteJpeg("Truncated JPEG segment length.")
        size = int.from_bytes(data[at:at + 2], "big")
        if size < 2:
            raise InvalidJpeg("Invalid JPEG segment length.")
        if at + size > min(len(data), MAX_BYTES):
            raise IncompleteJpeg("Truncated JPEG segment.")
        payload = data[at + 2:at + size]
        at += size
        segments += 1
        if segments > 100000:
            raise InvalidJpeg("Too many JPEG segments.")
        if marker in (0xc0, 0xc2):
            if frame is not None or len(payload) < 6 or payload[0] != 8:
                raise InvalidJpeg("Unsupported JPEG frame.")
            frame = marker
            if baseline_only and frame != 0xc0:
                raise InvalidJpeg("Fragment reconstruction requires baseline JPEG.")
            height, width = int.from_bytes(payload[1:3], "big"), int.from_bytes(payload[3:5], "big")
            count = payload[5]
            if not width or not height or width * height > MAX_PIXELS or count not in (1, 3, 4) or len(payload) != 6 + 3 * count:
                raise InvalidJpeg("Invalid or oversized JPEG dimensions/components.")
            for index in range(count):
                identifier, sampling, quantizer = payload[6 + index * 3:9 + index * 3]
                h, v = sampling >> 4, sampling & 15
                if identifier in components or not 1 <= h <= 4 or not 1 <= v <= 4 or quantizer > 3:
                    raise InvalidJpeg("Invalid JPEG frame component.")
                components[identifier] = h, v, quantizer
                successive_bits[identifier] = [-1] * 64
            if sum(c[0] * c[1] for c in components.values()) > 10:
                raise InvalidJpeg("Too many blocks per JPEG MCU.")
        elif marker == 0xdb:
            pos = 0
            while pos < len(payload):
                descriptor = payload[pos]
                precision, identifier = descriptor >> 4, descriptor & 15
                amount = 64 * (precision + 1)
                if precision > 1 or identifier > 3 or pos + 1 + amount > len(payload):
                    raise InvalidJpeg("Invalid JPEG quantization table.")
                values = payload[pos + 1:pos + 1 + amount]
                if any(int.from_bytes(values[i:i + precision + 1], "big") == 0 for i in range(0, amount, precision + 1)):
                    raise InvalidJpeg("Zero JPEG quantizer.")
                quantizers.add(identifier)
                pos += 1 + amount
        elif marker == 0xc4:
            pos = 0
            while pos < len(payload):
                if pos + 17 > len(payload):
                    raise InvalidJpeg("Truncated JPEG Huffman table.")
                kind, identifier = payload[pos] >> 4, payload[pos] & 15
                counts = payload[pos + 1:pos + 17]
                total = sum(counts)
                if kind > 1 or identifier > 3 or total > 256 or pos + 17 + total > len(payload):
                    raise InvalidJpeg("Invalid JPEG Huffman table length.")
                tables[kind, identifier] = _huffman(counts, payload[pos + 17:pos + 17 + total])
                pos += 17 + total
        elif marker == 0xdd:
            if len(payload) != 2:
                raise InvalidJpeg("Invalid JPEG restart interval.")
            interval = int.from_bytes(payload, "big")
        elif marker == 0xda:
            if frame is None or not payload or not 1 <= payload[0] <= len(components) or len(payload) != 4 + 2 * payload[0]:
                raise InvalidJpeg("Invalid JPEG scan header.")
            selectors = []
            for index in range(payload[0]):
                identifier, table = payload[1 + 2 * index:3 + 2 * index]
                if identifier not in components or identifier in [s[0] for s in selectors] or table >> 4 > 3 or table & 15 > 3:
                    raise InvalidJpeg("Invalid JPEG scan component.")
                if components[identifier][2] not in quantizers:
                    raise InvalidJpeg("Missing JPEG quantization table.")
                selectors.append((identifier, table >> 4, table & 15))
            ss, se, successive = payload[-3:]
            if frame == 0xc0:
                if (ss, se, successive) != (0, 63, 0) or any(s[0] in seen or (0, s[1]) not in tables or (1, s[2]) not in tables for s in selectors):
                    raise InvalidJpeg("Invalid sequential JPEG scan.")
            elif not 0 <= ss <= se <= 63 or (ss == 0 and se != 0) or (ss and len(selectors) != 1) or successive >> 4 > 13 or successive & 15 > 13:
                raise InvalidJpeg("Invalid progressive JPEG scan.")
            else:
                high, low = successive >> 4, successive & 15
                if high and low != high - 1:
                    raise InvalidJpeg("Invalid progressive JPEG refinement.")
                for identifier, dc, ac in selectors:
                    if ((ss == 0 and (ac or (not high and (0, dc) not in tables)))
                            or (ss and (dc or (1, ac) not in tables or successive_bits[identifier][0] < 0))):
                        raise InvalidJpeg("Missing progressive JPEG table or initial DC scan.")
                    for coefficient in range(ss, se + 1):
                        previous = successive_bits[identifier][coefficient]
                        if previous != (high if high else -1):
                            raise InvalidJpeg("Out-of-order progressive JPEG coefficient scan.")
                        successive_bits[identifier][coefficient] = low
            start = at
            while True:
                marker_at = data.find(b"\xff", at)
                if marker_at < 0 or marker_at + 1 >= len(data):
                    raise IncompleteJpeg("JPEG lacks a complete EOI marker.")
                end = marker_at + 1
                while end < len(data) and data[end] == 255:
                    end += 1
                if end == len(data):
                    raise IncompleteJpeg("Truncated entropy marker.")
                code = data[end]
                if code == 0 or 0xd0 <= code <= 0xd7:
                    at = end + 1
                    continue
                at = marker_at
                break
            if at == start:
                raise InvalidJpeg("Empty JPEG entropy data.")
            if frame == 0xc0:
                _entropy(data[start:at], components, selectors, tables, width, height, interval)
            seen.update(s[0] for s in selectors)
            scans += 1
        elif not (0xe0 <= marker <= 0xef or marker == 0xfe):
            raise InvalidJpeg("Unsupported JPEG coding process/marker.")
    raise IncompleteJpeg("JPEG exceeds available bytes or the size limit.")


def observed_path(start):
    return f"/Carved/JPEG/jpeg_{start:016x}.jpg"


def _candidate(start, info, extents):
    reconstructed = len(extents) > 1
    return dict(inode=None, kind="file", size=info["size"], observed_path=observed_path(start),
                original_path=None, path_evidence="content_only", recovery_method="jpeg_carving",
                carving=dict(format="jpeg", image_offset=start, sha256=info["sha256"], extents=extents,
                             allocation="unallocated_clusters", validation=info["validation"],
                             reconstructed=reconstructed, width=info["width"], height=info["height"]),
                warnings=["按 JPEG 内容生成名称，原文件名和目录未知；结构检查不证明与删除前内容一致。"] +
                         (["此文件由碎片推测重组，可能存在错误拼接，请逐张核对图片内容。"] if reconstructed else []) +
                         (["渐进式 JPEG 仅检查标记结构，未验证全部熵编码。"] if info["validation"] == "progressive_markers" else []))


def _trim(extents, size):
    result = []
    for start, amount in extents:
        used = min(amount, size)
        if used:
            result.append([start, used])
        size -= used
        if not size:
            break
    return result


def scan_jpeg(image: Path, offset: int, sector_size: int, *, backend, max_candidates: int, reassemble=False):
    volume = geometry(image, offset, sector_size)
    bitmap = allocation_bitmap(image, offset, sector_size, volume, backend)
    candidates, cursor, checks, ambiguous, limited = [], volume.start, 0, 0, 0
    budget = max(16 * 1024 * 1024, 2 * (volume.end - volume.start))
    saved = partial()
    if saved:
        candidates, cursor, checks, ambiguous, limited, budget = (saved[k] for k in ("candidates", "cursor", "checks", "ambiguous", "limited", "budget"))
        if (not isinstance(candidates, list) or len(candidates) > max_candidates
                or any(type(n) is not int or n < 0 for n in (cursor, checks, ambiguous, limited, budget))
                or not volume.start <= cursor <= volume.end or checks > MAX_CHECKS
                or budget > max(16 * 1024 * 1024, 2 * (volume.end - volume.start))):
            raise RecoveryError("Invalid JPEG scan checkpoint.")
        for item in candidates:
            validate_candidate(item)
    with image.open("rb") as stream:
        def read(start, size):
            nonlocal budget
            if size > budget:
                raise RecoveryError("JPEG validation byte budget exhausted; reduce the scan scope.")
            checkpoint()
            stream.seek(start)
            data = stream.read(size)
            if len(data) != size:
                raise RecoveryError("Source image became unreadable during JPEG scanning.")
            budget -= size
            return data

        def reconstruct(start, run_end):
            nonlocal ambiguous, limited
            matches = {}
            # First try physical-order free runs. This covers multiple extents
            # separated by allocated files without incorporating their bytes.
            data, spans = bytearray(), []
            for a, b in free_runs(bitmap, volume):
                if b <= start:
                    continue
                a = max(a, start)
                if a - start > 32 * 1024 * 1024 or len(spans) == 32 or len(data) >= REASSEMBLY_BYTES:
                    break
                amount = min(b - a, REASSEMBLY_BYTES - len(data))
                if amount > budget:
                    limited += 1
                    return None
                data.extend(read(a, amount))
                spans.append([a, amount])
                try:
                    info = inspect_jpeg(data, baseline_only=True)
                    matches[info["sha256"]] = (info, _trim(spans, info["size"]))
                    break
                except IncompleteJpeg:
                    continue
                except InvalidJpeg:
                    break
            # Two-fragment search, bounded to the first 8 head boundaries and
            # 128 free tail clusters within 4 MiB, in either physical direction.
            boundary = volume.start + ((start - volume.start) // volume.cluster_size + 1) * volume.cluster_size
            cuts = list(range(boundary, min(run_end, start + REASSEMBLY_BYTES - 1,
                                           boundary + (HEAD_CLUSTERS - 1) * volume.cluster_size) + 1, volume.cluster_size))
            if not cuts:
                return next(iter(matches.values())) if len(matches) == 1 else None
            prefix = read(start, cuts[-1] - start) if cuts[-1] - start <= budget else b""
            locations = []
            for a, b in free_runs(bitmap, volume):
                lower = max(a, start - 4 * 1024 * 1024)
                lower = volume.start + max(0, math.ceil((lower - volume.start) / volume.cluster_size)) * volume.cluster_size
                for tail in range(lower, min(b, start + 4 * 1024 * 1024), volume.cluster_size):
                    if not start <= tail < boundary and tail != boundary:
                        locations.append((tail, b))
            # Nearby tails first; at most 16,384 cluster starts in this window.
            locations.sort(key=lambda pair: (abs(pair[0] - start), pair[0] < start))
            if len(locations) > TAIL_CLUSTERS:
                limited += 1
            for tail, end in locations[:TAIL_CLUSTERS]:
                maximum = min(end - tail, REASSEMBLY_BYTES - len(prefix))
                amount = min(maximum, 65536)
                while amount:
                    if amount > budget or not prefix:
                        limited += 1
                        return next(iter(matches.values())) if len(matches) == 1 else None
                    suffix = read(tail, amount)
                    incomplete = False
                    for cut in cuts:
                        if tail == cut:
                            continue
                        checkpoint()
                        try:
                            info = inspect_jpeg(prefix[:cut - start] + suffix, baseline_only=True)
                        except IncompleteJpeg:
                            incomplete = True
                            continue
                        except InvalidJpeg:
                            continue
                        spans = _trim([[start, cut - start], [tail, amount]], info["size"])
                        if len(spans) != 2 or max(start, tail) < min(cut, tail + spans[1][1]):
                            continue
                        matches[info["sha256"]] = (info, spans)
                        if len(matches) > 1:
                            ambiguous += 1
                            return None
                    if not incomplete or amount == maximum:
                        break
                    amount = min(amount * 2, maximum)
            return next(iter(matches.values())) if len(matches) == 1 else None

        for start, end in free_runs(bitmap, volume):
            if cursor >= end:
                continue
            position = max(start, cursor)
            tail = b""
            if position > start:
                stream.seek(max(start, position - 2))
                tail = stream.read(min(2, position - start))
            while position < end:
                progress("jpeg", position - volume.start, volume.end - volume.start, "查找 JPEG 内容与碎片")
                stream.seek(position)
                block = stream.read(min(BLOCK, end - position))
                if not block:
                    raise RecoveryError("Source image became unreadable during JPEG scanning.")
                data, base, search = tail + block, position - len(tail), 0
                while (found := data.find(SIGNATURE, search)) >= 0:
                    search = found + len(SIGNATURE)
                    absolute = base + found
                    if any(c["carving"]["image_offset"] == absolute for c in candidates):
                        continue
                    checks += 1
                    if checks > MAX_CHECKS:
                        raise RecoveryError("JPEG signature count limit exceeded.")
                    amount = min(64 * 1024, end - absolute, MAX_BYTES)
                    result = None
                    while amount:
                        payload = read(absolute, amount)
                        try:
                            info = inspect_jpeg(payload)
                            result = info, [[absolute, info["size"]]]
                            break
                        except IncompleteJpeg:
                            expanded = min(amount * 2, end - absolute, MAX_BYTES)
                            if expanded > amount:
                                amount = expanded
                                continue
                        except InvalidJpeg:
                            pass
                        break
                    if result is None and reassemble:
                        result = reconstruct(absolute, end)
                    if result:
                        if len(candidates) >= max_candidates:
                            raise RecoveryError("JPEG carving exceeds --max-candidates.")
                        info, extents = result
                        candidates.append(_candidate(absolute, info, extents))
                position += len(block)
                tail = data[-2:]
                save_partial(dict(candidates=candidates, cursor=position, checks=checks,
                                  ambiguous=ambiguous, limited=limited, budget=budget))
    return candidates, dict(status="completed", format="jpeg", candidate_count=len(candidates),
        reconstructed_count=sum(c["carving"]["reconstructed"] for c in candidates), signature_checks=checks,
        ambiguous_headers=ambiguous, limited_searches=limited, max_file_bytes=MAX_BYTES,
        reconstruction_scope="baseline JPEG; ordered free extents or bounded two-fragment search; content remains unverified")


def validate_candidate(candidate):
    info = candidate.get("carving")
    if not isinstance(info, dict):
        raise RecoveryError("Invalid JPEG descriptor.")
    start, extents, size = info.get("image_offset"), info.get("extents"), candidate.get("size")
    if (type(start) is not int or not 0 <= start < 1 << 63 or type(size) is not int or not 4 <= size <= MAX_BYTES
            or candidate.get("inode") is not None or candidate.get("original_path") is not None
            or candidate.get("kind") != "file" or candidate.get("path_evidence") != "content_only"
            or candidate.get("observed_path") != observed_path(start) or info.get("format") != "jpeg"
            or info.get("allocation") != "unallocated_clusters"
            or info.get("validation") not in ("baseline_entropy", "progressive_markers")
            or not isinstance(info.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", info["sha256"])
            or not isinstance(extents, list) or not 1 <= len(extents) <= 32):
        raise RecoveryError("Invalid JPEG carving candidate.")
    for extent in extents:
        if (not isinstance(extent, list) or len(extent) != 2 or any(type(n) is not int for n in extent)
                or extent[0] < 0 or extent[1] <= 0 or sum(extent) >= 1 << 63):
            raise RecoveryError("Invalid JPEG extent.")
    ordered = sorted(extents)
    if (extents[0][0] != start or sum(e[1] for e in extents) != size
            or any(a[0] + a[1] > b[0] for a, b in zip(ordered, ordered[1:]))
            or type(info.get("reconstructed")) is not bool or info["reconstructed"] != (len(extents) > 1)
            or (len(extents) > 1 and (info["validation"] != "baseline_entropy" or size > REASSEMBLY_BYTES))):
        raise RecoveryError("Invalid or overlapping JPEG extents.")


def extract_jpeg(image, offset, sector_size, candidate, output, *, backend):
    validate_candidate(candidate)
    volume = geometry(image, offset, sector_size)
    bitmap = allocation_bitmap(image, offset, sector_size, volume, backend)
    data = bytearray()
    with image.open("rb") as stream:
        for start, amount in candidate["carving"]["extents"]:
            if not _unallocated(bitmap, volume, start, start + amount):
                raise RecoveryError("JPEG extent is not in unallocated source clusters.")
            stream.seek(start)
            while amount:
                checkpoint()
                block = stream.read(min(65536, amount))
                if not block:
                    raise RecoveryError("Truncated JPEG source extent.")
                data.extend(block)
                amount -= len(block)
    try:
        info = inspect_jpeg(data, baseline_only=candidate["carving"]["reconstructed"])
    except InvalidJpeg as exc:
        raise RecoveryError(f"JPEG candidate no longer validates: {exc}") from exc
    if info["size"] != candidate["size"] or info["sha256"] != candidate["carving"]["sha256"]:
        raise RecoveryError("JPEG candidate changed since the scan.")
    for at in range(0, len(data), 65536):
        checkpoint()
        output.write(data[at:at + 65536])
