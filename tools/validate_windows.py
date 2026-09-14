#!/usr/bin/env python3
"""Record Windows image-workflow checks without accessing raw volumes.

Runs the complete unit suite, TSK startup, Windows PowerShell syntax checks,
read-only volume enumeration, and the real Qt/NIST image demo. Each invocation
requires a new output directory. VHD creation and UAC are deliberately separate
manual acceptance checks; an image-workflow pass cannot certify live recovery.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recovery_core.common import new_directory, write_json


def test_result_summary(result: unittest.TestResult) -> dict:
    return {
        "schema_version": 1,
        "tests_run": result.testsRun,
        "passed": result.wasSuccessful() and result.testsRun > 0,
        "failures": [test.id() for test, _ in result.failures],
        "errors": [test.id() for test, _ in result.errors],
        "skipped": [{"test": test.id(), "reason": reason} for test, reason in result.skipped],
        "expected_failures": [test.id() for test, _ in result.expectedFailures],
        "unexpected_successes": [test.id() for test in result.unexpectedSuccesses],
    }


def run_unit_tests(report_path: Path) -> int:
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    summary = test_result_summary(result)
    write_json(report_path, summary)
    return 0 if summary["passed"] else 1


def run_step(name: str, command: list[str], output: Path, environment: dict,
             *, timeout: float = 120) -> dict:
    log_path = output / (name + ".log")
    started = time.monotonic()
    record = {"name": name, "log": log_path.name, "status": "failed", "returncode": None}
    with log_path.open("xb") as log:
        try:
            with subprocess.Popen(
                    command, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT,
                    **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {})) as child:
                try:
                    code = child.wait(timeout=timeout)
                    record.update(returncode=code, status="passed" if code == 0 else "failed")
                finally:
                    if child.poll() is None:
                        # The Windows venv redirector and demo workers can own
                        # child processes. Kill this check's tree before its
                        # parent exits, so descendants cannot retain the log.
                        try:
                            if os.name == "nt":
                                taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"
                                cleanup = subprocess.run(
                                    [str(taskkill), "/PID", str(child.pid), "/T", "/F"],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=15, check=False, creationflags=subprocess.CREATE_NO_WINDOW)
                                if cleanup.returncode and child.poll() is None:
                                    record["cleanup_error"] = f"taskkill exited with {cleanup.returncode}"
                        except (OSError, subprocess.TimeoutExpired) as exc:
                            record["cleanup_error"] = str(exc)
                        finally:
                            if child.poll() is None:
                                child.kill()
                            child.wait()
        except subprocess.TimeoutExpired:
            record.update(status="timed_out", error=f"Exceeded {timeout:g} seconds")
        except OSError as exc:
            record["error"] = str(exc)
        if record.get("error"):
            log.write(("\n" + record["error"] + "\n").encode("utf-8"))
    record["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return record


def powershell_check_command() -> list[str]:
    # Parse only: never dot-source or execute the fixture/shortcut scripts.
    script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$failures = 0
foreach ($file in Get-ChildItem -LiteralPath 'tools/windows' -Filter '*.ps1') {
    $tokens = $null
    $errors = $null
    $null = [System.Management.Automation.Language.Parser]::ParseFile(
        $file.FullName, [ref]$tokens, [ref]$errors)
    Write-Output ($file.Name + ': ' + $errors.Count + ' parse errors')
    foreach ($error in $errors) { Write-Output $error.Message }
    $failures += $errors.Count
}
Write-Output ('Windows PowerShell ' + $PSVersionTable.PSVersion)
if ($failures) { exit 1 }
"""
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return [str(executable), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tsk-bin", type=Path, required=True)
    parser.add_argument("--fixture-cache", type=Path, default=ROOT / "artifacts/fixtures")
    parser.add_argument("--qt-platform", choices=("offscreen", "windows"), default="offscreen",
                        help="Use windows to exercise the native Qt platform and show the demo window")
    args = parser.parse_args(argv)
    if os.name != "nt":
        parser.error("Run this validation on Windows; use the individual demos on other systems.")

    output = new_directory(args.output)
    tsk_bin = args.tsk_bin.resolve()
    environment = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONUTF8="1",
                       PYTHONDONTWRITEBYTECODE="1", QT_QPA_PLATFORM="offscreen")
    metadata = {
        "schema_version": 1, "started_utc": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(), "python": sys.version,
        "process_bits": 64 if sys.maxsize > 2**32 else 32,
        "desktop_qt_platform_requested": args.qt_platform,
    }
    write_json(output / "environment.json", metadata)
    python = [sys.executable, "-B"]
    test_command = python + ["-c", "from pathlib import Path; import sys; "
                            "from tools.validate_windows import run_unit_tests; "
                            "sys.exit(run_unit_tests(Path(sys.argv[1])))", str(output / "tests.json")]
    discovery = python + ["-c", "import json; from recovery_core.windows import list_volumes; "
                          "rows = list_volumes(); print(json.dumps({'ntfs_volume_count': len(rows), "
                          "'physical_disk_count': len({n for r in rows for n in r['disk_numbers']})}))"]
    steps = []
    commands = [
        ("unit-tests", test_command),
        ("tsk-startup", python + ["-m", "recovery_core", "doctor", "--tsk-bin", str(tsk_bin)]),
        ("powershell-syntax", powershell_check_command()),
        ("volume-discovery", discovery),
    ]
    for name, command in commands:
        step = run_step(name, command, output, environment)
        steps.append(step)
        print(f"{name}: {step['status']}", flush=True)

    desktop_environment = dict(environment, QT_QPA_PLATFORM=args.qt_platform)
    demo = python + [str(ROOT / "tools/run_desktop_demo.py"), "--output", str(output / "desktop"),
                     "--fixture-cache", str(args.fixture_cache.resolve()), "--tsk-bin", str(tsk_bin)]
    desktop_step = run_step("desktop-image-workflow", demo, output, desktop_environment, timeout=600)
    steps.append(desktop_step)
    print(f"{desktop_step['name']}: {desktop_step['status']}", flush=True)

    tests_path = output / "tests.json"
    tests = json.loads(tests_path.read_text(encoding="utf-8")) if tests_path.is_file() else None
    passed = all(step["status"] == "passed" for step in steps) and bool(tests and tests["passed"])
    summary = {
        "schema_version": 1, "status": "passed" if passed else "failed",
        "scope": "windows_image_workflow", "steps": steps, "tests": tests,
        "windows_image_workflow_verified": desktop_step["status"] == "passed",
        "full_windows_acceptance_verified": False,
        "remaining_manual_checks": [
            "UAC elevation and cancellation",
            "Isolated VHD creation, direct deletion and Windows Shell recycle-bin emptying",
            "Live-volume reads, source/destination disk isolation and device disconnection",
            "Portable package startup, high-DPI layouts and physical-media recovery",
        ],
    }
    write_json(output / "validation.json", summary)
    print(json.dumps({"output": str(output), "status": summary["status"],
                      "tests_run": tests["tests_run"] if tests else None,
                      "skipped_tests": len(tests["skipped"]) if tests else None,
                      "full_windows_acceptance_verified": False}, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
