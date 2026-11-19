/**
 * Where the rule box goes, in map pixels: right of the shape if it fits, else
 * left of it, else wherever keeps it inside the map. Level with the shape's
 * top, clamped. Pure, so the edge cases are tested without a map.
 */
export function anchorBox(
  shape: { minX: number; minY: number; maxX: number; maxY: number },
  map: { width: number; height: number },
  box: { width: number; height: number },
  margin = 8,
): { left: number; top: number } {
  const clamp = (v: number, lo: number, hi: number): number => Math.max(lo, Math.min(v, hi));
  const right = shape.maxX + margin;
  const left = shape.minX - margin - box.width;
  const x =
    right + box.width <= map.width - margin
      ? right
      : left >= margin
        ? left
        : clamp(right, margin, map.width - margin - box.width);
  const y = clamp(shape.minY, margin, map.height - margin - box.height);
  return { left: x, top: y };
}
