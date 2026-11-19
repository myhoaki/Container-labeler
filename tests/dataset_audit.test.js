const test = require('node:test');
const assert = require('node:assert/strict');
const audit = require('../audit_app/static/dataset_audit.js');

const box = { cx: 0.5, cy: 0.5, width: 0.4, height: 0.2 };

 test('YOLO labels validate the single container class and normalized box geometry', () => {
  const parsed = audit.parseYolo('0 0.5 0.5 0.4 0.2\n0 0.25 0.25 0.2 0.2');
  assert.equal(parsed.boxes.length, 2);
  assert.deepEqual(parsed.errors, []);
});

test('YOLO label parser reports malformed rows, unsupported classes, zero area, and out-of-bounds boxes', () => {
  const parsed = audit.parseYolo('one two\n1 0.5 0.5 0.2 0.2\n0 0.5 0.5 0 0.1\n0 0.95 0.5 0.2 0.2');
  assert.deepEqual(parsed.errors.map(error => error.type), [
    'malformed_label', 'unsupported_class_id', 'zero_area_box', 'invalid_coordinates'
  ]);
  assert.equal(parsed.boxes.length, 0);
});

test('split names are inferred from conventional directory components', () => {
  assert.equal(audit.inferSplit('images/train/cam1.jpg'), 'train');
  assert.equal(audit.inferSplit('labels/validation/cam1.txt'), 'validation');
  assert.equal(audit.inferSplit('port/cam1.jpg'), 'unspecified');
});

test('dataset summary counts annotation and object distributions from measured records', () => {
  const analyses = [
    { imagePath: 'train/a.jpg', split: 'train', width: 640, height: 480, labelExists: true, emptyLabel: true, boxes: [], errors: [] },
    { imagePath: 'train/b.jpg', split: 'train', width: 640, height: 480, labelExists: true, boxes: [box], errors: [] },
    { imagePath: 'val/c.jpg', split: 'validation', width: 1280, height: 720, labelExists: true, boxes: [box, box], errors: [] },
    { imagePath: 'val/d.jpg', split: 'validation', width: 1280, height: 720, labelExists: true, boxes: [box, box, box], errors: [] }
  ];
  const report = audit.buildReport(analyses, { exactGroups: [], exactPairs: 0, exactCrossSplitGroups: 0, nearGroups: [], nearPairs: 0, crossSplitNearPairs: 0, videoLikePairs: 0, nearEdges: [], scanTruncated: false }, []);
  assert.equal(report.summary.totalImages, 4);
  assert.equal(report.summary.readableImages, 4);
  assert.equal(report.summary.validAnnotatedImages, 3);
  assert.equal(report.summary.imagesWithNoAnnotations, 1);
  assert.equal(report.summary.totalBoxes, 6);
  assert.deepEqual(report.summary.objectsPerImage, { '0': 1, '1': 1, '2': 1, '3+': 1 });
  assert.equal(report.resolutions[0].resolution, '1280 × 720');
  assert.equal(report.distributions.bboxWidthPercent.median, 40);
  assert.equal(report.distributions.bboxWidthPixels.median, 512);
  assert.equal(report.summary.objectsPerImagePercentages['3+'], 25);
});

test('duplicate detector finds exact and perceptually close files across splits', () => {
  const duplicates = audit.findDuplicates([
    { imagePath: 'train/a_001.jpg', split: 'train', sha256: 'same', phash: '0000000000000000' },
    { imagePath: 'train/a_001-copy.jpg', split: 'train', sha256: 'same', phash: '0000000000000000' },
    { imagePath: 'val/a_002.jpg', split: 'validation', sha256: 'other', phash: '0000000000000001' }
  ]);
  assert.equal(duplicates.exactGroups.length, 1);
  assert.equal(duplicates.exactPairs, 1);
  assert.equal(duplicates.nearPairs, 2);
  assert.equal(duplicates.crossSplitNearPairs, 2);
  assert.equal(duplicates.videoLikePairs, 0);
  const adjacentFrames = audit.findDuplicates([
    { imagePath: 'train/frame_100.jpg', split: 'train', sha256: 'frame-a', phash: '0000000000000000' },
    { imagePath: 'train/frame_101.jpg', split: 'train', sha256: 'frame-b', phash: '0000000000000001' }
  ]);
  assert.equal(adjacentFrames.videoLikePairs, 1);
  assert.equal(duplicates.nearGroups.length, 1);
});

test('readiness requires train and validation splits and flags exact leakage as NOT READY', () => {
  const duplicateData = audit.findDuplicates([
    { imagePath: 'train/a.jpg', split: 'train', sha256: 'identical', phash: '0000000000000000' },
    { imagePath: 'val/a.jpg', split: 'validation', sha256: 'identical', phash: '0000000000000000' }
  ]);
  const analyses = [
    { imagePath: 'train/a.jpg', split: 'train', width: 640, height: 480, labelExists: true, boxes: [box], errors: [] },
    { imagePath: 'val/a.jpg', split: 'validation', width: 640, height: 480, labelExists: true, boxes: [box], errors: [] }
  ];
  const report = audit.buildReport(analyses, duplicateData, []);
  assert.equal(report.readiness.verdict, 'NOT READY');
  assert.match(report.readiness.reasons.join(' '), /exact duplicate/);
});

test('identical perceptual hashes from different bytes are retained as advisory near-duplicates', () => {
  const result = audit.findDuplicates([
    { imagePath: 'train/first.jpg', split: 'train', sha256: 'one', phash: 'abc0000000000000' },
    { imagePath: 'train/second.jpg', split: 'train', sha256: 'two', phash: 'abc0000000000000' }
  ]);
  assert.equal(result.nearPairs, 1);
  assert.equal(result.nearGroups[0].count, 2);
});

test('near-duplicate indexing finds hashes within five bit changes across separate bands', () => {
  const mask = (1n << 60n) | (1n << 48n) | (1n << 36n) | (1n << 24n) | (1n << 12n);
  const result = audit.findDuplicates([
    { imagePath: 'train/a.jpg', split: 'train', sha256: 'band-a', phash: '0000000000000000' },
    { imagePath: 'train/b.jpg', split: 'train', sha256: 'band-b', phash: mask.toString(16).padStart(16, '0') }
  ]);
  assert.equal(result.nearPairs, 1);
  assert.equal(result.nearEdges[0].hammingDistance, 5);
});

test('readiness reports READY WITH WARNINGS for usable splits with advisory near-duplicates', () => {
  const duplicateData = audit.findDuplicates([
    { imagePath: 'train/frame_1.jpg', split: 'train', sha256: 'frame1', phash: '0000000000000000' },
    { imagePath: 'val/frame_2.jpg', split: 'validation', sha256: 'frame2', phash: '0000000000000001' }
  ]);
  const report = audit.buildReport([
    { imagePath: 'train/frame_1.jpg', split: 'train', width: 640, height: 480, labelExists: true, boxes: [box], errors: [] },
    { imagePath: 'val/frame_2.jpg', split: 'validation', width: 640, height: 480, labelExists: true, boxes: [box], errors: [] }
  ], duplicateData, []);
  assert.equal(report.readiness.verdict, 'READY WITH WARNINGS');
  assert.ok(report.readiness.warnings.some(message => message.includes('near-duplicate')));
});

test('readiness is READY with a clean, conventionally split, varied-size sample', () => {
  const analyses = Array.from({ length: 51 }, (_, i) => ({
    imagePath: `${i < 30 ? 'train' : i < 50 ? 'val' : 'test'}/frame_${i}.jpg`,
    split: i < 30 ? 'train' : i < 50 ? 'validation' : 'test',
    width: 640 + (i % 3) * 320, height: 480 + (i % 2) * 240,
    labelExists: true, boxes: [{ cx: .5, cy: .5, width: .1 + (i % 4) * .2, height: .1 + (i % 3) * .2 }], errors: []
  }));
  const report = audit.buildReport(analyses, { exactGroups: [], exactPairs: 0, exactCrossSplitGroups: 0, nearGroups: [], nearPairs: 0, crossSplitNearPairs: 0, videoLikePairs: 0, nearEdges: [], scanTruncated: false }, []);
  assert.equal(report.readiness.verdict, 'READY');
});

test('readiness rejects a flat dataset without a detected validation split', () => {
  const analyses = Array.from({ length: 60 }, (_, i) => ({
    imagePath: `container_${i}.jpg`, split: 'unspecified', width: 1920, height: 1080,
    labelExists: true, boxes: [box], errors: []
  }));
  const report = audit.buildReport(analyses, { exactGroups: [], exactPairs: 0, exactCrossSplitGroups: 0, nearGroups: [], nearPairs: 0, crossSplitNearPairs: 0, videoLikePairs: 0, nearEdges: [], scanTruncated: false }, []);
  assert.equal(report.readiness.verdict, 'NOT READY');
  assert.match(report.readiness.reasons.join(' '), /validation split/);
});
