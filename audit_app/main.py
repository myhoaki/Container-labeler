from __future__ import annotations

import os
import base64
import csv
import hashlib
import io
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).parent
DATASET_ROOT = Path(os.getenv("DATASET_ROOT", "/server-dataset/container_images_verified"))
STATE_DIR = Path(os.getenv("AUDIT_STATE_DIR", "/state"))
DB_PATH = STATE_DIR / "quality-audit.sqlite3"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".avif", ".tif", ".tiff"}
IGNORED_DIRS = {"_rejected", ".container-labeler", "_annotation_backups"}
app = FastAPI(title="Container Dataset Audit", version="1.0.0")
_jobs_lock = threading.Lock()
_active_job: str | None = None


@app.on_event("startup")
def recover_interrupted_jobs():
    """Expose resumable state when a process restarts during a background job."""
    with db() as connection:
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            "UPDATE jobs SET status='failed',updated_at=?,error='Audit interrupted by server restart; completed image results are preserved and can be resumed.' WHERE status='running'",
            (now,),
        )


def db():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, status TEXT NOT NULL, processed INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, dataset_fingerprint TEXT, report_json TEXT, error TEXT)")
    connection.execute("CREATE TABLE IF NOT EXISTS results (job_id TEXT NOT NULL, image_path TEXT NOT NULL, result_json TEXT NOT NULL, PRIMARY KEY(job_id,image_path))")
    connection.execute("CREATE TABLE IF NOT EXISTS job_items (job_id TEXT NOT NULL, position INTEGER NOT NULL, image_path TEXT NOT NULL, size INTEGER NOT NULL, mtime REAL NOT NULL, PRIMARY KEY(job_id,position))")
    connection.commit()
    return connection


def dataset_inventory():
    root = dataset_path()
    images: list[dict[str, Any]] = []
    labels: list[str] = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in IGNORED_DIRS]
        folder = Path(base)
        for name in files:
            path = folder / name
            rel = path.relative_to(root).as_posix()
            if path.suffix.lower() in IMAGE_EXTENSIONS:
                try:
                    stat = path.stat()
                    images.append({"path": rel, "size": stat.st_size, "mtime": stat.st_mtime})
                except OSError:
                    images.append({"path": rel, "size": 0, "mtime": 0})
            elif path.suffix.lower() == ".txt":
                labels.append(rel)
    images.sort(key=lambda item: item["path"].casefold())
    labels.sort(key=str.casefold)
    digest = hashlib.sha256()
    for item in images:
        digest.update(f"I\0{item['path']}\0{item['size']}\0{item['mtime']}\n".encode())
    for label in labels:
        digest.update(f"L\0{label}\n".encode())
    return root, images, labels, digest.hexdigest()


def dataset_path(path: str = "") -> Path:
    if not DATASET_ROOT.is_dir():
        raise HTTPException(404, "The configured server dataset folder is unavailable")
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise HTTPException(400, "Expected a safe relative dataset path")
    target = (DATASET_ROOT / relative).resolve()
    if not target.is_relative_to(DATASET_ROOT.resolve()):
        raise HTTPException(400, "Expected a path inside the server dataset")
    return target


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/dataset")
def dataset_index():
    root, images, labels, fingerprint = dataset_inventory()
    return {"name": root.name, "images": images, "labels": labels}


def label_for_image(image_path: str, labels: set[str]) -> str | None:
    stem = image_path.rsplit(".", 1)[0]
    parts = stem.split("/")
    candidates = [f"{stem}.txt"]
    try:
        image_index = next(i for i, part in enumerate(parts) if part.lower() == "images")
        parts[image_index] = "labels"
        candidates.insert(0, f"{'/'.join(parts)}.txt")
    except StopIteration:
        candidates.insert(0, f"labels/{stem}.txt")
    return next((path for path in candidates if path in labels), None)


def infer_split(path: str) -> str:
    for part in Path(path).parts:
        name=part.lower()
        if name in {"train","training"}: return "train"
        if name in {"val","valid","validation"}: return "validation"
        if name in {"test","testing"}: return "test"
    return "unspecified"


def parse_label_text(raw: str) -> dict:
    result={"boxes":[],"errors":[],"status":"empty"}
    for line_number,line in enumerate(raw.splitlines(),1):
        if not line.strip(): continue
        fields=line.split()
        if len(fields)!=5:
            result["errors"].append(f"malformed_label:{line_number}"); continue
        try: class_id,cx,cy,width,height=map(float,fields)
        except ValueError:
            result["errors"].append(f"non_numeric_label:{line_number}"); continue
        if not class_id.is_integer() or class_id != 0:
            result["errors"].append(f"unsupported_class_id:{line_number}"); continue
        if width <= 0 or height <= 0:
            result["errors"].append(f"invalid_box_size:{line_number}"); continue
        if min(cx-width/2,cy-height/2,cx,cy,width,height)<0 or max(cx+width/2,cy+height/2,cx,cy,width,height)>1:
            result["errors"].append(f"invalid_coordinates:{line_number}"); continue
        result["boxes"].append({"cx":cx,"cy":cy,"width":width,"height":height,"area":width*height,"aspect":width/height})
    result["status"]="invalid" if result["errors"] else ("annotated" if result["boxes"] else "empty")
    return result


def audit_worker(job_id: str, items: list[dict], labels: list[str], fingerprint: str):
    global _active_job
    try:
        from PIL import Image, ImageStat, ImageFilter
        records = []
        label_set = set(labels)
        total = len(items)
        with db() as connection:
            completed={row[0] for row in connection.execute("SELECT image_path FROM results WHERE job_id=?",(job_id,))}
            for index, item in enumerate(items):
                with db() as check_connection:
                    job=check_connection.execute("SELECT status FROM jobs WHERE id=?",(job_id,)).fetchone()
                if not job or job["status"] != "running":
                    return
                rel = item["path"]
                if rel in completed:
                    with db() as saved_connection:
                        saved=saved_connection.execute("SELECT result_json FROM results WHERE job_id=? AND image_path=?",(job_id,rel)).fetchone()
                    if saved: records.append(json.loads(saved[0]))
                    continue
                image = dataset_path(rel)
                record: dict[str, Any] = {"path": rel, "split": infer_split(rel), "size": item["size"], "labelPath": None, "labelStatus": "missing", "width": None, "height": None, "boxes": [], "warnings": [], "errors": [], "sha256": None, "phash": None, "brightness": None, "contrast": None, "sharpness": None}
                try:
                    if item["size"] == 0:
                        record["errors"].append("zero_byte_image")
                    h = hashlib.sha256()
                    with image.open("rb") as stream:
                        for block in iter(lambda: stream.read(1024 * 1024), b""):
                            h.update(block)
                    record["sha256"] = h.hexdigest()
                    with Image.open(image) as source:
                        source.verify()
                    with Image.open(image) as source:
                        record["width"], record["height"] = source.size
                        if not all(source.size): record["errors"].append("invalid_dimensions")
                        thumb = source.convert("RGB").resize((64, 64))
                        pixels = list(thumb.getdata())
                        gray = [0.299*r + 0.587*g + 0.114*b for r,g,b in pixels]
                        mean = sum(gray)/len(gray); variance = sum((value-mean)**2 for value in gray)/len(gray)
                        record["brightness"], record["contrast"] = mean/255, variance**0.5/255
                        record["sharpness"] = ImageStat.Stat(thumb.filter(ImageFilter.FIND_EDGES)).var[0]**0.5
                        lum = [int(value) for value in gray]
                        bits = 0
                        for y in range(8):
                            for x in range(8):
                                bits = (bits << 1) | int(lum[y*9+x] > lum[y*9+x+1])
                        record["phash"] = f"{bits:016x}"
                except Exception:
                    record["errors"].append("unreadable_image")
                label = label_for_image(rel, label_set)
                record["labelPath"] = label
                if label:
                    record["labelStatus"] = "empty" if not (dataset_path(label).exists() and dataset_path(label).stat().st_size) else "annotated"
                    try:
                        raw = dataset_path(label).read_text(encoding="utf-8-sig")
                        parsed=parse_label_text(raw)
                        record["boxes"]=parsed["boxes"]; record["errors"].extend(parsed["errors"]); record["labelStatus"]=parsed["status"]
                        for box in record["boxes"]:
                            if box["area"] < 0.0005: record["warnings"].append("tiny_box")
                            if box["area"] > 0.7: record["warnings"].append("large_box")
                            if min(box["cx"]-box["width"]/2,box["cy"]-box["height"]/2,1-box["cx"]-box["width"]/2,1-box["cy"]-box["height"]/2)<0.01: record["warnings"].append("box_near_edge")
                        if record["errors"]: record["labelStatus"]="invalid"
                    except OSError:
                        record["labelStatus"] = "invalid"; record["errors"].append("unreadable_label")
                else:
                    record["warnings"].append("missing_label")
                records.append(record)
                connection.execute("INSERT OR REPLACE INTO results(job_id,image_path,result_json) VALUES(?,?,?)", (job_id, rel, json.dumps(record)))
                if (index + 1) % 25 == 0 or index + 1 == total:
                    now = datetime.now(timezone.utc).isoformat()
                    connection.execute("UPDATE jobs SET processed=?,updated_at=? WHERE id=?", (index+1, now, job_id)); connection.commit()
        report = summarize_records(records, labels, fingerprint)
        with db() as connection:
            connection.execute("UPDATE jobs SET status='complete',processed=?,updated_at=?,report_json=? WHERE id=?", (len(records), datetime.now(timezone.utc).isoformat(), json.dumps(report), job_id))
    except Exception as error:
        with db() as connection:
            connection.execute("UPDATE jobs SET status='failed',updated_at=?,error=? WHERE id=? AND status='running'", (datetime.now(timezone.utc).isoformat(), str(error)[:500], job_id))
    finally:
        with _jobs_lock:
            _active_job = None


def summarize_records(records: list[dict], labels: list[str], fingerprint: str):
    by_hash: dict[str, list[dict]] = {}
    by_name: dict[str, list[str]] = {}
    phash_groups: dict[str, list[dict]] = {}
    for row in records:
        if row["sha256"]: by_hash.setdefault(row["sha256"], []).append(row)
        by_name.setdefault(Path(row["path"]).name.casefold(), []).append(row["path"])
        if row["phash"]: phash_groups.setdefault(row["phash"], []).append(row)
    exact = [group for group in by_hash.values() if len(group)>1]
    # Pair candidates via bounded Hamming buckets; each hash is compared once to prior hashes.
    near = []; comparisons = 0; candidate_count = 0; cross_split_near = 0; truncated = False
    hashes = list(phash_groups.items())
    for i, (hash_a, rows_a) in enumerate(hashes):
        for hash_b, rows_b in hashes[i+1:]:
            distance=(int(hash_a,16)^int(hash_b,16)).bit_count(); comparisons += 1
            if distance <= 5:
                candidate_count += 1
                cross_split = rows_a[0]["split"] != rows_b[0]["split"]
                cross_split_near += cross_split
                if len(near) < 10000:
                    near.append({"first":rows_a[0]["path"],"second":rows_b[0]["path"],"distance":distance,"crossSplit":cross_split})
            if comparisons >= 2_000_000: truncated=True; break
        if truncated: break
    errors = sum(bool(row["errors"]) for row in records)
    missing = sum(row["labelStatus"] == "missing" for row in records)
    boxes = [box for row in records for box in row["boxes"]]
    orphan_labels = sorted(set(labels)-{row["labelPath"] for row in records if row["labelPath"]})
    splits: dict[str, dict] = {}
    for row in records:
        split=splits.setdefault(row["split"],{"images":0,"annotated":0,"boxes":0}); split["images"]+=1; split["annotated"]+=bool(row["boxes"]); split["boxes"]+=len(row["boxes"])
    return {"metadata":{"generatedAt":datetime.now(timezone.utc).isoformat(),"datasetFingerprint":fingerprint,"className":"container","classId":0,"method":"static Pillow/hash analysis; no ML"},"summary":{"images":len(records),"readable":sum(not any(e=="unreadable_image" for e in r["errors"]) for r in records),"boxes":len(boxes),"invalidImages":errors,"missingLabels":missing,"emptyLabels":sum(r["labelStatus"]=="empty" for r in records),"orphanLabels":len(orphan_labels),"exactDuplicateGroups":len(exact),"nearDuplicatePairs":candidate_count,"crossSplitNearDuplicatePairs":cross_split_near,"nearDuplicateCandidatesStored":len(near),"nearDuplicateScanTruncated":truncated,"reviewImages":sum(bool(r["warnings"] or r["errors"]) for r in records)},"splits":splits,"boxArea":stats([b["area"] for b in boxes]),"boxAspect":stats([b["aspect"] for b in boxes]),"centerX":stats([b["cx"] for b in boxes]),"centerY":stats([b["cy"] for b in boxes]),"brightness":stats([r["brightness"] for r in records if r["brightness"] is not None]),"contrast":stats([r["contrast"] for r in records if r["contrast"] is not None]),"sharpnessProxy":stats([r["sharpness"] for r in records if r["sharpness"] is not None]),"exactDuplicates":[[r["path"] for r in group] for group in exact[:1000]],"nearDuplicates":near,"duplicateFilenameConflicts":[paths for paths in by_name.values() if len(paths)>1],"orphanLabels":orphan_labels[:500],"findings":[{"severity":"error","description":"Images have invalid or unreadable data/labels","count":errors},{"severity":"warning","description":"Images have no matching label; verify whether these are intentional negatives","count":missing},{"severity":"warning","description":"Near-duplicate image candidates; inspect for cross-split leakage","count":candidate_count},{"severity":"warning","description":"Near-duplicate candidates crossing train/validation/test splits","count":cross_split_near},{"severity":"info","description":"Orphan label files have no matching image","count":len(orphan_labels)}],"limitations":["Brightness, contrast, sharpness, and perceptual hashes are lightweight proxies, not semantic scene-diversity measurements.","Near-duplicate findings are review candidates and may include false positives.","Near-duplicate pair details are capped at 10,000; total counts may exceed stored details.","Missing labels are not assumed to be verified negative images."]}


def stats(values: list[float]):
    values=sorted(values)
    if not values:return {"count":0,"min":None,"median":None,"p90":None,"max":None,"mean":None}
    return {"count":len(values),"min":values[0],"median":values[len(values)//2],"p90":values[min(len(values)-1,int(len(values)*.9))],"max":values[-1],"mean":sum(values)/len(values)}


def start_audit(sample: int | None = None):
    global _active_job
    with _jobs_lock:
        if _active_job:
            raise HTTPException(409, "An audit is already running")
        root, images, labels, fingerprint = dataset_inventory()
        if sample:
            if sample < 1 or sample > 10000: raise HTTPException(400, "Sample size must be between 1 and 10000")
            stride=max(1,len(images)//sample); images=images[::stride][:sample]
        job_id=uuid.uuid4().hex
        now=datetime.now(timezone.utc).isoformat()
        with db() as connection:
            connection.execute("INSERT INTO jobs(id,status,total,created_at,updated_at,dataset_fingerprint) VALUES(?,?,?,?,?,?)",(job_id,"running",len(images),now,now,fingerprint))
            connection.executemany("INSERT INTO job_items(job_id,position,image_path,size,mtime) VALUES(?,?,?,?,?)",[(job_id,index,item["path"],item["size"],item["mtime"]) for index,item in enumerate(images)])
        _active_job=job_id
        thread=threading.Thread(target=audit_worker,args=(job_id,images,labels,fingerprint),daemon=True)
        thread.start()
    return {"id":job_id,"status":"running","total":len(images),"datasetFingerprint":fingerprint}


@app.post("/api/audits")
def create_audit(sample: int | None = None):
    return start_audit(sample)


@app.post("/api/audits/{job_id}/resume")
def resume_audit(job_id: str):
    global _active_job
    with _jobs_lock:
        if _active_job: raise HTTPException(409,"An audit is already running")
        with db() as connection:
            job=connection.execute("SELECT status,dataset_fingerprint FROM jobs WHERE id=?",(job_id,)).fetchone()
            if not job: raise HTTPException(404,"Audit job not found")
            if job["status"] not in {"failed","running"}: raise HTTPException(409,"Only interrupted or failed audits can resume")
            items=[{"path":row["image_path"],"size":row["size"],"mtime":row["mtime"]} for row in connection.execute("SELECT image_path,size,mtime FROM job_items WHERE job_id=? ORDER BY position",(job_id,))]
            labels=dataset_inventory()[2]
            connection.execute("UPDATE jobs SET status='running',error=NULL,updated_at=? WHERE id=?",(datetime.now(timezone.utc).isoformat(),job_id))
        _active_job=job_id
        threading.Thread(target=audit_worker,args=(job_id,items,labels,job["dataset_fingerprint"]),daemon=True).start()
    return {"id":job_id,"status":"running","total":len(items)}


@app.get("/api/audits/latest")
def latest_audit():
    with db() as connection: row=connection.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
    if not row: return {"status":"none"}
    result=dict(row)
    if result.get("report_json"): result["report"]=json.loads(result["report_json"])
    return result


@app.get("/api/audits/{job_id}")
def audit_status(job_id: str):
    with db() as connection: row=connection.execute("SELECT id,status,processed,total,created_at,updated_at,dataset_fingerprint,error,report_json FROM jobs WHERE id=?",(job_id,)).fetchone()
    if not row: raise HTTPException(404,"Audit job not found")
    result=dict(row)
    if result["report_json"]: result["report"]=json.loads(result.pop("report_json"))
    else: result.pop("report_json",None)
    return result


@app.get("/api/audits/{job_id}/report.html", response_class=HTMLResponse)
def audit_html_report(job_id: str):
    with db() as connection: row=connection.execute("SELECT report_json FROM jobs WHERE id=? AND status='complete'",(job_id,)).fetchone()
    if not row: raise HTTPException(404,"Completed report not found")
    report=json.loads(row["report_json"])
    summary=report["summary"]
    escaped=json.dumps(report,ensure_ascii=False,indent=2).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    return f"<!doctype html><meta charset='utf-8'><title>Dataset quality report</title><h1>Dataset quality report</h1><p>Generated {report['metadata']['generatedAt']} · fingerprint {report['metadata']['datasetFingerprint']}</p><h2>Overview</h2><p>{summary['images']} images · {summary['boxes']} boxes · {summary['invalidImages']} images with errors · {summary['missingLabels']} missing labels · {summary['nearDuplicatePairs']} near duplicate candidates</p><h2>Findings and measurements</h2><pre>{escaped}</pre>"


@app.get("/api/model/status")
def model_status():
    model_path=Path(os.getenv("YOLO26_MODEL_PATH","/models/yolo26n.pt"))
    return {"available":model_path.is_file(),"model":"yolo26n.pt","weightsConfigured":model_path.is_file(),"supportedClasses":"Pretrained COCO labels do not provide a dedicated shipping-container class; probe is informational only.","ultralyticsInstalled":False,"note":"Ultralytics is loaded only in an explicit future model job. No model weights are downloaded by the static audit."}


@app.post("/api/audits/{job_id}/cancel")
def cancel_audit(job_id: str):
    with _jobs_lock:
        with db() as connection:
            result=connection.execute("UPDATE jobs SET status='failed',updated_at=?,error='Audit interrupted; completed image results are preserved and can be resumed.' WHERE id=? AND status='running'",(datetime.now(timezone.utc).isoformat(),job_id))
            if not result.rowcount: raise HTTPException(409,"Audit is not running")
        global _active_job
        if _active_job==job_id: _active_job=None
    return {"id":job_id,"status":"interrupted","canResume":True}


@app.get("/api/audits/{job_id}/images")
def audit_images(job_id: str, page: int = 1, page_size: int = 24, filter: str = "all"):
    page=max(1,page); page_size=max(1,min(100,page_size))
    filters={"invalid":"json_extract(result_json,'$.errors') != '[]'","missing":"json_extract(result_json,'$.labelStatus')='missing'","empty":"json_extract(result_json,'$.labelStatus')='empty'","tiny":"json_extract(result_json,'$.warnings') LIKE '%tiny_box%'","large":"json_extract(result_json,'$.warnings') LIKE '%large_box%'","lowres":"CAST(json_extract(result_json,'$.width') AS INTEGER)*CAST(json_extract(result_json,'$.height') AS INTEGER)<307200","near":"image_path IN (SELECT json_extract(value,'$.first') FROM json_each((SELECT report_json FROM jobs WHERE id=?),'$.nearDuplicates') UNION SELECT json_extract(value,'$.second') FROM json_each((SELECT report_json FROM jobs WHERE id=?),'$.nearDuplicates'))","leakage":"image_path IN (SELECT json_extract(value,'$.first') FROM json_each((SELECT report_json FROM jobs WHERE id=?),'$.nearDuplicates') WHERE json_extract(value,'$.crossSplit')=1 UNION SELECT json_extract(value,'$.second') FROM json_each((SELECT report_json FROM jobs WHERE id=?),'$.nearDuplicates') WHERE json_extract(value,'$.crossSplit')=1)"}
    with db() as connection:
        job=connection.execute("SELECT status FROM jobs WHERE id=?",(job_id,)).fetchone()
        if not job: raise HTTPException(404,"Audit job not found")
        query="SELECT image_path,result_json FROM results WHERE job_id=?"; params=[job_id]
        if filter in filters:
            query+=f" AND {filters[filter]}"
            if filter in {"near", "leakage"}: params.extend([job_id,job_id])
        total=connection.execute(query.replace("SELECT image_path,result_json","SELECT COUNT(*)"),params).fetchone()[0]
        rows=connection.execute(query+" ORDER BY image_path LIMIT ? OFFSET ?",params+[page_size,(page-1)*page_size]).fetchall()
    return {"jobStatus":job["status"],"page":page,"pageSize":page_size,"total":total,"items":[json.loads(row["result_json"]) for row in rows]}


@app.get("/api/audits/{job_id}/image")
def audit_image_detail(job_id: str, path: str):
    target=dataset_path(path)
    with db() as connection: row=connection.execute("SELECT result_json FROM results WHERE job_id=? AND image_path=?",(job_id,path)).fetchone()
    if not row: raise HTTPException(404,"Image not found in this audit")
    return {"record":json.loads(row["result_json"]),"imageUrl":f"/api/dataset/file?path={path}"}


@app.get("/api/audits/{job_id}/images/{filter_name}")
def audit_filter_alias(job_id: str, filter_name: str, page: int = 1, page_size: int = 24):
    return audit_images(job_id,page,page_size,filter_name)


@app.get("/api/audits/{job_id}/export/{kind}")
def export_audit(job_id: str, kind: str):
    with db() as connection:
        job=connection.execute("SELECT report_json FROM jobs WHERE id=? AND status='complete'",(job_id,)).fetchone()
        rows=connection.execute("SELECT result_json FROM results WHERE job_id=? ORDER BY image_path",(job_id,)).fetchall()
    if not job: raise HTTPException(404,"Completed report not found")
    report=json.loads(job["report_json"]); records=[json.loads(row["result_json"]) for row in rows]
    if kind=="json": return JSONResponse(report)
    if kind in {"images.csv","boxes.csv"}:
        output=io.StringIO(); writer=csv.writer(output)
        if kind=="images.csv":
            writer.writerow(["path","split","label_status","width","height","object_count","errors","warnings","brightness","contrast","sharpness_proxy"])
            for r in records: writer.writerow([r["path"],r["split"],r["labelStatus"],r["width"],r["height"],len(r["boxes"]),";".join(r["errors"]),";".join(r["warnings"]),r["brightness"],r["contrast"],r["sharpness"]])
        else:
            writer.writerow(["image_path","split","cx","cy","width_normalized","height_normalized","area_fraction","aspect_ratio","width_pixels","height_pixels"])
            for r in records:
                for b in r["boxes"]: writer.writerow([r["path"],r["split"],b["cx"],b["cy"],b["width"],b["height"],b["area"],b["aspect"],b["width"]*r["width"],b["height"]*r["height"]])
        return StreamingResponse(iter([output.getvalue()]),media_type="text/csv",headers={"Content-Disposition":f"attachment; filename=dataset-{kind}"})
    raise HTTPException(404,"Unsupported export")


@app.get("/api/dataset/file")
def dataset_file(path: str):
    target = dataset_path(path)
    if not target.is_file():
        raise HTTPException(404, "Dataset file not found")
    return FileResponse(target, filename=target.name)


@app.get("/")
def home():
    return FileResponse(ROOT / "static" / "index.html")


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
