import copy
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from recovery_core.cli import main
from recovery_core.common import RecoveryError
from recovery_core.fixture_validation import validate_fixture
from recovery_core.service import scan
from recovery_core.tsk import parse_body
from test_core import body


class StagedBackend:
    versions = {"fls": "controlled-fixture", "icat": "controlled-fixture"}

    def __init__(self, listings, payloads):
        self.listings = listings
        self.payloads = payloads
        self.calls = []
        self.failed_stage = None
        self.cancelled_stage = None

    def scan(self, image, offset, sector):
        self.calls.append((image.name, offset, sector))
        if image.stem == self.failed_stage:
            raise RecoveryError("Controlled scan failure")
        if image.stem == self.cancelled_stage:
            raise KeyboardInterrupt
        return parse_body(self.listings[image.stem])

    def extract(self, image, offset, sector, inode, output, limit):
        output.write(self.payloads[inode])


class FixtureValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.fixture = self.base / "中文 样本"
        self.fixture.mkdir()
        self.output = self.base / "验收 结果"
        self.fixture_id = "1" * 32
        self.names = ("before-delete", "after-direct-delete", "after-move-to-recycle-bin", "after-empty-recycle-bin")
        samples = [
            ("Samples/direct/empty.txt", "direct-file", b""),
            ("Samples/direct/note.txt", "direct-file", b"direct note"),
            ("Samples/deleted-folder/nested/report.txt", "direct-directory", b"nested report"),
            ("Samples/deleted-folder/photo.png", "direct-directory", b"synthetic image bytes"),
            ("Samples/recycle-a/中文.txt", "recycle-bin", "中文原件".encode()),
            ("Samples/recycle-a/same-name.txt", "recycle-bin", b"same bytes"),
            ("Samples/recycle-b/same-name.txt", "recycle-bin", b"same bytes"),
            ("Samples/recycle-b/photo.png", "recycle-bin", b"another image"),
            ("Samples/retained/control.txt", "retained-control", b"retained"),
        ]
        self.originals, evidence, direct, recycled = [], [], [], []
        payloads = {}
        self.index_inodes = []
        for index, (relative, scenario, data) in enumerate(samples):
            path = self.fixture / "originals" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            original = {"original_path": "Z:\\" + relative.replace("/", "\\"), "relative_path": relative,
                        "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                        "scenario": scenario, "synthetic": True}
            self.originals.append(original)
            inode = f"{65 + index}-128-1"
            payloads[inode] = data
            if index < 4:
                direct.append(body("/" + relative, inode=inode, size=len(data)))
            elif index < 8:
                parent = "$Recycle.Bin/S-1-5-21-1-2-3-1001/"
                suffix = f"ABC{index:03}.txt"
                index_path, content_path = parent + "$I" + suffix, parent + "$R" + suffix
                encoded = (original["original_path"] + "\0").encode("utf-16le")
                metadata = struct.pack("<QQQI", 2, len(data), 133444736000000000, len(encoded) // 2) + encoded
                index_inode = f"{100 + index}-128-1"
                self.index_inodes.append(index_inode)
                payloads[index_inode] = metadata
                recycled.extend((body("/" + index_path, inode=index_inode, size=len(metadata)),
                                 body("/" + content_path, inode=inode, size=len(data))))
                evidence.append({"original_path": original["original_path"], "size": len(data),
                                 "sha256": original["sha256"], "index_version": 2,
                                 "index_path": "Z:\\" + index_path.replace("/", "\\"),
                                 "content_path": "Z:\\" + content_path.replace("/", "\\")})
        self.listings = {self.names[0]: "", self.names[1]: "".join(direct),
                         self.names[2]: "".join(direct), self.names[3]: "".join(direct + recycled)}
        self.backend = StagedBackend(self.listings, payloads)
        self.write("originals.manifest.json", {"schema_version": 1, "fixture_id": self.fixture_id, "files": self.originals})
        self.write("environment.json", {"fixture_id": self.fixture_id, "script_sha256": "a" * 64,
                                        "note": "Controlled unit-test bundle, not an executed Windows fixture"})
        self.write("recycle-evidence.json", evidence)
        self.stages = []
        for index, name in enumerate(self.names):
            image = bytearray(4096)
            image[1027:1035] = b"NTFS    "
            image[1035:1037] = (512).to_bytes(2, "little")
            image[1534:1536] = b"\x55\xaa"
            image[-1] = index
            (self.fixture / (name + ".img")).write_bytes(image)
            targets = [] if index == 0 else self.originals[:8 if index == 3 else 4]
            self.write(name + ".manifest.json", {"schema_version": 1, "fixture_id": self.fixture_id,
                       "stage": name, "sector_size": 512, "offset_sectors": 2, "files": targets})
            self.stages.append({"stage": name, "image": name + ".img", "manifest": name + ".manifest.json",
                                "bytes": len(image), "sha256": hashlib.sha256(image).hexdigest(),
                                "sector_size": 512, "offset_sectors": 2, "deleted_target_count": len(targets)})
        self.save_stages()

    def write(self, relative, value):
        (self.fixture / relative).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def read(self, relative):
        return json.loads((self.fixture / relative).read_text(encoding="utf-8"))

    def save_stages(self):
        self.write("stages.json", self.stages)
        self.write("result.json", {"status": "complete", "fixture_id": self.fixture_id, "stages": self.stages})

    def validate(self):
        return validate_fixture(self.fixture, self.output, backend=self.backend)

    def add_write_pressure(self):
        name = "after-additional-writes"
        row = dict(self.stages[-1], stage=name, image=name + ".img", manifest=name + ".manifest.json")
        (self.fixture / row["image"]).write_bytes((self.fixture / self.stages[-1]["image"]).read_bytes())
        self.write(row["manifest"], dict(self.read(self.stages[-1]["manifest"]), stage=name))
        self.backend.listings[name] = self.backend.listings[self.names[-1]]
        self.stages.append(row)
        self.save_stages()
        self.write("environment.json", dict(self.read("environment.json"), profile="expanded", write_pressure_mib=1))
        self.write("write-pressure.json", {
            "schema_version": 1, "fixture_id": self.fixture_id, "stage": name, "bulk_mib": 1,
            "small_file_count": 64, "files": [
                {"relative_path": f"Samples/additional-writes/write-{index}.bin",
                 "size": 257 if index < 64 else 1024 ** 2, "sha256": "a" * 64} for index in range(65)]})

    def test_additional_writes_retests_same_targets_without_counting_workload(self):
        self.add_write_pressure()
        result = self.validate()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["profile"], "expanded")
        self.assertEqual(result["write_pressure_mib"], 1)
        self.assertEqual(result["unique_deleted_targets"], 8)
        self.assertEqual(result["planned_target_observations"], 24)
        self.assertEqual([row["total_targets"] for row in result["stages"]], [0, 4, 4, 8, 8])

    def test_breakdowns_count_targets_and_empty_files_using_verified_bytes(self):
        self.backend.payloads["66-128-1"] = b"wrong bytes"
        result = self.validate()
        self.assertEqual(result["stages"][0]["by_size_bytes"], [])
        self.assertEqual(result["stages"][1]["by_scenario"], [
            {"scenario": "direct-directory", "total_targets": 2, "exact_content_matches": 2, "correct_paths": 2},
            {"scenario": "direct-file", "total_targets": 2, "exact_content_matches": 1, "correct_paths": 1}])
        self.assertEqual(result["stages"][1]["by_size_bytes"][0], {
            "size_bytes": 0, "total_targets": 1, "exact_content_matches": 1, "correct_paths": 1})
        for stage in result["stages"]:
            for dimension in ("by_scenario", "by_size_bytes"):
                for field in ("total_targets", "exact_content_matches", "correct_paths"):
                    self.assertEqual(sum(row[field] for row in stage[dimension]), stage[field])

    def test_cancellation_before_additional_writes_keeps_fifth_stage_pending(self):
        self.add_write_pressure()
        self.backend.cancelled_stage = self.names[-1]
        result = self.validate()
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual([row["status"] for row in result["stages"]],
                         ["baseline", "passed", "passed", "cancelled", "not_run"])
        self.assertEqual(result["planned_target_observations"], 24)
        self.assertEqual(result["exact_content_observations"], 8)

    def test_missing_write_pressure_stage_or_evidence_is_rejected(self):
        self.write("environment.json", dict(self.read("environment.json"), write_pressure_mib=1))
        self.assert_rejected()
        self.add_write_pressure()
        (self.fixture / "write-pressure.json").unlink()
        self.assert_rejected()

    def test_invalid_write_pressure_evidence_is_rejected_before_scan(self):
        self.add_write_pressure()
        original = self.read("write-pressure.json")
        for mutation in ("duplicate", "escape", "size", "schema", "boolean", "count"):
            with self.subTest(mutation=mutation):
                pressure = copy.deepcopy(original)
                if mutation == "duplicate":
                    pressure["files"][1] = pressure["files"][0]
                elif mutation == "escape":
                    pressure["files"][0]["relative_path"] = "Samples/additional-writes/../control.txt"
                elif mutation == "size":
                    pressure["files"][0]["size"] = 1024 ** 2
                elif mutation == "schema":
                    pressure["schema_version"] = True
                elif mutation == "boolean":
                    pressure["bulk_mib"] = True
                else:
                    pressure["files"].pop()
                self.write("write-pressure.json", pressure)
                self.assert_rejected()

    def assert_rejected(self):
        with self.assertRaises((RecoveryError, OSError)):
            self.validate()
        self.assertFalse(self.output.exists())
        self.assertEqual(self.backend.calls, [])

    def test_full_pipeline_keeps_baseline_and_repeated_targets_distinct(self):
        result = self.validate()
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["all_deleted_targets_verified"])
        self.assertEqual(result["original_copies_verified"], 9)
        self.assertEqual(result["unique_deleted_targets"], 8)
        self.assertEqual(result["planned_target_observations"], 16)
        self.assertEqual(result["exact_content_observations"], 16)
        self.assertEqual([stage["total_targets"] for stage in result["stages"]], [0, 4, 4, 8])
        self.assertEqual([stage["status"] for stage in result["stages"]], ["baseline", "passed", "passed", "passed"])
        baseline = json.loads((self.output / self.names[0] / "verification.json").read_text(encoding="utf-8"))
        self.assertIsNone(baseline["content_match_rate"])
        self.assertFalse(baseline["all_targets_verified"])
        self.assertEqual(self.backend.calls, [(name + ".img", 2, 512) for name in self.names])
        self.assertEqual(json.loads((self.output / "fixture-validation.json").read_text(encoding="utf-8")), result)

    def test_unicode_recycle_names_and_empty_file_match_actual_originals(self):
        self.validate()
        final = self.output / self.names[3]
        report = json.loads((final / "verification.json").read_text(encoding="utf-8"))
        self.assertTrue(all(row["path_matches"] for row in report["targets"]))
        self.assertEqual((final / "recovered/files/Samples/recycle-a/中文.txt").read_bytes(), "中文原件".encode())
        self.assertEqual((final / "recovered/files/Samples/direct/empty.txt").read_bytes(), b"")

    def test_missing_recycle_index_cannot_borrow_ground_truth_names(self):
        rows = self.backend.listings[self.names[3]].splitlines(keepends=True)
        self.backend.listings[self.names[3]] = "".join(row for row in rows if self.index_inodes[0] not in row)
        result = self.validate()
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["stages"][3]["exact_content_matches"], 8)
        self.assertEqual(result["stages"][3]["correct_paths"], 7)
        self.assertFalse(result["all_deleted_targets_verified"])

    def test_matching_lengths_with_wrong_content_do_not_pass(self):
        self.backend.payloads["66-128-1"] = b"wrong bytes"
        result = self.validate()
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["exact_content_observations"], 13)
        self.assertEqual(result["stages"][1]["exact_content_matches"], 3)

    def test_scan_failure_is_recorded_and_later_stages_still_run(self):
        self.backend.failed_stage = self.names[1]
        result = self.validate()
        self.assertEqual(result["status"], "failed")
        self.assertEqual([row["status"] for row in result["stages"]], ["baseline", "failed", "passed", "passed"])
        self.assertIn("Controlled scan failure", result["stages"][1]["error"])
        self.assertFalse((self.output / self.names[1] / "recovered").exists())

    def test_cancellation_keeps_completed_results_and_marks_unstarted_stages(self):
        extract = self.backend.extract

        def cancel_export(image, offset, sector, inode, output, limit):
            if image.stem == self.names[2] and inode == "66-128-1":
                raise KeyboardInterrupt
            return extract(image, offset, sector, inode, output, limit)

        for phase in ("scan", "export"):
            with self.subTest(phase=phase):
                self.output = self.base / ("cancelled-" + phase)
                self.backend.cancelled_stage = self.names[2] if phase == "scan" else None
                with patch.object(self.backend, "extract", side_effect=cancel_export):
                    result = self.validate()
                self.assertEqual(result["status"], "cancelled")
                self.assertEqual([row["status"] for row in result["stages"]], ["baseline", "passed", "cancelled", "not_run"])
                self.assertEqual(result["exact_content_observations"], 4)
                self.assertTrue((self.output / self.names[1] / "verification.json").is_file())
                self.assertFalse((self.output / self.names[3]).exists())
                if phase == "export":
                    partial = self.output / self.names[2] / "recovered"
                    report = json.loads((partial / "recovery.json").read_text(encoding="utf-8"))
                    self.assertEqual(report["status"], "cancelled")
                    self.assertEqual((partial / "files/Samples/direct/empty.txt").read_bytes(), b"")
                    self.assertEqual(result["stages"][2]["recovery_counts"]["exported_unverified_count"], 1)

    def test_input_manifest_edits_cannot_change_frozen_reference_targets(self):
        def mutate_then_scan(*args, **kwargs):
            manifest = self.read(self.names[1] + ".manifest.json")
            manifest["files"] = []
            self.write(self.names[1] + ".manifest.json", manifest)
            return scan(*args, **kwargs)
        with patch("recovery_core.fixture_validation.scan", side_effect=mutate_then_scan):
            result = self.validate()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["stages"][1]["total_targets"], 4)

    def test_image_changes_after_preflight_are_rejected_before_export(self):
        def mutate_then_scan(image, *args, **kwargs):
            if image.stem == self.names[1]:
                data = bytearray(image.read_bytes())
                data[0] ^= 1
                image.write_bytes(data)
            return scan(image, *args, **kwargs)
        with patch("recovery_core.fixture_validation.scan", side_effect=mutate_then_scan):
            result = self.validate()
        self.assertEqual(result["status"], "failed")
        self.assertIn("changed after input validation", result["stages"][1]["error"])
        self.assertFalse((self.output / self.names[1] / "recovered").exists())

    def test_incomplete_fixture_is_rejected_before_any_scan(self):
        result = self.read("result.json")
        result["status"] = "failed"
        self.write("result.json", result)
        self.assert_rejected()

    def test_empty_original_set_is_rejected(self):
        manifest = self.read("originals.manifest.json")
        manifest["files"] = []
        self.write("originals.manifest.json", manifest)
        self.assert_rejected()

    def test_tampered_original_copy_is_rejected(self):
        (self.fixture / "originals" / self.originals[1]["relative_path"]).write_bytes(b"tampered")
        self.assert_rejected()

    def test_corrupted_last_image_prevents_all_scans(self):
        with (self.fixture / (self.names[3] + ".img")).open("ab") as stream:
            stream.write(b"changed")
        self.assert_rejected()

    def test_missing_or_reordered_stages_are_rejected(self):
        original = copy.deepcopy(self.stages)
        for stages in (original[:3], original[::-1], []):
            with self.subTest(stages=len(stages)):
                self.stages = stages
                self.save_stages()
                self.assert_rejected()

    def test_wrong_stage_geometry_or_count_is_rejected(self):
        original = copy.deepcopy(self.stages)
        for field, value in (("offset_sectors", True), ("offset_sectors", -1), ("offset_sectors", 9),
                             ("sector_size", 1024), ("bytes", False), ("deleted_target_count", 0),
                             ("deleted_target_count", True), ("sha256", "invalid")):
            with self.subTest(field=field, value=value):
                self.stages = copy.deepcopy(original)
                self.stages[1][field] = value
                self.save_stages()
                self.assert_rejected()

    def test_stage_cannot_omit_targets_or_include_live_recycle_and_control_files(self):
        name = self.names[2] + ".manifest.json"
        original = self.read(name)
        for files in (self.originals[:3], self.originals[:8], self.originals[:4] + [self.originals[-1]]):
            with self.subTest(targets=len(files)):
                manifest = dict(original, files=files)
                self.write(name, manifest)
                self.assert_rejected()

    def test_mixed_fixture_ids_are_rejected(self):
        name = self.names[1] + ".manifest.json"
        manifest = self.read(name)
        manifest["fixture_id"] = "2" * 32
        self.write(name, manifest)
        self.assert_rejected()

    def test_duplicate_original_paths_are_rejected_after_drive_normalization(self):
        manifest = self.read("originals.manifest.json")
        manifest["files"].append(dict(self.originals[0], original_path=self.originals[0]["original_path"].lower()))
        self.write("originals.manifest.json", manifest)
        self.assert_rejected()

    def test_invalid_original_types_and_manifest_version_are_rejected(self):
        original = self.read("originals.manifest.json")
        for field, value in (("size", True), ("sha256", "x" * 64), ("relative_path", "../outside"),
                             ("scenario", []), ("synthetic", 1)):
            with self.subTest(field=field):
                manifest = copy.deepcopy(original)
                manifest["files"][0][field] = value
                self.write("originals.manifest.json", manifest)
                self.assert_rejected()
        self.write("originals.manifest.json", dict(original, schema_version=True))
        self.assert_rejected()

    def test_manifest_and_image_paths_cannot_escape_or_alias_stages(self):
        original = copy.deepcopy(self.stages)
        for field, value in (("image", "../outside.img"), ("image", "C:\\outside.img"),
                             ("image", "\\\\.\\C:"), ("image", original[0]["image"]),
                             ("manifest", "../outside.json")):
            with self.subTest(value=value):
                self.stages = copy.deepcopy(original)
                self.stages[1][field] = value
                self.save_stages()
                self.assert_rejected()

    def test_wrong_or_duplicate_recycle_evidence_is_rejected(self):
        original = self.read("recycle-evidence.json")
        for evidence in (original[:-1], [original[0]] * 4,
                         [dict(original[0], sha256="0" * 64)] + original[1:],
                         [dict(original[0], content_path=original[1]["content_path"])] + original[1:]):
            with self.subTest(evidence=evidence[0]["content_path"]):
                self.write("recycle-evidence.json", evidence)
                self.assert_rejected()

    def test_existing_output_is_not_overwritten(self):
        self.output.mkdir()
        keep = self.output / "keep.txt"
        keep.write_bytes(b"original output")
        with self.assertRaises(RecoveryError):
            self.validate()
        self.assertEqual(keep.read_bytes(), b"original output")
        self.assertEqual(self.backend.calls, [])

    def test_output_inside_fixture_is_rejected(self):
        self.output = self.fixture / "results"
        self.assert_rejected()

    def test_cli_returns_distinct_success_mismatch_error_and_cancellation_codes(self):
        for expected, mode in ((0, "passed"), (1, "incomplete"), (2, "failed"), (130, "cancelled")):
            with self.subTest(mode=mode):
                backend = StagedBackend(dict(self.listings), dict(self.backend.payloads))
                if mode == "incomplete":
                    backend.listings[self.names[1]] = ""
                elif mode == "failed":
                    backend.failed_stage = self.names[1]
                elif mode == "cancelled":
                    backend.cancelled_stage = self.names[1]
                with patch("recovery_core.cli.Tsk", return_value=backend), patch("sys.stdout", new=io.StringIO()):
                    code = main(["validate-fixture", str(self.fixture), "--output", str(self.base / mode)])
                self.assertEqual(code, expected)


if __name__ == "__main__":
    unittest.main()
