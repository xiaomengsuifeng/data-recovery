from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .common import RecoveryError, write_json
from .service import recover, scan
from .tsk import Tsk


def positive(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NTFS 镜像恢复原型（仅文件输入，不访问真实设备）")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="检查外部 TSK 工具")
    scan_parser = commands.add_parser("scan", help="扫描 NTFS 镜像中的删除候选")
    scan_parser.add_argument("image", type=Path)
    scan_parser.add_argument("--output", type=Path, required=True, help="新的扫描会话目录")
    scan_parser.add_argument("--offset", type=int, default=0, help="文件系统起始扇区")
    scan_parser.add_argument("--sector-size", type=int, choices=(512, 1024, 2048, 4096), default=512)
    scan_parser.add_argument("--max-candidates", type=positive, default=10000)
    recovery_parser = commands.add_parser("recover", help="将候选导出到新目录")
    recovery_parser.add_argument("session", type=Path)
    recovery_parser.add_argument("--destination", type=Path, required=True)
    recovery_parser.add_argument("--id", action="append", dest="candidate_ids", help="仅导出指定候选，可重复")
    recovery_parser.add_argument("--max-file-bytes", type=positive, default=1024 ** 3)
    verify_parser = commands.add_parser("verify", help="用测试原件清单验证实际导出文件")
    verify_parser.add_argument("recovery", type=Path, help="包含 recovery.json 的导出目录")
    verify_parser.add_argument("--manifest", type=Path, required=True)
    verify_parser.add_argument("--output", type=Path, help="新的 JSON 报告文件，默认只输出到终端")
    for child in (doctor, scan_parser, recovery_parser):
        child.add_argument("--tsk-bin", type=Path, help="包含 fls 和 icat 的目录")
        child.add_argument("--timeout", type=positive, default=120, help="每个 TSK 子进程的秒数上限")
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            from .verification import verify
            result = verify(args.recovery, args.manifest)
            if args.output:
                write_json(args.output, result)
        else:
            backend = Tsk(args.tsk_bin, timeout=args.timeout)
            if args.command == "doctor":
                result = {"prototype_version": __version__, "executables": backend.executables,
                          "versions": backend.versions, "input_mode": "raw_image_only"}
            elif args.command == "scan":
                report = scan(args.image, args.output, backend=backend, offset=args.offset,
                              sector_size=args.sector_size, max_candidates=args.max_candidates)
                result = {"status": report["status"], "candidate_count": report["candidate_count"],
                          "session": str((args.output / "session.json").absolute()),
                          "warnings": report["warnings"]}
            else:
                report = recover(args.session, args.destination, backend=backend,
                                 candidate_ids=args.candidate_ids, max_file_bytes=args.max_file_bytes)
                result = {k: v for k, v in report.items() if k not in ("source", "results")}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("status") == "cancelled":
            return 130
        if result.get("status") == "source_changed":
            return 2
        if args.command == "recover" and any(result.get(key) for key in ("failed_count", "partial_count", "skipped_count")):
            return 1
        if args.command == "verify" and (not result["recovery_completed"] or result["exact_content_matches"] != result["total_targets"]):
            return 1
        return 0
    except KeyboardInterrupt:
        print("已取消。保留已创建的输出目录供检查；重试请使用新目录。", file=sys.stderr)
        return 130
    except (RecoveryError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
