#!/usr/bin/env python3
"""Fetch the pinned public NIST DFR-01 NTFS fixture outside the repository.

Requires Python 3.10+ and curl (system curl on macOS is sufficient). No Python
packages are installed. The full image is 1 GiB + 512 bytes; download is 2.15 MiB.
Hashes below pin bytes observed on 2026-09-12, not NIST-signed reference hashes.
See tests/integration/fixture-source.md for provenance and interpretation.
"""

from __future__ import annotations

import argparse
import bz2
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile


URL = "https://cfreds-archive.nist.gov/dfr-images/dfr-01-ntfs.dd.bz2"
COMPRESSED_NAME = "dfr-01-ntfs.dd.bz2"
IMAGE_NAME = "dfr-01-ntfs.dd"
COMPRESSED_SIZE = 2_257_102
COMPRESSED_SHA256 = "9625d8946bc7712953d0ca7e73e4eaeff26381006951d3f7603d1797b50d19e0"
IMAGE_SIZE = 1_073_742_336
IMAGE_SHA256 = "c863ccad01804b840a6dfa623a94996ca876e15ded41c6c0d8ae148620eb6493"
DELETED_FILE_SHA256 = "be9f9a4f99b5ce961d5c2759fff20543d0ffbedbd27be0c5157174bb46be8b85"


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def verified(path: Path, size: int, sha256: str) -> bool:
    if not path.exists():
        return False
    if path.stat().st_size != size or digest(path) != sha256:
        raise RuntimeError(f"Existing file fails pinned size/hash check: {path}")
    return True


def fetch(destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    compressed = destination / COMPRESSED_NAME
    image = destination / IMAGE_NAME
    if not verified(compressed, COMPRESSED_SIZE, COMPRESSED_SHA256):
        curl = shutil.which("curl")
        if curl is None:
            raise RuntimeError("curl is required to download the public fixture")
        # curl uses the system TLS trust configuration; never disable validation.
        with tempfile.NamedTemporaryFile(dir=destination, prefix="download-", delete=False) as stream:
            temporary = Path(stream.name)
        try:
            subprocess.run(
                [curl, "--fail", "--location", "--silent", "--show-error",
                 "--proto", "=https", "--proto-redir", "=https", "--max-time", "120",
                 "--max-filesize", str(COMPRESSED_SIZE), "--output", str(temporary), URL],
                check=True,
            )
            verified(temporary, COMPRESSED_SIZE, COMPRESSED_SHA256)
            os.replace(temporary, compressed)
        finally:
            temporary.unlink(missing_ok=True)
    if not verified(image, IMAGE_SIZE, IMAGE_SHA256):
        with tempfile.NamedTemporaryFile(dir=destination, prefix="expand-", delete=False) as stream:
            temporary = Path(stream.name)
            try:
                length = 0
                sha256 = hashlib.sha256()
                with bz2.open(compressed, "rb") as source:
                    for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
                        length += len(chunk)
                        if length > IMAGE_SIZE:
                            raise RuntimeError("Fixture expands beyond the pinned size")
                        stream.write(chunk)
                        sha256.update(chunk)
                if length != IMAGE_SIZE or sha256.hexdigest() != IMAGE_SHA256:
                    raise RuntimeError("Expanded fixture fails pinned size/hash check")
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                stream.close()
                temporary.unlink(missing_ok=True)
                raise
        try:
            os.replace(temporary, image)
        finally:
            temporary.unlink(missing_ok=True)
    # Independent of TSK: cross-check the documented partition and file extent.
    with image.open("rb") as source:
        mbr = source.read(512)
        if mbr[510:512] != b"\x55\xaa" or mbr[450] != 7:
            raise RuntimeError("Expected MBR/NTFS partition type missing")
        if struct.unpack_from("<II", mbr, 454) != (61, 586881):
            raise RuntimeError("Partition layout differs from documented fixture")
        source.seek(61 * 512)
        boot = source.read(512)
        if boot[3:11] != b"NTFS    " or struct.unpack_from("<H", boot, 11)[0] != 512:
            raise RuntimeError("Expected NTFS boot sector missing")
        source.seek(107005 * 512)
        expected = source.read(4296)
        if hashlib.sha256(expected).hexdigest() != DELETED_FILE_SHA256:
            raise RuntimeError("Documented Bunda.txt extent differs from pinned bytes")
    return image


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=Path("/tmp/data-recovery-fixtures"))
    args = parser.parse_args()
    image = fetch(args.destination.expanduser().resolve())
    print(json.dumps({
        "fixture": "NIST CFReDS DFR-01 NTFS",
        "image": str(image),
        "image_bytes": IMAGE_SIZE,
        "image_sha256": IMAGE_SHA256,
        "filesystem_offset_sectors": 61,
        "sector_size": 512,
        "documented_deleted_file": {
            "name": "Bunda.txt", "length": 4296,
            "absolute_image_offset": 107005 * 512,
            "fixture_extent_sha256": DELETED_FILE_SHA256,
            "hash_provenance": "Computed from documented post-deletion fixture extent; not a separate original",
        },
    }, indent=2))


if __name__ == "__main__":
    main()
