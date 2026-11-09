/**
 * The legend's distribution strip: where a value sits among all the others.
 *
 * The legend used to carry a colour ramp, its endpoints, the median, and the
 * ramp's percentile basis. That renders a *pattern* — it does not let anyone
 * place a specific block inside one. Two territories with identical min,
 * median and max can have completely different distributions and would draw
 * an identical legend.
 *
 * So this draws the shape, marks the part of it the colour ramp actually
 * resolves, and marks the feature under the cursor. The question "this block
 * reads dark — is that unusual, or is most of the territory dark?" becomes a
 * glance instead of an export.
 *
 * ⚠ **The bins come from the server, and that is a constraint rather than a
 * preference.** The obvious implementation is `queryRenderedFeatures` over
 * the overlay source, and `selection-metrics` already abandoned exactly that:
 * it "silently undercounted whenever the renderer hadn't loaded the relevant
 * tiles (zoomed out, layer toggled off, slow tile fetches mid-drag)". For a
 * count that is an error; for a distribution it is worse, because the shape
 * would change while you read it and nothing would say which shape was real.
 * `/indicators/{slug}/stats?bins=N` answers from `curated_geo`, scoped to the
 * same AOI and feature type as every other number in the legend.
 *
 * Rendered as inline SVG rather than canvas: it is at most a few dozen rects,
 * it has to scale with the sidebar, and a rect carries a `<title>` that
 * assistive technology and a hover tooltip both read for free.
 */
import type { ValueHistogram } from "./api";
import { fmtNumber as formatNumber } from "./format";

/** Bins requested from the API. */
export const HISTOGRAM_BINS = 24;

const VIEW_W = 240;
const VIEW_H = 44;

export interface DistributionMarks {
  /** Low end of the colour ramp — usually p05. */
  rampMin: number;
  /** High end of the colour ramp — usually p95. */
  rampMax: number;
  /** Observed median, when known. */
  median: number | null;
  /** The feature currently under the cursor, when there is one. */
  hovered?: number | null;
}

/** Fraction of the way across the histogram's range, clamped to [0, 1]. */
export function positionOf(value: number, histogram: ValueHistogram): number {
  const span = histogram.hi - histogram.lo;
  // Every value identical: the single-bin case. Anything maps to the middle,
  // which is both the only honest answer and the one that keeps a marker
  // visible instead of hard against an edge.
  if (span <= 0) return 0.5;
  return Math.max(0, Math.min(1, (value - histogram.lo) / span));
}

/**
 * Bin index a value falls in, or ``null`` when it is outside the histogram.
 *
 * Mirrors the server's `width_bucket` + `LEAST` folding, so a value equal to
 * `hi` highlights the last bin rather than none.
 */
export function binOf(value: number, histogram: ValueHistogram): number | null {
  if (!Number.isFinite(value)) return null;
  if (value < histogram.lo || value > histogram.hi) return null;
  if (histogram.width <= 0) return 0;
  const raw = Math.floor((value - histogram.lo) / histogram.width);
  return Math.max(0, Math.min(histogram.bins - 1, raw));
}

/** Inclusive-exclusive edges of a bin, as a readable range. */
export function binLabel(
  index: number,
  histogram: ValueHistogram,
  unit: string,
): string {
  const suffix = unit ? ` ${unit}` : "";
  if (histogram.width <= 0) {
    return `${formatNumber(histogram.lo)}${suffix}`;
  }
  const lo = histogram.lo + index * histogram.width;
  const hi = lo + histogram.width;
  return `${formatNumber(lo)} – ${formatNumber(hi)}${suffix}`;
}

function escapeAttr(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/**
 * SVG markup for one histogram.
 *
 * Returns an empty string when there is nothing to draw, so a caller can
 * assign it unconditionally and get no element rather than an empty frame
 * implying a population of none.
 */
export function distributionSvg(
  histogram: ValueHistogram | null,
  marks: DistributionMarks,
  unit = "",
): string {
  if (!histogram || histogram.counts.length === 0) return "";
  const peak = Math.max(...histogram.counts);
  if (peak <= 0) return "";

  const slot = VIEW_W / histogram.counts.length;
  // A one-count bin in a territory whose peak is 400 would round to half a
  // pixel and disappear. A floor of 1.5 px keeps "something is here" visible,
  // which matters more at the tails than exact proportionality does — and the
  // tails are where the interesting blocks are.
  const barFor = (count: number): number =>
    count === 0 ? 0 : Math.max(1.5, (count / peak) * (VIEW_H - 6));

  const hoveredBin =
    marks.hovered != null ? binOf(marks.hovered, histogram) : null;

  const bars = histogram.counts
    .map((count, index) => {
      const h = barFor(count);
      const x = index * slot;
      const y = VIEW_H - h;
      const current = hoveredBin === index;
      const cls = current ? "dist__bar dist__bar--current" : "dist__bar";
      const label = `${count} at ${binLabel(index, histogram, unit)}`;
      return (
        `<rect class="${cls}" x="${x.toFixed(2)}" y="${y.toFixed(2)}" ` +
        `width="${Math.max(0, slot - 1).toFixed(2)}" height="${h.toFixed(2)}">` +
        `<title>${escapeAttr(label)}</title></rect>`
      );
    })
    .join("");

  // The span the colour ramp resolves, drawn behind the bars. Outside it the
  // choropleth is saturated at an endpoint, so two very different values
  // share a colour — that is a property of the ramp a reader should be able
  // to see rather than infer.
  const rampX = positionOf(marks.rampMin, histogram) * VIEW_W;
  const rampW =
    (positionOf(marks.rampMax, histogram) - positionOf(marks.rampMin, histogram)) *
    VIEW_W;
  const ramp =
    rampW > 0
      ? `<rect class="dist__ramp" x="${rampX.toFixed(2)}" y="0" ` +
        `width="${rampW.toFixed(2)}" height="${VIEW_H}"></rect>`
      : "";

  const median =
    marks.median != null
      ? `<line class="dist__median" x1="${(positionOf(marks.median, histogram) * VIEW_W).toFixed(2)}" ` +
        `x2="${(positionOf(marks.median, histogram) * VIEW_W).toFixed(2)}" ` +
        `y1="0" y2="${VIEW_H}"></line>`
      : "";

  const cursor =
    marks.hovered != null
      ? `<line class="dist__cursor" x1="${(positionOf(marks.hovered, histogram) * VIEW_W).toFixed(2)}" ` +
        `x2="${(positionOf(marks.hovered, histogram) * VIEW_W).toFixed(2)}" ` +
        `y1="0" y2="${VIEW_H}"></line>`
      : "";

  return (
    `<svg class="dist" viewBox="0 0 ${VIEW_W} ${VIEW_H}" ` +
    `preserveAspectRatio="none" role="img" ` +
    `aria-label="Distribution of ${escapeAttr(histogram.counts.reduce((a, b) => a + b, 0).toString())} values">` +
    `${ramp}${bars}${median}${cursor}</svg>`
  );
}

/**
 * Where a value sits among the others, as a percentage, or ``null``.
 *
 * The **mid-rank** estimator: everything in lower bins, plus half of the
 * value's own bin. Two reasons, and the second is the one that shows.
 *
 * A bin's members are indistinguishable at this resolution, so counting the
 * whole bin as "below" would be a claim the data does not support. And
 * counting it as below puts every value in the top bin at 100 %, which
 * renders as "higher than 100% of blocks" — a sentence that is both wrong
 * and obviously wrong, on exactly the outlier a planner is most likely to
 * click.
 *
 * Returns ``null`` for a population of one, where there is no comparison to
 * make and "higher than 50%" would be an artefact of the arithmetic rather
 * than a finding.
 */
export function percentileOf(
  value: number,
  histogram: ValueHistogram,
): number | null {
  const total = histogram.counts.reduce((a, b) => a + b, 0);
  if (total < 2) return null;
  const bin = binOf(value, histogram);
  if (bin === null) return null;
  let below = 0;
  for (let i = 0; i < bin; i += 1) below += histogram.counts[i];
  const rank = below + histogram.counts[bin] / 2;
  return Math.round((rank / total) * 100);
}
