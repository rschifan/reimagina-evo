import type { FeatureCollection, MultiPolygon, Polygon, Position } from "geojson";

import { describeGroup, type EditGroup } from "../scenario";
import type { SelectionShape } from "../selection";

/**
 * A scenario's rules as map geometry.
 *
 * The API stores a rule's area as the WKT `ST_MakeValid` produced — always a
 * `POLYGON` or `MULTIPOLYGON` (`add_edit` keeps only the polygonal part) — and
 * not the tool it was drawn with. So the area is parsed here, and the shape
 * kind that decides which handles an edit gets is inferred from the ring.
 */

/** Parse the two WKT types a rule's area can be. Throws on anything else. */
export function wktToGeometry(wkt: string): Polygon | MultiPolygon {
  const text = wkt.trim();
  const match = /^(MULTIPOLYGON|POLYGON)\s*(\(.*\))$/is.exec(text);
  if (!match) throw new Error(`not a polygon: ${text.slice(0, 40)}`);
  const nested = parseNested(match[2]);
  if (match[1].toUpperCase() === "POLYGON") {
    return { type: "Polygon", coordinates: nested as Position[][] };
  }
  return { type: "MultiPolygon", coordinates: nested as Position[][][] };
}

/** `((x y, x y), (x y))` → nested arrays, a pair of numbers at the bottom. */
function parseNested(text: string): unknown[] {
  const json = text
    .replace(/(-?[\d.eE+-]+)\s+(-?[\d.eE+-]+)/g, "[$1,$2]")
    .replace(/\(/g, "[")
    .replace(/\)/g, "]");
  return JSON.parse(json) as unknown[];
}

/**
 * Which drawing tool would have made this area, for the edit handles.
 *
 * A rectangle is 4 corners with axis-aligned edges; a circle is terra-draw's
 * ≥ 32-vertex ring whose vertices all sit within 2 % of one radius from their
 * centroid; anything else edits as a polygon. A shape in several pieces cannot
 * be dragged as one — `null`.
 */
export function inferShape(geometry: Polygon | MultiPolygon): SelectionShape | null {
  if (geometry.type === "MultiPolygon") return null;
  const ring = openRing(geometry.coordinates[0] ?? []);
  if (ring.length === 4 && isAxisAligned(ring)) return "rectangle";
  if (ring.length >= 32 && isRound(ring)) return "circle";
  return "polygon";
}

function openRing(ring: Position[]): Position[] {
  const [first, last] = [ring[0], ring[ring.length - 1]];
  const closed = first && last && first[0] === last[0] && first[1] === last[1];
  return closed ? ring.slice(0, -1) : ring;
}

function isAxisAligned(ring: Position[]): boolean {
  return ring.every((point, i) => {
    const next = ring[(i + 1) % ring.length];
    return point[0] === next[0] || point[1] === next[1];
  });
}

function isRound(ring: Position[]): boolean {
  const cx = ring.reduce((sum, p) => sum + p[0], 0) / ring.length;
  const cy = ring.reduce((sum, p) => sum + p[1], 0) / ring.length;
  // On the ground, not in degrees: terra-draw's circles are round in metres,
  // so a degree of longitude is shrunk by cos(latitude) — at Mexicali's 32.6°
  // a circle is ~1.19× wider than tall in raw degrees.
  const kx = Math.cos((cy * Math.PI) / 180);
  const radii = ring.map((p) => Math.hypot((p[0] - cx) * kx, p[1] - cy));
  const mean = radii.reduce((sum, r) => sum + r, 0) / radii.length;
  return mean > 0 && radii.every((r) => Math.abs(r - mean) <= 0.02 * mean);
}

/**
 * The rules as one GeoJSON source, numbered from 1 in "later wins" order —
 * which is also the order MapLibre draws them in, so a later rule paints over
 * an earlier one where they overlap, as it wins there.
 */
export function rulesToFeatureCollection(groups: EditGroup[]): FeatureCollection {
  const ordered = [...groups].sort((a, b) => a.ordinal - b.ordinal);
  const features: FeatureCollection["features"] = [];
  ordered.forEach((group, i) => {
    let geometry: Polygon | MultiPolygon;
    try {
      geometry = wktToGeometry(group.areaWkt);
    } catch {
      // One unreadable rule must not take the whole layer down.
      return;
    }
    features.push({
      type: "Feature",
      geometry,
      properties: {
        index: i + 1,
        label: describeGroup(group, i + 1),
        editIds: group.editIds.join(","),
      },
    });
  });
  return { type: "FeatureCollection", features };
}
