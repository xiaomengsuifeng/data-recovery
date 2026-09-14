#!/usr/bin/env python3
"""Download locked upstream runtime/source archives for the offline builder."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def fetch(item, cache):
    destination = cache / item["filename"]
    expected = item["sha256"]
    if destination.exists():
        with destination.open("rb") as existing:
            actual = hashlib.file_digest(existing, "sha256").hexdigest()
        if destination.stat().st_size == item["size"] and actual == expected:
            return destination.name + " already verified"
        raise ValueError("Existing archive does not match lock: " + destination.name)
    part = destination.with_name(destination.name + ".incomplete")
    digest, length = hashlib.sha256(), 0
    request = urllib.request.Request(item["url"], headers={"User-Agent": "ShiHui-build/0.2.0"})
    with urllib.request.urlopen(request, timeout=60) as source, part.open("xb") as target:
        while block := source.read(1024 * 1024):
            length += len(block)
            if length > item["size"]:
                raise ValueError("Archive exceeded expected length")
            digest.update(block)
            target.write(block)
    if length != item["size"] or digest.hexdigest() != expected:
        raise ValueError("Downloaded archive does not match lock: " + destination.name)
    part.rename(destination)
    return destination.name + " verified"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=ROOT / "artifacts/downloads")
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    lock = json.loads((ROOT / "tools/runtime-lock.json").read_text())
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fetch, item, args.cache) for item in lock["archives"]]
        for future in as_completed(futures):
            print(future.result(), flush=True)
