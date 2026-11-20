import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "audit_app" / "main.py"
SPEC = importlib.util.spec_from_file_location("quality_audit", MODULE_PATH)
quality = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(quality)


class QualityAuditTests(unittest.TestCase):
    def test_yolo_valid_and_empty_label_states_are_distinct(self):
        valid = quality.parse_label_text("0 0.5 0.5 0.2 0.3")
        self.assertEqual(valid["status"], "annotated")
        self.assertEqual(len(valid["boxes"]), 1)
        self.assertEqual(quality.parse_label_text("\n  ")["status"], "empty")

    def test_invalid_syntax_coordinates_and_classes_are_reported(self):
        result = quality.parse_label_text("x 0.5 0.5 0.2 0.2\n1 0.5 0.5 0.2 0.2\n0 1.1 0.5 0.2 0.2\n0 0.5 0.5 0 0.2")
        self.assertEqual(len(result["errors"]), 4)
        self.assertEqual(result["status"], "invalid")

    def test_label_mapping_handles_images_labels_split_layout(self):
        self.assertEqual(quality.label_for_image("images/train/a.jpg", {"labels/train/a.txt"}), "labels/train/a.txt")
        self.assertIsNone(quality.label_for_image("images/val/no_label.png", set()))

    def test_duplicate_hash_groups_and_split_leakage_summary(self):
        rows = [
            {"path":"images/train/a.jpg","sha256":"same","phash":"0000000000000000","split":"train","labelPath":"labels/train/a.txt","errors":[],"warnings":[],"labelStatus":"annotated","boxes":[{"area":.1,"aspect":1,"cx":.5,"cy":.5}],"brightness":.5,"contrast":.2,"sharpness":1},
            {"path":"images/val/a.jpg","sha256":"same","phash":"0000000000000001","split":"validation","labelPath":"labels/val/a.txt","errors":[],"warnings":[],"labelStatus":"annotated","boxes":[{"area":.1,"aspect":1,"cx":.5,"cy":.5}],"brightness":.5,"contrast":.2,"sharpness":1},
        ]
        report = quality.summarize_records(rows, ["labels/train/a.txt","labels/val/a.txt"], "fingerprint")
        self.assertEqual(report["summary"]["exactDuplicateGroups"], 1)
        self.assertEqual(report["summary"]["nearDuplicatePairs"], 1)
        self.assertEqual(report["summary"]["crossSplitNearDuplicatePairs"], 1)
        self.assertTrue(report["nearDuplicates"][0]["crossSplit"])
        self.assertTrue(report["metadata"]["datasetFingerprint"])

    def test_restart_recovery_changes_running_jobs_to_resumable_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            previous_state = quality.STATE_DIR
            previous_db = quality.DB_PATH
            quality.STATE_DIR = Path(directory)
            quality.DB_PATH = Path(directory) / "test.sqlite3"
            try:
                with quality.db() as connection:
                    connection.execute("INSERT INTO jobs(id,status,total,created_at,updated_at) VALUES('audit','running',1,'now','now')")
                quality.recover_interrupted_jobs()
                with quality.db() as connection:
                    audit = connection.execute("SELECT status,error FROM jobs WHERE id='audit'").fetchone()
                self.assertEqual(audit["status"], "failed")
                self.assertIn("can be resumed", audit["error"])
            finally:
                quality.STATE_DIR = previous_state
                quality.DB_PATH = previous_db

    def test_static_worker_source_does_not_import_ml_libraries(self):
        source = MODULE_PATH.read_text()
        worker = source.split("def audit_worker", 1)[1].split("def summarize_records", 1)[0]
        self.assertNotIn("import torch", worker)
        self.assertNotIn("import ultralytics", worker)


if __name__ == "__main__":
    unittest.main()
