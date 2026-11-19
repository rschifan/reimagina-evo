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
 * the overlay source, and it silently undercounts whenever the renderer has
 * not loaded the relevant tiles (zoomed out, layer toggled off, slow tile
 * fetches mid-drag). For a count that is an error; for a distribution it is
 * worse, because the shape
 * would change while you read it and nothing would say which shape was real.
 * `/indicators/{slug}/stats?bins=N` answers from `curated_geo`, scoped to the
 * same AOI and feature type as every other number in the legend.
 *
 * Rendered as inline SVG rather than canvas: it is at most a few dozen rects,
 * it has to scale with the sidebar, and a rect carries a `<title>` that
 * assistive technology and a hover tooltip both read for free.
 */
import type { ValueHistogram } from "./api";
import { fmtNumber, formatValue, unitLabel } from "./format";

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

/** What a bin label needs to know about the indicator: its unit and cap. */
export type BinIndicator = { unit: string | null; cap_value?: number | null };

/**
 * Inclusive-exclusive edges of a bin, as a readable range. The one-bin
 * histogram of identical values is a value, so it reads like every other
 * value the legend prints (`1,500 m+ (cap)` at the cap).
 */
export function binLabel(
  index: number,
  histogram: ValueHistogram,
  indicator: BinIndicator,
): string {
  const suffix = indicator.unit ? ` ${unitLabel(indicator.unit)}` : "";
  if (histogram.width <= 0) {
    return formatValue(histogram.lo, indicator);
  }
  const lo = histogram.lo + index * histogram.width;
  const hi = lo + histogram.width;
  return `${fmtNumber(lo)} – ${fmtNumber(hi)}${suffix}`;
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
  indicator: BinIndicator = { unit: null },
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

  const bars = histogram.counts
    .map((count, index) => {
      const h = barFor(count);
      const x = index * slot;
      const y = VIEW_H - h;
      const label = `${count} at ${binLabel(index, histogram, indicator)}`;
      return (
        `<rect class="dist__bar" x="${x.toFixed(2)}" y="${y.toFixed(2)}" ` +
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

  // The hovered feature's mark, drawn once and hidden; `markLegendValue`
  // moves it.
  const cursor =
    `<line class="dist__cursor" x1="0" x2="0" y1="0" y2="${VIEW_H}" visibility="hidden"></line>`;

  return (
    `<svg class="dist" viewBox="0 0 ${VIEW_W} ${VIEW_H}" ` +
    `preserveAspectRatio="none" role="img" ` +
    `aria-label="Distribution of ${escapeAttr(histogram.counts.reduce((a, b) => a + b, 0).toString())} values">` +
    `${ramp}${bars}${median}${cursor}</svg>`
  );
}


/**
 * Move the legend's cursor to ``value``, or hide it when ``null``, and mark
 * the bin it falls in.
 *
 * Called from the hover path, so it must be cheap and must not re-render the
 * strip: rebuilding it on every mousemove would drop the `<title>` tooltips
 * mid-hover and churn the DOM at pointer rate. It moves the one cursor line
 * {@link distributionSvg} drew and toggles one class on the bars.
 *
 * Only a mark, no rank: the client has 24 histogram bins, and a bin-level rank
 * would disagree with the exact one the server gives elsewhere.
 */
export function markLegendValue(
  container: ParentNode,
  histogram: ValueHistogram | null,
  value: number | null,
): void {
  const cursor = container.querySelector(".dist__cursor");
  if (!cursor || !histogram) return;
  const bin = value === null ? null : binOf(value, histogram);
  container.querySelectorAll(".dist__bar").forEach((bar, index) => {
    bar.classList.toggle("dist__bar--current", index === bin);
  });
  if (value === null) {
    cursor.setAttribute("visibility", "hidden");
    return;
  }
  const x = (positionOf(value, histogram) * VIEW_W).toFixed(2);
  cursor.setAttribute("x1", x);
  cursor.setAttribute("x2", x);
  cursor.removeAttribute("visibility");
}
