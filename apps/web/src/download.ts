/**
 * Download UI: CSV / GeoJSON / GeoParquet buttons.
 *
 * Rather than fetching + turning the payload into a Blob in JS, we just
 * navigate the browser to the API download URL — it handles the
 * Content-Disposition header and streaming on its own. This keeps the
 * client tiny and avoids buffering the whole file in memory.
 */
import { downloadUrl } from "./api";

type DownloadFormat = "csv" | "geojson" | "parquet";


export function wireDownloadButtons(
  container: HTMLElement,
  getActiveSlug: () => string | null,
  // ⚠ The scenario the map is showing, read at click time. Without it the
  // export is the baseline's values while the choropleth above it draws the
  // scenario's — the same slug, a different city, and nothing in the file
  // says which. The export routes take `scenario_id` for this reason.
  getActiveScenarioId: () => string | null = () => null,
): void {
  const buttons = container.querySelectorAll<HTMLButtonElement>("button[data-format]");
  for (const button of buttons) {
    button.addEventListener("click", () => {
      const slug = getActiveSlug();
      if (!slug) return;
      const format = button.dataset.format as DownloadFormat | undefined;
      if (!format) return;
      const href = downloadUrl(slug, format, getActiveScenarioId());
      // Use a transient anchor rather than `window.location.href` so the
      // page doesn't navigate away — feels better for power users who
      // may export multiple formats back to back.
      const anchor = document.createElement("a");
      anchor.href = href;
      anchor.rel = "noopener";
      anchor.style.display = "none";
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
    });
  }
}


export function setDownloadAvailability(
  container: HTMLElement,
  available: boolean,
): void {
  container.hidden = !available;
  const buttons = container.querySelectorAll<HTMLButtonElement>("button[data-format]");
  for (const button of buttons) {
    button.disabled = !available;
  }
}
