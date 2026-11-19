/**
 * The one banner over the map, with one channel per writer.
 *
 * Each writer sets or clears only its own channel, so a finished load cannot
 * hide a tile error it never showed. The banner shows the most important
 * active message: errors (an alert) before loading (a status).
 */
export type BannerChannel = "api" | "map" | "values" | "loading";

/** Most important first. */
const ORDER: readonly BannerChannel[] = ["api", "map", "values", "loading"];

/** Returns `set(channel, message)`; `null` clears that channel. */
export function createBanner(el: HTMLElement | null): (channel: BannerChannel, message: string | null) => void {
  const messages = new Map<BannerChannel, string>();
  let shown: string | null = null; // "channel\0text" on screen, or null
  return (channel, message) => {
    if (message === null) messages.delete(channel);
    else messages.set(channel, message);
    if (!el) return;
    const top = ORDER.find((c) => messages.has(c));
    // Unchanged: no DOM write, so a standing alert is not announced again.
    const key = top === undefined ? null : `${top}\0${messages.get(top)}`;
    if (key === shown) return;
    shown = key;
    const error = top !== undefined && top !== "loading";
    el.hidden = top === undefined;
    // Errors interrupt; loading messages wait their turn.
    el.setAttribute("role", error ? "alert" : "status");
    el.dataset.kind = error ? "error" : "loading";
    el.textContent = top === undefined ? "" : (messages.get(top) ?? "");
  };
}
