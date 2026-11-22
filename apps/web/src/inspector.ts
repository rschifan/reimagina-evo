/**
 * The block inspector: the card a click on the map pins.
 *
 * It shows the clicked feature's id, the active indicator's value with its
 * exact rank (from the server, never from the legend's histogram bins), and
 * the feature's other current values; clicking one of those switches the map
 * to it. main.ts owns the request, the pinned outline and when it closes.
 */
import type { FeatureIndicatorValue } from "./api";
import { formatValue, grainNoun, shareText } from "./format";

/**
 * Where a value sits among the AOI's features of the same grain, in words.
 *
 * Counts strictly lower values, so "Higher than 31%" is never inflated by
 * ties; a value at the cap reports the share sharing it instead, since
 * "farther than" means nothing past the cutoff. Fewer than two values is no
 * comparison at all.
 */
export function rankText(row: FeatureIndicatorValue, grain: string | null = "block"): string {
  if (row.count < 2) return "";
  const word = grainNoun(grain, true);
  // "nearly all" / "under 1%" rather than ">99%" / "<1%" spliced after "than".
  const share = (n: number): string => {
    const s = shareText(n, row.count);
    return s === ">99" ? "nearly all" : s === "<1" ? "under 1% of" : `${s}% of`;
  };
  if (row.cap_value !== null && row.value >= row.cap_value) {
    return `At the cap, with ${share(row.count_equal)} ${word}`;
  }
  return `${row.unit === "m" ? "Farther" : "Higher"} than ${share(row.count_below)} ${word}`;
}

/**
 * Fill `container` with the card. `rows` as a string is a status line
 * (loading, or the failure message) shown in place of the values.
 */
export function renderInspector(
  container: HTMLElement,
  feature: { id: string; grain: string | null; estimated?: string | null },
  rows: FeatureIndicatorValue[] | string,
  activeSlug: string | null,
  onPick: (slug: string) => void,
  onClose: () => void,
): void {
  const el = (tag: string, cls: string, text = ""): HTMLElement => {
    const node = document.createElement(tag);
    node.className = cls;
    node.textContent = text;
    return node;
  };

  const head = el("div", "inspector__head");
  const noun = grainNoun(feature.grain, false);
  const kind = noun[0].toUpperCase() + noun.slice(1);
  const id = el("code", "inspector__id", feature.id);
  id.title = "Click to select, then copy";
  const close = el("button", "inspector__close", "✕") as HTMLButtonElement;
  close.type = "button";
  close.setAttribute("aria-label", "Close");
  close.addEventListener("click", onClose);
  head.append(el("span", "inspector__kind", kind), id, close);
  container.replaceChildren(head);
  container.hidden = false;
  // The tile's `estimated`: this block's scenario inputs are a guess.
  if (feature.estimated) {
    container.append(el("p", "inspector__estimated", "Estimated \u2014 buildings not fully mapped"));
  }

  if (typeof rows === "string") {
    container.append(el("p", "inspector__status", rows));
    return;
  }
  if (rows.length === 0) {
    container.append(el("p", "inspector__status", "No indicator values for this feature."));
    return;
  }

  const active = rows.find((r) => r.slug === activeSlug);
  const activeBox = el("div", "inspector__active");
  if (active) {
    const rank = rankText(active, feature.grain);
    activeBox.append(
      el("div", "inspector__name", active.name),
      el("div", "inspector__value", formatValue(active.value, active)),
    );
    if (rank) activeBox.append(el("div", "inspector__rank", rank));
  } else {
    activeBox.append(el("p", "inspector__status", "No value for the indicator on the map."));
  }
  container.append(activeBox);

  const others = rows
    .filter((r) => r.slug !== activeSlug)
    .sort((a, b) => a.name.localeCompare(b.name));
  if (others.length === 0) return;
  const list = el("div", "inspector__list");
  for (const r of others) {
    const btn = el("button", "inspector__row") as HTMLButtonElement;
    btn.type = "button";
    btn.title = `Show ${r.name} on the map`;
    btn.append(el("span", "inspector__row-name", r.name), el("span", "inspector__row-value", formatValue(r.value, r)));
    btn.addEventListener("click", () => onPick(r.slug));
    list.append(btn);
  }
  container.append(el("div", "inspector__subhead", "Other values"), list);
}
