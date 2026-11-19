"""Dependency-light SQLite/API-logic smoke checks: python3 tests/smoke.py"""
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app.main as service


class ApiSmoke(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        service.DATA = Path(self.tmp.name)
        service.DB = Path(self.tmp.name) / "smoke.sqlite3"
        service.initialize()
        self.sid = service.begin(service.Begin(root_name="synthetic"))["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_duplicate_paths_normalized_boxes_and_validation(self):
        service.index_batch(self.sid, service.IndexBatch(items=[
            service.Item(path="landscape/cam.jpg", size=100, mtime=1),
            service.Item(path="portrait/cam.jpg", size=200, mtime=2)]))
        for path, box in (("landscape/cam.jpg", [.5125, .47, .65, .42]),
                          ("portrait/cam.jpg", [.5, .5, .25, .7])):
            service.update_image(self.sid, path, service.Review(status="verified", boxes=[box], labels_written=True))
            self.assertEqual(service.get_image(self.sid, path)["boxes"], [box])
        with self.assertRaises(Exception):
            service.update_image(self.sid, "portrait/cam.jpg", service.Review(status="verified", boxes=[[1.1, .5, .2, .2]], labels_written=True))

    def test_backup_reject_and_changed_file_recovery(self):
        service.index_batch(self.sid, service.IndexBatch(items=[service.Item(path="a.jpg", size=10, mtime=1)]))
        service.update_image(self.sid, "a.jpg", service.Review(status="verified", boxes=[[.5, .5, .4, .2]], labels_written=True, existing_label="old label\n"))
        self.assertEqual(service.backup_list(self.sid)[0]["content"], "old label\n")
        service.update_image(self.sid, "a.jpg", service.Review(status="rejected"))
        self.assertEqual(service.get_image(self.sid, "a.jpg")["status"], "rejected")
        service.index_batch(self.sid, service.IndexBatch(items=[service.Item(path="a.jpg", size=11, mtime=2)]))
        row = service.get_image(self.sid, "a.jpg")
        self.assertEqual(row["status"], "unreviewed")
        self.assertEqual(row["boxes"], [])

    def test_crop_metadata_is_bound_to_original_record(self):
        service.index_batch(self.sid, service.IndexBatch(items=[service.Item(path="sub/camera_001.jpg", size=100, mtime=1)]))
        crop={"x":0,"y":0,"width":800,"height":600,"source_width":1920,"source_height":1080,"original_boxes":[]}
        boxes=[[.5,.5,.65,.42]]
        service.update_image(self.sid,"sub/camera_001.jpg",service.Review(status="verified",boxes=boxes,labels_written=True,crop=crop,derivative_path="sub/camera_001_cropped.jpg"))
        row=service.get_image(self.sid,"sub/camera_001.jpg")
        self.assertEqual(row["path"],"sub/camera_001.jpg")
        self.assertEqual(row["crop"],crop)
        self.assertEqual(row["derivative_path"],"sub/camera_001_cropped.jpg")
        with self.assertRaises(Exception):
            service.update_image(self.sid,"sub/camera_001.jpg",service.Review(status="verified",boxes=boxes,labels_written=True,crop={**crop,"width":3000},derivative_path="../outside.jpg"))

    def test_server_dataset_index_is_scoped_to_configured_root(self):
        root = Path(self.tmp.name) / "dataset"
        (root / "images").mkdir(parents=True)
        (root / "labels").mkdir()
        (root / "images" / "one.jpg").write_bytes(b"image")
        (root / "labels" / "one.txt").write_text("0 .5 .5 .2 .2\n")
        with patch.dict("os.environ", {"SERVER_DATASET_ROOT": str(root)}), patch.object(service, "SERVER_DATASET", root):
            self.assertEqual(service.server_dataset_info()["images"], 1)
            self.assertEqual(service.server_dataset_index()["items"][0]["path"], "images/one.jpg")
            self.assertEqual(service.server_dataset_path("images/one.jpg"), root / "images" / "one.jpg")
            with self.assertRaises(Exception):
                service.server_dataset_path("../outside.jpg")

    def test_server_text_crop_is_generated_and_cached(self):
        from PIL import Image
        root = Path(self.tmp.name) / "dataset"
        (root / "images").mkdir(parents=True)
        Image.new("RGB", (64, 32), "red").save(root / "images" / "one.jpg")
        with patch.dict("os.environ", {"SERVER_DATASET_ROOT": str(root)}), patch.object(service, "SERVER_DATASET", root):
            request = {"items": [{"source": "images/one.jpg", "box": [.5, .5, .5, .5]}]}
            first = service.generate_text_crops(request)["items"][0]
            second = service.generate_text_crops(request)["items"][0]
            self.assertEqual(first["path"], second["path"])
            with Image.open(root / first["path"]) as crop:
                self.assertEqual(crop.size, (32, 16))
            self.assertEqual(service.server_dataset_index()["items"][0]["path"], "images/one.jpg")

    def test_text_crop_job_runs_and_persists_progress_server_side(self):
        from PIL import Image
        root = Path(self.tmp.name) / "dataset"
        (root / "images").mkdir(parents=True)
        (root / "labels").mkdir()
        Image.new("RGB", (64, 32), "blue").save(root / "images" / "one.jpg")
        (root / "labels" / "one.txt").write_text("0 0.5 0.5 0.5 0.5\n")
        service.index_batch(self.sid, service.IndexBatch(items=[service.Item(path="images/one.jpg", size=1, mtime=1)]))
        text_session = service.begin_text_session(service.TextBegin(root_name="dataset", source_session_id=self.sid))
        with patch.dict("os.environ", {"SERVER_DATASET_ROOT": str(root)}), patch.object(service, "SERVER_DATASET", root):
            job = service.prepare_text_crops(text_session["id"])
            deadline = time.time() + 10
            state = job
            while state["status"] in {"queued", "running"} and time.time() < deadline:
                time.sleep(0.02)
                state = service.text_crop_job_status(job["id"])
            self.assertEqual(state["status"], "complete")
            self.assertEqual(state["crops"], 1)
            rows = service.text_image_list(text_session["id"], status="all")["items"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["container_box"], [.5, .5, .5, .5])
            self.assertTrue((root / rows[0]["cached_crop_path"]).is_file())

    def test_text_region_session_is_independent_from_container_annotations(self):
        path = "cropped/cam_001.jpg"
        service.index_batch(self.sid, service.IndexBatch(items=[service.Item(path=path, size=123, mtime=10)]))
        container_box = [.5, .5, .8, .7]
        service.update_image(self.sid, path, service.Review(status="verified", boxes=[container_box], labels_written=True))

        text_session = service.begin_text_session(service.TextBegin(root_name="cropped containers"))
        text_sid = text_session["id"]
        self.assertEqual((text_session["class_name"], text_session["class_id"]), ("container_number", 0))
        service.index_text_batch(text_sid, service.TextIndexBatch(items=[service.Item(path=path, size=123, mtime=10)]))
        text_box = [.48, .25, .4, .08]
        second_text_box = [.72, .7, .2, .06]
        text_boxes = [text_box, second_text_box]
        service.update_text_image(text_sid, path, service.TextReview(status="unreviewed", boxes=text_boxes, labels_written=True))

        self.assertEqual(service.get_image(self.sid, path)["boxes"], [container_box])
        self.assertEqual(service.get_image(self.sid, path)["status"], "verified")
        self.assertEqual(service.get_text_image(text_sid, path)["boxes"], text_boxes)
        self.assertEqual(service.get_text_image(text_sid, path)["status"], "unreviewed")
        service.initialize()
        self.assertEqual(service.get_text_image(text_sid, path)["boxes"], text_boxes)

    def test_text_sessions_prevent_same_path_collision_between_source_folders(self):
        first = service.begin_text_session(service.TextBegin(root_name="folder A"))["id"]
        second = service.begin_text_session(service.TextBegin(root_name="folder B"))["id"]
        same_path = "container_01.jpg"
        for sid in (first, second):
            service.index_text_batch(sid, service.TextIndexBatch(items=[service.Item(path=same_path, size=10, mtime=1)]))
        service.update_text_image(first, same_path, service.TextReview(status="verified", boxes=[[.5, .5, .5, .1]], labels_written=True))
        self.assertEqual(service.get_text_image(first, same_path)["status"], "verified")
        self.assertEqual(service.get_text_image(second, same_path)["status"], "unreviewed")
        self.assertEqual(service.get_text_image(second, same_path)["boxes"], [])

    def test_container_count_labels_are_independent_and_export_with_matching_crops(self):
        from PIL import Image
        root = Path(self.tmp.name) / "dataset"
        cache = root / ".container-labeler" / "text-crops"
        cache.mkdir(parents=True)
        crop_path = ".container-labeler/text-crops/stable-crop.png"
        second_crop = ".container-labeler/text-crops/another-crop.png"
        Image.new("RGB", (24, 12), "green").save(root / crop_path)
        Image.new("RGB", (24, 12), "blue").save(root / second_crop)
        text_session = service.begin_text_session(service.TextBegin(root_name="dataset", source_session_id=self.sid))
        sid = text_session["id"]
        path = "images/truck.jpg.__container_box_1__"
        second_path = "other/truck.jpg.__container_box_1__"
        service.index_text_batch(sid, service.TextIndexBatch(items=[service.Item(path=path, size=42, mtime=10), service.Item(path=second_path, size=42, mtime=10)]))
        with service.connect() as db:
            db.execute("UPDATE text_annotations SET source_path=?,box_index=0,container_box=?,cached_crop_path=? WHERE session_id=? AND path=?", ("images/truck.jpg", "[0.5,0.5,0.7,0.6]", crop_path, sid, path))
            db.execute("UPDATE text_annotations SET source_path=?,box_index=0,container_box=?,cached_crop_path=? WHERE session_id=? AND path=?", ("other/truck.jpg", "[0.5,0.5,0.7,0.6]", second_crop, sid, second_path))
        self.assertEqual(service.container_count_list(sid)["counts"], {"all": 2, "unlabeled": 2, "1": 0, "2": 0})
        service.bulk_update_container_count(sid, {"paths": [path, second_path], "container_count": 2})
        self.assertEqual(service.container_count_list(sid, status="2", limit=1)["total"], 2)
        service.update_container_count(sid, path, {"container_count": 1})
        self.assertEqual(service.container_count_list(sid)["counts"], {"all": 2, "unlabeled": 0, "1": 1, "2": 1})
        with self.assertRaises(Exception):
            service.update_container_count(sid, path, {"container_count": 3})
        with patch.dict("os.environ", {"SERVER_DATASET_ROOT": str(root)}), patch.object(service, "SERVER_DATASET", root):
            result = service.export_container_counts(sid)
            with zipfile.ZipFile(result.path) as archive:
                self.assertIn("labels.csv", archive.namelist())
                manifest = archive.read("labels.csv").decode()
                self.assertIn("images/stable-crop.png,1,labeled,images/truck.jpg,0", manifest)
                self.assertEqual(archive.read("images/stable-crop.png"), (root / crop_path).read_bytes())

    def test_text_session_is_bound_to_its_container_session(self):
        first = service.begin_text_session(service.TextBegin(root_name="synthetic", source_session_id=self.sid))
        self.assertEqual(first["source_session_id"], self.sid)
        another = service.begin(service.Begin(root_name="synthetic copy"))["id"]
        with self.assertRaises(Exception):
            service.begin_text_session(service.TextBegin(session_id=first["id"], root_name="synthetic", source_session_id=another))


if __name__ == "__main__":
    unittest.main(verbosity=2)
