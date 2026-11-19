/** Format an arbitrary number with adaptive decimal places — integers
 *  pass through, ``|n|>=100`` rounds to 0dp, ``|n|>=1`` to 2dp, smaller
 *  to 3dp. Locale-aware (uses ``toLocaleString``) so thousands
 *  separators match the user's runtime locale. */
export function fmtNumber(value: number): string {
  if (Number.isInteger(value)) return value.toLocaleString();
  const abs = Math.abs(value);
  const digits = abs >= 100 ? 0 : abs >= 1 ? 2 : 3;
  return value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}


/** Escape the five HTML-significant characters for safe insertion
 *  into innerHTML / template-literal markup. Use when a user-supplied
 *  string is concatenated into HTML rather than set via textContent. */
export function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}


/** The catalog's unit codes as words: `buildings_per_ha` → `buildings / ha`,
 *  `ratio_0_1` → `ratio 0–1`. */
export function unitLabel(unit: string | null): string {
  if (!unit) return "unitless";
  return unit.replace(/_0_1$/, " 0–1").replaceAll("_per_", " / ").replaceAll("_", " ");
}


/**
 * One indicator value as a reader sees it: `1,234 m`, `12.5 units / ha`,
 * `0.41`, and at or past a declared cap `1,500 m+ (cap)`.
 *
 * A value at the cap is not a measurement: the distance indicators report the
 * cutoff for a block nothing is reachable from, so it reads as "the cap or
 * more", never as a distance. Only `cap_value` triggers this; `valid_min` /
 * `valid_max` are validation ranges and say nothing about meaning.
 *
 * A 0–1 score or ratio is shown bare: its unit is a scale, not a suffix.
 * Trailing zeros are dropped (`0.41`, not `0.410`).
 */
export function formatValue(
  value: number,
  indicator: { unit: string | null; cap_value?: number | null },
): string {
  const cap = indicator.cap_value ?? null;
  const unit = indicator.unit && !indicator.unit.endsWith("_0_1") ? ` ${unitLabel(indicator.unit)}` : "";
  if (cap !== null && value >= cap) return `${plainNumber(cap)}${unit}+ (cap)`;
  return `${plainNumber(value)}${unit}`;
}


function plainNumber(value: number): string {
  const abs = Math.abs(value);
  return value.toLocaleString(undefined, { maximumFractionDigits: abs >= 100 ? 0 : abs >= 1 ? 2 : 3 });
}
