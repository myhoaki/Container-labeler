const test = require('node:test');
const assert = require('node:assert/strict');
const TextRegion = require('../app/static/text_region.js');

test('formats one complete container number as class-zero normalized YOLO', () => {
  const line = TextRegion.formatYoloBox([0.5, 0.48, 0.6, 0.16]);
  assert.equal(line, '0 0.500000 0.480000 0.600000 0.160000\n');
  assert.deepEqual(TextRegion.parseYolo(line), [[0.5, 0.48, 0.6, 0.16]]);
});

test('formats multiple complete number regions as separate class-zero YOLO rows', () => {
  const boxes = [[0.4, 0.5, 0.3, 0.1], [0.75, 0.6, 0.2, 0.08]];
  const text = TextRegion.formatYoloBoxes(boxes);
  assert.equal(text, '0 0.400000 0.500000 0.300000 0.100000\n0 0.750000 0.600000 0.200000 0.080000\n');
  assert.deepEqual(TextRegion.parseYolo(text), boxes);
});

test('rejects out-of-bounds boxes and other classes', () => {
  assert.throws(() => TextRegion.formatYoloBox([0.05, 0.5, 0.2, 0.2]));
  assert.throws(() => TextRegion.parseYolo('1 0.5 0.5 0.2 0.2\n'));
});

test('converts normalized container boxes to pixel crops clipped to image edges', () => {
  assert.deepEqual(TextRegion.cropRect([0.5, 0.5, 0.5, 0.25], 1000, 800), {
    x: 250, y: 300, width: 500, height: 200,
  });
  assert.deepEqual(TextRegion.cropRect([0.05, 0.1, 0.2, 0.2], 100, 100), {
    x: 0, y: 0, width: 16, height: 20,
  });
});
