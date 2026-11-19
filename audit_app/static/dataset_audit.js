(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.DatasetAuditEngine = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const INVALID_ANNOTATION_TYPES = new Set(['unreadable_image', 'missing_label', 'malformed_label', 'unsupported_class_id', 'zero_area_box', 'invalid_coordinates']);
  const SPLIT_NAMES = new Map([
    ['train', 'train'], ['training', 'train'], ['val', 'validation'],
    ['valid', 'validation'], ['validation', 'validation'], ['test', 'test'], ['testing', 'test']
  ]);
  const SIZE_BUCKETS = [0, 0.01, 0.05, 0.15, 0.30, Infinity];
  const ASPECT_BUCKETS = [0, 0.5, 0.8, 1.2, 2, Infinity];

  function inferSplit(path) {
    for (const part of String(path).replace(/\\/g, '/').split('/')) {
      const split = SPLIT_NAMES.get(part.toLowerCase());
      if (split) return split;
    }
    return 'unspecified';
  }

  function parseYolo(text, path = '') {
    const result = { boxes: [], errors: [] };
    const lines = String(text ?? '').split(/\r?\n/);
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].trim();
      if (!line) continue;
      const fields = line.split(/\s+/);
      const lineNumber = i + 1;
      if (fields.length !== 5) {
        result.errors.push({ path, line: lineNumber, type: 'malformed_label', message: `Expected 5 YOLO values, found ${fields.length}.` });
        continue;
      }
      const values = fields.map(Number);
      if (values.some(value => !Number.isFinite(value))) {
        result.errors.push({ path, line: lineNumber, type: 'malformed_label', message: 'Label contains a non-numeric value.' });
        continue;
      }
      const [classId, cx, cy, width, height] = values;
      if (!Number.isInteger(classId) || classId !== 0) {
        result.errors.push({ path, line: lineNumber, type: 'unsupported_class_id', message: `Expected container class ID 0; found ${classId}.` });
      }
      if (width === 0 || height === 0) {
        result.errors.push({ path, line: lineNumber, type: 'zero_area_box', message: 'Bounding box width and height must be greater than zero.' });
        continue;
      }
      if (cx < 0 || cx > 1 || cy < 0 || cy > 1 || width < 0 || width > 1 || height < 0 || height > 1 || cx - width / 2 < 0 || cy - height / 2 < 0 || cx + width / 2 > 1 || cy + height / 2 > 1) {
        result.errors.push({ path, line: lineNumber, type: 'invalid_coordinates', message: 'YOLO center, size, and box edges must stay within normalized image bounds [0, 1].' });
        continue;
      }
      if (classId === 0) result.boxes.push({ cx, cy, width, height });
    }
    return result;
  }

  function quantile(sorted, q) {
    if (!sorted.length) return null;
    const index = (sorted.length - 1) * q;
    const lo = Math.floor(index), hi = Math.ceil(index);
    return sorted[lo] + (sorted[hi] - sorted[lo]) * (index - lo);
  }

  function distribution(values) {
    const sorted = values.filter(Number.isFinite).sort((a, b) => a - b);
    if (!sorted.length) return { count: 0, min: null, p25: null, median: null, p75: null, p90: null, max: null, mean: null };
    return {
      count: sorted.length, min: sorted[0], p25: quantile(sorted, 0.25), median: quantile(sorted, 0.5),
      p75: quantile(sorted, 0.75), p90: quantile(sorted, 0.9), max: sorted[sorted.length - 1],
      mean: sorted.reduce((sum, value) => sum + value, 0) / sorted.length
    };
  }

  function histogram(values, boundaries, labels) {
    const bins = labels.map(label => ({ label, count: 0 }));
    for (const value of values) {
      if (!Number.isFinite(value)) continue;
      for (let i = 0; i < boundaries.length - 1; i++) {
        if (value >= boundaries[i] && value < boundaries[i + 1]) { bins[i].count++; break; }
      }
    }
    return bins;
  }

  function hamming64(a, b) {
    let value = BigInt(`0x${a}`) ^ BigInt(`0x${b}`), count = 0;
    while (value) { value &= value - 1n; count++; }
    return count;
  }

  function findDuplicates(samples, options = {}) {
    const threshold = options.threshold ?? 5;
    const maxCandidates = options.maxCandidates ?? 250000;
    const exactByHash = new Map();
    for (const sample of samples) {
      if (!sample.sha256) continue;
      if (!exactByHash.has(sample.sha256)) exactByHash.set(sample.sha256, []);
      exactByHash.get(sample.sha256).push(sample);
    }
    const exactGroups = [...exactByHash.values()].filter(group => group.length > 1).map(group => ({
      paths: group.map(item => item.imagePath), sourcePaths: group.map(item => item.sourcePath || item.imagePath), splits: [...new Set(group.map(item => item.split))], count: group.length
    }));
    const exactPairs = exactGroups.reduce((sum, group) => sum + group.count * (group.count - 1) / 2, 0);
    const shaGroups = [...exactByHash.values()].map(items => ({ sample: items[0], items }));
    const perceptual = new Map();
    for (const item of shaGroups) {
      if (!item.sample.phash) continue;
      if (!perceptual.has(item.sample.phash)) perceptual.set(item.sample.phash, []);
      perceptual.get(item.sample.phash).push(item);
    }
    const hashGroups = [...perceptual.entries()].map(([hash, groups]) => ({ hash, groups }));
    const candidatePairs = new Set();
    let scanTruncated = false;
    const bandCount = Math.min(16, Math.max(1, threshold + 1));
    for (let band = 0; band < bandCount && !scanTruncated; band++) {
      const buckets = new Map();
      const from = Math.floor(band * 16 / bandCount), to = Math.floor((band + 1) * 16 / bandCount);
      for (let i = 0; i < hashGroups.length; i++) {
        const key = hashGroups[i].hash.slice(from, to);
        if (!buckets.has(key)) buckets.set(key, []);
        buckets.get(key).push(i);
      }
      for (const bucket of buckets.values()) {
        for (let i = 0; i < bucket.length && !scanTruncated; i++) {
          for (let j = i + 1; j < bucket.length; j++) {
            if (candidatePairs.size >= maxCandidates) { scanTruncated = true; break; }
            candidatePairs.add(`${bucket[i]},${bucket[j]}`);
          }
        }
        if (scanTruncated) break;
      }
    }

    const union = new Map();
    const find = x => { if (!union.has(x)) union.set(x, x); if (union.get(x) !== x) union.set(x, find(union.get(x))); return union.get(x); };
    const join = (a, b) => { const x = find(a), y = find(b); if (x !== y) union.set(y, x); };
    const nearEdges = [];
    let nearPairs = 0, crossSplitNearPairs = 0, videoLikePairs = 0, nearEdgesTruncated = false, candidatesChecked = 0;
    const splitCounts = items => { const counts = new Map(); for (const item of items) if (item.split !== 'unspecified') counts.set(item.split, (counts.get(item.split) || 0) + 1); return counts; };
    const crossSplits = (left, right) => { const a = splitCounts(left), b = splitCounts(right); let count = 0; for (const [name, n] of a) for (const [other, m] of b) if (name !== other) count += n * m; return count; };
    const withinCrossSplits = items => { const counts = splitCounts(items), values = [...counts.values()]; let total = 0; for (let i = 0; i < values.length; i++) for (let j = i + 1; j < values.length; j++) total += values[i] * values[j]; return total; };
    const videoPairs = (left, right, sameGroup = false) => {
      const index = items => { const parents = new Map(); for (const item of items) { const path = String(item.imagePath).replace(/\\/g, '/'), match = path.match(/(\d+)(?=\.[^.]+$)/); if (!match) continue; const parent = path.replace(/\/[^/]*$/, ''), id = Number(match[1]); if (!parents.has(parent)) parents.set(parent, new Map()); const ids = parents.get(parent); if (!ids.has(id)) ids.set(id, new Map()); const shas = ids.get(id); const sha = item.sha256 || item.imagePath; shas.set(sha, (shas.get(sha) || 0) + 1); } return parents; };
      const a = index(left), b = sameGroup ? a : index(right); let total = 0;
      for (const [parent, idsA] of a) { const idsB = b.get(parent); if (!idsB) continue; for (const [idA, shasA] of idsA) for (let delta = 1; delta <= 5; delta++) { const shasB = idsB.get(idA + delta); if (!shasB) continue; let countA = 0, countB = 0, overlap = 0; for (const n of shasA.values()) countA += n; for (const n of shasB.values()) countB += n; for (const [sha, n] of shasA) overlap += n * (shasB.get(sha) || 0); total += countA * countB - overlap; } }
      return total;
    };
    const noteNearEdge = (left, right, distance, sameGroup = false) => {
      const leftItems = left.flatMap(group => group.items), rightItems = sameGroup ? leftItems : right.flatMap(group => group.items);
      let pairCount, crossCount, videoCount;
      if (sameGroup) {
        pairCount = leftItems.length * (leftItems.length - 1) / 2 - left.reduce((sum, group) => sum + group.items.length * (group.items.length - 1) / 2, 0);
        crossCount = withinCrossSplits(leftItems) - left.reduce((sum, group) => sum + withinCrossSplits(group.items), 0);
        videoCount = videoPairs(leftItems, [], true) - left.reduce((sum, group) => sum + videoPairs(group.items, [], true), 0);
      } else {
        pairCount = leftItems.length * rightItems.length;
        crossCount = crossSplits(leftItems, rightItems);
        videoCount = videoPairs(leftItems, rightItems);
      }
      nearPairs += pairCount; crossSplitNearPairs += crossCount; videoLikePairs += videoCount;
      const first = leftItems[0], second = sameGroup ? leftItems.find(item => item.sha256 !== first.sha256) || leftItems[1] : rightItems[0];
      if (first && second && nearEdges.length < (options.maxEdges ?? 1000)) {
        nearEdges.push({ first: first.imagePath, second: second.imagePath, firstSourcePath: first.sourcePath || first.imagePath, secondSourcePath: second.sourcePath || second.imagePath, hammingDistance: distance,
          splits: [...new Set([...leftItems, ...rightItems].map(item => item.split))], crossSplit: crossCount > 0,
          videoLike: videoCount > 0, pairCount });
      } else if (first && second) nearEdgesTruncated = true;
    };
    const intrinsicNearGroups = new Set();
    for (let i = 0; i < hashGroups.length; i++) {
      const group = hashGroups[i];
      if (group.groups.length < 2) continue;
      intrinsicNearGroups.add(i);
      union.set(i, i);
      noteNearEdge(group.groups, [], 0, true);
    }
    for (const pair of candidatePairs) {
      candidatesChecked++;
      const [ai, bi] = pair.split(',').map(Number), a = hashGroups[ai], b = hashGroups[bi];
      const distance = hamming64(a.hash, b.hash);
      if (distance > threshold) continue;
      join(ai, bi);
      noteNearEdge(a.groups, b.groups, distance);
    }
    const components = new Map();
    for (const index of new Set([...union.keys(), ...intrinsicNearGroups])) {
      const key = find(index);
      if (!components.has(key)) components.set(key, []);
      components.get(key).push(index);
    }
    const nearGroups = [...components.values()].map(group => group.flatMap(index => hashGroups[index].groups.flatMap(item => item.items))).filter(items => items.length > 1).map(items => ({
      paths: [...new Set(items.map(item => item.imagePath))], splits: [...new Set(items.map(item => item.split))], count: items.length
    }));
    const exactCrossSplitGroups = exactGroups.filter(group => group.splits.filter(split => split !== 'unspecified').length > 1).length;
    return { exactGroups, exactPairs, exactCrossSplitGroups, nearGroups, nearPairs, crossSplitNearPairs, videoLikePairs,
      nearEdges, candidatesChecked, scanTruncated, nearEdgesTruncated, threshold };
  }

  function assessReadiness(report) {
    const reasons = [], warnings = [];
    const s = report.summary, splits = report.splits;
    if (!s.totalImages) reasons.push('No verified images were found to audit.');
    if (!splits.train?.images) reasons.push('No training split was detected. Use a path component named train or training.');
    if (!splits.validation?.images) reasons.push('No validation split was detected. Use val, valid, or validation in the image path.');
    if (!s.validAnnotatedImages) reasons.push('No image with a valid container annotation was found.');
    const criticalTypes = ['unreadable_image', 'missing_label', 'malformed_label', 'unsupported_class_id', 'zero_area_box', 'invalid_coordinates'];
    for (const type of criticalTypes) {
      const count = s.annotationErrors.byType[type] || 0;
      if (count) reasons.push(`${count} ${type.replaceAll('_', ' ')} issue(s) must be fixed before training.`);
    }
    if (report.duplicates.exactCrossSplitGroups) reasons.push(`${report.duplicates.exactCrossSplitGroups} exact duplicate group(s) cross split boundaries, creating data leakage.`);
    if (reasons.length) return { verdict: 'NOT READY', reasons, warnings };
    if (report.duplicates.crossSplitNearPairs) warnings.push(`${report.duplicates.crossSplitNearPairs} near-duplicate candidate pair(s) cross split boundaries; inspect them for possible video-frame leakage.`);
    if (report.duplicates.nearPairs) warnings.push(`${report.duplicates.nearPairs} near-duplicate pair candidate(s) were found. Treat these as advisory and review before cleanup.`);
    if (report.duplicates.exactPairs) warnings.push(`${report.duplicates.exactPairs} exact duplicate pair(s) were found within the dataset.`);
    if (s.validAnnotatedImages < 50) warnings.push(`Only ${s.validAnnotatedImages} clean annotated image(s) are available. This can support a smoke test, but coverage may be too narrow for dependable results.`);
    if (report.duplicates.scanTruncated) warnings.push('Near-duplicate comparison reached its safety limit; candidate scan is incomplete.');
    if (report.duplicates.nearEdgesTruncated) warnings.push('The detailed near-duplicate sample list is capped; aggregate candidate counts were still calculated.');
    if (report.auditWarnings?.length) warnings.push(`${report.auditWarnings.length} image(s) could not be byte-hashed; exact duplicate detection is incomplete.`);
    if (report.orphanLabels.length) warnings.push(`${report.orphanLabels.length} label file(s) do not match an audited verified image.`);
    if (!(splits.test?.images)) warnings.push('No test split was detected. Keep a held-out test set for final evaluation.');
    if ((s.imagesWithNoAnnotations / Math.max(1, s.totalImages)) > 0.5) warnings.push('More than half of audited images have no valid boxes. Confirm these are intentional negative examples.');
    if (s.totalBoxes && report.coverage.objectSizeBins.filter(count => count > 0).length < 2) warnings.push('Measured boxes occupy only one area-size band; review whether the dataset covers different apparent container sizes and distances.');
    if (s.totalBoxes && report.coverage.objectAspectBins.filter(count => count > 0).length < 2) warnings.push('Measured boxes occupy only one aspect-ratio band; inspect for limited viewing-angle or annotation variation.');
    if (report.coverage.uniqueResolutions === 1) warnings.push('Only one image resolution is present; confirm it represents the expected camera sources.')
    return { verdict: warnings.length ? 'READY WITH WARNINGS' : 'READY', reasons: [], warnings };
  }

  function buildReport(analyses, duplicateData, orphanLabels = [], generatedAt = new Date().toISOString()) {
    const errors = analyses.flatMap(image => image.errors || []), annotationErrors = errors.filter(error => error.type !== 'hash_unavailable'), auditWarnings = errors.filter(error => error.type === 'hash_unavailable'), byType = {};
    for (const error of annotationErrors) byType[error.type] = (byType[error.type] || 0) + 1;
    const readable = analyses.filter(item => item.width && item.height);
    const allBoxes = analyses.flatMap(item => item.boxes || []);
    const boxWidths = [], boxHeights = [], boxAreas = [], boxAspects = [];
    const resolutionCounts = new Map(), splitCounts = {};
    const objectCounts = { '0': 0, '1': 0, '2': 0, '3+': 0 };
    let validAnnotatedImages = 0, imagesWithNoAnnotations = 0;
    const boxWidthsPixels = [], boxHeightsPixels = [], boxAreasPixels = [], boxCentersX = [], boxCentersY = [], brightnessValues = [], contrastValues = [], sharpnessValues = [];
    const sizeBinCounts = [0, 0, 0, 0, 0], aspectBinCounts = [0, 0, 0, 0, 0];
    const objectsPerImage = [];
    for (const image of analyses) {
      const split = image.split || 'unspecified';
      if (!splitCounts[split]) splitCounts[split] = { images: 0, readable: 0, annotated: 0, boxes: 0 };
      splitCounts[split].images++;
      if (image.width && image.height) {
        splitCounts[split].readable++;
        const key = `${image.width} × ${image.height}`;
        resolutionCounts.set(key, (resolutionCounts.get(key) || 0) + 1);
      }
      const count = (image.boxes || []).length;
      const objectBin = count >= 3 ? '3+' : String(count);
      objectCounts[objectBin]++;
      objectsPerImage.push(count);
      if (image.emptyLabel) imagesWithNoAnnotations++;
      if (count > 0 && !(image.errors || []).some(error => INVALID_ANNOTATION_TYPES.has(error.type))) { validAnnotatedImages++; splitCounts[split].annotated++; }
      splitCounts[split].boxes += count;
      for (const box of image.boxes || []) {
        const area = box.width * box.height, aspect = box.width / box.height;
        boxCentersX.push(box.cx); boxCentersY.push(box.cy);
        boxWidths.push(box.width * 100); boxHeights.push(box.height * 100); boxAreas.push(area * 100); boxAspects.push(aspect);
        if (image.width && image.height) { boxWidthsPixels.push(box.width * image.width); boxHeightsPixels.push(box.height * image.height); boxAreasPixels.push(area * image.width * image.height); }
        const sizeIndex = SIZE_BUCKETS.findIndex((edge, i) => i < SIZE_BUCKETS.length - 1 && area >= edge && area < SIZE_BUCKETS[i + 1]);
        if (sizeIndex >= 0) sizeBinCounts[sizeIndex]++;
        const aspectIndex = ASPECT_BUCKETS.findIndex((edge, i) => i < ASPECT_BUCKETS.length - 1 && aspect >= edge && aspect < ASPECT_BUCKETS[i + 1]);
        if (aspectIndex >= 0) aspectBinCounts[aspectIndex]++;
      }
    }
    const splitList = Object.entries(splitCounts).sort(([a], [b]) => a.localeCompare(b)).map(([name, counts]) => ({ name, ...counts }));
    const report = {
      metadata: { generatedAt, className: 'container', classId: 0, source: 'server dataset images and YOLO labels', visualModelUsed: false },
      summary: {
        totalImages: analyses.length, readableImages: readable.length, validAnnotatedImages, imagesWithNoAnnotations,
        totalBoxes: allBoxes.length, objectsPerImage: objectCounts,
        objectsPerImagePercentages: Object.fromEntries(Object.entries(objectCounts).map(([key, count]) => [key, analyses.length ? count / analyses.length * 100 : 0])),
        annotationErrors: { total: annotationErrors.length, byType },
        duplicateCandidates: duplicateData.exactGroups.length + duplicateData.nearGroups.length
      },
      distributions: {
        bboxWidthPixels: distribution(boxWidthsPixels), bboxHeightPixels: distribution(boxHeightsPixels), bboxAreaPixels: distribution(boxAreasPixels),
        bboxWidthPercent: distribution(boxWidths), bboxHeightPercent: distribution(boxHeights),
        bboxAreaPercent: distribution(boxAreas), bboxAspectRatio: distribution(boxAspects),
        imageWidthPixels: distribution(readable.map(image => image.width)), imageHeightPixels: distribution(readable.map(image => image.height)),
        imageAspectRatio: distribution(readable.map(image => image.width / image.height)),
        brightness: distribution(brightnessValues), contrast: distribution(contrastValues), sharpnessProxy: distribution(sharpnessValues),
        bboxCenterX: distribution(boxCentersX), bboxCenterY: distribution(boxCentersY),
        objectsPerImage: distribution(objectsPerImage),
        objectAreaHistogram: histogram(boxAreas.map(value => value / 100), SIZE_BUCKETS, ['<1%', '1–5%', '5–15%', '15–30%', '≥30%']),
        bboxAspectHistogram: histogram(boxAspects, ASPECT_BUCKETS, ['<0.5', '0.5–0.8', '0.8–1.2', '1.2–2', '≥2']),
        objectsPerImageHistogram: ['0', '1', '2', '3+'].map(label => ({ label, count: objectCounts[label] }))
      },
      resolutions: [...resolutionCounts].map(([resolution, count]) => ({ resolution, count, percentage: analyses.length ? count / analyses.length * 100 : 0 })).sort((a, b) => b.count - a.count || a.resolution.localeCompare(b.resolution)),
      splits: Object.fromEntries(splitList.map(item => [item.name, { images: item.images, readable: item.readable, annotated: item.annotated, boxes: item.boxes }])),
      annotationErrors,
      auditWarnings,
      duplicates: duplicateData,
      orphanLabels,
      suspiciousSamples: [...new Map([
        ...errors.map(error => [error.path, { path: error.path, sourcePath: error.sourcePath || error.imagePath || error.path, reasons: [error.type] }]),
        ...duplicateData.nearEdges.flatMap(edge => [[edge.first, { path: edge.first, sourcePath: edge.firstSourcePath || edge.first, reasons: ['near duplicate candidate'] }], [edge.second, { path: edge.second, sourcePath: edge.secondSourcePath || edge.second, reasons: ['near duplicate candidate'] }]]),
        ...duplicateData.exactGroups.flatMap(group => group.paths.map((path, index) => [path, { path, sourcePath: group.sourcePaths?.[index] || path, reasons: ['exact duplicate'] }]))
      ]).values()].slice(0, 500),
      coverage: {
        measuredOnly: true,
        note: 'No vision model was used. Camera angle, lighting, occlusion, color, and background clutter were not visually classified.',
        objectSizeBins: sizeBinCounts,
        objectAspectBins: aspectBinCounts,
        uniqueResolutions: resolutionCounts.size, brightnessProxy: distribution(brightnessValues), contrastProxy: distribution(contrastValues), sharpnessProxy: distribution(sharpnessValues),
        boxWidthPercent: distribution(boxWidths), boxHeightPercent: distribution(boxHeights), boxAreaPercent: distribution(boxAreas),
        boxWidthPixels: distribution(boxWidthsPixels), boxHeightPixels: distribution(boxHeightsPixels), boxAreaPixels: distribution(boxAreasPixels)
      }
    };
    report.readiness = assessReadiness(report);
    return report;
  }

  function bytesToHex(buffer) { return [...new Uint8Array(buffer)].map(value => value.toString(16).padStart(2, '0')).join(''); }
  async function digest(file) {
    if (!globalThis.crypto?.subtle) throw new Error('Web Crypto is unavailable in this browser context.');
    return bytesToHex(await globalThis.crypto.subtle.digest('SHA-256', await file.arrayBuffer()));
  }

  async function run(items, loadItem, onProgress = () => {}, options = {}) {
    const analyses = [], duplicateSamples = [], usedLabels = new Set(), allLabelPaths = new Set(options.labelPaths || []);
    for (let i = 0; i < items.length; i++) {
      const item = items[i], analysis = { imagePath: item.imagePath, sourcePath: item.sourcePath || item.imagePath, split: item.split || inferSplit(item.imagePath), width: null, height: null, boxes: [], errors: [], labelExists: false };
      try {
        const loaded = await loadItem(item);
        const file = loaded.imageFile;
        if (!file) throw new Error('Image file could not be opened.');
        analysis.size = file.size;
        if (!file.size) analysis.errors.push({ path: item.imagePath, imagePath: item.imagePath, sourcePath: item.sourcePath || item.imagePath, type: 'zero_byte_image', message: 'Image file is empty.' });
        try { analysis.sha256 = await digest(file); }
        catch (error) { analysis.errors.push({ path: item.imagePath, imagePath: item.imagePath, sourcePath: item.sourcePath || item.imagePath, type: 'hash_unavailable', message: error.message }); }
        try {
          if (typeof createImageBitmap !== 'function') throw new Error('Image decoding is unavailable in this browser.');
          const bitmap = await createImageBitmap(file);
          analysis.width = bitmap.width; analysis.height = bitmap.height;
          try { const visual = await measureBitmap(bitmap); analysis.phash = visual.phash; analysis.brightness = visual.brightness; analysis.contrast = visual.contrast; analysis.sharpness = visual.sharpness; brightnessValues.push(visual.brightness); contrastValues.push(visual.contrast); sharpnessValues.push(visual.sharpness); }
          finally { bitmap.close(); }
        } catch (error) { analysis.errors.push({ path: item.imagePath, imagePath: item.imagePath, sourcePath: item.sourcePath || item.imagePath, type: 'unreadable_image', message: error.message }); }
        const labelFile = loaded.labelFile;
        if (!labelFile) analysis.errors.push({ path: item.imagePath, imagePath: item.imagePath, sourcePath: item.sourcePath || item.imagePath, type: 'missing_label', message: 'No matching YOLO .txt label file was found.' });
        else {
          analysis.labelExists = true;
          analysis.labelPath = loaded.labelPath;
          usedLabels.add(loaded.labelPath);
          try {
            const labelText = await labelFile.text();
            analysis.emptyLabel = !labelText.trim();
            const parsed = parseYolo(labelText, loaded.labelPath);
            analysis.boxes = parsed.boxes;
            analysis.errors.push(...parsed.errors.map(error => ({ ...error, imagePath: item.imagePath, sourcePath: item.sourcePath || item.imagePath })));
          } catch (error) { analysis.errors.push({ path: loaded.labelPath, imagePath: item.imagePath, sourcePath: item.sourcePath || item.imagePath, type: 'malformed_label', message: `Could not read label: ${error.message}` }); }
        }
        if (analysis.sha256) duplicateSamples.push({ imagePath: item.imagePath, sourcePath: item.sourcePath || item.imagePath, split: analysis.split, sha256: analysis.sha256, phash: analysis.phash });
      } catch (error) {
        analysis.errors.push({ path: item.imagePath, imagePath: item.imagePath, sourcePath: item.sourcePath || item.imagePath, type: 'unreadable_image', message: error.message });
      }
      analyses.push(analysis);
      onProgress({ processed: i + 1, total: items.length, path: item.imagePath });
      if ((i + 1) % (options.yieldEvery || 8) === 0) await new Promise(resolve => setTimeout(resolve, 0));
    }
    const duplicates = findDuplicates(duplicateSamples, options.duplicates);
    const orphanLabels = [...allLabelPaths].filter(path => !usedLabels.has(path));
    const report = buildReport(analyses, duplicates, orphanLabels);
    return report;
  }

  async function measureBitmap(bitmap) {
    const canvas = typeof OffscreenCanvas === 'function' ? new OffscreenCanvas(9, 8) : Object.assign(document.createElement('canvas'), { width: 9, height: 8 });
    const context = canvas.getContext('2d', { willReadFrequently: true });
    context.drawImage(bitmap, 0, 0, 9, 8);
    const pixels = context.getImageData(0, 0, 9, 8).data;
    let bits = '';
    for (let y = 0; y < 8; y++) for (let x = 0; x < 8; x++) {
      const left = ((y * 9 + x) * 4), right = ((y * 9 + x + 1) * 4);
      const lumL = 0.299 * pixels[left] + 0.587 * pixels[left + 1] + 0.114 * pixels[left + 2];
      const lumR = 0.299 * pixels[right] + 0.587 * pixels[right + 1] + 0.114 * pixels[right + 2];
      bits += lumL > lumR ? '1' : '0';
    }
    const gray = []; let sum = 0, square = 0;
    for (let i = 0; i < pixels.length; i += 4) { const value = 0.299*pixels[i] + 0.587*pixels[i+1] + 0.114*pixels[i+2]; gray.push(value); sum += value; square += value*value; }
    const mean = sum / gray.length, variance = Math.max(0, square / gray.length - mean * mean); let laplace = 0, laplace2 = 0, count = 0;
    for (let y = 1; y < 7; y++) for (let x = 1; x < 7; x++) { const i = y*9+x, value = 4*gray[i]-gray[i-1]-gray[i+1]-gray[i-9]-gray[i+9]; laplace += value; laplace2 += value*value; count++; }
    const lapMean = laplace/count, sharpness = Math.max(0, laplace2/count-lapMean*lapMean);
    return { phash: BigInt(`0b${bits}`).toString(16).padStart(16, '0'), brightness: mean/255, contrast: Math.sqrt(variance)/255, sharpness };
  }

  return { inferSplit, parseYolo, distribution, findDuplicates, assessReadiness, buildReport, run };
});
