// Where the browser sends API requests. Unset means the localhost dev mode (the API on
// port 8000). Empty means same-origin: /api/... on this site, proxied by the Next server
// to API_INTERNAL_URL (app/api/[...path]/route.ts). The production image builds with it
// empty, so production needs no CORS. Inlined at build time.
export const API = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

// All APIs share the same revocable HTTP-only session cookie. A custom request
// header protects state-changing endpoints against cross-site form requests.
async function apiFetch(url: string, init: RequestInit = {}): Promise<Response> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    headers.set("X-TicketFlow-Request", "1");
  }
  try {
    return await fetch(url, { ...init, headers, credentials: "include", cache: "no-store" });
  } catch {
    // fetch only rejects when no answer came back: offline, server down, or dropped mid-request.
    throw new ApiError("Couldn't reach the Quarvera server. Check your connection and try again.", "network");
  }
}

export type Role = "owner" | "manager" | "clerk" | "viewer";
export type Permission =
  | "records.read" | "tickets.upload" | "tickets.edit" | "tickets.flag" | "tickets.verify"
  | "tickets.skip" | "tickets.price_override" | "projects.manage" | "pricing.manage"
  | "invoices.manage" | "team.manage" | "scans.resolve" | "scans.confirm_blank" | "system.read"
  | "usage.read" | "trucking.edit" | "trucking.approve";
export type Account = {
  id: number;
  username: string;
  display_name: string | null;
  role: Role;
  role_label: string;
  permissions: Permission[];
  must_change_password: boolean;
  organization_name: string | null;
};

export async function getAuthMe(): Promise<Account> {
  return (await check(await apiFetch(`${API}/api/auth/me`))).json();
}

export async function changePassword(current_password: string, new_password: string): Promise<Account> {
  return (await check(await apiFetch(`${API}/api/auth/password`, json("POST", { current_password, new_password })))).json();
}

export async function logIn(username: string, password: string): Promise<Account> {
  return (await check(await apiFetch(`${API}/api/auth/login`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  }))).json();
}

/** Whether this server has no accounts yet. A server without the check answers 404, meaning no. */
export async function getSetupStatus(): Promise<{ needed: boolean }> {
  return (await check(await apiFetch(`${API}/api/setup/status`))).json();
}
export async function setUpCompany(body: { company_name: string; display_name: string; username: string; password: string }): Promise<Account> {
  return (await check(await apiFetch(`${API}/api/setup`, json("POST", body)))).json();
}

export type AccessRequest = {
  company_name: string; contact_name: string; email: string; phone?: string;
  monthly_tickets?: "under_500" | "500_2000" | "2000_10000" | "over_10000"; message?: string; website?: string;
};
/** Asks for a pilot workspace. It creates no account; someone follows up. */
export async function requestAccess(body: AccessRequest): Promise<void> {
  await check(await apiFetch(`${API}/api/access-requests`, json("POST", body)));
}

export async function logOut(): Promise<void> {
  await check(await apiFetch(`${API}/api/auth/logout`, { method: "POST" }));
}

export type Project = { id: number; code: string; name: string; job_location: string | null; description: string | null; status: string };
export type FileOut = { id: number; original_name: string; status: string; error: string | null };
/** How far the reader has got, while a batch is processed. */
export type BatchProgress = { expected: number; read: number; failed: number; reading_again: number; files_waiting: number };
export type Batch = { id: number; project_id: number | null; expected_ticket_count?: number | null; progress?: BatchProgress | null; layout: string; intake_mode?: "extract" | "manual"; status: string; created_at: string; files: FileOut[] };
export type Ticket = {
  id: number;
  batch_id: number;
  source_page: number;
  position: number;
  ticket_number: string | null;
  delivery_date: string | null;
  material_type: string | null;
  tonnage: string | null; // decimal string from the API; never parsed to float for money math
  supplier: string | null;
  job_location: string | null;
  hauler_id?: number | null;
  truck_number?: string | null;
  haul_billing?: string | null;
  haul_unit_rate?: string | null;
  haul_cost?: string | null;
  flags: string[];
  status: "needs_review" | "verified" | "flagged" | "skipped" | "failed";
  /** Why the ticket number may really be the job, PO or truck number. Never blocks approval. */
  number_warning?: string | null;
  /** Catalog names these probably mean, apart from words like Inc or Trucking. Suggestions only. */
  similar?: Partial<Record<"supplier" | "material_type", string>>;
  tonnage_confirmed: boolean;
  error: string | null;
  unit_price_override: string | null;
  price_snapshot_state: "pending" | "priced" | "unpriced" | null;
  unit_price: string | null;
  price_source: "project_rate" | "rate" | "override" | null;
  cost: string | null;
  duplicates: string[];
  pricing_notes: string[];
  verified_by: string | null; // null when unverified or approved before individual accounts
  verified_at: string | null;
  suggestions?: Record<string, string>; // catalog name for a known spelling; a person applies it
  as_read?: Record<string, string>; // what the reader returned, separate from current values
  flag_note?: string | null; // why someone set this ticket aside
  revision?: string; // sent back in If-Match, so nobody approves values they weren't looking at
};
export type BatchQueueRow = {
  id: number; project_id: number | null; project_code: string | null; project_name: string | null;
  status: string; intake_mode: "extract" | "manual"; created_at: string; created_by: string | null;
  files: number; failed_files: number; expected_ticket_count: number | null;
  tickets: number; needs_review: number; flagged: number; verified: number; skipped: number; failed: number;
  faint_slots: number;
};
export async function getBatchQueue(openOnly = true): Promise<BatchQueueRow[]> {
  return (await check(await apiFetch(`${API}/api/batches/queue?open_only=${openOnly}`))).json();
}
export type HistoryEntry = { field: string; old: string | null; new: string | null; at: string; actor: string | null };

export async function getTicketHistory(id: number): Promise<HistoryEntry[]> {
  return (await check(await apiFetch(`${API}/api/tickets/${id}/audit`))).json();
}
export type Supplier = { id: number; name: string; contact: string | null };
export type Material = { id: number; name: string; unit: string };
// project_id null: company-wide. effective_from null: no start date (YYYY-MM-DD otherwise).
export type Rate = { id: number; supplier_id: number; material_id: number; unit_price: string; project_id: number | null; effective_from: string | null; effective_to?: string | null; freight?: "included" | "separate" | null };
// effective_to: the last day the price is valid (a quote's expiry). Left out or null: open-ended.
export type RateScope = { project_id: number | null; effective_from: string | null; effective_to?: string | null };
export type TimelineSegment = { rate_id: number; price: string; from: string | null; to: string | null; declared_to: string | null; state: "past" | "current" | "future" };
export type RateTimeline = {
  today: string;
  pairs: { supplier: string; material: string; waiting_unpriced: number; warnings: number;
    scopes: { project_id: number | null; project: string | null; label: string; segments: TimelineSegment[];
      warnings: { kind: "overlap" | "gap" | "expired" | "expiring"; text: string }[] }[] }[];
};
export async function getRateTimeline(): Promise<RateTimeline> {
  return (await check(await apiFetch(`${API}/api/rates/timeline`))).json();
}

/** An API refusal. `code` is set when the UI should react, not just show text. */
export class ApiError extends Error {
  code?: string;
  status?: number;
  /** The server's own reference for this request, to quote if it keeps happening. */
  reference?: string;
  retryAfterSeconds?: number;
  constructor(message: string, code?: string, details: { status?: number; reference?: string; retryAfterSeconds?: number } = {}) {
    super(message);
    this.code = code;
    Object.assign(this, details);
  }
}

/** A failed save or approval, in words for the person who just tried it. */
export type Failure = { message: string; reference?: string; canRetry: boolean; signIn?: boolean };

/**
 * `noun` names what was attempted ("save", "approval"). `savedFirst` means the
 * typed values reached the server before the next step failed, so they're safe.
 */
export function describeFailure(error: unknown, noun: string, savedFirst = false): Failure {
  const api = error instanceof ApiError ? error : new ApiError((error as Error)?.message ?? "Something went wrong.");
  const start = savedFirst ? `Your changes were saved, but this ${noun}` : `This ${noun}`;
  const typed = savedFirst ? "" : " What you typed is still here.";
  const reference = api.reference;
  if (api.code === "network") {
    return { message: `${start} may not have gone through, because Quarvera couldn't reach the server. Check your connection, then try again.${typed}`, canRetry: true };
  }
  if (api.status === 401) {
    return { message: `${start} did not go through, because you've been signed out. Sign in again and reopen this ticket.${savedFirst ? "" : " What you typed is kept on this computer."}`, canRetry: false, signIn: true };
  }
  if (api.status === 429) {
    const wait = api.retryAfterSeconds ? `about ${api.retryAfterSeconds} second${api.retryAfterSeconds === 1 ? "" : "s"}` : "a minute";
    return { message: `${start} did not go through, because too many requests reached the server at once. Wait ${wait}, then try again.${typed}`, reference, canRetry: true };
  }
  if (api.status === 503) {
    const wait = api.retryAfterSeconds ? `in about ${api.retryAfterSeconds} second${api.retryAfterSeconds === 1 ? "" : "s"}` : "in a moment";
    return { message: `${start} did not go through, because the server is busy or temporarily unavailable. Try again ${wait}.${typed}`, reference, canRetry: true };
  }
  if (api.status !== undefined && api.status >= 500) {
    return { message: `${start} did not go through, because something went wrong on the server. Try again; if it keeps happening, quote the reference to whoever looks after Quarvera.${typed}`, reference, canRetry: true };
  }
  // A refusal the server explained: trying the same thing again won't change the answer.
  return { message: api.message, canRetry: false };
}

const FALLBACK_MESSAGE: Record<number, string> = {
  429: "Too many requests reached the server at once. Wait a minute, then try again.",
  503: "The server is busy or temporarily unavailable. Try again in a moment.",
};

async function check(res: Response) {
  if (!res.ok) {
    let detail = FALLBACK_MESSAGE[res.status] ?? (res.status >= 500 ? "Something went wrong on the server. Try again." : res.statusText);
    let code: string | undefined;
    let reference = res.headers.get("X-Request-ID") ?? undefined;
    const retryAfter = Number(res.headers.get("Retry-After"));
    try {
      const body = await res.json();
      if (typeof body.request_id === "string") reference = body.request_id;
      if (typeof body.detail === "string") {
        detail = body.detail;
      } else if (body.detail && typeof body.detail.message === "string") {
        detail = body.detail.message;
        code = body.detail.code;
        // A temporary password must be replaced before any other work.
        if (body.detail.code === "password_change_required" && typeof window !== "undefined"
          && window.location.pathname !== "/account") {
          window.location.replace("/account");
        }
      } else if (Array.isArray(body.detail)) {
        // The framework's field-by-field validation answer.
        detail = body.detail.map((item: { loc?: unknown[]; msg?: string }) =>
          [item.loc?.[item.loc.length - 1], item.msg].filter(Boolean).join(": ")).join("; ");
      } else if (body.detail !== undefined) {
        detail = JSON.stringify(body.detail);
      }
    } catch {}
    throw new ApiError(detail, code, {
      status: res.status, reference, retryAfterSeconds: retryAfter > 0 ? retryAfter : undefined,
    });
  }
  return res;
}

export async function uploadBatch(files: File[], layout: string, projectId?: number, intakeMode: "extract" | "manual" = "extract", expectedTickets?: number): Promise<Batch> {
  const form = new FormData();
  if (expectedTickets !== undefined) form.append("expected_ticket_count", String(expectedTickets));
  form.append("layout", layout);
  form.append("intake_mode", intakeMode);
  if (projectId !== undefined) form.append("project_id", String(projectId));
  files.forEach((f) => form.append("files", f));
  return (await check(await apiFetch(`${API}/api/batches`, { method: "POST", body: form }))).json();
}

// Scan check: every physical slot on every page, ticket or not.
export type ManifestSlot = {
  id: number; position: number;
  state: "ticket" | "suspected_blank" | "empty" | "confirmed_blank";
  detection: "content" | "faint" | "empty"; decision: string | null; rotation: number;
  crop_box: number[]; crop_sha256: string;
  ticket_id: number | null; ticket_number: string | null; ticket_status: string | null;
};
export type Manifest = {
  batch_id: number; layout: string;
  status: "processing" | "needs_attention" | "count_not_confirmed" | "count_matches";
  expected_ticket_count: number | null;
  counts: {
    files: number; failed_files: number; legacy_files: number; pages: number; slots: number;
    records: number; suspected_blank: number; empty: number; confirmed_blank: number;
    needs_review: number; flagged: number; verified: number; failed_tickets: number; skipped: number;
  };
  blockers: string[];
  files: {
    id: number; original_name: string; sha256: string; status: string; error: string | null;
    legacy: boolean; pages: { page: number; slots: ManifestSlot[] }[];
  }[];
};
export const slotImageUrl = (id: number) => `${API}/api/scan-slots/${id}/image`;
export const pageImageUrl = (fileId: number, page: number) => `${API}/api/files/${fileId}/pages/${page}/image`;
export async function getManifest(batchId: number): Promise<Manifest> {
  return (await check(await apiFetch(`${API}/api/batches/${batchId}/manifest`))).json();
}
export async function setExpectedCount(batchId: number, expected: number | null): Promise<Manifest> {
  return (await check(await apiFetch(`${API}/api/batches/${batchId}/expected-count`,
    json("PUT", { expected_ticket_count: expected })))).json();
}
export async function buildManifest(batchId: number): Promise<Manifest> {
  return (await check(await apiFetch(`${API}/api/batches/${batchId}/manifest/rebuild`, { method: "POST" }))).json();
}
export async function markSlotTicket(slotId: number): Promise<Manifest> {
  return (await check(await apiFetch(`${API}/api/scan-slots/${slotId}/mark-ticket`, { method: "POST" }))).json();
}
export async function confirmSlotBlank(slotId: number): Promise<Manifest> {
  return (await check(await apiFetch(`${API}/api/scan-slots/${slotId}/confirm-blank`, { method: "POST" }))).json();
}
export async function rotateSlot(slotId: number, rotation: number): Promise<Manifest> {
  return (await check(await apiFetch(`${API}/api/scan-slots/${slotId}/rotate`, json("POST", { rotation })))).json();
}

export async function getBatch(id: number): Promise<Batch> {
  return (await check(await apiFetch(`${API}/api/batches/${id}`))).json();
}

export async function getTickets(batchId: number): Promise<Ticket[]> {
  return (await check(await apiFetch(`${API}/api/batches/${batchId}/tickets`))).json();
}

export async function patchTicket(id: number, body: Partial<Record<string, string | number | null>>, revision?: string): Promise<Ticket> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (revision) headers["If-Match"] = revision;
  return (
    await check(
      await apiFetch(`${API}/api/tickets/${id}`, { method: "PATCH", headers, body: JSON.stringify(body) }),
    )
  ).json();
}

export async function ticketAction(id: number, action: "verify" | "flag" | "skip", revision?: string,
  extra?: { note?: string; confirm_duplicate?: boolean }): Promise<Ticket> {
  const headers: Record<string, string> = {};
  if (revision) headers["If-Match"] = revision;
  const init: RequestInit = { method: "POST", headers };
  if (extra) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(extra);
  }
  return (await check(await apiFetch(`${API}/api/tickets/${id}/${action}`, init))).json();
}

export async function retryBatch(id: number): Promise<void> {
  await check(await apiFetch(`${API}/api/batches/${id}/retry`, { method: "POST" }));
}

export const ticketImageUrl = (id: number) => `${API}/api/tickets/${id}/image`;
export const exportUrl = (batchId: number) => `${API}/api/batches/${batchId}/export.xlsx`;

const json = (method: string, body?: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: body === undefined ? undefined : JSON.stringify(body),
});

export type PilotReport = {
  project_id: number | null; unresolved_items: number;
  intake: Record<string, number>;
  review: Record<string, number | string>;
  reconciliation: Record<string, number | string | Record<string, number>>;
  batches_needing_attention: { batch_id: number; reasons: string[] }[];
  review_timing: {
    enabled: boolean;
    manual: { samples: number; median_seconds: string | null; with_corrections: number };
    extract: { samples: number; median_seconds: string | null; with_corrections: number };
  };
};
export async function getPilotReport(projectId?: string): Promise<PilotReport> {
  return (await check(await apiFetch(`${API}/api/reports/pilot${projectId ? `?project_id=${projectId}` : ""}`))).json();
}
export const pilotReportCsvUrl = (projectId?: string) => `${API}/api/reports/pilot.csv${projectId ? `?project_id=${projectId}` : ""}`;
export async function recordReviewTiming(ticketId: number, seconds: number) {
  await check(await apiFetch(`${API}/api/review-timings`, json("POST", { ticket_id: ticketId, seconds })));
}
export type SystemCheck = { name: string; state: "ok" | "off" | "attention" | "problem"; message: string; action: string | null };
export type SystemStatus = {
  healthy: boolean; checks: SystemCheck[];
  queue: { pending: number; running: number; failed_jobs: number; oldest_pending_seconds: number | null };
  worker: { running: boolean; last_seen_seconds: number | null };
  database: { kind: string; revision: string | null; expected_revision: string | null };
  invoice_reader: string;
};
export async function getSystemStatus(): Promise<SystemStatus> {
  return (await check(await apiFetch(`${API}/api/system/status`))).json();
}
export type Onboarding = { complete: boolean; steps: { key: string; title: string; detail: string; href: string; done: boolean }[] };
export async function getOnboarding(): Promise<Onboarding> {
  return (await check(await apiFetch(`${API}/api/onboarding`))).json();
}
export type RateSheetPlan = {
  applied: boolean; adds: number; changes: number; unchanged: number; new_suppliers: string[]; new_materials: string[]; scope: string;
  plan: { line: number; supplier: string; material: string; price: string; old_price: string | null; action: "add" | "change" | "unchanged"; new_supplier: boolean; new_material: boolean }[];
};
export async function importRateSheet(text: string, apply: boolean, scope: RateScope = { project_id: null, effective_from: null }): Promise<RateSheetPlan> {
  return (await check(await apiFetch(`${API}/api/rates/import`, json("POST", { text, apply, ...scope })))).json();
}
export async function getStatus(): Promise<{ extraction_configured: boolean; model: string; review_timing_enabled?: boolean; worker_running?: boolean }> {
  return (await check(await apiFetch(`${API}/api/status`))).json();
}
export async function listSuppliers(): Promise<Supplier[]> {
  return (await check(await apiFetch(`${API}/api/suppliers`))).json();
}
export async function listMaterials(): Promise<Material[]> {
  return (await check(await apiFetch(`${API}/api/materials`))).json();
}
export async function listRates(): Promise<Rate[]> {
  return (await check(await apiFetch(`${API}/api/rates`))).json();
}
export async function createSupplier(name: string, contact?: string) {
  await check(await apiFetch(`${API}/api/suppliers`, json("POST", { name, ...(contact ? { contact } : {}) })));
}
export async function createMaterial(name: string) {
  await check(await apiFetch(`${API}/api/materials`, json("POST", { name })));
}
export async function saveRate(supplier_id: number, material_id: number, unit_price: string, scope: RateScope = { project_id: null, effective_from: null },
  freight?: "included" | "separate" | null) {
  await check(await apiFetch(`${API}/api/rates`, json("PUT", { supplier_id, material_id, unit_price, ...scope, ...(freight !== undefined ? { freight } : {}) })));
}
export type PricingEventRow = {
  id: number;
  action: "supplier_added" | "supplier_removed" | "material_added" | "material_removed" | "rate_set" | "rate_changed" | "rate_validity_changed" | "rate_removed" | "freight_changed";
  supplier: string | null; material: string | null;
  old: string | null; new: string | null; // decimal strings, never parsed for money math
  scope: string | null; // which rate: "Company-wide", or a project and start date
  at: string; actor: string | null;
};
export type NameAliasRow = {
  id: number; kind: "supplier" | "material"; alias: string; target: string;
  added_by: string | null; added_at: string; removed_by: string | null; removed_at: string | null;
};
export async function listAliases(): Promise<NameAliasRow[]> {
  return (await check(await apiFetch(`${API}/api/name-aliases`))).json();
}
export async function addAlias(body: { kind: "supplier" | "material"; alias: string; target_id: number }) {
  await check(await apiFetch(`${API}/api/name-aliases`, json("POST", body)));
}
export async function removeAlias(id: number) {
  await check(await apiFetch(`${API}/api/name-aliases/${id}`, { method: "DELETE" }));
}
export async function getPricingHistory(): Promise<PricingEventRow[]> {
  return (await check(await apiFetch(`${API}/api/pricing/history`))).json();
}
export async function deleteItem(kind: "suppliers" | "materials" | "rates", id: number) {
  await check(await apiFetch(`${API}/api/${kind}/${id}`, { method: "DELETE" }));
}

export type Filters = { project_id?: string; supplier?: string; material?: string; status?: string; date_from?: string; date_to?: string; q?: string };
const qs = (f: Filters) => {
  const p = new URLSearchParams();
  Object.entries(f).forEach(([k, v]) => v && p.set(k, v));
  const t = p.toString();
  return t ? `?${t}` : "";
};
export async function listProjects(includeArchived = false): Promise<Project[]> {
  return (await check(await apiFetch(`${API}/api/projects?include_archived=${includeArchived}`))).json();
}
export async function createProject(body: { code: string; name: string; job_location?: string; description?: string }): Promise<Project> {
  return (await check(await apiFetch(`${API}/api/projects`, json("POST", body)))).json();
}
/** A project with what it holds. Costs are approved figures only; `unpriced_tickets` are approved with no rate. */
export type ProjectSummary = Project & {
  created_at: string; tickets: number; open_tickets: number; open_shifts: number; verified_tickets: number;
  verified_tons: string; material_cost: string; unpriced_tickets: number; trucking_cost: string; verified_cost: string;
  last_activity: string | null;
};
export type QueueKind = "tickets" | "failed_files" | "truck_shifts" | "invoice_lines";
export type QueueItem = {
  kind: QueueKind; id: number; title: string; detail: string; count: number; attention: boolean;
  project: { id: number; code: string; name: string } | null; created_at: string | null; href: string;
};
export type ReviewQueue = { summary: Record<QueueKind | "total", number>; items: QueueItem[] };
/** What's waiting for a person, oldest first. Each item links to where the work is done. */
export async function getReviewQueue(projectId?: string, kind?: QueueKind): Promise<ReviewQueue> {
  const params = new URLSearchParams();
  if (projectId) params.set("project_id", projectId);
  if (kind) params.set("kind", kind);
  const query = params.toString();
  return (await check(await apiFetch(`${API}/api/review-queue${query ? `?${query}` : ""}`))).json();
}
export type CostRow = { name: string; tickets: number; tons: string; cost: string; unpriced: number };
export type HaulerCost = { name: string; shifts: number; hours: string; cost: string; loads: number; per_hour: string | null; deliveries?: number };
export type ProjectCost = { id: number; code: string; name: string; status: string; tickets: number; tons: string; material: string; unpriced: number; hours: string; trucking: string; total: string };
export type UnitPrice = { supplier: string; material: string; tons: string; cost: string; average: string; low: string | null; high: string | null };
export type PeriodRow = { period: string; tickets: number; tons: string; material: string; unpriced: number; hours: string; trucking: string; loads: number; total: string };
/** Approved figures only. `unpriced` counts approved tickets with no rate: their cost is missing, not zero. */
export type CostReport = {
  period: "day" | "week" | "month"; date_from: string | null; date_to: string | null;
  material: { tickets: number; tons: string; cost: string; unpriced: number; waiting: number; undated: { tickets: number; tons: string; cost: string; unpriced: number };
    by_material: CostRow[]; by_supplier: CostRow[] };
  trucking: { shifts: number; hours: string; cost: string; loads: number; by_hauler: HaulerCost[]; billing_methods: { method: string; cost: string }[] };
  costs: { material: string; trucking: string; total: string; complete: boolean; unpriced: number };
  by_period: PeriodRow[]; by_project: ProjectCost[]; unit_prices: UnitPrice[];
  invoices: { invoices: number; billed: string; matched: number; with_differences: number; incomplete: number; difference: string; totals_only: number; rows: RegisterInvoice[] };
};
export type CostReportQuery = { project_id?: string; date_from?: string; date_to?: string; period: "day" | "week" | "month" };
const reportParams = (q: CostReportQuery) => new URLSearchParams(Object.entries(q).filter(([, v]) => v) as [string, string][]).toString();
export async function getCostReport(q: CostReportQuery): Promise<CostReport> {
  return (await check(await apiFetch(`${API}/api/reports/summary?${reportParams(q)}`))).json();
}
export const costReportXlsxUrl = (q: CostReportQuery) => `${API}/api/reports/summary.xlsx?${reportParams(q)}`;
export async function getProjectDirectory(): Promise<ProjectSummary[]> {
  return (await check(await apiFetch(`${API}/api/projects/directory`))).json();
}
export async function updateProject(id: number, body: { name?: string; job_location?: string | null; description?: string | null }): Promise<Project> {
  return (await check(await apiFetch(`${API}/api/projects/${id}`, json("PATCH", body)))).json();
}
export async function setProjectStatus(id: number, status: "active" | "archived") {
  await check(await apiFetch(`${API}/api/projects/${id}`, json("PATCH", { status })));
}
export async function getRecords(f: Filters): Promise<Ticket[]> {
  return (await check(await apiFetch(`${API}/api/records${qs(f)}`))).json();
}
/** With an open saved view, `viewId` also applies the view's own column names. */
export const recordsExportUrl = (f: Filters, format: "xlsx" | "csv" = "xlsx", columns?: string[], viewId?: number) => {
  const params = new URLSearchParams();
  Object.entries(f).forEach(([k, v]) => v && params.set(k, v));
  if (viewId) params.set("view_id", String(viewId));
  if (columns?.length) params.set("columns", columns.join(","));
  const query = params.toString().replaceAll("%2C", ",");
  return `${API}/api/records/export.${format}${query ? `?${query}` : ""}`;
};
export type RecordsSort = "date" | "ticket_number" | "tons" | "supplier" | "material" | "status" | "job";
export type RecordsPage = {
  rows: Ticket[]; total: number; page: number; page_size: number;
  summary: { records: number; by_status: Record<string, number>; verified_tons: string; verified_cost: string; verified_unpriced: number };
};
export async function searchRecords(f: Filters, options: { page: number; pageSize: number; sort: RecordsSort; direction: "asc" | "desc" }): Promise<RecordsPage> {
  const params = new URLSearchParams();
  Object.entries(f).forEach(([k, v]) => v && params.set(k, v));
  params.set("page", String(options.page));
  params.set("page_size", String(options.pageSize));
  params.set("sort", options.sort);
  params.set("direction", options.direction);
  return (await check(await apiFetch(`${API}/api/records/search?${params}`))).json();
}
export type BulkResult = { changed: number; unchanged: number; left_alone: Record<string, number> };
export async function bulkEditTickets(ticketIds: number[], field: string, value: string): Promise<BulkResult> {
  return (await check(await apiFetch(`${API}/api/tickets/bulk-edit`, json("POST", { ticket_ids: ticketIds, field, value })))).json();
}
export async function renameOnTickets(field: "supplier" | "material_type", fromValue: string, toValue: string): Promise<BulkResult> {
  return (await check(await apiFetch(`${API}/api/records/rename`, json("POST", { field, from_value: fromValue, to_value: toValue })))).json();
}
export type NameVariant = {
  value: string; state: "catalog" | "same_name" | "alias" | "unknown"; target: string | null;
  tickets: number; verified: number; unverified: number;
};
export async function getNameVariants(field: "supplier" | "material"): Promise<{ field: string; catalog: string[]; rows: NameVariant[] }> {
  return (await check(await apiFetch(`${API}/api/records/name-variants?field=${field}`))).json();
}
/** `labels` names columns to match a team's own spreadsheet headers, by column key. */
export type SavedView = { id: number; name: string; filters: Filters; columns: string[]; labels?: Record<string, string>; created_by: string | null; created_at: string };
export async function listSavedViews(): Promise<SavedView[]> {
  return (await check(await apiFetch(`${API}/api/saved-views`))).json();
}
export async function createSavedView(name: string, filters: Filters, columns: string[], labels: Record<string, string> = {}): Promise<SavedView> {
  const kept = Object.fromEntries(Object.entries(filters).filter(([, v]) => v));
  const named = Object.fromEntries(Object.entries(labels).map(([key, text]) => [key, text.trim()]).filter(([key, text]) => text && columns.includes(key)));
  const body = Object.keys(named).length ? { name, filters: kept, columns, labels: named } : { name, filters: kept, columns };
  return (await check(await apiFetch(`${API}/api/saved-views`, json("POST", body)))).json();
}
export async function deleteSavedView(id: number) {
  await check(await apiFetch(`${API}/api/saved-views/${id}`, { method: "DELETE" }));
}
export async function moveBatch(batchId: number, projectId: number): Promise<Batch> {
  return (await check(await apiFetch(`${API}/api/batches/${batchId}/project`, json("PATCH", { project_id: projectId })))).json();
}
export type ReportGroup = {
  tickets: number; verified_tickets: number; verified_tons: string; cost: string; unpriced: number;
  pending_tickets: number; pending_tons: string;
};
export type ProjectReport = {
  project: { id: number; code: string; name: string; status: string };
  first_delivery: string | null; last_delivery: string | null;
  totals: ReportGroup;
  by_supplier_material: (ReportGroup & { supplier: string; material: string })[];
  by_month: (ReportGroup & { month: string })[];
  invoices: {
    billed_total: string; verified_total: string; lines_unresolved: number; by_status: Record<string, number>;
    lines: {
      id: number; invoice_number: string; supplier: string; material: string; date_from: string; date_to: string;
      status: string; comparison_mode: string; billed_tons: string; verified_tons: string; tons_difference: string;
      billed_subtotal: string; verified_subtotal: string | null; subtotal_difference: string | null;
    }[];
  };
};
export async function getProjectReport(id: string): Promise<ProjectReport> {
  return (await check(await apiFetch(`${API}/api/reports/project/${id}`))).json();
}
export const projectReportUrl = (id: string) => `${API}/api/reports/project/${id}/report.xlsx`;

/** An endpoint this server doesn't have yet answers with the framework's own 404. */
const missingEndpoint = (error: unknown) => error instanceof ApiError && error.status === 404 && error.message === "Not Found";

// Approved deliveries no invoice line lists or covers. Rows stop at 500; totals cover everything.
export type UnbilledRow = {
  ticket_id: number; batch_id: number; ticket_number: string | null; delivery_date: string | null;
  supplier: string | null; material: string | null; tons: string | null; cost: string | null;
};
export type UnbilledGap = { supplier: string; material: string; date_from: string; date_to: string; tickets: number; tons: string };
export type UnbilledReport = {
  project: { id: number; code: string; name: string };
  total_tickets: number; total_tons: string; total_cost: string | null;
  rows: UnbilledRow[]; truncated: boolean; gaps: UnbilledGap[];
};
/** Null when this server doesn't offer the check yet. */
export async function getUnbilled(projectId: string): Promise<UnbilledReport | null> {
  try {
    return await (await check(await apiFetch(`${API}/api/reports/project/${projectId}/unbilled`))).json();
  } catch (error) {
    if (missingEndpoint(error)) return null;
    throw error;
  }
}
export const unbilledCsvUrl = (projectId: string) => `${API}/api/reports/project/${projectId}/unbilled.csv`;
export const discrepanciesCsvUrl = (filters: { project_id?: string; supplier?: string }) => {
  const params = new URLSearchParams(Object.entries(filters).filter(([, value]) => value) as [string, string][]);
  const query = params.toString();
  return `${API}/api/invoice-comparisons/discrepancies.csv${query ? `?${query}` : ""}`;
};

/** Fetch a file and hand it to the browser, so a refusal shows as a message, not an error page. */
export async function downloadFile(url: string, filename: string): Promise<void> {
  let response: Response;
  try {
    response = await check(await apiFetch(url));
  } catch (error) {
    if (missingEndpoint(error)) throw new ApiError("This server doesn't offer that download yet. It arrives with the next server update.", "missing_endpoint", { status: 404 });
    throw error;
  }
  const link = document.createElement("a");
  const href = URL.createObjectURL(await response.blob());
  try {
    link.href = href;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
  } finally {
    setTimeout(() => URL.revokeObjectURL(href), 1000);
  }
}

export type Group = { name: string; tickets: number; tons: string | number; cost: string | number };
export type Dashboard = {
  tickets: number; needs_review: number; flagged: number; verified: number; total_tons: string | number; total_cost: string | number;
  pending_tons: string | number; unpriced_tickets: number; pending_cost_tickets: number;
  by_supplier: Group[]; by_material: Group[];
  recent_batches: { id: number; status: string; created_at: string; files: number; project: string | null }[];
  // Newer servers only.
  failed?: number;
  projects?: { id: number; code: string; name: string; tickets: number; open: number; verified_tons: string; last_upload: string | null }[];
  activity?: { at: string; action: "approved" | "flagged" | "skipped"; ticket_id: number; ticket_number: string | null; batch_id: number; by: string | null }[];
  // Approved hourly trucking, apart from material cost. Per project through each shift's allocations.
  hauling?: { approved_cost: string; approved_hours: string; approved_with_material: string; awaiting_approval: number; disputed: number };
};
export async function getDashboard(projectId?: string): Promise<Dashboard> {
  return (await check(await apiFetch(`${API}/api/dashboard${projectId ? `?project_id=${projectId}` : ""}`))).json();
}


// A supplier invoice LINE, manually entered and dynamically reconciled against
// approved tickets. Comparison status is NOT payment approval.
export type InvoiceLineInput = {
  project_id: number;
  invoice_number: string;
  supplier_name: string;
  material_name: string;
  date_from: string;
  date_to: string;
  billed_tons: string;
  billed_unit_price: string;
  billed_subtotal: string;
  // Optional: the invoice's ticket numbers, one per row, each optionally
  // followed by billed tons (pasted from Excel). Empty = totals-only.
  ticket_list: string;
};
export type CheckedTicket = {
  ticket_id: number; batch_id: number; ticket_number: string | null;
  delivery_date: string | null; supplier: string | null; material: string | null;
  tons: string | null; review_status: string; project: string | null;
};
export type TicketCheckResult =
  | "matched" | "tons_differ" | "not_found" | "outside_scope"
  | "ambiguous" | "needs_review" | "not_on_invoice";
export type TicketCheckRow = {
  result: TicketCheckResult;
  line: number | null; // null = a delivery the invoice does not list
  invoice_ticket_number: string | null;
  billed_tons: string | null;
  ticket: CheckedTicket | null;
  candidates: CheckedTicket[];
  verified_tons: string | null;
  tons_difference: string | null;
  flags: ("duplicate_on_invoice" | "on_another_invoice")[];
  notes: string[];
  evidence?: "unmatched" | "candidate" | "confirmed" | "conflicting" | "invalidated";
  link_id?: number | null;
};
export type EvidenceSummary = {
  listed_tickets: number; confirmed: number; candidates: number; conflicting: number;
  invalidated: number; unmatched: number; confirmed_tons: string;
};
export type InvoiceTicketMatch = {
  ticket_id: number; batch_id: number; ticket_number: string | null;
  tons: string | null; confirmed_price: string | null;
  review_status: string; scope_match: "exact" | "unclassified";
};
export type TonsBridge = {
  start: string; billed: string; complete: boolean; rows_without_tons: number; unexplained: string | null;
  items: { kind: string; label: string; tons: string; count: number; tickets: string[] }[];
};
export type MoneyBridge = {
  start: string; quantity_effect: string; rate_effect: string; arithmetic_effect: string; rounding: string; billed: string;
  rate_groups: { locked_rate: string; tickets: number; tons: string; effect: string }[];
};
export type InvoiceDiagnosis = {
  headline: string; tons: TonsBridge | null; money: MoneyBridge | null; money_unresolved_reason: string | null;
  next_steps: { text: string; href: string | null }[];
};
export type InvoiceOverview = {
  lines: number; by_status: Record<string, number>; billed: string; verified: string; lines_unresolved: number;
  by_supplier: { supplier: string; lines: number; billed: string; verified: string; lines_unresolved: number; open: number }[];
  exceptions: { kind: string; title: string; detail: string; line_ids: number[] }[];
};
/** One row per supplier or trucking invoice. `compared` is null while any part can't be priced. */
export type RegisterInvoice = {
  kind: "supplier" | "trucking"; id: number; invoice_number: string; company: string | null;
  project: { id: number; code: string; name: string } | null; period_from: string; period_to: string; lines: number;
  billed: string; compared: string | null; difference: string | null; status: "matched" | "discrepancy" | "incomplete";
  ticket_level: boolean; has_document: boolean; href: string;
};
export type InvoiceRegister = { totals: { invoices: number; billed: string; with_differences: number; incomplete: number }; invoices: RegisterInvoice[] };
export async function getInvoiceRegister(projectId?: string): Promise<InvoiceRegister> {
  return (await check(await apiFetch(`${API}/api/invoice-comparisons/register${projectId ? `?project_id=${projectId}` : ""}`))).json();
}
export async function getInvoiceOverview(): Promise<InvoiceOverview> {
  return (await check(await apiFetch(API + "/api/invoice-comparisons/overview"))).json();
}
export type InvoiceLineComparison = InvoiceLineInput & {
  id: number;
  revision?: string;
  diagnosis?: InvoiceDiagnosis | null;
  billed_extension: string;
  invoice_arithmetic_difference: string;
  verified_tons: string;
  verified_subtotal: string | null;
  quantity_difference: string;
  subtotal_difference: string | null;
  verified_count: number;
  pending_count: number;
  unclassified_count: number;
  unpriced_count: number;
  rate_mismatch_count: number;
  status: "matches" | "discrepancy" | "incomplete" | "no_verified_tickets";
  comparison_mode: "itemized" | "totals_only";
  warnings: string[];
  tickets: InvoiceTicketMatch[];
  ticket_check: TicketCheckRow[];
  ticket_check_counts: Record<string, number>;
  itemized_billed_tons: string | null;
  evidence?: EvidenceSummary | null;
  source_document_id?: number | null;
  source_page?: number | null;
  created_at: string;
};
// Uploaded invoice PDFs and their drafts. A draft counts for nothing until confirmed.
export type InvoiceDraftLine = {
  id: number; revision?: string; page: number; source_text: string | null; state: "pending_review" | "confirmed" | "rejected";
  project_id: number | null; invoice_number: string | null; supplier_name: string | null; material_name: string | null;
  billed_quantity: string | null; unit: string | null; billed_unit_price: string | null; billed_subtotal: string | null;
  date_from: string | null; date_to: string | null; ticket_list: string;
  comparison_id: number | null; blockers: string[]; warnings: string[];
};
export type InvoiceDocument = {
  id: number; project_id: number | null; original_name: string; sha256: string; page_count: number;
  status: "needs_review" | "unsupported" | "failed" | "voided"; extractor: string; message: string | null;
  unread_rows: string[]; created_at: string; lines: InvoiceDraftLine[];
};
const DOCS = `${API}/api/invoice-documents`;
export const invoicePageUrl = (id: number, page: number) => `${DOCS}/${id}/pages/${page}/image`;
export const invoiceOriginalUrl = (id: number) => `${DOCS}/${id}/original`;
export async function listInvoiceDocuments(): Promise<InvoiceDocument[]> {
  return (await check(await apiFetch(DOCS))).json();
}
export async function getInvoiceDocument(id: number): Promise<InvoiceDocument> {
  return (await check(await apiFetch(`${DOCS}/${id}`))).json();
}
export async function uploadInvoiceDocument(file: File, projectId?: number): Promise<InvoiceDocument> {
  const form = new FormData();
  form.append("file", file);
  if (projectId !== undefined) form.append("project_id", String(projectId));
  return (await check(await apiFetch(DOCS, { method: "POST", body: form }))).json();
}
export async function addDraftLine(id: number, page: number) {
  await check(await apiFetch(`${DOCS}/${id}/lines?page=${page}`, { method: "POST" }));
}
// The revision the person was editing: the server refuses the save if someone else saved first.
export async function saveDraftLine(id: number, lineId: number, body: Record<string, string | number | null>, revision?: string) {
  const init = json("PATCH", body);
  if (revision) (init.headers as Record<string, string>)["If-Match"] = revision;
  await check(await apiFetch(`${DOCS}/${id}/lines/${lineId}`, init));
}
export async function confirmDraftLine(id: number, lineId: number, revision?: string) {
  await check(await apiFetch(`${DOCS}/${id}/lines/${lineId}/confirm`, { method: "POST", headers: revision ? { "If-Match": revision } : {} }));
}
export async function rejectDraftLine(id: number, lineId: number) {
  await check(await apiFetch(`${DOCS}/${id}/lines/${lineId}/reject`, { method: "POST" }));
}
export async function voidInvoiceDocument(id: number) {
  await check(await apiFetch(`${DOCS}/${id}/void`, { method: "POST" }));
}

export async function getInvoiceComparisons(): Promise<InvoiceLineComparison[]> {
  return (await check(await apiFetch(API + "/api/invoice-comparisons?summary=true"))).json();
}
export async function getInvoiceComparison(id: number): Promise<InvoiceLineComparison> {
  return (await check(await apiFetch(API + "/api/invoice-comparisons/" + id))).json();
}
export async function saveInvoiceComparison(
  body: InvoiceLineInput, id?: number, revision?: string,
): Promise<InvoiceLineComparison> {
  const suffix = id !== undefined ? "/" + id : "";
  const init = json(id !== undefined ? "PUT" : "POST", body);
  if (revision) (init.headers as Record<string, string>)["If-Match"] = revision;
  return (await check(await apiFetch(API + "/api/invoice-comparisons" + suffix, init))).json();
}
export async function confirmTicketLinks(id: number, ticketIds: number[]): Promise<InvoiceLineComparison> {
  return (await check(await apiFetch(API + "/api/invoice-comparisons/" + id + "/ticket-links",
    json("POST", { ticket_ids: ticketIds })))).json();
}
export async function releaseTicketLink(id: number, linkId: number): Promise<InvoiceLineComparison> {
  return (await check(await apiFetch(API + "/api/invoice-comparisons/" + id + "/ticket-links/" + linkId + "/release",
    { method: "POST" }))).json();
}
export async function getInvoiceHistory(id: number): Promise<HistoryEntry[]> {
  return (await check(await apiFetch(API + "/api/invoice-comparisons/" + id + "/audit"))).json();
}


/** Download a tenant-scoped, non-payable invoice reconciliation snapshot. */
export async function downloadInvoiceComparisonReport(id: number): Promise<void> {
  const response = await check(await apiFetch(
    API + "/api/invoice-comparisons/" + id + "/report.csv",
  ));
  const url = URL.createObjectURL(await response.blob());
  try {
    const link = document.createElement("a");
    link.href = url;
    link.download = "quarvera-invoice-" + id + ".csv";
    document.body.appendChild(link);
    link.click();
    link.remove();
  } finally {
    // Allow the browser to finish initiating the download before releasing it.
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}


// ---- Team administration (owner only; enforced by the server) ----
export type TeamMember = {
  id: number;
  username: string;
  display_name: string | null;
  role: Role;
  role_label: string;
  active: boolean;
  must_change_password: boolean;
  created_at: string | null;
  is_you: boolean;
};
export type IssuedPassword = { member: TeamMember; temporary_password: string };
export type AccountEventRow = {
  id: number; action: string; old: string | null; new: string | null; at: string;
  actor: string | null; actor_kind: "person" | "terminal"; target: string | null;
};

export async function listTeam(): Promise<TeamMember[]> {
  return (await check(await apiFetch(`${API}/api/team/members`))).json();
}
export async function addTeamMember(body: { username: string; display_name?: string; role: Role }): Promise<IssuedPassword> {
  return (await check(await apiFetch(`${API}/api/team/members`, json("POST", body)))).json();
}
export async function updateTeamMember(id: number, body: { role?: Role; active?: boolean; display_name?: string | null }): Promise<TeamMember> {
  return (await check(await apiFetch(`${API}/api/team/members/${id}`, json("PATCH", body)))).json();
}
export async function resetTeamMemberPassword(id: number): Promise<IssuedPassword> {
  return (await check(await apiFetch(`${API}/api/team/members/${id}/reset-password`, { method: "POST" }))).json();
}
export async function listAccountEvents(): Promise<AccountEventRow[]> {
  return (await check(await apiFetch(`${API}/api/team/events`))).json();
}

// ---------------------------------------------------------------- invoice exceptions center
export type ExceptionCategory = "rate" | "quantity" | "duplicate" | "missing_ticket" | "not_invoiced" | "unmatched_line"
  | "needs_review" | "invoice_arithmetic";
export type ExceptionStatus = "open" | "investigating" | "disputed" | "recovered" | "resolved" | "dismissed";
export type ExceptionSeverity = "high" | "medium" | "low";
// Money and tons are decimal strings from the server: shown, never added up here.
export type BillingException = {
  key: string; category: ExceptionCategory; category_label: string; severity: ExceptionSeverity;
  title: string; explanation: string; project_id: number | null; project: string | null;
  supplier: string; material: string | null; invoice_numbers: string[]; line_ids: number[];
  ticket_number: string | null; ticket_ids: number[]; billed_tons: string | null; approved_tons: string | null;
  amount: string | null; line_billed: string | null; line_approved: string | null;
  unbilled_tickets: number | null; detected: boolean; shift_id?: number | null; hauling_invoice_id?: number | null;
  status: ExceptionStatus; assignee_user_id: number | null; assignee: string | null;
  recovered_amount: string | null; note: string | null; revision: string; updated_at: string | null; updated_by: string | null;
  // Closed by a person, then its figures changed: it needs a person again. Their status is kept.
  changed_since_closed: boolean; needs_attention: boolean;
};
export type ExceptionsSummary = {
  open: number; by_category: Record<ExceptionCategory, number>; by_severity: Record<ExceptionSeverity, number>;
  potential_overcharge: string; billed_below_approved: string; disputed_amount: string; recovered: string;
  money_unknown: number; unassigned: number; changed_since_closed: number;
};
export type ClearedException = { key: string; title: string; status: ExceptionStatus; recovered_amount: string | null; note: string | null; assignee: string | null; updated_at: string };
export type ExceptionsPage = {
  summary: ExceptionsSummary; items: BillingException[]; cleared: ClearedException[];
  categories: Record<ExceptionCategory, string>; people: { id: number; name: string }[];
  projects: { id: number; code: string }[]; can_manage: boolean; note: string;
};
export type ExceptionFilters = { project_id?: string; category?: string; severity?: string; status?: string; assignee?: string; supplier?: string };
export type ExceptionEventRow = { field: string; old: string | null; new: string | null; at: string; actor: string | null };

const query = (filters: Record<string, string | undefined>) => {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) if (value) params.set(key, value);
  const text = params.toString();
  return text ? `?${text}` : "";
};

export async function listExceptions(filters: ExceptionFilters = {}): Promise<ExceptionsPage> {
  return (await check(await apiFetch(`${API}/api/exceptions${query(filters)}`))).json();
}
export async function reviewException(key: string, revision: string, changes: {
  status?: ExceptionStatus; assignee_user_id?: number | null; recovered_amount?: string; note?: string;
}): Promise<BillingException> {
  const init = json("POST", { key, ...changes });
  (init.headers as Record<string, string>)["If-Match"] = revision;
  return (await check(await apiFetch(`${API}/api/exceptions/review`, init))).json();
}
export async function exceptionHistory(key: string): Promise<ExceptionEventRow[]> {
  return (await check(await apiFetch(`${API}/api/exceptions/history${query({ key })}`))).json();
}
export const exceptionsCsvUrl = (filters: ExceptionFilters = {}) => `${API}/api/exceptions.csv${query({ project_id: filters.project_id, status: filters.status })}`;

export type RatePreview = {
  scope: string; tickets: number; newly_priced: number; cost_change: string; truncated: boolean; note: string;
  rows: { ticket_id: number; batch_id: number; ticket_number: string | null; delivery_date: string | null; project: string | null;
    tons: string | null; price_now: string | null; price_after: string | null; cost_change: string | null }[];
};
export async function previewRate(supplier_id: number, material_id: number, unit_price: string, scope: RateScope): Promise<RatePreview> {
  return (await check(await apiFetch(`${API}/api/rates/preview`, json("POST", { supplier_id, material_id, unit_price, ...scope })))).json();
}

// ---------------------------------------------------------------- hourly trucking
// Hours and money are decimal strings from the server: shown, never added up here.
export const EQUIPMENT_LABEL: Record<string, string> = {
  tandem: "Tandem", tri_axle: "Tri-axle", quad_axle: "Quad-axle", semi_end_dump: "Semi end dump", belly_dump: "Belly dump", other: "Other",
};
export type HaulerRow = { id: number; name: string; contact: string | null; trucks: { id: number; identifier: string; equipment_type: string; active: boolean }[] };
export type HaulingRate = {
  id: number; hauler_id: number; project_id: number | null; equipment_type: string | null;
  billing_method?: "hourly" | "per_ton" | "per_load"; hourly_rate: string | null; unit_rate?: string | null;
  minimum_hours: string | null; standby_rate: string | null; effective_from: string | null; effective_to: string | null;
  billing_increment: string | null; increment_rounding: "up" | "nearest" | null;
};
export type TruckShift = {
  id: number; revision: string; status: "needs_review" | "approved" | "disputed";
  project_id: number; project: string | null; work_date: string; truck_id: number; truck: string; equipment_type: string;
  hauler_id: number; hauler: string; driver_name: string | null; start_at: string; end_at: string;
  unpaid_break_minutes: number; standby_hours: string; claimed_hours: string | null; loads: number | null;
  field_signoff: string | null; cost_code: string | null; notes: string | null;
  on_site_hours: string | null; billed_time_hours?: string | null; work_hours: string | null;
  source?: "manual" | "extracted"; timesheet?: { name: string } | null;
  rate: { id: number; hourly_rate: string; standby_rate: string | null; minimum_hours: string | null } | null;
  cost_lines: { label: string; hours: string; rate: string; amount: string }[];
  cost: string | null; cost_is_locked: boolean;
  approved: { work_hours: string; standby_hours: string; billable_hours: string; hourly_rate: string; standby_rate: string | null;
    minimum_hours: string | null; by: string | null; at: string } | null;
  flags: string[];
  deliveries: { count: number; tons: string | null; ticket_ids: number[]; cost_per_ton: string | null };
};
export type ShiftsPage = {
  shifts: TruckShift[]; truncated: boolean;
  summary: { shifts: number; awaiting_approval: number; disputed: number; approved_hours: string; approved_cost: string; loads: number; with_flags: number };
};
export type ShiftInput = {
  project_id: number; truck_id: number; start_at: string; end_at: string; unpaid_break_minutes: number; standby_hours: string;
  claimed_hours: string | null; loads: number | null; driver_name: string | null; field_signoff: string | null;
  cost_code: string | null; notes: string | null;
};
export type ShiftFilters = { project_id?: string; hauler_id?: string; status?: string; date_from?: string; date_to?: string };

const withRevision = (init: RequestInit, revision: string): RequestInit => {
  (init.headers as Record<string, string>)["If-Match"] = revision;
  return init;
};
export async function listHaulers(): Promise<HaulerRow[]> {
  return (await check(await apiFetch(`${API}/api/haulers`))).json();
}
export async function createHauler(name: string, contact?: string): Promise<void> {
  await check(await apiFetch(`${API}/api/haulers`, json("POST", { name, ...(contact ? { contact } : {}) })));
}
export async function createTruck(hauler_id: number, identifier: string, equipment_type: string): Promise<void> {
  await check(await apiFetch(`${API}/api/trucks`, json("POST", { hauler_id, identifier, equipment_type })));
}
export async function listHaulingRates(): Promise<HaulingRate[]> {
  return (await check(await apiFetch(`${API}/api/hauling-rates`))).json();
}
export async function createHaulingRate(body: Omit<HaulingRate, "id">): Promise<void> {
  await check(await apiFetch(`${API}/api/hauling-rates`, json("POST", body)));
}
export async function deleteHaulingRate(id: number): Promise<void> {
  await check(await apiFetch(`${API}/api/hauling-rates/${id}`, { method: "DELETE" }));
}
export async function listShifts(filters: ShiftFilters = {}): Promise<ShiftsPage> {
  const params = new URLSearchParams();
  Object.entries(filters).forEach(([k, v]) => v && params.set(k, v));
  return (await check(await apiFetch(`${API}/api/truck-shifts${params.toString() ? "?" + params : ""}`))).json();
}
export async function createShift(body: ShiftInput): Promise<TruckShift> {
  return (await check(await apiFetch(`${API}/api/truck-shifts`, json("POST", body)))).json();
}
export async function editShift(id: number, revision: string, body: ShiftInput): Promise<TruckShift> {
  return (await check(await apiFetch(`${API}/api/truck-shifts/${id}`, withRevision(json("PUT", body), revision)))).json();
}
export async function approveShift(id: number, revision: string, body: {
  work_hours: string; standby_hours: string; allocations?: { project_id: number; cost_code: string | null; hours: string }[];
}): Promise<TruckShift> {
  return (await check(await apiFetch(`${API}/api/truck-shifts/${id}/approve`, withRevision(json("POST", body), revision)))).json();
}
export async function reopenShift(id: number, revision: string, reason: string): Promise<TruckShift> {
  return (await check(await apiFetch(`${API}/api/truck-shifts/${id}/reopen`, withRevision(json("POST", { reason }), revision)))).json();
}
export async function linkShiftDeliveries(id: number, ticket_ids: number[]): Promise<TruckShift> {
  return (await check(await apiFetch(`${API}/api/truck-shifts/${id}/deliveries`, json("PUT", { ticket_ids })))).json();
}
export async function shiftHistory(id: number): Promise<{ field: string; old: string | null; new: string | null; at: string; actor: string | null }[]> {
  return (await check(await apiFetch(`${API}/api/truck-shifts/${id}/history`))).json();
}

export async function disputeShift(id: number, revision: string, status: "disputed" | "needs_review", reason: string): Promise<TruckShift> {
  return (await check(await apiFetch(`${API}/api/truck-shifts/${id}/dispute`, withRevision(json("POST", { status, reason }), revision)))).json();
}
async function upload(url: string, file: File) {
  const form = new FormData();
  form.append("file", file);
  return (await check(await apiFetch(url, { method: "POST", body: form }))).json();
}
export const attachTimesheet = (id: number, file: File): Promise<TruckShift> => upload(`${API}/api/truck-shifts/${id}/timesheet`, file);
export const timesheetUrl = (id: number) => `${API}/api/truck-shifts/${id}/timesheet`;

export type HaulingInvoiceLine = {
  line_no?: number; kind: "hours" | "standby" | "minimum" | "fee" | "credit"; truck_identifier: string | null; work_date: string | null;
  hours: string | null; rate: string | null; amount: string; description: string | null;
  result?: "matched" | "differs" | "repeat" | "no_shift" | "not_approved" | "ambiguous" | "fee" | "credit" | null;
  shift_id?: number | null; supported?: string | null; difference?: string | null;
};
export type HaulingInvoice = {
  id: number; revision: string; hauler_id: number; hauler: string | null; project_id: number | null; project: string | null;
  invoice_number: string; invoice_date: string | null; period_from: string; period_to: string; document: { name: string } | null;
  billed: string; supported: string | null; difference: string | null; exceptions: number; lines: HaulingInvoiceLine[];
};
export type HaulingInvoiceInput = Pick<HaulingInvoice, "hauler_id" | "project_id" | "invoice_number" | "invoice_date" | "period_from" | "period_to"> & {
  lines: HaulingInvoiceLine[];
};
export async function listHaulingInvoices(): Promise<HaulingInvoice[]> {
  return (await check(await apiFetch(`${API}/api/hauling-invoices`))).json();
}
export async function saveHaulingInvoice(body: HaulingInvoiceInput, existing?: { id: number; revision: string }): Promise<HaulingInvoice> {
  const init = json(existing ? "PUT" : "POST", body);
  return (await check(await apiFetch(`${API}/api/hauling-invoices${existing ? "/" + existing.id : ""}`, existing ? withRevision(init, existing.revision) : init))).json();
}
export const attachHaulingInvoice = (id: number, file: File): Promise<HaulingInvoice> => upload(`${API}/api/hauling-invoices/${id}/document`, file);
export const haulingInvoiceUrl = (id: number) => `${API}/api/hauling-invoices/${id}/document`;
