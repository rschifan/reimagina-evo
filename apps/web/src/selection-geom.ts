/**
 * Geometry helpers shared by selection-related modules.
 *
 * Used by ``selection-mask`` (mask-overlay polygon), ``selection-filter``
 * (point-in-polygon classification of value-layer features), and
 * ``selection-handle`` (centroid + planar distance for the radius
 * handle math). All functions are pure and stateless.
 *
 * Coordinates are in EPSG:4326 lng/lat. The point-in-polygon test
 * uses ray-cast at the prototype's scale (no antimeridian crossings).
 */


/** Bounding box of a single ring as ``[minX, minY, maxX, maxY]``.
 *  Returns ``null`` when the ring is empty or all-NaN. */
export function polygonBbox(
  ring: number[][],
): [number, number, number, number] | null {
  if (!ring || ring.length === 0) return null;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const [x, y] of ring) {
    if (x < minX) minX = x;
    if (y < minY) minY = y;
    if (x > maxX) maxX = x;
    if (y > maxY) maxY = y;
  }
  if (!Number.isFinite(minX)) return null;
  return [minX, minY, maxX, maxY];
}


/** Pick a representative point of any GeoJSON geometry. Polygons +
 *  multipolygons resolve to the area-weighted centroid of the outer
 *  ring; lines / multipoints to their first vertex; points return
 *  themselves. ``null`` for unsupported / empty geometries. */
export function representativePoint(
  geom: GeoJSON.Geometry | null,
): [number, number] | null {
  if (!geom) return null;
  switch (geom.type) {
    case "Point":
      return geom.coordinates as [number, number];
    case "MultiPoint":
    case "LineString":
      return (geom.coordinates[0] as [number, number]) ?? null;
    case "MultiLineString":
      return (geom.coordinates[0]?.[0] as [number, number]) ?? null;
    case "Polygon":
      return polygonCentroid(geom.coordinates);
    case "MultiPolygon":
      if (!geom.coordinates.length) return null;
      return polygonCentroid(geom.coordinates[0]);
    default:
      return null;
  }
}


/** Area-weighted centroid (shoelace formula) of the outer ring. Falls
 *  back to vertex mean for degenerate (zero-area) rings. Holes are
 *  ignored — sufficient for the manzana / AGEB / municipality shapes
 *  the selection tool runs against. Internal — only ``representativePoint``
 *  needs it, and routing through that helper keeps geometry-type
 *  dispatch in one place. */
function polygonCentroid(rings: number[][][]): [number, number] | null {
  const ring = rings[0];
  if (!ring || ring.length < 3) return null;
  let area = 0, cx = 0, cy = 0;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const xi = ring[i][0], yi = ring[i][1];
    const xj = ring[j][0], yj = ring[j][1];
    const cross = xj * yi - xi * yj;
    area += cross;
    cx += (xj + xi) * cross;
    cy += (yj + yi) * cross;
  }
  area *= 0.5;
  if (Math.abs(area) < 1e-12) {
    let mx = 0, my = 0, n = 0;
    for (let i = 0; i < ring.length - 1; i++) {
      mx += ring[i][0]; my += ring[i][1]; n++;
    }
    if (n === 0) return null;
    return [mx / n, my / n];
  }
  return [cx / (6 * area), cy / (6 * area)];
}


/** Ray-cast point-in-polygon. Treats the first ring as the outer and
 *  subsequent rings as holes — a point is inside iff it's inside the
 *  outer AND not inside any hole. */
export function pointInPolygon(
  pt: [number, number],
  rings: GeoJSON.Polygon["coordinates"],
): boolean {
  if (!rings.length) return false;
  if (!_pointInRing(pt, rings[0])) return false;
  for (let i = 1; i < rings.length; i++) {
    if (_pointInRing(pt, rings[i])) return false;
  }
  return true;
}


function _pointInRing(pt: [number, number], ring: number[][]): boolean {
  const [x, y] = pt;
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const xi = ring[i][0], yi = ring[i][1];
    const xj = ring[j][0], yj = ring[j][1];
    // The ``yi > y !== yj > y`` half guarantees ``y`` lies strictly
    // between the edge endpoints, so ``yj - yi`` is non-zero by the
    // time the division runs. No epsilon needed — adding one would
    // shift the intersection x slightly and bias points exactly on
    // a vertical edge.
    const intersect =
      yi > y !== yj > y &&
      x < ((xj - xi) * (y - yi)) / (yj - yi) + xi;
    if (intersect) inside = !inside;
  }
  return inside;
}
