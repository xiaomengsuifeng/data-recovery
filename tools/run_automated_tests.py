#!/usr/bin/env python3
"""Run the complete software suite with per-case evidence and honest coverage.

No device acquisition, VHD creation, UAC or network access is performed here.
Native Qt/TSK and physical-media acceptance have separate, documented runners.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))


class RecordedResult(unittest.TextTestResult):
    """Keep skips, failures, subtests and durations distinct from test count."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = []
        self.current = None

    def startTest(self, test):
        super().startTest(test)
        self.started = time.monotonic()
        self.current = {"id": test.id(), "status": "running", "subtests": []}
        self.cases.append(self.current)

    def stopTest(self, test):
        if self.current["status"] == "running" and any(case["status"] == "skipped" for case in self.current["subtests"]):
            self.current["status"] = "incomplete"
        self.current["elapsed_seconds"] = round(time.monotonic() - self.started, 6)
        super().stopTest(test)

    def record(self, test, status, detail=None):
        # setUpClass/tearDownClass errors have no startTest call.
        if self.current is None or self.current["id"] != test.id():
            self.current = {"id": test.id(), "subtests": [], "elapsed_seconds": 0}
            self.cases.append(self.current)
        self.current["status"] = status
        if detail:
            self.current["detail"] = detail

    def addSuccess(self, test):
        super().addSuccess(test)
        self.record(test, "passed")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        parent = getattr(test, "test_case", None)
        if parent is not None and self.current is not None and self.current["id"] == parent.id():
            self.current["subtests"].append({"id": test.id(), "status": "skipped", "detail": reason})
        else:
            self.record(test, "skipped", reason)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.record(test, "failed", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self.record(test, "error", self._exc_info_to_string(err, test))

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.record(test, "expected_failure", self._exc_info_to_string(err, test))

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.record(test, "unexpected_success")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        status = "passed" if err is None else "failed" if issubclass(err[0], test.failureException) else "error"
        entry = {"id": subtest.id(), "status": status}
        if err is not None:
            entry["detail"] = self._exc_info_to_string(err, subtest)
            self.current["status"] = status
        self.current["subtests"].append(entry)


def summarize(result, *, require_no_skips=False):
    skips = [{"id": test.id(), "reason": reason} for test, reason in result.skipped]
    completed = result.testsRun > 0 and result.wasSuccessful()
    return {"schema_version": 1,
            "status": "failed" if not completed else "incomplete" if skips else "passed",
            "exit_code": 1 if not completed or (require_no_skips and skips) else 0,
            "tests_run": result.testsRun,
            "passed_tests": sum(case["status"] == "passed" for case in result.cases),
            "failure_count": len(result.failures), "error_count": len(result.errors),
            "skipped": skips, "expected_failures": len(result.expectedFailures),
            "unexpected_successes": len(result.unexpectedSuccesses), "cases": result.cases,
            "subtests_run": sum(len(case["subtests"]) for case in result.cases)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New evidence directory; never overwrites an earlier run")
    parser.add_argument("--require-no-skips", action="store_true")
    parser.add_argument("--min-lines", type=float, default=0)
    parser.add_argument("--min-branches", type=float, default=0)
    args = parser.parse_args(argv)
    if any(not 0 <= value <= 100 for value in (args.min_lines, args.min_branches)):
        parser.error("Coverage thresholds must be between 0 and 100.")
    try:
        import coverage
    except ImportError:
        parser.error('Install development test dependencies: python -m pip install -e ".[desktop,test]"')
    output = args.output.resolve()
    output.mkdir()  # No parents=True or exist_ok: preserve previous evidence.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PYTHONUTF8"] = "1"
    sys.dont_write_bytecode = True
    os.chdir(ROOT)
    collector = coverage.Coverage(source=[str(ROOT / "src")], branch=True,
                                  data_file=str(output / ".coverage"), config_file=False)
    metadata = {"started_utc": datetime.now(timezone.utc).isoformat(), "platform": platform.platform(),
                "python": sys.version, "executable": sys.executable, "coverage_version": coverage.__version__,
                "is_admin": False, "qt_platform": os.environ["QT_QPA_PLATFORM"]}
    if os.name == "nt":
        import ctypes
        metadata["is_admin"] = bool(ctypes.windll.shell32.IsUserAnAdmin())
    collector.start()
    try:
        with (output / "tests.log").open("x", encoding="utf-8") as log:
            suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
            result = unittest.TextTestRunner(stream=log, verbosity=2, resultclass=RecordedResult).run(suite)
    finally:
        collector.stop()
        collector.save()
    summary = summarize(result, require_no_skips=args.require_no_skips)
    collector.json_report(outfile=str(output / "coverage.json"))
    collector.html_report(directory=str(output / "html"))
    with (output / "coverage.txt").open("x", encoding="utf-8") as report:
        collector.report(file=report, show_missing=True)
    totals = json.loads((output / "coverage.json").read_text(encoding="utf-8"))["totals"]
    lines = 100 * totals["covered_lines"] / max(totals["num_statements"], 1)
    branches = 100 * totals["covered_branches"] / max(totals["num_branches"], 1)
    summary.update(environment=metadata, coverage={"line_percent": lines, "branch_percent": branches,
                   "minimum_lines": args.min_lines, "minimum_branches": args.min_branches,
                   "scope": "All src Python files in the test process. Native Qt, TSK and child-process execution are validated separately; no source files are excluded."},
                   physical_media_tested=False)
    if lines < args.min_lines or branches < args.min_branches:
        summary.update(status="failed", exit_code=1, coverage_gate_passed=False)
    else:
        summary["coverage_gate_passed"] = True
    with (output / "tests.json").open("x", encoding="utf-8") as report:
        json.dump(summary, report, ensure_ascii=False, indent=2)
        report.write("\n")
    print(json.dumps({key: summary[key] for key in ("status", "tests_run", "passed_tests", "subtests_run", "failure_count", "error_count", "skipped", "coverage")},
                     ensure_ascii=False, indent=2))
    print(f"Evidence: {output}")
    return summary["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
