export const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// All APIs share the same revocable HTTP-only session cookie. A custom request
// header protects state-changing endpoints against cross-site form requests.
async function apiFetch(url: string, init: RequestInit = {}): Promise<Response> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    headers.set("X-TicketFlow-Request", "1");
  }
  return fetch(url, { ...init, headers, credentials: "include", cache: "no-store" });
}

export type Role = "owner" | "manager" | "clerk" | "viewer";
export type Permission =
  | "records.read" | "tickets.upload" | "tickets.edit" | "tickets.flag" | "tickets.verify"
  | "tickets.skip" | "tickets.price_override" | "projects.manage" | "pricing.manage"
  | "invoices.manage" | "team.manage" | "scans.resolve" | "scans.confirm_blank" | "system.read";
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

export async function logOut(): Promise<void> {
  await check(await apiFetch(`${API}/api/auth/logout`, { method: "POST" }));
}

export type Project = { id: number; code: string; name: string; job_location: string | null; description: string | null; status: string };
export type FileOut = { id: number; original_name: string; status: string; error: string | null };
export type Batch = { id: number; project_id: number | null; expected_ticket_count?: number | null; layout: string; intake_mode?: "extract" | "manual"; status: string; created_at: string; files: FileOut[] };
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
  flags: string[];
  status: "needs_review" | "verified" | "flagged" | "skipped" | "failed";
  tonnage_confirmed: boolean;
  error: string | null;
  unit_price_override: string | null;
  price_snapshot_state: "pending" | "priced" | "unpriced" | null;
  unit_price: string | null;
  price_source: "rate" | "override" | null;
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
export type Rate = { id: number; supplier_id: number; material_id: number; unit_price: string };

/** An API refusal. `code` is set when the UI should react, not just show text. */
export class ApiError extends Error {
  code?: string;
  constructor(message: string, code?: string) {
    super(message);
    this.code = code;
  }
}

async function check(res: Response) {
  if (!res.ok) {
    let detail = res.statusText;
    let code: string | undefined;
    try {
      const body = await res.json();
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
      } else {
        detail = JSON.stringify(body.detail);
      }
    } catch {}
    throw new ApiError(detail, code);
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

export async function patchTicket(id: number, body: Partial<Record<string, string | null>>, revision?: string): Promise<Ticket> {
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
  applied: boolean; adds: number; changes: number; unchanged: number; new_suppliers: string[]; new_materials: string[];
  plan: { line: number; supplier: string; material: string; price: string; old_price: string | null; action: "add" | "change" | "unchanged"; new_supplier: boolean; new_material: boolean }[];
};
export async function importRateSheet(text: string, apply: boolean): Promise<RateSheetPlan> {
  return (await check(await apiFetch(`${API}/api/rates/import`, json("POST", { text, apply })))).json();
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
export async function createSupplier(name: string) {
  await check(await apiFetch(`${API}/api/suppliers`, json("POST", { name })));
}
export async function createMaterial(name: string) {
  await check(await apiFetch(`${API}/api/materials`, json("POST", { name })));
}
export async function saveRate(supplier_id: number, material_id: number, unit_price: string) {
  await check(await apiFetch(`${API}/api/rates`, json("PUT", { supplier_id, material_id, unit_price })));
}
export type PricingEventRow = {
  id: number;
  action: "supplier_added" | "supplier_removed" | "material_added" | "material_removed" | "rate_set" | "rate_changed" | "rate_removed";
  supplier: string | null; material: string | null;
  old: string | null; new: string | null; // decimal strings, never parsed for money math
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
export async function createProject(body: { code: string; name: string; job_location?: string; description?: string }) {
  await check(await apiFetch(`${API}/api/projects`, json("POST", body)));
}
export async function setProjectStatus(id: number, status: "active" | "archived") {
  await check(await apiFetch(`${API}/api/projects/${id}`, json("PATCH", { status })));
}
export async function getRecords(f: Filters): Promise<Ticket[]> {
  return (await check(await apiFetch(`${API}/api/records${qs(f)}`))).json();
}
export const recordsExportUrl = (f: Filters, format: "xlsx" | "csv" = "xlsx", columns?: string[]) => {
  const query = qs(f);
  const extra = columns?.length ? `${query ? "&" : "?"}columns=${columns.join(",")}` : "";
  return `${API}/api/records/export.${format}${query}${extra}`;
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
export type SavedView = { id: number; name: string; filters: Filters; columns: string[]; created_by: string | null; created_at: string };
export async function listSavedViews(): Promise<SavedView[]> {
  return (await check(await apiFetch(`${API}/api/saved-views`))).json();
}
export async function createSavedView(name: string, filters: Filters, columns: string[]): Promise<SavedView> {
  const kept = Object.fromEntries(Object.entries(filters).filter(([, v]) => v));
  return (await check(await apiFetch(`${API}/api/saved-views`, json("POST", { name, filters: kept, columns })))).json();
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

export type Group = { name: string; tickets: number; tons: string | number; cost: string | number };
export type Dashboard = {
  tickets: number; needs_review: number; flagged: number; verified: number; total_tons: string | number; total_cost: string | number;
  pending_tons: string | number; unpriced_tickets: number; pending_cost_tickets: number;
  by_supplier: Group[]; by_material: Group[];
  recent_batches: { id: number; status: string; created_at: string; files: number; project: string | null }[];
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
export async function saveDraftLine(id: number, lineId: number, body: Record<string, string | number | null>) {
  await check(await apiFetch(`${DOCS}/${id}/lines/${lineId}`, json("PATCH", body)));
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
  return (await check(await apiFetch(API + "/api/invoice-comparisons"))).json();
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
    link.download = "ticketflow-invoice-" + id + ".csv";
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
