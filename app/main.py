from __future__ import annotations

import json
import hashlib
import math
import os
import sqlite3
import threading
import time
import csv
import io
import tempfile
import zipfile
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

ROOT = Path(__file__).parent
DATA = Path(os.getenv("DATA_DIR", ROOT.parent / "data"))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / "container-labeler.sqlite3"
SERVER_DATASET = Path(os.getenv("SERVER_DATASET_ROOT", ""))
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".avif", ".tif", ".tiff"}
app = FastAPI(title="Manual Container Labeler", version="1.0.0")
_text_jobs_lock = threading.Lock()
_active_text_job: str | None = None


def connect():
    db = sqlite3.connect(DB, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


@app.on_event("startup")
def initialize():
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions(
          id TEXT PRIMARY KEY, root_name TEXT NOT NULL, created REAL NOT NULL,
          last_seen REAL NOT NULL, current_path TEXT NOT NULL DEFAULT '',
          class_name TEXT NOT NULL DEFAULT 'container', class_id INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS images(
          session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
          path TEXT NOT NULL, size INTEGER, mtime REAL, status TEXT NOT NULL DEFAULT 'unreviewed'
            CHECK(status IN ('unreviewed','verified','rejected')),
          boxes TEXT NOT NULL DEFAULT '[]', labels_written INTEGER NOT NULL DEFAULT 0,
          rejection_reason TEXT, crop_json TEXT, derivative_path TEXT, updated REAL NOT NULL,
          PRIMARY KEY(session_id,path)
        );
        CREATE TABLE IF NOT EXISTS annotation_backups(
          id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, path TEXT NOT NULL,
          content TEXT NOT NULL, created REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS text_sessions(
          id TEXT PRIMARY KEY, root_name TEXT NOT NULL, created REAL NOT NULL,
          last_seen REAL NOT NULL, current_path TEXT NOT NULL DEFAULT '',
          source_session_id TEXT NOT NULL DEFAULT '',
          class_name TEXT NOT NULL DEFAULT 'container_number' CHECK(class_name='container_number'),
          class_id INTEGER NOT NULL DEFAULT 0 CHECK(class_id=0)
        );
        CREATE TABLE IF NOT EXISTS text_annotations(
          session_id TEXT NOT NULL REFERENCES text_sessions(id) ON DELETE CASCADE,
          path TEXT NOT NULL, size INTEGER, mtime REAL,
          status TEXT NOT NULL DEFAULT 'unreviewed'
            CHECK(status IN ('unreviewed','annotated','verified','skipped')),
          boxes TEXT NOT NULL DEFAULT '[]', labels_written INTEGER NOT NULL DEFAULT 0,
          source_path TEXT, box_index INTEGER, container_box TEXT, cached_crop_path TEXT,
          active INTEGER NOT NULL DEFAULT 1,
          updated REAL NOT NULL,
          PRIMARY KEY(session_id,path)
        );
        CREATE TABLE IF NOT EXISTS text_crop_jobs(
          id TEXT PRIMARY KEY, session_id TEXT NOT NULL, source_session_id TEXT NOT NULL,
          status TEXT NOT NULL, processed INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
          crops INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0,
          current_path TEXT NOT NULL DEFAULT '', source_fingerprint TEXT NOT NULL DEFAULT '',
          error TEXT, created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS container_counts(
          session_id TEXT NOT NULL REFERENCES text_sessions(id) ON DELETE CASCADE,
          path TEXT NOT NULL, container_count INTEGER CHECK(container_count IN (1,2)),
          updated REAL NOT NULL, PRIMARY KEY(session_id,path)
        );
        CREATE TABLE IF NOT EXISTS source_container_counts(
          source_path TEXT PRIMARY KEY, container_count INTEGER NOT NULL CHECK(container_count IN (1,2)),
          updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS casting_hole_annotations(
          session_id TEXT NOT NULL REFERENCES text_sessions(id) ON DELETE CASCADE,
          path TEXT NOT NULL, source_path TEXT NOT NULL, cached_crop_path TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'unreviewed' CHECK(status IN ('unreviewed','annotated','skipped')),
          boxes TEXT NOT NULL DEFAULT '[]', yolo_labels TEXT NOT NULL DEFAULT '',
          image_width INTEGER, image_height INTEGER,
          updated REAL NOT NULL, PRIMARY KEY(session_id,path)
        );
        CREATE INDEX IF NOT EXISTS text_annotations_status ON text_annotations(session_id,status,path);
        CREATE INDEX IF NOT EXISTS images_status ON images(session_id,status,path);
        """)
        columns = {row[1] for row in db.execute("PRAGMA table_info(images)")}
        if "crop_json" not in columns:
            db.execute("ALTER TABLE images ADD COLUMN crop_json TEXT")
        if "derivative_path" not in columns:
            db.execute("ALTER TABLE images ADD COLUMN derivative_path TEXT")
        text_columns = {row[1] for row in db.execute("PRAGMA table_info(text_sessions)")}
        if "source_session_id" not in text_columns:
            db.execute("ALTER TABLE text_sessions ADD COLUMN source_session_id TEXT NOT NULL DEFAULT ''")
        annotation_columns = {row[1] for row in db.execute("PRAGMA table_info(text_annotations)")}
        for name, declaration in (("source_path", "TEXT"), ("box_index", "INTEGER"), ("container_box", "TEXT"), ("cached_crop_path", "TEXT"), ("active", "INTEGER NOT NULL DEFAULT 1")):
            if name not in annotation_columns:
                db.execute(f"ALTER TABLE text_annotations ADD COLUMN {name} {declaration}")
        # Older versions tracked "annotated" and "skipped" separately. Fold
        # those records into the review states used by Container Detection.
        annotation_schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='text_annotations'").fetchone()[0]
        if "'annotated'" in annotation_schema or "'skipped'" in annotation_schema:
            db.executescript("""
            CREATE TABLE text_annotations_new(
              session_id TEXT NOT NULL REFERENCES text_sessions(id) ON DELETE CASCADE,
              path TEXT NOT NULL, size INTEGER, mtime REAL,
              status TEXT NOT NULL DEFAULT 'unreviewed' CHECK(status IN ('unreviewed','verified','rejected')),
              boxes TEXT NOT NULL DEFAULT '[]', labels_written INTEGER NOT NULL DEFAULT 0,
              source_path TEXT, box_index INTEGER, container_box TEXT, cached_crop_path TEXT,
              active INTEGER NOT NULL DEFAULT 1, updated REAL NOT NULL,
              PRIMARY KEY(session_id,path)
            );
            INSERT INTO text_annotations_new
            SELECT session_id,path,size,mtime,
              CASE status WHEN 'annotated' THEN 'unreviewed' WHEN 'skipped' THEN 'rejected' ELSE status END,
              boxes,labels_written,source_path,box_index,container_box,cached_crop_path,active,updated
            FROM text_annotations;
            DROP TABLE text_annotations;
            ALTER TABLE text_annotations_new RENAME TO text_annotations;
            CREATE INDEX IF NOT EXISTS text_annotations_status ON text_annotations(session_id,status,path);
            """)
        db.execute("UPDATE text_crop_jobs SET status='queued',processed=0,current_path='',updated=? WHERE status='running'", (time.time(),))
    _start_text_job_worker()
    if os.getenv("SERVER_DATASET_ROOT"):
        with connect() as db:
            latest_text_session = db.execute("SELECT id FROM text_sessions WHERE root_name=? AND source_session_id<>'' ORDER BY last_seen DESC LIMIT 1", (SERVER_DATASET.name,)).fetchone()
        if latest_text_session:
            prepare_text_crops(latest_text_session["id"])


class Begin(BaseModel):
    session_id: str | None = None
    root_name: str = "Local folder"
    class_name: str = "container"
    class_id: int = 0


class Item(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    size: int | None = None
    mtime: float | None = None


class IndexBatch(BaseModel):
    items: list[Item] = Field(max_length=1000)


class Review(BaseModel):
    status: Literal["unreviewed", "verified", "rejected"]
    boxes: list[list[float]] = Field(default_factory=list, max_length=1000)
    labels_written: bool = False
    rejection_reason: str | None = None
    existing_label: str | None = None
    crop: dict | None = None
    derivative_path: str | None = None


class TextBegin(BaseModel):
    session_id: str | None = None
    root_name: str = "Local folder"
    source_session_id: str | None = None


class TextIndexBatch(BaseModel):
    items: list[Item] = Field(max_length=1000)


class TextReview(BaseModel):
    status: Literal["unreviewed", "verified", "rejected"]
    boxes: list[list[float]] = Field(default_factory=list, max_length=1000)
    labels_written: bool = False


class CastingHoleReview(BaseModel):
    boxes: list[list[float]] = Field(min_length=1, max_length=1000)
    image_width: int = Field(gt=0)
    image_height: int = Field(gt=0)


def valid_path(path: str):
    p = Path(path)
    if p.is_absolute() or ".." in p.parts or not path.strip():
        raise HTTPException(400, "Expected a safe relative image path")
    return path


def server_dataset_path(path: str = "") -> Path:
    if not os.getenv("SERVER_DATASET_ROOT") or not SERVER_DATASET.is_dir():
        raise HTTPException(404, "No server dataset is configured")
    if path:
        valid_path(path)
    target = (SERVER_DATASET / path).resolve()
    if not target.is_relative_to(SERVER_DATASET.resolve()):
        raise HTTPException(400, "Expected a path inside the server dataset")
    return target


def _server_training_exclusions():
    """Find server images needing detector-label or container-count repair."""
    root = server_dataset_path()
    image_root = root / "images"
    if not image_root.is_dir():
        image_root = root
    labels_root = root / "labels"
    detector = []
    crop_counts: dict[str, set[int]] = {}
    with connect() as db:
        for row in db.execute("""SELECT a.source_path,c.container_count FROM text_sessions t
            JOIN text_annotations a ON a.session_id=t.id
            JOIN container_counts c ON c.session_id=a.session_id AND c.path=a.path
            WHERE t.root_name=? AND a.active=1 AND a.source_path IS NOT NULL AND a.cached_crop_path IS NOT NULL
              AND c.container_count IN (1,2)""", (SERVER_DATASET.name,)):
            crop_counts.setdefault(row["source_path"], set()).add(int(row["container_count"]))
        direct = {row["source_path"]: int(row["container_count"])
                  for row in db.execute("SELECT source_path,container_count FROM source_container_counts")}
    classifier = []
    for image in image_root.rglob("*"):
        if not image.is_file() or image.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        relative = image.relative_to(root).as_posix()
        relative_image = image.relative_to(image_root)
        label = (labels_root / relative_image).with_suffix(".txt")
        if not label.is_file():
            fallback = labels_root / f"{image.stem}.txt"
            if fallback.is_file():
                label = fallback
            else:
                detector.append({"path": relative, "reason": "missing detection label"})
        if label.is_file():
            try:
                for number, line in enumerate(label.read_text(encoding="utf-8-sig").splitlines(), 1):
                    if not line.strip():
                        continue
                    fields = line.split()
                    if len(fields) != 5:
                        raise ValueError(f"line {number}: expected 5 YOLO values")
                    class_id, cx, cy, width, height = map(float, fields)
                    if class_id != 0 or not class_id.is_integer() or width <= 0 or height <= 0 or min(cx-width/2, cy-height/2) < 0 or max(cx+width/2, cy+height/2) > 1:
                        raise ValueError(f"line {number}: invalid YOLO box")
            except (OSError, UnicodeError, ValueError) as error:
                detector.append({"path": relative, "reason": f"invalid detection label: {error}"})
        if relative in direct:
            continue
        labels = crop_counts.get(relative, set())
        if len(labels) != 1:
            classifier.append({"path": relative, "reason": "conflicting container counts" if labels else "missing container count"})
    return {"detector": detector, "classifier": classifier}


@app.get("/api/server-dataset/training-exclusions")
def server_training_exclusions():
    return _server_training_exclusions()


@app.get("/api/text-sessions/{sid}/training-exclusions")
def classifier_training_exclusions(sid: str, offset: int = 0, limit: int = 100):
    limit, offset = min(max(limit, 1), 500), max(offset, 0)
    with connect() as db:
        if not db.execute("SELECT 1 FROM text_sessions WHERE id=?", (sid,)).fetchone():
            raise HTTPException(404, "Text crop session not found")
    rows = _server_training_exclusions()["classifier"]
    items = [{"path": row["path"], "source_path": row["path"], "cached_crop_path": row["path"], "box_index": None, "container_count": None, "source_level": True, "reason": row["reason"]} for row in rows]
    return {"items": items[offset:offset + limit], "total": len(items)}


@app.put("/api/text-sessions/{sid}/source-container-count")
def update_source_container_count(sid: str, path: str, data: dict):
    path = valid_path(path)
    value = data.get("container_count")
    if value not in (1, 2):
        raise HTTPException(422, "container_count must be 1 or 2")
    if not server_dataset_path(path).is_file():
        raise HTTPException(404, "Source image not found")
    with connect() as db:
        if not db.execute("SELECT 1 FROM text_sessions WHERE id=?", (sid,)).fetchone():
            raise HTTPException(404, "Text crop session not found")
        db.execute("INSERT INTO source_container_counts(source_path,container_count,updated) VALUES(?,?,?) ON CONFLICT(source_path) DO UPDATE SET container_count=excluded.container_count,updated=excluded.updated", (path, value, time.time()))
        db.execute("UPDATE text_sessions SET last_seen=? WHERE id=?", (time.time(), sid))
    return {"saved": True, "container_count": value, "source_path": path}


@app.get("/api/health")
def health():
    with connect() as db:
        db.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/api/server-dataset")
def server_dataset_info():
    root = server_dataset_path()
    images = 0
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in {"_rejected", ".container-labeler", "_annotation_backups"}]
        images += sum(Path(name).suffix.lower() in IMAGE_EXTENSIONS for name in files)
    return {"name": root.name, "images": images}


@app.get("/api/server-dataset/index")
def server_dataset_index():
    root = server_dataset_path()
    items = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in {"_rejected", ".container-labeler", "_annotation_backups"}]
        folder = Path(base)
        for name in files:
            path = folder / name
            if path.suffix.lower() in IMAGE_EXTENSIONS:
                stat = path.stat()
                items.append({"path": path.relative_to(root).as_posix(), "size": stat.st_size, "mtime": stat.st_mtime})
    items.sort(key=lambda item: item["path"].casefold())
    return {"items": items}


@app.post("/api/server-dataset/text-crops")
def generate_text_crops(data: dict):
    items = data.get("items")
    if items is None:
        items = [data]
    if not isinstance(items, list) or len(items) > 10000:
        raise HTTPException(422, "Expected up to 10000 source and box pairs")
    return {"items": [generate_text_crop(item) for item in items]}


def generate_text_crop(data: dict):
    source = valid_path(str(data.get("source", "")))
    box = data.get("box")
    if not isinstance(box, list) or len(box) != 4:
        raise HTTPException(422, "A normalized container box is required")
    try:
        cx, cy, width, height = (float(value) for value in box)
    except (TypeError, ValueError):
        raise HTTPException(422, "Invalid normalized container box")
    if not all(0 <= value <= 1 for value in (cx, cy, width, height)) or width <= 0 or height <= 0 or cx < width/2 or cy < height/2 or cx+width/2 > 1 or cy+height/2 > 1:
        raise HTTPException(422, "Container box must be normalized and inside the image")
    target = server_dataset_path(source)
    if not target.is_file() or target.suffix.lower() not in IMAGE_EXTENSIONS:
        raise HTTPException(404, "Source image not found")
    stat = target.stat()
    identity = json.dumps([source, stat.st_size, stat.st_mtime_ns, *[round(value, 8) for value in (cx, cy, width, height)]], separators=(",", ":"))
    cache_id = hashlib.sha256(identity.encode()).hexdigest()
    cache_dir = server_dataset_path(".container-labeler/text-crops")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"{cache_id}.png"
    if not cached.is_file():
        try:
            from PIL import Image, ImageOps
            with Image.open(target) as source_image:
                image = ImageOps.exif_transpose(source_image)
                image.load()
                left = max(0, int((cx-width/2) * image.width))
                top = max(0, int((cy-height/2) * image.height))
                right = min(image.width, math.ceil((cx+width/2) * image.width))
                bottom = min(image.height, math.ceil((cy+height/2) * image.height))
                if right <= left or bottom <= top:
                    raise ValueError("Container crop is empty")
                crop = image.crop((left, top, right, bottom)).convert("RGB")
                temporary = cached.with_name(f".{cached.name}.{uuid4().hex}.tmp")
                crop.save(temporary, format="PNG")
                temporary.replace(cached)
        except Exception as error:
            raise HTTPException(422, f"Could not generate container crop: {error}")
    return {"id": cache_id, "path": f".container-labeler/text-crops/{cache_id}.png", "size": cached.stat().st_size, "mtime": cached.stat().st_mtime}


_text_jobs_event = threading.Event()
_text_worker_started = False


def _start_text_job_worker():
    global _text_worker_started
    if not os.getenv("SERVER_DATASET_ROOT"):
        return
    with _text_jobs_lock:
        if _text_worker_started:
            return
        _text_worker_started = True
    threading.Thread(target=_text_job_worker, daemon=True, name="text-crop-worker").start()
    _text_jobs_event.set()


def _container_label_boxes(image_path: str):
    stem = str(Path(image_path).with_suffix(""))
    candidates = [f"{stem}.txt"]
    parts = Path(stem).parts
    image_dir = next((i for i, part in enumerate(parts) if part.lower() == "images"), None)
    if image_dir is not None:
        label_parts = list(parts)
        label_parts[image_dir] = "labels"
        candidates.append(f"{Path(*label_parts)}.txt")
    else:
        candidates.append(f"labels/{stem}.txt")
    for candidate in dict.fromkeys(candidates):
        try:
            lines = server_dataset_path(candidate).read_text().splitlines()
        except FileNotFoundError:
            continue
        except HTTPException as error:
            if error.status_code == 404:
                continue
            raise
        boxes = []
        for line in lines:
            if not line.strip():
                continue
            try:
                values = [float(value) for value in line.split()]
            except ValueError:
                return []
            if len(values) != 5 or not values[0].is_integer():
                return []
            box = values[1:]
            cx, cy, width, height = box
            if values[0] < 0 or not (0 <= cx < 1 and 0 <= cy < 1 and 0 < width <= 1 and 0 < height <= 1 and cx >= width/2 and cy >= height/2 and cx+width/2 <= 1 and cy+height/2 <= 1):
                return []
            boxes.append(box)
        if boxes:
            return boxes
    return []


def _virtual_text_path(source_path: str, index: int, box: list[float]):
    signature = "_".join(str(math.floor(value * 1_000_000 + 0.5)) for value in box)
    return f"{source_path}.__container_box_{index + 1}_{signature}__"


def _text_source_fingerprint(source_session_id: str):
    digest = hashlib.sha256()
    with connect() as db:
        rows = db.execute("SELECT path,size,mtime,status,boxes,derivative_path FROM images WHERE session_id=? ORDER BY path", (source_session_id,))
        for row in rows:
            digest.update(json.dumps(list(row), separators=(",", ":")).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def _process_text_crop_job(job_id: str):
    global _active_text_job
    with connect() as db:
        job = db.execute("SELECT session_id,source_session_id FROM text_crop_jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            return
        rows = [dict(row) for row in db.execute("SELECT path,size,mtime,status,boxes,labels_written,derivative_path,crop_json FROM images WHERE session_id=? ORDER BY path", (job["source_session_id"],))]
        db.execute("UPDATE text_annotations SET active=0 WHERE session_id=?", (job["session_id"],))
        db.execute("UPDATE text_crop_jobs SET status='running',total=?,processed=0,crops=0,errors=0,updated=? WHERE id=?", (len(rows), time.time(), job_id))
    crop_count = errors = 0
    try:
        for position, row in enumerate(rows, 1):
            source_path = row["derivative_path"] or row["path"]
            boxes = json.loads(row["boxes"] or "[]")
            try:
                if row["status"] != "rejected" and not boxes:
                    boxes = _container_label_boxes(source_path)
                    if boxes:
                        with connect() as db:
                            db.execute("UPDATE images SET boxes=?,labels_written=1,updated=? WHERE session_id=? AND path=?", (json.dumps(boxes), time.time(), job["source_session_id"], row["path"]))
                if row["status"] != "rejected":
                    for index, box in enumerate(boxes):
                        cached = generate_text_crop({"source": source_path, "box": box})
                        text_path = _virtual_text_path(row["path"], index, box)
                        with connect() as db:
                            old = db.execute("SELECT size,mtime,status FROM text_annotations WHERE session_id=? AND path=?", (job["session_id"], text_path)).fetchone()
                            changed = old and (old["size"] != cached["size"] or old["mtime"] != cached["mtime"])
                            if changed:
                                db.execute("UPDATE text_annotations SET size=?,mtime=?,status='unreviewed',boxes='[]',labels_written=0,source_path=?,box_index=?,container_box=?,cached_crop_path=?,active=1,updated=? WHERE session_id=? AND path=?", (cached["size"], cached["mtime"], source_path, index, json.dumps(box), cached["path"], time.time(), job["session_id"], text_path))
                            else:
                                db.execute("INSERT INTO text_annotations(session_id,path,size,mtime,source_path,box_index,container_box,cached_crop_path,active,updated) VALUES(?,?,?,?,?,?,?,?,1,?) ON CONFLICT(session_id,path) DO UPDATE SET source_path=excluded.source_path,box_index=excluded.box_index,container_box=excluded.container_box,cached_crop_path=excluded.cached_crop_path,active=1,updated=excluded.updated", (job["session_id"], text_path, cached["size"], cached["mtime"], source_path, index, json.dumps(box), cached["path"], time.time()))
                        crop_count += 1
            except Exception:
                errors += 1
            with connect() as db:
                db.execute("UPDATE text_crop_jobs SET processed=?,crops=?,errors=?,current_path=?,updated=? WHERE id=?", (position, crop_count, errors, row["path"], time.time(), job_id))
                db.execute("UPDATE text_sessions SET last_seen=? WHERE id=?", (time.time(), job["session_id"]))
        fingerprint = _text_source_fingerprint(job["source_session_id"])
        with connect() as db:
            db.execute("UPDATE text_crop_jobs SET status='complete',processed=total,crops=?,errors=?,current_path='',source_fingerprint=?,error=?,updated=? WHERE id=?", (crop_count, errors, fingerprint, f"{errors} source image(s) could not be processed" if errors else None, time.time(), job_id))
    except Exception as error:
        with connect() as db:
            db.execute("UPDATE text_crop_jobs SET status='failed',error=?,updated=? WHERE id=?", (str(error)[:500], time.time(), job_id))
    finally:
        with _text_jobs_lock:
            _active_text_job = None
        _text_jobs_event.set()


def _text_job_worker():
    global _active_text_job
    while True:
        _text_jobs_event.wait()
        _text_jobs_event.clear()
        while True:
            with _text_jobs_lock:
                with connect() as db:
                    queued = db.execute("SELECT id FROM text_crop_jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
                if not queued:
                    break
                _active_text_job = queued["id"]
            _process_text_crop_job(queued["id"])


@app.post("/api/text-sessions/{sid}/prepare-crops")
def prepare_text_crops(sid: str):
    with connect() as db:
        session = db.execute("SELECT source_session_id FROM text_sessions WHERE id=?", (sid,)).fetchone()
        if not session:
            raise HTTPException(404, "Text annotation session not found")
        source_session_id = session["source_session_id"]
        if not source_session_id:
            raise HTTPException(409, "Text session is not linked to a Container Detection session")
        total = db.execute("SELECT COUNT(*) FROM images WHERE session_id=?", (source_session_id,)).fetchone()[0]
    fingerprint = _text_source_fingerprint(source_session_id)
    now = time.time()
    with _text_jobs_lock, connect() as db:
        active = db.execute("SELECT * FROM text_crop_jobs WHERE session_id=? AND status IN ('queued','running') ORDER BY created DESC LIMIT 1", (sid,)).fetchone()
        if active:
            return dict(active)
        previous = db.execute("SELECT * FROM text_crop_jobs WHERE session_id=? AND status='complete' ORDER BY created DESC LIMIT 1", (sid,)).fetchone()
        if previous and previous["source_fingerprint"] == fingerprint:
            return dict(previous)
        job_id = uuid4().hex
        db.execute("INSERT INTO text_crop_jobs(id,session_id,source_session_id,status,total,source_fingerprint,created,updated) VALUES(?,?,?,'queued',?,?,?,?)", (job_id, sid, source_session_id, total, fingerprint, now, now))
    _start_text_job_worker()
    _text_jobs_event.set()
    with connect() as db:
        return dict(db.execute("SELECT * FROM text_crop_jobs WHERE id=?", (job_id,)).fetchone())


@app.get("/api/text-crop-jobs/{job_id}")
def text_crop_job_status(job_id: str):
    with connect() as db:
        row = db.execute("SELECT * FROM text_crop_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Text crop job not found")
    return dict(row)


@app.get("/api/server-dataset/children")
def server_dataset_children(path: str = ""):
    folder = server_dataset_path(path)
    if not folder.is_dir():
        raise HTTPException(404, "Directory not found")
    result = []
    for child in folder.iterdir():
        if child.name == "_annotation_backups":
            continue
        if child.is_dir():
            result.append({"name": child.name, "kind": "directory"})
        elif child.is_file():
            stat = child.stat()
            result.append({"name": child.name, "kind": "file", "size": stat.st_size, "mtime": stat.st_mtime})
    result.sort(key=lambda item: (item["kind"] != "directory", item["name"].casefold()))
    return result


@app.get("/api/server-dataset/file")
def server_dataset_file(path: str):
    target = server_dataset_path(path)
    if not target.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(target, filename=target.name)


@app.head("/api/server-dataset/file")
def server_dataset_file_exists(path: str):
    if not server_dataset_path(path).is_file():
        raise HTTPException(404, "File not found")
    return {}


@app.post("/api/server-dataset/directory")
def create_server_dataset_directory(path: str):
    target = server_dataset_path(path)
    target.mkdir(parents=True, exist_ok=True)
    return {"created": True}


@app.put("/api/server-dataset/file")
async def write_server_dataset_file(path: str, request: Request):
    if not path:
        raise HTTPException(400, "A file path is required")
    target = server_dataset_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as output:
            async for chunk in request.stream():
                output.write(chunk)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"saved": True}


@app.delete("/api/server-dataset/file")
def delete_server_dataset_file(path: str):
    if not path:
        raise HTTPException(400, "A file path is required")
    target = server_dataset_path(path)
    if target.is_dir():
        raise HTTPException(400, "Only files may be removed")
    elif target.exists():
        target.unlink()
    else:
        raise HTTPException(404, "File not found")
    return {"removed": True}


@app.post("/api/sessions")
def begin(data: Begin):
    sid = data.session_id or str(uuid4())
    now = time.time()
    with connect() as db:
        row = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if row:
            db.execute("UPDATE sessions SET last_seen=?,root_name=?,class_name=?,class_id=? WHERE id=?",
                       (now, data.root_name, data.class_name, data.class_id, sid))
        else:
            db.execute("INSERT INTO sessions(id,root_name,created,last_seen,class_name,class_id) VALUES(?,?,?,?,?,?)",
                       (sid, data.root_name, now, now, data.class_name, data.class_id))
        row = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    return dict(row)


@app.post("/api/text-sessions")
def begin_text_session(data: TextBegin):
    sid = data.session_id or str(uuid4())
    now = time.time()
    with connect() as db:
        if data.source_session_id and not db.execute("SELECT 1 FROM sessions WHERE id=?", (data.source_session_id,)).fetchone():
            raise HTTPException(404, "Container Detection source session not found")
        row = db.execute("SELECT * FROM text_sessions WHERE id=?", (sid,)).fetchone()
        if row:
            if data.source_session_id and row["source_session_id"] and data.source_session_id != row["source_session_id"]:
                raise HTTPException(409, "This text session is linked to another Container Detection session; create a new Text Region Detection session")
            db.execute("UPDATE text_sessions SET last_seen=?,root_name=?,source_session_id=COALESCE(?,source_session_id) WHERE id=?", (now, data.root_name, data.source_session_id, sid))
        else:
            db.execute("INSERT INTO text_sessions(id,root_name,created,last_seen,source_session_id) VALUES(?,?,?,?,?)", (sid, data.root_name, now, now, data.source_session_id or ""))
        return dict(db.execute("SELECT * FROM text_sessions WHERE id=?", (sid,)).fetchone())


@app.get("/api/sessions")
def sessions():
    with connect() as db:
        return [dict(r) for r in db.execute("SELECT s.*, COUNT(i.path) AS image_count FROM sessions s LEFT JOIN images i ON i.session_id=s.id GROUP BY s.id ORDER BY last_seen DESC LIMIT 50")]


@app.get("/api/text-sessions")
def text_sessions():
    with connect() as db:
        return [dict(r) for r in db.execute("SELECT s.*, COUNT(a.path) AS image_count FROM text_sessions s LEFT JOIN text_annotations a ON a.session_id=s.id AND a.active=1 GROUP BY s.id ORDER BY last_seen DESC LIMIT 50")]


@app.get("/api/text-sessions/{sid}/container-counts")
def container_count_list(sid: str, status: str = "all", offset: int = 0, limit: int = 500):
    if status not in {"all", "unlabeled", "1", "2"}:
        raise HTTPException(400, "Invalid container count filter")
    limit, offset = min(max(limit, 1), 1000), max(offset, 0)
    where = "a.session_id=? AND a.active=1 AND a.cached_crop_path IS NOT NULL"
    args: tuple = (sid,)
    if status == "unlabeled":
        where += " AND (c.container_count IS NULL)"
    elif status in {"1", "2"}:
        where += " AND c.container_count=?"
        args += (int(status),)
    with connect() as db:
        if not db.execute("SELECT 1 FROM text_sessions WHERE id=?", (sid,)).fetchone():
            raise HTTPException(404, "Text crop session not found")
        rows = [dict(r) for r in db.execute(f"SELECT a.path,a.source_path,a.box_index,a.container_box,a.cached_crop_path,a.size,a.mtime,c.container_count FROM text_annotations a LEFT JOIN container_counts c ON c.session_id=a.session_id AND c.path=a.path WHERE {where} ORDER BY a.path LIMIT ? OFFSET ?", (*args, limit, offset))]
        totals = db.execute("SELECT COUNT(*) total, SUM(CASE WHEN c.container_count IS NULL THEN 1 ELSE 0 END) unlabeled, SUM(CASE WHEN c.container_count=1 THEN 1 ELSE 0 END) one_count, SUM(CASE WHEN c.container_count=2 THEN 1 ELSE 0 END) two_count FROM text_annotations a LEFT JOIN container_counts c ON c.session_id=a.session_id AND c.path=a.path WHERE a.session_id=? AND a.active=1 AND a.cached_crop_path IS NOT NULL", (sid,)).fetchone()
        total = db.execute(f"SELECT COUNT(*) FROM text_annotations a LEFT JOIN container_counts c ON c.session_id=a.session_id AND c.path=a.path WHERE {where}", args).fetchone()[0]
    return {"items": rows, "total": total, "counts": {"all": totals["total"], "unlabeled": totals["unlabeled"] or 0, "1": totals["one_count"] or 0, "2": totals["two_count"] or 0}}


def _casting_hole_item(db, sid: str, path: str):
    return db.execute("""SELECT a.path,a.source_path,a.box_index,a.cached_crop_path,c.container_count,
        h.status,h.boxes,h.yolo_labels,h.image_width,h.image_height
        FROM text_annotations a
        JOIN container_counts c ON c.session_id=a.session_id AND c.path=a.path
        LEFT JOIN casting_hole_annotations h ON h.session_id=a.session_id AND h.path=a.path
        WHERE a.session_id=? AND a.path=? AND a.active=1 AND a.cached_crop_path IS NOT NULL
          AND c.container_count=2""", (sid, path)).fetchone()


@app.get("/api/casting-holes/{sid}/images")
def casting_hole_images(sid: str, status: str = "all"):
    if status not in {"all", "unreviewed", "annotated", "skipped"}:
        raise HTTPException(400, "Invalid casting-hole annotation status")
    with connect() as db:
        if not db.execute("SELECT 1 FROM text_sessions WHERE id=?", (sid,)).fetchone():
            raise HTTPException(404, "Container crop session not found")
        where = "a.session_id=? AND a.active=1 AND a.cached_crop_path IS NOT NULL AND c.container_count=2"
        args: tuple = (sid,)
        if status != "all":
            where += " AND COALESCE(h.status,'unreviewed')=?"
            args += (status,)
        rows = [dict(row) for row in db.execute(f"""SELECT a.path,a.source_path,a.box_index,
            a.cached_crop_path,c.container_count,COALESCE(h.status,'unreviewed') status,
            COALESCE(h.boxes,'[]') boxes,COALESCE(h.yolo_labels,'') yolo_labels,
            h.image_width,h.image_height
            FROM text_annotations a
            JOIN container_counts c ON c.session_id=a.session_id AND c.path=a.path
            LEFT JOIN casting_hole_annotations h ON h.session_id=a.session_id AND h.path=a.path
            WHERE {where} ORDER BY a.path""", args)]
        counts = db.execute("""SELECT COUNT(*) total,
            SUM(CASE WHEN h.status='annotated' THEN 1 ELSE 0 END) annotated,
            SUM(CASE WHEN h.status='skipped' THEN 1 ELSE 0 END) skipped
            FROM text_annotations a
            JOIN container_counts c ON c.session_id=a.session_id AND c.path=a.path
            LEFT JOIN casting_hole_annotations h ON h.session_id=a.session_id AND h.path=a.path
            WHERE a.session_id=? AND a.active=1 AND a.cached_crop_path IS NOT NULL AND c.container_count=2""", (sid,)).fetchone()
    for row in rows:
        row["boxes"] = json.loads(row["boxes"])
    return {"items": rows, "total": counts["total"] or 0,
            "counts": {"annotated": counts["annotated"] or 0, "skipped": counts["skipped"] or 0,
                       "unreviewed": (counts["total"] or 0) - (counts["annotated"] or 0) - (counts["skipped"] or 0)}}


@app.get("/api/casting-holes/{sid}/export")
def export_casting_hole_annotations(sid: str):
    with connect() as db:
        if not db.execute("SELECT 1 FROM text_sessions WHERE id=?", (sid,)).fetchone():
            raise HTTPException(404, "Container crop session not found")
        rows = [dict(row) for row in db.execute("""SELECT a.path,a.source_path,a.cached_crop_path,
            COALESCE(h.status,'unreviewed') status,COALESCE(h.boxes,'[]') boxes,
            COALESCE(h.yolo_labels,'') yolo_labels,h.image_width,h.image_height
            FROM text_annotations a
            JOIN container_counts c ON c.session_id=a.session_id AND c.path=a.path
            LEFT JOIN casting_hole_annotations h ON h.session_id=a.session_id AND h.path=a.path
            WHERE a.session_id=? AND a.active=1 AND a.cached_crop_path IS NOT NULL
              AND c.container_count=2 ORDER BY a.path""", (sid,))]

    annotated = []
    for row in rows:
        row["boxes"] = json.loads(row["boxes"])
        if row["status"] == "annotated" and row["boxes"] and row["yolo_labels"].strip():
            annotated.append(row)
    if not annotated:
        raise HTTPException(409, "There are no saved casting-hole annotations to export")

    archive_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="casting-holes-", suffix=".zip", dir=DATA, delete=False) as tmp:
            archive_path = Path(tmp.name)
        manifest = io.StringIO(newline="")
        writer = csv.writer(manifest)
        writer.writerow(["image_filename", "label_path_in_export", "source_image", "cached_crop_path", "annotation_status", "box_count", "image_width", "image_height", "exported"])
        exported_by_path = {}
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            for row in annotated:
                image_stem = valid_path(row["path"])
                label_name = f"labels/{image_stem}.txt"
                archive.writestr(label_name, row["yolo_labels"])
                exported_by_path[row["path"]] = (f"images/{image_stem}.png", label_name, len(row["boxes"]))
            for row in rows:
                item = exported_by_path.get(row["path"])
                writer.writerow([item[0] if item else f"images/{row['path']}.png", item[1] if item else "", row["source_path"], row["cached_crop_path"], row["status"], item[2] if item else len(row["boxes"]), row["image_width"] or "", row["image_height"] or "", "yes" if item else "no"])
            archive.writestr("metadata.csv", manifest.getvalue())
            archive.writestr("classes.txt", "0 casting_hole\n")
            archive.writestr("README.txt", "Casting Holes Labeling label-only export\n\nThis ZIP contains no images. Each labels/<image-path>.txt uses the same image-path stem as its matching images/<image-path>.png and contains normalized YOLO rows: class_id x_center y_center width height. Class 0 is casting_hole. metadata.csv maps exported filenames to the cached crop path already present in the raw dataset. Skipped and unreviewed images are listed but have no exported label. Original source images and classification/detection labels are not included or modified.\n")
        return FileResponse(archive_path, media_type="application/zip", filename=f"casting-holes-{sid}.zip",
                            background=BackgroundTask(archive_path.unlink, missing_ok=True))
    except Exception:
        if archive_path:
            archive_path.unlink(missing_ok=True)
        raise


@app.put("/api/casting-holes/{sid}/image")
def save_casting_hole_image(sid: str, path: str, data: CastingHoleReview):
    path, now = valid_path(path), time.time()
    if not data.boxes:
        raise HTTPException(422, "Add at least one casting-hole bounding box before saving")
    for box in data.boxes:
        if len(box) != 4 or any(not math.isfinite(value) for value in box):
            raise HTTPException(422, "Each casting-hole box must contain four finite normalized values")
        cx, cy, width, height = box
        if not (0 <= cx < 1 and 0 <= cy < 1 and 0 < width <= 1 and 0 < height <= 1
                and cx >= width / 2 and cy >= height / 2
                and cx + width / 2 <= 1 and cy + height / 2 <= 1):
            raise HTTPException(422, "Invalid normalized YOLO box")
    labels = "".join("0 " + " ".join(f"{value:.6f}" for value in box) + "\n" for box in data.boxes)
    with connect() as db:
        source = _casting_hole_item(db, sid, path)
        if not source:
            raise HTTPException(404, "Image is not an eligible 2-container crop")
        db.execute("""INSERT INTO casting_hole_annotations
            (session_id,path,source_path,cached_crop_path,status,boxes,yolo_labels,image_width,image_height,updated)
            VALUES(?,?,?,?, 'annotated',?,?,?,?,?)
            ON CONFLICT(session_id,path) DO UPDATE SET source_path=excluded.source_path,
              cached_crop_path=excluded.cached_crop_path,status='annotated',boxes=excluded.boxes,
              yolo_labels=excluded.yolo_labels,image_width=excluded.image_width,
              image_height=excluded.image_height,updated=excluded.updated""",
            (sid, path, source["source_path"], source["cached_crop_path"], json.dumps(data.boxes),
             labels, data.image_width, data.image_height, now))
    return {"saved": True, "status": "annotated", "class_id": 0,
            "class_name": "casting_hole", "yolo_labels": labels}


@app.post("/api/casting-holes/{sid}/skip")
def skip_casting_hole_image(sid: str, path: str):
    path, now = valid_path(path), time.time()
    with connect() as db:
        source = _casting_hole_item(db, sid, path)
        if not source:
            raise HTTPException(404, "Image is not an eligible 2-container crop")
        db.execute("""INSERT INTO casting_hole_annotations
            (session_id,path,source_path,cached_crop_path,status,updated)
            VALUES(?,?,?,?,'skipped',?)
            ON CONFLICT(session_id,path) DO UPDATE SET source_path=excluded.source_path,
              cached_crop_path=excluded.cached_crop_path,
              status=CASE WHEN casting_hole_annotations.status='annotated' THEN 'annotated' ELSE 'skipped' END,
              updated=excluded.updated""",
            (sid, path, source["source_path"], source["cached_crop_path"], now))
        status_row = db.execute("SELECT status FROM casting_hole_annotations WHERE session_id=? AND path=?", (sid, path)).fetchone()
    return {"saved": False, "status": status_row["status"]}


@app.put("/api/text-sessions/{sid}/container-count")
def update_container_count(sid: str, path: str, data: dict):
    path, value = valid_path(path), data.get("container_count")
    if value not in (1, 2):
        raise HTTPException(422, "container_count must be 1 or 2")
    now = time.time()
    with connect() as db:
        if not db.execute("SELECT 1 FROM text_annotations WHERE session_id=? AND path=? AND active=1 AND cached_crop_path IS NOT NULL", (sid, path)).fetchone():
            raise HTTPException(404, "Persistent container crop not found")
        db.execute("INSERT INTO container_counts(session_id,path,container_count,updated) VALUES(?,?,?,?) ON CONFLICT(session_id,path) DO UPDATE SET container_count=excluded.container_count,updated=excluded.updated", (sid, path, value, now))
        db.execute("UPDATE text_sessions SET last_seen=? WHERE id=?", (now, sid))
    return {"saved": True, "container_count": value}


@app.post("/api/text-sessions/{sid}/container-count-bulk")
def bulk_update_container_count(sid: str, data: dict):
    paths, value = data.get("paths"), data.get("container_count")
    if not isinstance(paths, list) or not paths or len(paths) > 50000 or any(not isinstance(path, str) for path in paths):
        raise HTTPException(422, "Select between 1 and 50000 crop paths")
    if value not in (1, 2):
        raise HTTPException(422, "container_count must be 1 or 2")
    paths = list(dict.fromkeys(valid_path(path) for path in paths))
    now = time.time()
    with connect() as db:
        if not db.execute("SELECT 1 FROM text_sessions WHERE id=?", (sid,)).fetchone():
            raise HTTPException(404, "Text crop session not found")
        placeholders = ",".join("?" for _ in paths)
        found = {row[0] for row in db.execute(f"SELECT path FROM text_annotations WHERE session_id=? AND active=1 AND cached_crop_path IS NOT NULL AND path IN ({placeholders})", (sid, *paths))}
        if found != set(paths):
            raise HTTPException(404, "One or more selected container crops are unavailable")
        db.executemany("INSERT INTO container_counts(session_id,path,container_count,updated) VALUES(?,?,?,?) ON CONFLICT(session_id,path) DO UPDATE SET container_count=excluded.container_count,updated=excluded.updated", [(sid, path, value, now) for path in paths])
        db.execute("UPDATE text_sessions SET last_seen=? WHERE id=?", (now, sid))
    return {"saved": len(paths), "container_count": value, "paths": paths}


@app.get("/api/text-sessions/{sid}/container-count-export")
def export_container_counts(sid: str):
    import csv
    import io
    import zipfile
    with connect() as db:
        if not db.execute("SELECT 1 FROM text_sessions WHERE id=?", (sid,)).fetchone():
            raise HTTPException(404, "Text crop session not found")
        rows = [dict(r) for r in db.execute("SELECT a.path,a.source_path,a.box_index,a.cached_crop_path,c.container_count FROM text_annotations a JOIN container_counts c ON c.session_id=a.session_id AND c.path=a.path WHERE a.session_id=? AND a.active=1 AND a.cached_crop_path IS NOT NULL AND c.container_count IS NOT NULL ORDER BY a.path", (sid,))]
    target = DATA / f"container-count-{sid}.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        manifest = io.StringIO(newline="")
        writer = csv.writer(manifest)
        writer.writerow(["image_path", "container_count", "labeling_status", "source_image_path", "container_box_index"])
        for row in rows:
            image_path = f"images/{Path(row['cached_crop_path']).name}"
            source = server_dataset_path(row["cached_crop_path"])
            if not source.is_file():
                continue
            writer.writerow([image_path, row["container_count"], "labeled", row["source_path"], row["box_index"]])
            archive.write(source, image_path)
        archive.writestr("labels.csv", manifest.getvalue())
    return FileResponse(target, filename=f"container-count-{sid}.zip", media_type="application/zip")


@app.post("/api/text-sessions/{sid}/index")
def index_text_batch(sid: str, data: TextIndexBatch):
    now = time.time()
    with connect() as db:
        if not db.execute("SELECT 1 FROM text_sessions WHERE id=?", (sid,)).fetchone():
            raise HTTPException(404, "Text annotation session not found")
        for item in data.items:
            path = valid_path(item.path)
            old = db.execute("SELECT size,mtime,status FROM text_annotations WHERE session_id=? AND path=?", (sid, path)).fetchone()
            changed = old and ((old["size"] is not None and item.size is not None and old["size"] != item.size)
                               or (old["mtime"] is not None and item.mtime is not None and abs(old["mtime"] - item.mtime) > 1))
            if changed:
                db.execute("UPDATE text_annotations SET size=?,mtime=?,status='unreviewed',boxes='[]',labels_written=0,updated=? WHERE session_id=? AND path=?", (item.size, item.mtime, now, sid, path))
            else:
                db.execute("INSERT INTO text_annotations(session_id,path,size,mtime,updated) VALUES(?,?,?,?,?) ON CONFLICT(session_id,path) DO UPDATE SET size=COALESCE(excluded.size,text_annotations.size),mtime=COALESCE(excluded.mtime,text_annotations.mtime),updated=excluded.updated", (sid, path, item.size, item.mtime, now))
        db.execute("UPDATE text_sessions SET last_seen=? WHERE id=?", (now, sid))
    return {"indexed": len(data.items)}


@app.get("/api/text-sessions/{sid}/images")
def text_image_list(sid: str, status: str = "all", offset: int = 0, limit: int = 500):
    allowed = ("all", "unreviewed", "verified", "rejected")
    if status not in allowed:
        raise HTTPException(400, "Invalid text annotation status")
    limit, offset = min(max(limit, 1), 1000), max(offset, 0)
    with connect() as db:
        if not db.execute("SELECT 1 FROM text_sessions WHERE id=?", (sid,)).fetchone():
            raise HTTPException(404, "Text annotation session not found")
        where = "session_id=?" + (" AND status=?" if status != "all" else "")
        args = (sid, status) if status != "all" else (sid,)
        where += " AND active=1"
        rows = [dict(r) for r in db.execute(f"SELECT path,size,mtime,status,boxes,labels_written,source_path,box_index,container_box,cached_crop_path FROM text_annotations WHERE {where} ORDER BY path LIMIT ? OFFSET ?", (*args, limit, offset))]
        counts = {r["status"]: r["n"] for r in db.execute("SELECT status,COUNT(*) n FROM text_annotations WHERE session_id=? AND active=1 GROUP BY status", (sid,))}
        total = db.execute(f"SELECT COUNT(*) FROM text_annotations WHERE {where}", args).fetchone()[0]
    for row in rows:
        row["boxes"] = json.loads(row["boxes"])
        row["container_box"] = json.loads(row["container_box"]) if row.get("container_box") else None
    return {"items": rows, "total": total, "counts": {key: counts.get(key, 0) for key in allowed[1:]}}


@app.get("/api/text-sessions/{sid}/image")
def get_text_image(sid: str, path: str):
    path = valid_path(path)
    with connect() as db:
        row = db.execute("SELECT path,size,mtime,status,boxes,labels_written,source_path,box_index,container_box,cached_crop_path FROM text_annotations WHERE session_id=? AND path=? AND active=1", (sid, path)).fetchone()
        if not row:
            raise HTTPException(404, "Text annotation image not indexed")
    result = dict(row)
    result["boxes"] = json.loads(result["boxes"])
    return result


@app.put("/api/text-sessions/{sid}/image")
def update_text_image(sid: str, path: str, data: TextReview):
    path, now = valid_path(path), time.time()
    for box in data.boxes:
        if len(box) != 4 or any(not isinstance(v, (int, float)) for v in box) or not (0 <= box[0] < 1 and 0 <= box[1] < 1 and 0 < box[2] <= 1 and 0 < box[3] <= 1 and box[0] >= box[2] / 2 and box[1] >= box[3] / 2 and box[0] + box[2] / 2 <= 1 and box[1] + box[3] / 2 <= 1):
            raise HTTPException(422, "Invalid normalized YOLO box")
    if data.status == "verified" and (not data.boxes or not data.labels_written):
        raise HTTPException(400, "A verified text annotation requires at least one box and a persisted YOLO label")
    if data.status == "rejected" and data.boxes:
        raise HTTPException(400, "Rejected images cannot have a text-region box")
    with connect() as db:
        if not db.execute("SELECT 1 FROM text_annotations WHERE session_id=? AND path=?", (sid, path)).fetchone():
            raise HTTPException(404, "Text annotation image not indexed")
        db.execute("UPDATE text_annotations SET status=?,boxes=?,labels_written=?,updated=? WHERE session_id=? AND path=?", (data.status, json.dumps(data.boxes), int(data.labels_written), now, sid, path))
        db.execute("UPDATE text_sessions SET last_seen=?,current_path=? WHERE id=?", (now, path, sid))
    return {"saved": True, "status": data.status, "class_id": 0, "class_name": "container_number"}


@app.post("/api/text-sessions/{sid}/current")
def set_text_current(sid: str, data: dict):
    path = valid_path(data.get("path", ""))
    with connect() as db:
        db.execute("UPDATE text_sessions SET current_path=?,last_seen=? WHERE id=?", (path, time.time(), sid))
    return {"saved": True}


@app.get("/api/text-sessions/{sid}/export")
def export_text_progress(sid: str):
    with connect() as db:
        session = db.execute("SELECT * FROM text_sessions WHERE id=?", (sid,)).fetchone()
        if not session:
            raise HTTPException(404, "Text annotation session not found")
        annotations = [dict(row) for row in db.execute("SELECT * FROM text_annotations WHERE session_id=?", (sid,))]
    target = DATA / f"text-{sid}-progress.json"
    target.write_text(json.dumps({"session": dict(session), "images": annotations, "class_id": 0, "class_name": "container_number"}, indent=2))
    return FileResponse(target, filename=f"text-{sid}-progress.json")


@app.post("/api/sessions/{sid}/index")
def index_batch(sid: str, data: IndexBatch):
    now = time.time()
    with connect() as db:
        if not db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
            raise HTTPException(404, "Session not found")
        for item in data.items:
            path = valid_path(item.path)
            old = db.execute("SELECT size,mtime,status FROM images WHERE session_id=? AND path=?", (sid,path)).fetchone()
            # A changed file needs fresh review; same metadata preserves progress.
            changed = old and ((old['size'] is not None and item.size is not None and old['size'] != item.size)
                               or (old['mtime'] is not None and item.mtime is not None and abs(old['mtime']-item.mtime)>1))
            if changed:
                db.execute("UPDATE images SET size=?,mtime=?,status='unreviewed',boxes='[]',labels_written=0,crop_json=NULL,derivative_path=NULL,updated=? WHERE session_id=? AND path=?", (item.size,item.mtime,now,sid,path))
            else:
                db.execute("INSERT INTO images(session_id,path,size,mtime,updated) VALUES(?,?,?,?,?) ON CONFLICT(session_id,path) DO UPDATE SET size=COALESCE(excluded.size,images.size),mtime=COALESCE(excluded.mtime,images.mtime),updated=excluded.updated", (sid,path,item.size,item.mtime,now))
        db.execute("UPDATE sessions SET last_seen=? WHERE id=?", (now,sid))
    return {"indexed": len(data.items)}


@app.get("/api/sessions/{sid}/images")
def image_list(sid: str, status: str = "all", offset: int = 0, limit: int = 500):
    if status not in ("all","unreviewed","verified","rejected"): raise HTTPException(400,"Invalid status")
    limit = min(max(limit,1),1000); offset=max(offset,0)
    with connect() as db:
        if not db.execute("SELECT 1 FROM sessions WHERE id=?",(sid,)).fetchone(): raise HTTPException(404,"Session not found")
        where = "session_id=?" + (" AND status=?" if status != "all" else "")
        args = (sid,status) if status != "all" else (sid,)
        rows = [dict(r) for r in db.execute(f"SELECT path,size,mtime,status,boxes,labels_written,rejection_reason,crop_json,derivative_path FROM images WHERE {where} ORDER BY path LIMIT ? OFFSET ?",(*args,limit,offset))]
        counts = {r['status']:r['n'] for r in db.execute("SELECT status,COUNT(*) n FROM images WHERE session_id=? GROUP BY status",(sid,))}
        total = db.execute(f"SELECT COUNT(*) FROM images WHERE {where}",args).fetchone()[0]
    for row in rows:
        row['boxes']=json.loads(row['boxes'])
        row['crop']=json.loads(row.pop('crop_json')) if row.get('crop_json') else None
    return {"items":rows,"total":total,"counts":{k:counts.get(k,0) for k in ("unreviewed","verified","rejected")}}


@app.get("/api/sessions/{sid}/image")
def get_image(sid: str, path: str):
    path=valid_path(path)
    with connect() as db:
        row=db.execute("SELECT path,size,mtime,status,boxes,labels_written,crop_json,derivative_path FROM images WHERE session_id=? AND path=?",(sid,path)).fetchone()
        if not row: raise HTTPException(404,"Image not indexed")
    result=dict(row); result['boxes']=json.loads(result['boxes']); result['crop']=json.loads(result.pop('crop_json')) if result.get('crop_json') else None; return result


@app.put("/api/sessions/{sid}/image")
def update_image(sid: str, path: str, data: Review):
    path=valid_path(path); now=time.time()
    if data.derivative_path is not None:
        data.derivative_path = valid_path(data.derivative_path)
    if data.crop is not None:
        try:
            x,y,w,h=(float(data.crop[k]) for k in ("x","y","width","height"))
            sw,sh=(float(data.crop[k]) for k in ("source_width","source_height"))
        except (KeyError,TypeError,ValueError):
            raise HTTPException(422,"Crop requires x, y, width, height, source_width, and source_height")
        if x<0 or y<0 or w<=0 or h<=0 or sw<=0 or sh<=0 or x+w>sw or y+h>sh or not data.derivative_path:
            raise HTTPException(422,"Crop must be inside the source image and have a derivative path")
    for box in data.boxes:
        if len(box)!=4 or any(not isinstance(v,(int,float)) for v in box) or not (0<=box[0]<1 and 0<=box[1]<1 and 0<box[2]<=1 and 0<box[3]<=1 and box[0]>=box[2]/2 and box[1]>=box[3]/2 and box[0]+box[2]/2<=1 and box[1]+box[3]/2<=1):
            raise HTTPException(422,"Invalid normalized YOLO box")
    if data.status=="verified" and (not data.boxes or not data.labels_written): raise HTTPException(400,"Verified images require boxes and a successfully written local label file")
    with connect() as db:
        if not db.execute("SELECT 1 FROM images WHERE session_id=? AND path=?",(sid,path)).fetchone(): raise HTTPException(404,"Image not indexed")
        if data.existing_label is not None:
            db.execute("INSERT INTO annotation_backups(session_id,path,content,created) VALUES(?,?,?,?)",(sid,path,data.existing_label,now))
        db.execute("UPDATE images SET status=?,boxes=?,labels_written=?,rejection_reason=?,crop_json=?,derivative_path=?,updated=? WHERE session_id=? AND path=?", (data.status,json.dumps(data.boxes),int(data.labels_written),data.rejection_reason,json.dumps(data.crop) if data.crop else None,data.derivative_path,now,sid,path))
        db.execute("UPDATE sessions SET last_seen=?,current_path=? WHERE id=?",(now,path,sid))
    return {"saved":True,"status":data.status}


@app.post("/api/sessions/{sid}/current")
def set_current(sid: str, data: dict):
    path=valid_path(data.get("path",""))
    with connect() as db: db.execute("UPDATE sessions SET current_path=?,last_seen=? WHERE id=?",(path,time.time(),sid))
    return {"saved":True}


@app.get("/api/sessions/{sid}/backups")
def backup_list(sid: str):
    with connect() as db:
        return [dict(r) for r in db.execute("SELECT id,path,content,created FROM annotation_backups WHERE session_id=? ORDER BY id DESC LIMIT 1000",(sid,))]


@app.get("/api/sessions/{sid}/export")
def export_db(sid: str):
    with connect() as db:
        if not db.execute("SELECT 1 FROM sessions WHERE id=?",(sid,)).fetchone(): raise HTTPException(404,"Session not found")
    target=DATA/f"{sid}-progress.json"
    with connect() as db:
        target.write_text(json.dumps({"session":dict(db.execute("SELECT * FROM sessions WHERE id=?",(sid,)).fetchone()),"images":[dict(r) for r in db.execute("SELECT * FROM images WHERE session_id=?",(sid,))],"backups":[dict(r) for r in db.execute("SELECT * FROM annotation_backups WHERE session_id=?",(sid,))]},indent=2))
    return FileResponse(target,filename=f"{sid}-progress.json")


@app.get("/")
def home(): return FileResponse(ROOT/"static"/"index.html")

app.mount("/static",StaticFiles(directory=ROOT/"static"),name="static")
