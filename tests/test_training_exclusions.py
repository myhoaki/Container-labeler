import sqlite3
import os
import tempfile
import unittest
from pathlib import Path

from app import main


class TrainingExclusionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old_db = main.DB
        self.old_data = main.DATA
        self.old_dataset = main.SERVER_DATASET
        self.old_dataset_env = os.environ.get("SERVER_DATASET_ROOT")
        os.environ["SERVER_DATASET_ROOT"] = str(self.root / "dataset")
        main.DATA = self.root / "data"
        main.DATA.mkdir()
        main.DB = main.DATA / "labeler.sqlite3"
        main.SERVER_DATASET = self.root / "dataset"
        (main.SERVER_DATASET / "images").mkdir(parents=True)
        (main.SERVER_DATASET / "labels").mkdir()
        (main.SERVER_DATASET / "images" / "missing.jpg").write_bytes(b"image")
        (main.SERVER_DATASET / "images" / "invalid.jpg").write_bytes(b"image")
        (main.SERVER_DATASET / "images" / "good.jpg").write_bytes(b"image")
        (main.SERVER_DATASET / "labels" / "invalid.txt").write_text("0 1.5 0.5 0.2 0.2\n")
        (main.SERVER_DATASET / "labels" / "good.txt").write_text("0 0.5 0.5 0.2 0.2\n")
        main.initialize()
        with main.connect() as db:
            db.execute("INSERT INTO sessions(id,root_name,created,last_seen) VALUES('container','dataset',0,0)")
            db.execute("INSERT INTO text_sessions(id,root_name,created,last_seen,source_session_id) VALUES('count','dataset',0,0,'container')")
            # Two conflicting crop labels on one source, and no count labels on the other sources.
            db.execute("INSERT INTO text_annotations(session_id,path,source_path,cached_crop_path,active,updated) VALUES('count','crop-a','images/good.jpg','crop-a.png',1,0)")
            db.execute("INSERT INTO text_annotations(session_id,path,source_path,cached_crop_path,active,updated) VALUES('count','crop-b','images/good.jpg','crop-b.png',1,0)")
            db.execute("INSERT INTO container_counts(session_id,path,container_count,updated) VALUES('count','crop-a',1,0)")
            db.execute("INSERT INTO container_counts(session_id,path,container_count,updated) VALUES('count','crop-b',2,0)")

    def tearDown(self):
        main.DB, main.DATA, main.SERVER_DATASET = self.old_db, self.old_data, self.old_dataset
        if self.old_dataset_env is None: os.environ.pop("SERVER_DATASET_ROOT", None)
        else: os.environ["SERVER_DATASET_ROOT"] = self.old_dataset_env
        self.temp.cleanup()

    def test_detector_and_classifier_exclusions_are_separated_and_fixable(self):
        result = main._server_training_exclusions()
        self.assertEqual({item["path"] for item in result["detector"]}, {"images/missing.jpg", "images/invalid.jpg"})
        by_path = {item["path"]: item["reason"] for item in result["classifier"]}
        self.assertEqual(by_path, {
            "images/missing.jpg": "missing container count",
            "images/invalid.jpg": "missing container count",
            "images/good.jpg": "conflicting container counts",
        })
        page = main.classifier_training_exclusions("count", limit=50)
        self.assertEqual(page["total"], 3)
        self.assertTrue(all(item["source_level"] for item in page["items"]))
        main.update_source_container_count("count", "images/good.jpg", {"container_count": 2})
        result = main._server_training_exclusions()
        self.assertNotIn("images/good.jpg", {item["path"] for item in result["classifier"]})
        main.update_source_container_count("count", "images/missing.jpg", {"container_count": 1})
        result = main._server_training_exclusions()
        self.assertNotIn("images/missing.jpg", {item["path"] for item in result["classifier"]})


if __name__ == "__main__":
    unittest.main()
