/**
 * Ops API client.
 *
 * Deliberately not importing `../catalog/api.ts`, for the reason that file
 * gives for not importing `../api.ts`: coupling two pages' clients makes every
 * change to one a change to the other. All three import `../http.ts` instead,
 * and each declares the fields it actually reads.
 */
import { jsonRequest } from "../http";

/** The `orchestration_run.status` CHECK vocabulary (r028). */
export type RunStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "partial"
  | "failed"
  | "cancelling"
  | "cancelled";

/** The `job_unit.status` CHECK vocabulary (r028) — note `skipped`, absent above. */
export type UnitStatus =
  "queued" | "running" | "succeeded" | "failed" | "skipped" | "cancelled";

export const RUN_STATUSES: RunStatus[] = [
  "queued",
  "running",
  "succeeded",
  "partial",
  "failed",
  "cancelling",
  "cancelled",
];

export interface RunRow {
  run_id: string;
  status: RunStatus;
  scenario_id: string | null;
  aoi_id: string | null;
  code_version: string;
  submitted_at: string;
  completed_at: string | null;
  units_total: number;
  units_terminal: number;
  units_succeeded: number;
  units_failed: number;
  units_running: number;
  units_last_terminal_at: string | null;
}

export interface ExecutorRow {
  executor_id: string;
  role: string;
  app_version: string | null;
  heartbeat_age_s: number;
}

export interface RunListResponse {
  runs: RunRow[];
  /** The clock every elapsed time is measured against; see `derive.serverNow`. */
  server_now: string;
  total: number;
  status_counts: Record<string, number>;
  executors: ExecutorRow[];
}

export interface UnitDetail {
  indicator: string;
  status: UnitStatus;
  detail: string | null;
  queued_at: string;
  started_at: string | null;
  completed_at: string | null;
  /** Prerequisites within this run — not the whole registry graph. */
  depends_on: string[];
}

export interface RunDetailResponse {
  run: RunRow;
  units: UnitDetail[];
  server_now: string;
}

export interface AOIRow {
  aoi_id: string;
  name: string;
}

export function listRuns(
  opts: { statuses?: RunStatus[]; limit?: number } = {},
  init: { signal?: AbortSignal } = {},
): Promise<RunListResponse> {
  const p = new URLSearchParams();
  for (const s of opts.statuses ?? []) p.append("status", s);
  if (opts.limit) p.set("limit", String(opts.limit));
  const qs = p.toString();
  return jsonRequest<RunListResponse>(`/jobs${qs ? `?${qs}` : ""}`, init);
}

export function getRunDetail(
  runId: string,
  init: { signal?: AbortSignal } = {},
): Promise<RunDetailResponse> {
  return jsonRequest<RunDetailResponse>(
    `/jobs/${encodeURIComponent(runId)}/detail`,
    init,
  );
}

/** Three lines rather than an import from the catalog page — see the header. */
export function listAois(
  init: { signal?: AbortSignal } = {},
): Promise<AOIRow[]> {
  return jsonRequest<AOIRow[]>("/aois", init);
}

export interface SubmitRunBody {
  aoi_id?: string | null;
  scenario_id?: string | null;
  changed_attributes?: string[];
  /** Recompute exactly these, skipping change-set derivation. */
  indicators?: string[];
}

export interface ImpactPreview {
  indicators: string[];
  count: number;
  /** True when nothing would be recomputed — a correct answer, not an error. */
  noop: boolean;
  reason: string;
}

export interface IndicatorSummary {
  slug: string;
  name: string;
  model_family: string;
}

/** Dry-run the planner. Persists nothing; safe to call on every keystroke. */
export function previewImpact(
  body: SubmitRunBody,
  init: { signal?: AbortSignal } = {},
): Promise<ImpactPreview> {
  return jsonRequest<ImpactPreview>("/jobs/preview", {
    ...init,
    method: "POST",
    body,
  });
}

export function listIndicators(
  init: { signal?: AbortSignal } = {},
): Promise<IndicatorSummary[]> {
  return jsonRequest<IndicatorSummary[]>("/indicators", init);
}

export interface SubmitRunResponse {
  /** Null for a "noop" submit — the dirty set was empty, so no run exists.
   *  Polling this id would 404, which is why the API suppresses it. */
  run_id: string | null;
  status: string;
}

export function submitRun(
  body: SubmitRunBody,
  init: { signal?: AbortSignal } = {},
): Promise<SubmitRunResponse> {
  // `wait` is deliberately never sent: it blocks the request for the whole
  // compute, up to the API's 300 s cap.
  return jsonRequest<SubmitRunResponse>("/jobs", {
    ...init,
    method: "POST",
    body,
  });
}

export interface RerunResponse extends SubmitRunResponse {
  rerun_of: string;
  indicators: string[];
}

export function cancelRun(
  runId: string,
  init: { signal?: AbortSignal } = {},
): Promise<unknown> {
  return jsonRequest(`/jobs/${encodeURIComponent(runId)}/cancel`, {
    ...init,
    method: "POST",
    body: {},
  });
}

export function rerunRun(
  runId: string,
  init: { signal?: AbortSignal } = {},
): Promise<RerunResponse> {
  return jsonRequest<RerunResponse>(`/jobs/${encodeURIComponent(runId)}/rerun`, {
    ...init,
    method: "POST",
    body: {},
  });
}

export interface DeletedRuns {
  deleted: number;
}

/** Forget one finished run. History only — computed values are not touched. */
export function deleteRun(
  runId: string,
  init: { signal?: AbortSignal } = {},
): Promise<DeletedRuns> {
  return jsonRequest<DeletedRuns>(`/jobs/${encodeURIComponent(runId)}`, {
    ...init,
    method: "DELETE",
  });
}

/** Forget every finished run. Live runs are left alone. */
export function deleteFinishedRuns(
  init: { signal?: AbortSignal } = {},
): Promise<DeletedRuns> {
  return jsonRequest<DeletedRuns>("/jobs", { ...init, method: "DELETE" });
}
