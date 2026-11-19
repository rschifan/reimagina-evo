/**
 * Download chips: CSV / GeoJSON / GeoParquet links for the indicator on the
 * map, scoped to the AOI and grain it is drawn at.
 *
 * Plain anchors pointing at the API download URL rather than a fetch into a
 * Blob: the browser streams the file and the API sets Content-Disposition, so
 * nothing is buffered in memory here.
 */
import { downloadUrl } from "./api";

type DownloadFormat = "csv" | "geojson" | "parquet";


/** Point every chip at `slug`'s values in `scope`, or hide them with `null`. */
export function renderDownloads(
  container: HTMLElement,
  slug: string | null,
  scope: { aoiId: string | null; grain: string | null },
): void {
  container.hidden = slug === null;
  for (const link of container.querySelectorAll<HTMLAnchorElement>("a[data-format]")) {
    if (slug === null) link.removeAttribute("href");
    else link.href = downloadUrl(slug, link.dataset.format as DownloadFormat, scope);
  }
}
