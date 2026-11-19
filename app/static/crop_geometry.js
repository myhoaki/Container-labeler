((root) => {
  const point = (p, view) => ({ x: (p.x - view.x) / view.scale, y: (p.y - view.y) / view.scale });
  function rectFromDrag(start, end, view, width, height) {
    if (!Number.isFinite(view.scale) || view.scale <= 0) throw new Error('Invalid view scale');
    const a = point(start, view), b = point(end, view);
    const ax = Math.max(0, Math.min(width, a.x)), ay = Math.max(0, Math.min(height, a.y));
    const bx = Math.max(0, Math.min(width, b.x)), by = Math.max(0, Math.min(height, b.y));
    const x = Math.min(ax, bx), y = Math.min(ay, by);
    return { x, y, width: Math.abs(bx - ax), height: Math.abs(by - ay) };
  }
  function remapBoxes(boxes, sourceWidth, sourceHeight, crop) {
    const right = crop.x + crop.width, bottom = crop.y + crop.height;
    return boxes.flatMap(([cx, cy, nw, nh]) => {
      const x = cx * sourceWidth, y = cy * sourceHeight;
      const w = nw * sourceWidth, h = nh * sourceHeight;
      const left = Math.max(crop.x, x - w / 2), top = Math.max(crop.y, y - h / 2);
      const clippedRight = Math.min(right, x + w / 2), clippedBottom = Math.min(bottom, y + h / 2);
      if (clippedRight <= left || clippedBottom <= top) return [];
      return [[(left + clippedRight - 2 * crop.x) / (2 * crop.width),
        (top + clippedBottom - 2 * crop.y) / (2 * crop.height),
        (clippedRight - left) / crop.width, (clippedBottom - top) / crop.height]];
    });
  }
  const api = { rectFromDrag, remapBoxes };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.CropGeometry = api;
})(globalThis);
