"""Bounded, passive previews. Never launches recovered files or evaluates markup."""
from __future__ import annotations

import io
from pathlib import Path
import zipfile
from xml.etree import ElementTree

from .common import RecoveryError, check_identity, read_json
from .service import _validate_candidates, extract_candidate
from .windows import ensure_safe_locations

MAX_PREVIEW_BYTES = 32 * 1024 * 1024
MAX_TEXT = 32000
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tif", ".tiff"}
TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".log", ".xml", ".html", ".ini", ".py", ".js", ".css", ".yaml", ".yml"}


def describe(data: bytes, name: str) -> dict:
    suffix = Path(name).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return {"kind": "image", "data": data, "note": "可显示的图像不代表文件与删除前完全一致。"}
    if suffix in {".docx", ".xlsx", ".pptx", ".odt"}:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = archive.namelist()
                if suffix == ".docx":
                    chosen = ["word/document.xml"]
                elif suffix == ".xlsx":
                    chosen = [n for n in names if n == "xl/sharedStrings.xml" or n.startswith("xl/worksheets/sheet")][:12]
                elif suffix == ".pptx":
                    chosen = sorted(n for n in names if n.startswith("ppt/slides/slide") and n.endswith(".xml"))[:12]
                else:
                    chosen = ["content.xml"]
                pieces = []
                for name in chosen:
                    info = archive.getinfo(name)
                    if info.file_size > 2 * 1024 * 1024:
                        raise RecoveryError("文档内容过大，暂不展开预览。")
                    raw = archive.read(name)
                    if b"\0" in raw or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                        raise RecoveryError("此文档包含不支持的 XML 声明。")
                    root = ElementTree.fromstring(raw)
                    pieces.append(" ".join(text for text in root.itertext() if text.strip())[:MAX_TEXT])
                return {"kind": "text", "text": "\n\n".join(pieces)[:MAX_TEXT], "note": "文档文字摘录；未执行宏，未还原排版。"}
        except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, RuntimeError, OSError) as exc:
            return {"kind": "text", "text": "文档结构未能解析。可以尝试保存文件后检查。", "note": str(exc)}
    if suffix in TEXT_EXTENSIONS:
        sample = data[:MAX_TEXT * 4]
        for encoding in ("utf-8-sig", "utf-16" if sample[:2] in (b"\xff\xfe", b"\xfe\xff") else "gb18030"):
            try:
                value = sample.decode(encoding)
                if "\0" not in value:
                    return {"kind": "text", "text": value[:MAX_TEXT], "note": "文字预览最多显示 32,000 个字符。"}
            except UnicodeError:
                pass
    lines = []
    for start in range(0, min(len(data), 2048), 16):
        block = data[start:start + 16]
        readable = "".join(chr(b) if 32 <= b < 127 else "." for b in block)
        lines.append(f"{start:08x}  {block.hex(' '):47}  {readable}")
    return {"kind": "text", "format": "hex", "text": "\n".join(lines),
            "note": "内容未能按支持的格式显示；以下为前 2 KiB 字节，仍可保存文件。"}


def preview(session: Path, candidate_id: str, backend) -> dict:
    report = read_json(session / "session.json")
    _validate_candidates(report.get("candidates"))
    selected = [item for item in report["candidates"] if item["id"] == candidate_id]
    if len(selected) != 1:
        raise RecoveryError("预览目标不存在。")
    item = selected[0]
    if item["size"] > MAX_PREVIEW_BYTES:
        raise RecoveryError("此文件超过 32 MiB 预览上限。可直接选择并保存，恢复不受此预览限制。")
    if backend.versions != report["backend"]["versions"]:
        raise RecoveryError("恢复引擎版本已变化，请重新扫描。")
    check_identity(report["source"])
    ensure_safe_locations(report["source"], session)
    with io.BytesIO() as output:
        extract_candidate(Path(report["source"]["path"]), report["offset"], report["sector_size"],
                          item, output, backend=backend)
        data = output.getvalue()
    check_identity(report["source"])
    result = describe(data, item["original_path"] or item["observed_path"])
    if item.get("recovery_method") in ("png_carving", "jpeg_carving"):
        result["note"] = "深度扫描生成名称，原名与目录未知。 " + result["note"]
        if item.get("carving", {}).get("reconstructed"):
            result["note"] = "JPEG 碎片推测重组，可能存在错误拼接，请核对图片内容。 " + result["note"]
    if item.get("recovery_method") == "ntfs_log":
        result["note"] = "旧日志关联的历史文件，内容仍需核对。 " + result["note"]
        if item["content_status"] == "fragment":
            info = item["ntfs_log"]
            result["note"] = (f"不完整片段：原文件 {info['original_size']} 字节，从字节 {info['file_offset']} 开始，"
                              f"本片段 {item['size']} 字节。 " + result["note"])
    result.update(candidate_id=candidate_id, size=len(data), expected_size=item["size"])
    if len(data) != item["size"]:
        result["note"] = "读取长度与记录不同，内容可能不完整。 " + result["note"]
    return result
