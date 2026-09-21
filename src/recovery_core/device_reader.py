"""Isolated, read-only acquisition worker. The parent enforces a hard timeout."""
import argparse
import os
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("offset", type=int)
    parser.add_argument("size", type=int)
    args = parser.parse_args()
    if args.offset < 0 or not 0 < args.size <= 64 * 1024 * 1024:
        return 2
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
        with open(args.path, "rb", buffering=0) as stream:
            stream.seek(args.offset)
            remaining = args.size
            while remaining:
                data = stream.read(min(1024 * 1024, remaining))
                if not data:
                    raise OSError("Unexpected source EOF")
                sys.stdout.buffer.write(data)
                remaining -= len(data)
            sys.stdout.buffer.flush()
        return 0
    except OSError as exc:
        kind = "SOURCE_UNAVAILABLE" if isinstance(exc, (PermissionError, FileNotFoundError)) or getattr(exc, "winerror", None) in (5, 21, 1167) else "READ_ERROR"
        print(f"{kind}: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
