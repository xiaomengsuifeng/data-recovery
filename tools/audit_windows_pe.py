#!/usr/bin/env python3
"""Static PE architecture/import audit; this does not execute or emulate Windows.

Development dependency: pefile==2024.8.26. Check-Package.cmd performs the real
Qt/TSK startup checks when the package is subsequently run on Windows.
"""
import argparse
import json
from pathlib import Path

import pefile

SYSTEM_DLLS = set("""
advapi32.dll authz.dll bcrypt.dll bcryptprimitives.dll cabinet.dll cfgmgr32.dll
comctl32.dll comdlg32.dll crypt32.dll cryptbase.dll cryptsp.dll d2d1.dll d3d9.dll
d3d11.dll d3d12.dll d3dcompiler_47.dll dbghelp.dll dnsapi.dll dwmapi.dll dwrite.dll
dxgi.dll gdi32.dll glu32.dll imm32.dll iphlpapi.dll kernel32.dll kernelbase.dll
mpr.dll msvcrt.dll ncrypt.dll netapi32.dll normaliz.dll ntdll.dll ole32.dll
oleaut32.dll opengl32.dll powrprof.dll propsys.dll psapi.dll rpcrt4.dll secur32.dll
setupapi.dll shell32.dll shlwapi.dll user32.dll userenv.dll uxtheme.dll version.dll
winhttp.dll wininet.dll winmm.dll winspool.drv wintrust.dll ws2_32.dll wtsapi32.dll
uiautomationcore.dll ucrtbase.dll
""".split())


def audit(root):
    groups = {
        "python_qt_x64": (0x8664, [root / "runtime", root / "runtime/Lib/site-packages/PySide6",
                                  root / "runtime/Lib/site-packages/shiboken6"]),
        "tsk_x86": (0x14c, [root / "vendor/tsk/bin"]),
    }
    errors, files, system = [], [], set()
    for group, (machine, search) in groups.items():
        libraries = {p.name.lower(): p for folder in search for p in folder.glob("*.dll")}
        base = search[0]
        for path in sorted(base.rglob("*")):
            if path.suffix.lower() not in (".exe", ".dll", ".pyd"):
                continue
            pe = pefile.PE(str(path), fast_load=True)
            pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                                                   pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"]])
            name = path.relative_to(root).as_posix()
            if pe.FILE_HEADER.Machine != machine:
                errors.append(f"{name}: expected architecture {machine:#x}, got {pe.FILE_HEADER.Machine:#x}")
            imports = sorted({e.dll.decode("ascii").lower() for key in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT")
                              for e in getattr(pe, key, [])})
            for dependency in imports:
                if dependency in libraries:
                    continue
                if dependency in SYSTEM_DLLS or dependency.startswith(("api-ms-win-", "ext-ms-win-")):
                    system.add(dependency)
                else:
                    errors.append(f"{name}: unresolved imported library {dependency}")
            files.append({"path": name, "group": group, "architecture": hex(pe.FILE_HEADER.Machine), "imports": imports})
            pe.close()
    if not files:
        errors.append("No Windows executables found")
    return {"schema_version": 1, "check": "static_pe_architecture_and_imports", "windows_execution_verified": False,
            "passed": not errors, "errors": errors, "files": files, "windows_system_dependencies": sorted(system)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.package)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps({"passed": result["passed"], "files_checked": len(result["files"]), "errors": result["errors"]}, indent=2))
    raise SystemExit(0 if result["passed"] else 1)
