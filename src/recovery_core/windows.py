"""Windows volume discovery and read-only source identification; no disk writes."""
from __future__ import annotations

import base64
import ctypes
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import re
import subprocess
from .control import checkpoint

from .common import RecoveryError
from .tsk import run_bounded


@dataclass(frozen=True)
class VolumeSource:
    mount: str


_DISCOVER = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
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
    return _query_volumes(_DISCOVER)


def _query_volumes(script: str) -> list[dict]:
    """Bound and cancel Storage queries just like the recovery subprocesses."""
    checkpoint()
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    output = io.BytesIO()
    try:
        run_bounded([str(executable), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                    output, limit=4 * 1024 * 1024, timeout=30)
    except (OSError, RecoveryError) as exc:
        raise RecoveryError("无法查询 Windows 磁盘，请检查连接、Storage 服务或管理员权限。") from exc
    try:
        data = json.loads(output.getvalue().decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise RecoveryError("Windows 返回了无效的磁盘列表，请刷新后重试。") from exc
    return validate_volume_rows(data)


def validate_volume_rows(data) -> list[dict]:
    if not isinstance(data, list):
        raise RecoveryError("Windows 返回了无效的磁盘列表。")
    for row in data:
        if (not isinstance(row, dict) or not isinstance(row.get("mount"), str)
                or not re.fullmatch(r"[A-Za-z]:\\", row["mount"])):
            raise RecoveryError("Windows 返回了无效的盘符。")
        if (type(row.get("size")) is not int or row["size"] <= 0
                or not isinstance(row.get("disk_numbers"), list)
                or any(type(n) is not int or n < 0 for n in row["disk_numbers"])
                or not isinstance(row.get("disk_ids"), list)
                or any(not isinstance(n, str) or not n for n in row["disk_ids"])
                or type(row.get("sector_size")) is not int
                or row["sector_size"] not in (512, 1024, 2048, 4096)
                or not isinstance(row.get("guid"), str)
                or not isinstance(row.get("label", ""), str)
                or type(row.get("system", False)) is not bool):
            raise RecoveryError("磁盘身份或布局不完整，未允许直接访问。")
    return data


def volume_identity(mount: str) -> dict:
    if os.name != "nt":
        raise RecoveryError("直接磁盘扫描仅在 Windows 上提供；此系统可扫描镜像。")
    if not isinstance(mount, str) or not re.fullmatch(r"[A-Za-z]:\\?", mount):
        raise RecoveryError("只支持明确的 Windows 卷盘符。")
    mount = mount[:2].upper() + "\\"
    matches = [v for v in list_volumes() if v["mount"].upper() == mount]
    if (len(matches) != 1 or not matches[0]["guid"] or not matches[0]["disk_numbers"]
            or not matches[0]["disk_ids"]):
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
    rows = _query_volumes(script)
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
    except OSError as exc:
        raise RecoveryError("源磁盘无法读取，可能已断开、锁定或发生读取错误。请检查连接后重新扫描。") from exc
    if boot[3:11] != b"NTFS    " or boot[510:512] != b"\x55\xaa":
        raise RecoveryError("未读到 NTFS 引导信息；磁盘可能被加密、锁定或不受支持。")
    return boot


def is_admin() -> bool:
    return os.name == "nt" and bool(ctypes.windll.shell32.IsUserAnAdmin())


def elevate(arguments: list[str]):
    if os.name != "nt":
        raise RecoveryError("此操作仅用于 Windows。")
    error = _shell_elevate(arguments)
    if error == 1223:  # ERROR_CANCELLED, including dismissal of the UAC prompt.
        raise RecoveryError("已取消管理员授权，当前窗口和扫描记录已保留。")
    if error:
        raise RecoveryError(f"管理员启动失败（Windows 错误 {error}），当前窗口已保留。")


def _shell_elevate(arguments: list[str]) -> int:
    from ctypes import wintypes
    import sys

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG),
                    ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
                    ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
                    ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE),
                    ("hProcess", wintypes.HANDLE)]

    execute = ctypes.WinDLL("shell32", use_last_error=True).ShellExecuteExW
    execute.argtypes, execute.restype = [ctypes.POINTER(ShellExecuteInfo)], wintypes.BOOL
    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x100 | 0x400  # Finish launch before returning; show errors in our window.
    info.lpVerb, info.lpFile = "runas", sys.executable
    info.lpParameters, info.nShow = subprocess.list2cmdline(arguments), 1
    if execute(ctypes.byref(info)):
        return 0
    return ctypes.get_last_error() or 1
