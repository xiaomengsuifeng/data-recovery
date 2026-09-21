from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .common import RecoveryError, write_json
from .service import recover, scan, resume_scan
from .tsk import Tsk


def positive(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NTFS / exFAT 恢复与只读镜像采集")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="检查外部 TSK 工具")
    scan_parser = commands.add_parser("scan", help="扫描 NTFS / exFAT 镜像中的删除候选")
    scan_parser.add_argument("image", type=Path)
    scan_parser.add_argument("--output", type=Path, required=True, help="新的扫描会话目录")
    scan_parser.add_argument("--offset", type=int, default=0, help="文件系统起始扇区")
    scan_parser.add_argument("--sector-size", type=int, choices=(512, 1024, 2048, 4096), default=512)
    scan_parser.add_argument("--max-candidates", type=positive, default=10000)
    scan_parser.add_argument("--deep-png", action="store_true", help="额外扫描未分配空间中的连续 PNG；原名与路径未知")
    scan_parser.add_argument("--deep-log", action="store_true", help="从旧 NTFS 日志恢复复用记录的数据；缺口标为片段，仅部分日志格式")
    scan_parser.add_argument("--deep-jpeg", action="store_true", help="查找未分配空间中的 JPEG")
    scan_parser.add_argument("--reassemble-jpeg", action="store_true", help="尝试有界的 JPEG 碎片重组，结果需要逐张核对")
    resume_parser = commands.add_parser("resume-scan", help="核对源镜像并继续中断的扫描")
    resume_parser.add_argument("session", type=Path)
    acquire_parser = commands.add_parser("acquire", help="只读采集镜像，记录坏区并保存续采进度")
    source_group = acquire_parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--file", type=Path, help="源镜像/文件")
    source_group.add_argument("--volume", help="Windows 卷盘符，例如 E:")
    source_group.add_argument("--disk", type=int, help="Windows 物理磁盘编号")
    acquire_parser.add_argument("--output", type=Path, required=True)
    acquire_parser.add_argument("--block-bytes", type=positive, default=16 * 1024 * 1024)
    acquire_parser.add_argument("--retries", type=int, default=1, help="坏扇区额外重试次数（0..10）")
    acquire_parser.add_argument("--timeout", type=positive, default=30, help="单次读取超时秒数（1..300）")
    resume_acquire_parser = commands.add_parser("resume-acquire", help="校验已采集数据，再续采并重试坏区")
    resume_acquire_parser.add_argument("directory", type=Path)
    recovery_parser = commands.add_parser("recover", help="将候选导出到新目录")
    recovery_parser.add_argument("session", type=Path)
    recovery_parser.add_argument("--destination", type=Path, required=True)
    recovery_parser.add_argument("--id", action="append", dest="candidate_ids", help="仅导出指定候选，可重复")
    recovery_parser.add_argument("--max-file-bytes", type=positive, default=1024 ** 3)
    verify_parser = commands.add_parser("verify", help="用测试原件清单验证实际导出文件")
    verify_parser.add_argument("recovery", type=Path, help="包含 recovery.json 的导出目录")
    verify_parser.add_argument("--manifest", type=Path, required=True)
    verify_parser.add_argument("--output", type=Path, help="新的 JSON 报告文件，默认只输出到终端")
    fixture_parser = commands.add_parser("validate-fixture", help="按隔离样本各阶段的原件清单验收恢复结果")
    fixture_parser.add_argument("fixture", type=Path, help="New-RecoveryFixture.ps1 生成的完整样本目录")
    fixture_parser.add_argument("--output", type=Path, required=True, help="样本目录之外的全新验收结果目录")
    fixture_parser.add_argument("--deep-png", action="store_true", help="每个阶段额外执行 PNG 深度扫描")
    fixture_parser.add_argument("--deep-log", action="store_true", help="每个阶段额外检查旧 NTFS 日志")
    for child in (doctor, scan_parser, resume_parser, recovery_parser, fixture_parser):
        child.add_argument("--tsk-bin", type=Path, help="包含 fls 和 icat 的目录")
        child.add_argument("--timeout", type=positive, default=120, help="每个 TSK 子进程的秒数上限")
    args = parser.parse_args(argv)
    try:
        if args.command in ("acquire", "resume-acquire"):
            from .acquisition import acquire, resume_acquisition
            from .windows import VolumeSource, DiskSource
            if args.command == "acquire":
                source = args.file if args.file else VolumeSource(args.volume) if args.volume else DiskSource(args.disk)
                report = acquire(source, args.output, block_bytes=args.block_bytes, retries=args.retries, timeout=args.timeout)
                directory = args.output
            else:
                report = resume_acquisition(args.directory)
                directory = args.directory
            result = {k: v for k, v in report.items() if k != "ranges"}
            result["report"] = str((directory / "acquisition.json").absolute())
        elif args.command == "verify":
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
                              sector_size=args.sector_size, max_candidates=args.max_candidates,
                              deep_png=args.deep_png, deep_log=args.deep_log,
                              deep_jpeg=args.deep_jpeg or args.reassemble_jpeg, reassemble_jpeg=args.reassemble_jpeg)
                result = {"status": report["status"], "candidate_count": report["candidate_count"],
                          "session": str((args.output / "session.json").absolute()),
                          "warnings": report["warnings"]}
            elif args.command == "resume-scan":
                report = resume_scan(args.session, backend=backend)
                result = {"status": report["status"], "candidate_count": report["candidate_count"],
                          "session": str((args.session / "session.json").absolute())}
            elif args.command == "validate-fixture":
                from .fixture_validation import validate_fixture
                result = validate_fixture(args.fixture, args.output, backend=backend,
                                          deep_png=args.deep_png, deep_log=args.deep_log)
            else:
                report = recover(args.session, args.destination, backend=backend,
                                 candidate_ids=args.candidate_ids, max_file_bytes=args.max_file_bytes)
                result = {k: v for k, v in report.items() if k not in ("source", "results")}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "validate-fixture":
            return {"passed": 0, "incomplete": 1, "failed": 2, "cancelled": 130}[result["status"]]
        if result.get("status") == "cancelled":
            return 130
        if result.get("status") == "source_changed":
            return 2
        if result.get("status") == "failed":
            return 2
        if result.get("status") == "completed_with_errors":
            return 1
        if args.command == "recover" and any(result.get(key) for key in ("failed_count", "partial_count", "skipped_count")):
            return 1
        if args.command == "verify" and (not result["recovery_completed"] or result["exact_content_matches"] != result["total_targets"]):
            return 1
        return 0
    except KeyboardInterrupt:
        print("已取消。已保存的镜像扫描和采集进度可用 resume-scan / resume-acquire 继续。", file=sys.stderr)
        return 130
    except (RecoveryError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
