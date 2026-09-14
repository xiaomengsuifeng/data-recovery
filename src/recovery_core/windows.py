"""Windows volume discovery and read-only source identification; no disk writes."""
from __future__ import annotations

import base64
import ctypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
from .control import checkpoint

from .common import RecoveryError


@dataclass(frozen=True)
class VolumeSource:
    mount: str


_DISCOVER = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$rows = @(Get-Volume | Where-Object { $_.DriveLetter -and $_.FileSystem -eq 'NTFS' } | ForEach-Object {
  $v = $_
  $p = @(Get-Partition -DriveLetter $v.DriveLetter -ErrorAction SilentlyContinue)
  $d = @($p | Get-Disk -ErrorAction SilentlyContinue)
  [pscustomobject]@{
    mount = ([string]$v.DriveLetter + ':\'); label = [string]$v.FileSystemLabel
    size = [long]$v.Size; free = [long]$v.SizeRemaining; guid = [string]$v.UniqueId
    disk_numbers = @($d | ForEach-Object { [int]$_.Number } | Sort-Object -Unique)
    disk_ids = @($d | ForEach-Object { [string]$_.UniqueId } | Sort-Object -Unique)
    sector_size = if ($d.Count) { [int]$d[0].LogicalSectorSize } else { 512 }
    system = [bool](@($d | Where-Object { $_.IsBoot -or $_.IsSystem }).Count)
  }
})
ConvertTo-Json -InputObject $rows -Depth 5 -Compress
"""


def list_volumes() -> list[dict]:
    if os.name != "nt":
        return []
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    encoded = base64.b64encode(_DISCOVER.encode("utf-16le")).decode("ascii")
    result = subprocess.run([str(executable), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW, check=False)
    if result.returncode:
        raise RecoveryError("无法枚举 Windows 磁盘，请检查 Storage 服务或管理员权限。")
    checkpoint()
    data = json.loads(result.stdout.decode("utf-8-sig"))
    return validate_volume_rows(data)


def validate_volume_rows(data) -> list[dict]:
    if not isinstance(data, list):
        raise RecoveryError("Windows 返回了无效的磁盘列表。")
    for row in data:
        if not isinstance(row, dict) or not re.fullmatch(r"[A-Za-z]:\\", row.get("mount", "")):
            raise RecoveryError("Windows 返回了无效的盘符。")
        if (type(row.get("size")) is not int or row["size"] <= 0
                or not isinstance(row.get("disk_numbers"), list)
                or any(type(n) is not int or n < 0 for n in row["disk_numbers"])
                or not isinstance(row.get("disk_ids"), list)
                or any(not isinstance(n, str) or not n for n in row["disk_ids"])
                or row.get("sector_size") not in (512, 1024, 2048, 4096)):
            raise RecoveryError("磁盘身份或布局不完整，未允许直接访问。")
    return data


def volume_identity(mount: str) -> dict:
    if os.name != "nt":
        raise RecoveryError("直接磁盘扫描仅在 Windows 上提供；此系统可扫描镜像。")
    if not re.fullmatch(r"[A-Za-z]:\\?", mount):
        raise RecoveryError("只支持明确的 Windows 卷盘符。")
    mount = mount[:2].upper() + "\\"
    matches = [v for v in list_volumes() if v["mount"].upper() == mount]
    if len(matches) != 1 or not matches[0]["guid"] or not matches[0]["disk_numbers"]:
        raise RecoveryError("无法确认此 NTFS 卷对应的物理磁盘。请检查磁盘连接。")
    value = matches[0]
    return dict(value, kind="windows_volume", path="\\\\.\\" + mount[:2])


def check_volume(expected: dict) -> dict:
    actual = volume_identity(expected["mount"])
    if expected.get("path") != actual["path"]:
        raise RecoveryError("扫描记录的设备路径与实际盘符不匹配。")
    for key in ("guid", "size", "disk_numbers", "disk_ids", "sector_size"):
        if actual.get(key) != expected.get(key):
            raise RecoveryError("源磁盘的身份或布局已变化，请重新扫描。")
    return actual


def ensure_other_disk(source: dict, destination: Path):
    if source.get("kind") != "windows_volume":
        return
    destination = destination.absolute()
    existing = destination
    while not existing.exists():
        if existing.parent == existing:
            raise RecoveryError("目标磁盘不可访问，请检查连接。")
        existing = existing.parent
    actual_path = existing.resolve(strict=True)
    drive = actual_path.drive
    if not re.fullmatch(r"[A-Za-z]:", drive):
        raise RecoveryError("恢复目标与工作目录需要位于另一块本地物理磁盘。")
    # Query all filesystem types for the destination, not just NTFS volumes.
    script = _DISCOVER.replace("$_.DriveLetter -and $_.FileSystem -eq 'NTFS'", "$_.DriveLetter")
    encoded = base64.b64encode(script.encode("utf-16le")).decode()
    ps = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe")
    result = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                            capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RecoveryError("无法核对目标物理磁盘。")
    checkpoint()
    rows = validate_volume_rows(json.loads(result.stdout.decode("utf-8-sig")))
    matches = [v for v in rows if v["mount"][:2].upper() == drive.upper()]
    if len(matches) != 1 or not matches[0]["disk_numbers"]:
        raise RecoveryError("无法确认目标位置所在的物理磁盘。")
    if set(source["disk_numbers"]) & set(matches[0]["disk_numbers"]):
        raise RecoveryError("工作目录或恢复目标与源文件位于同一块物理磁盘。请改选另一块磁盘。")


def ensure_safe_locations(source: dict, *locations: Path):
    """Reapply the placement policy when reopening an existing live session."""
    if source.get("kind") != "windows_volume":
        return
    import sys
    for location in (Path(sys.executable), Path(__file__), *locations):
        ensure_other_disk(source, location)


def read_volume_boot(identity: dict) -> bytes:
    if os.name != "nt":
        raise RecoveryError("Windows 卷读取不可用于当前系统。")
    # Python's Windows file opening uses a read-only handle. No format, repair,
    # TRIM, mount, snapshot, lock, or write operation is issued by this module.
    try:
        with open(identity["path"], "rb", buffering=0) as source:
            boot = source.read(4096)
    except PermissionError as exc:
        raise RecoveryError("读取磁盘需要管理员权限，请使用“以管理员身份重新启动”。") from exc
    if boot[3:11] != b"NTFS    " or boot[510:512] != b"\x55\xaa":
        raise RecoveryError("未读到 NTFS 引导信息；磁盘可能被加密、锁定或不受支持。")
    return boot


def is_admin() -> bool:
    return os.name == "nt" and bool(ctypes.windll.shell32.IsUserAnAdmin())


def elevate(arguments: list[str]):
    if os.name != "nt":
        raise RecoveryError("此操作仅用于 Windows。")
    import sys
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable,
                                                subprocess.list2cmdline(arguments), None, 1)
    if result <= 32:
        raise RecoveryError("管理员启动未完成。")
