# Container Labeler

This project runs two independent Docker apps: a manual image labeler and a read-only dataset quality audit. Static audit jobs run incrementally on the server and persist results in SQLite. The labeler loads the configured server dataset automatically.

## Run with Docker Compose

On Ubuntu, install Docker Engine and the Compose plugin, copy this project to the server, then run:

```bash
cd container-labeler
docker compose up -d --build
curl http://127.0.0.1:8766/api/health
curl http://127.0.0.1:8767/api/health
```

After updating the audit source, use `docker compose up -d --build dataset-audit` to rebuild only the audit app. Audit reports and per-image review results persist under `data/audit_state/` and are separate from the dataset. To rebuild both apps, use `docker compose up -d --build`. The labeler is published on all server interfaces at port `8766`; open `http://<server-ip>:8766` in your browser. Ensure the server firewall allows inbound TCP port 8766. The audit app remains bound to server loopback at port 8767.

Stop with `docker compose down`; restart with `docker compose up -d`; update source and rebuild with `docker compose up -d --build`. SQLite, WAL files, text crop cache, and progress exports live under `./data/` or the dataset’s hidden `.container-labeler/` directory. Back up `data/` and the dataset directory while stopped, or download a per-session JSON progress export from the UI.

From your laptop or phone on the same reachable network, open **http://<server-ip>:8766**. The page loads the configured server dataset automatically; switch between Container Detection and Text Region Detection with the task selector. No SSH tunnel, PowerShell, or batch file is needed. The server’s firewall/network policy must allow inbound TCP 8766. To keep the audit app private to the server, use a tunnel only if you separately need its loopback-only port 8767.

The server dataset is extracted into `data/server_dataset/container_images_verified`; the source ZIP remains untouched. Labels in `labels/` are loaded and written there, and crops/rejections are saved in the extracted copy. Existing label contents are backed up in SQLite before server progress is committed. In Text Region Detection, the first visit generates a pixel crop for every saved container box and caches it under `.container-labeler/text-crops/`; subsequent visits reuse the crop files. Text labels and YOLO exports remain separate from Container Detection labels.

## Workflow and shortcuts

Drag on the image to draw one or more boxes around containers. Select and drag a box to move it; use the eight handles to resize. Use **Crop** to drag a crop rectangle, adjust it with its handles, then Apply Crop. Cropping creates an editable local working view; Reset Crop restores the source view. Crop coordinates are measured against the browser-decoded source dimensions, so zooming and panning do not affect them. Existing boxes are clipped and remapped into crop-relative YOLO coordinates, and Reset Crop restores their original coordinates. The crop draft is saved in browser storage and recovered with the same image metadata.

Verifying a crop writes a separate `*_cropped` image and matching `.txt` label beside the source. The source is never changed. JPG/JPEG crops are encoded at 95% quality; PNG and WebP retain their browser-supported formats. Other source formats are written as PNG. Browser decoding applies EXIF orientation before cropping; canvas output normalizes that orientation and does not retain EXIF or other source metadata. Existing derivative images and labels require explicit replacement confirmation. A cropped image's annotations use the saved derivative dimensions. Rejecting a crop affects/quarantines the derivative only and leaves the original image in place.

For uncropped images, verification retains the original behavior and writes the label beside the original.

Use the **Task** selector to switch between **Container Detection** and **Text Region Detection**. The text task uses its own SQLite session and annotation tables and the fixed class `0: container_number`. Text Region Detection uses the configured server dataset and its Container Detection session to build a crop for every saved container bounding box; it does not depend on a `_cropped` filename or show the uncropped image as the annotation view. Draw one box around each complete readable identification number within a container crop, annotations autosave as **Unreviewed**, then use **Verify** to confirm a readable number. Each box becomes a class-0 row in that image’s YOLO label; do not draw separate boxes around characters. Move, resize, delete, undo, and redo use the shared canvas controls. Use **Reject** for a crop with no readable number. The queue uses the same Unreviewed, Verified, and Rejected statuses as Container Detection.

Container Count Classification reuses the persistent per-box crops made from Container Detection bounding boxes under `.container-labeler/text-crops/`. These show the complete area inside each detection box, not the smaller text-number boxes. It does not change source images or annotations. The task shows a paginated thumbnail grid (25, 50, or 100 per page; 50 by default), per-image 1/2 choices, larger image inspection, and selection/bulk labeling. Selections stay checked when changing pages or filters. Bulk actions confirm before replacing existing labels. Filter Unlabeled to work through remaining crops. Progress totals show labeled/unlabeled counts and percentage. Labels are stored separately in the `container_counts` SQLite table by text crop session and stable virtual crop path. **Export labeled crops + CSV** downloads a ZIP containing labeled crop images and `labels.csv`, including image path, count, and labeled status; unlabeled crops are excluded.

Text-region labels are stored independently under `.container-labeler/text-region-labels/<session-id>/`, with a separate label per source image and container box. Existing Container Detection YOLO labels can supply container boxes when the SQLite box record is empty; this task reads but never modifies those labels. **Export YOLO** in the text task generates each verified pixel crop from the source image, then copies that crop into `images/` and writes one class-0 label row per number box under `labels/`; nested source paths are retained. Text task progress is loaded from the server automatically when the page opens.

Zoom with +/-; use the buttons, hold Space and drag to pan, and Fit to return to the image. V verifies and advances; R rejects and advances; arrows navigate; Delete removes the selected box; Ctrl+Z/Y undo/redo; F fits; Esc cancels crop mode or drawing.

Reject defaults to reversible quarantine. It copies the image and matching label under `_rejected/` preserving nested paths, verifies the image copy, then removes source files. Undo last reject restores it. Permanent deletion requires enabling the warned setting and confirming each action. The API only records the outcome after local operations succeed.

Use **Validate labels** to download a report for missing/empty labels and invalid YOLO coordinates. **Export YOLO** copies verified files into `images/` and `labels/` under a destination directory you select. Paths are retained, including nested paths and same-named images in separate folders.

## Separate dataset audit app

The audit container is named `dataset-audit`, uses host port `8767`, and mounts `data/server_dataset` read-only. Open its page and choose **Sample (250 images)** to validate the workflow or **Full dataset** for a complete scan. Jobs run in the server background; you can close the page and return to the saved latest report. Only one static audit runs at once. The sample mode is evenly spaced through the sorted image list.

The static analyzer uses Pillow for integrity/dimensions and lightweight brightness, contrast, sharpness, and 64-bit perceptual-hash proxies. It checks YOLO class-0 syntax/coordinates, matching and orphan labels, box sizes/positions, duplicate filenames, exact hashes, near-duplicate candidates, and split names. The SQLite report includes a dataset fingerprint and per-image status. A server restart marks interrupted scans resumable; completed image results are kept. The paged review opens original images with ground-truth boxes overlaid and has a separate cross-split near-duplicate filter. JSON, image CSV, box CSV, and HTML exports are available. The dataset mount remains read-only; suspicious files are never moved or edited.

Missing labels are warnings for human review, not verified negatives. Brightness/color/sharpness/hash findings are proxies, not semantic scene-diversity judgments. Near-duplicate candidates are heuristic; scans cap comparisons at two million pairs and mark when truncated. The dataset currently has a flat `images/` and `labels/` layout, so split balance and train/validation leakage checks are limited until files are organized under standard split folders.

The official Ultralytics identifier is `yolo26n.pt`, and the documented Python interface uses `YOLO("yolo26n.pt")` followed by `model.predict(...)`. The pretrained COCO detector has no dedicated shipping-container class, so its predictions cannot validate container recall or annotation correctness. No Ultralytics or Torch dependencies/weights are installed or downloaded by static analysis. Model training and GT comparison remain unavailable until a domain-specific trained checkpoint and a safe, isolated ML runtime are configured; the existing project has no training-job/pipeline conventions to reuse. The dashboard reports this explicitly instead of treating pretrained COCO predictions as container evidence. [Ultralytics YOLO26 docs](https://docs.ultralytics.com/models/yolo26), [predict API docs](https://docs.ultralytics.com/modes/predict).

## Direct Python run (manual labeler)

```bash
cd container-labeler
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
DATA_DIR=./data SERVER_DATASET_ROOT=./data/server_dataset/container_images_verified uvicorn app.main:app --host 127.0.0.1 --port 8765
```

## Browser and operational limits

The labeler reads and writes the configured server dataset, so use a browser that supports the image formats in your dataset. The labeler listens on server interfaces at port 8766; direct access also depends on the server firewall/network allowing inbound TCP 8766. The separate audit container mounts the server dataset read-only. TIFF and other formats depend on browser decoding support. SQLite records file size/mtime; if either changes on re-index, old boxes are cleared for review. Missing files are reported when loading. Output folders are excluded from source image discovery. The Rejected queue separately scans `_rejected/` so quarantined images remain viewable after reload.

Health endpoints: `GET http://<server-ip>:8766/api/health` (labeler) and `GET http://127.0.0.1:8767/api/health` (audit). Docker logs: `docker compose logs -f container-labeler` or `docker compose logs -f dataset-audit`.

## Tests

```bash
python3 tests/smoke.py
node tests/crop_geometry.test.js
node --test tests/text_region_annotation.test.js
node --test tests/dataset_audit.test.js
node --check app/static/app.js
node --check audit_app/static/dataset_audit.js
node --check audit_app/static/quality.js
```

## Casting holes labeling

Choose **Casting Holes Labeling** from the Task menu after the server Container Detection crops and Container Count Classification labels are available. The queue contains only existing crops with the saved ground-truth count of 2. Use **Auto Label** to place two editable boxes centered near the top and bottom edges; width and height default to 250 × 150 px and are clamped to each crop. The Initial zoom (%) setting defaults to 150%, centers the crop in the viewer, and can be adjusted from 50% to 500%. Draw, move, resize, or delete boxes, then choose **Save** to persist class-0 `casting_hole` YOLO coordinates, or **Skip** to advance without saving the current boxes. **Export YOLO labels + manifest (ZIP)** downloads normalized class-0 YOLO label files and `metadata.csv` for all eligible images, without duplicating images. The CSV maps each exported label to its existing crop path in the raw dataset; skipped and unreviewed images are listed but have no exported label. Annotations and status are kept in the separate `casting_hole_annotations` table in the persistent labeler database; source crops and existing classification/detection labels are left unchanged.
