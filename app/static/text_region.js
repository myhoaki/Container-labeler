(function (scope) {
  'use strict';
  function validBox(box) {
    return Array.isArray(box) && box.length === 4 && box.every(Number.isFinite) &&
      box[0] >= 0 && box[0] <= 1 && box[1] >= 0 && box[1] <= 1 &&
      box[2] > 0 && box[2] <= 1 && box[3] > 0 && box[3] <= 1 &&
      box[0] - box[2] / 2 >= 0 && box[1] - box[3] / 2 >= 0 &&
      box[0] + box[2] / 2 <= 1 && box[1] + box[3] / 2 <= 1;
  }
  function formatYoloBoxes(boxes) {
    if (!Array.isArray(boxes) || !boxes.length || boxes.some(box => !validBox(box))) {
      throw new Error('Expected at least one valid normalized YOLO box.');
    }
    return boxes.map(box => `0 ${box.map(value => value.toFixed(6)).join(' ')}`).join('\n') + '\n';
  }
  function formatYoloBox(box) {
    return formatYoloBoxes([box]);
  }
  function parseYolo(text) {
    const lines = String(text).trim().split(/\r?\n/).filter(Boolean);
    if (!lines.length) return [];
    return lines.map((line) => {
      const values = line.trim().split(/\s+/).map(Number);
      if (values.length !== 5 || values[0] !== 0 || !validBox(values.slice(1))) {
        throw new Error('Expected class-0 normalized YOLO boxes.');
      }
      return values.slice(1);
    });
  }
  function cropRect(box, imageWidth, imageHeight) {
    if (!Array.isArray(box) || box.length !== 4 || !box.every(Number.isFinite) ||
        !Number.isInteger(imageWidth) || !Number.isInteger(imageHeight) || imageWidth < 1 || imageHeight < 1) {
      throw new TypeError('Invalid container box or source image dimensions');
    }
    const [cx, cy, width, height] = box;
    const x = Math.max(0, Math.floor((cx - width / 2) * imageWidth));
    const y = Math.max(0, Math.floor((cy - height / 2) * imageHeight));
    const right = Math.min(imageWidth, Math.ceil((cx + width / 2) * imageWidth));
    const bottom = Math.min(imageHeight, Math.ceil((cy + height / 2) * imageHeight));
    if (right <= x || bottom <= y) throw new RangeError('Container box is outside the image');
    return { x, y, width: right - x, height: bottom - y };
  }
  const api = { validBox, formatYoloBox, formatYoloBoxes, parseYolo, cropRect };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else scope.TextRegion = api;
})(typeof window === 'undefined' ? globalThis : window);
