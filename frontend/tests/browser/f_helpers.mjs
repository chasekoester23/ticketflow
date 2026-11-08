import { profile } from "./accounts.mjs";

// Fictional tickets. The mock follows backend/app/routers/api.py, including revisions:
// a stale If-Match is refused the way the server refuses it.
export const cors = {
  "access-control-allow-origin": "http://127.0.0.1:3000",
  "access-control-allow-credentials": "true",
  "access-control-allow-methods": "GET, POST, PATCH, PUT, DELETE, OPTIONS",
  "access-control-allow-headers": "content-type, if-match, x-ticketflow-request",
  "access-control-expose-headers": "x-request-id, retry-after",
};
export const json = (route, value, status = 200, headers = {}) => route.fulfill({
  status, contentType: "application/json", headers: { ...cors, ...headers }, body: JSON.stringify(value),
});
const PIXEL = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4//8/AwAI/AL+0I0YAAAAAElFTkSuQmCC", "base64");
export const image = (route) => route.fulfill({ contentType: "image/png", headers: cors, body: PIXEL });

export function ticket(id, fields = {}) {
  return {
    id, batch_id: 5, file_id: 1, source_page: 1, position: id - 101, ticket_number: null, delivery_date: null,
    material_type: null, tonnage: null, supplier: null, job_location: null, flags: [], status: "needs_review",
    tonnage_confirmed: false, error: null, unit_price_override: null, price_snapshot_state: "pending",
    unit_price: null, price_source: null, cost: null, duplicates: [], pricing_notes: ["No rate"], revision: `r${id}-1`,
    ...fields,
  };
}

export const complete = (id, fields = {}) => ticket(id, {
  ticket_number: String(10000 + id), delivery_date: "2026-10-15", material_type: "No. 57 Stone", tonnage: "20.500",
  supplier: "Example Aggregates", job_location: "Lot A", ...fields,
});

/**
 * The review API for one batch. `options.intercept(request, path, method)` can answer a request
 * itself, for failures and delays, by returning a function that takes the route.
 */
export async function mockReview(page, tickets, { role = "owner", account = {}, intake = "manual", intercept, manifest } = {}) {
  const log = { patched: [], verified: [], flagged: [], skipped: [], ifMatch: [], requests: [] };
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    if (method === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    log.requests.push(`${method} ${path}`);
    const special = intercept?.(request, path, method);
    if (special) return special(route);
    if (path === "/api/auth/me") return json(route, profile(role, { id: account.id ?? 1, ...account }));
    if (path === "/api/auth/logout") return json(route, { ok: true });
    if (path === "/api/status") return json(route, { extraction_configured: false, model: "none" });
    if (path === "/api/projects") return json(route, [{ id: 1, code: "24-115", name: "Fictional Road Job", job_location: null, description: null, status: "active" }]);
    if (path === "/api/suppliers" || path === "/api/materials") return json(route, []);
    if (path === "/api/batches/queue") return json(route, []);
    if (path === "/api/batches/5") return json(route, { id: 5, project_id: 1, layout: "grid_2x2", intake_mode: intake, status: "ready_for_review", created_at: "2026-10-15T11:00:00Z", files: [{ id: 1, original_name: "fictional.pdf", status: "ready_for_review", error: null }] });
    if (path === "/api/batches/5/tickets") return json(route, tickets.map((t) => ({ ...t })));
    if (path === "/api/batches/5/manifest") return manifest ? json(route, manifest()) : json(route, { detail: "n/a" }, 404);
    if (/\/image$/.test(path)) return image(route);
    const match = path.match(/^\/api\/tickets\/(\d+)(?:\/(verify|flag|skip|audit))?$/);
    if (match) {
      if (match[2] === "audit") return json(route, []);
      const t = tickets.find((x) => x.id === Number(match[1]));
      const given = request.headers()["if-match"];
      log.ifMatch.push(given);
      if (given && given !== t.revision) {
        return json(route, { detail: { code: "ticket_changed", message: "Someone else changed this ticket while you had it open. Nothing was saved." } }, 409);
      }
      const bump = () => { t.revision = `r${t.id}-${Number(t.revision.split("-")[1]) + 1}`; };
      if (method === "PATCH") {
        Object.assign(t, request.postDataJSON());
        bump();
        log.patched.push({ id: t.id, body: request.postDataJSON() });
        return json(route, { ...t });
      }
      if (match[2] === "verify") {
        t.status = "verified";
        bump();
        log.verified.push(t.id);
        return json(route, { ...t });
      }
      if (match[2] === "flag") {
        t.status = "flagged";
        t.flag_note = request.postDataJSON()?.note ?? null;
        bump();
        log.flagged.push({ id: t.id, note: t.flag_note });
        return json(route, { ...t });
      }
      if (match[2] === "skip") {
        t.status = "skipped";
        bump();
        log.skipped.push(t.id);
        return json(route, { ...t });
      }
    }
    return json(route, { detail: "Unmocked " + path }, 404);
  });
  return log;
}

export const draftKeys = (page) => page.evaluate(() => Object.keys(window.localStorage).filter((key) => key.startsWith("ticketflow.draft.")));

const PROJECT = { id: 1, code: "24-115", name: "Fictional Road Job", job_location: "Lot A", description: null, status: "active" };
const searchRow = (id, fields = {}) => complete(id, { batch_id: 5, status: id % 3 ? "verified" : "needs_review", verified_by: id % 3 ? "Fictional Owner" : null, verified_at: id % 3 ? "2026-10-16T15:00:00Z" : null, unit_price: "30.0000", cost: "615.00", ...fields });
const INVOICE_LINE = {
  id: 9, project_id: 1, invoice_number: "EA-2026-1107", supplier_name: "Example Aggregates", material_name: "No. 57 Stone",
  date_from: "2026-10-01", date_to: "2026-10-31", billed_tons: "61.500", billed_unit_price: "30.00", billed_subtotal: "1845.00",
  ticket_list: "", billed_extension: "1845.00", invoice_arithmetic_difference: "0.00", verified_tons: "41.000", verified_subtotal: "1230.00",
  quantity_difference: "20.500", subtotal_difference: "615.00", verified_count: 2, pending_count: 1, unclassified_count: 0, unpriced_count: 0,
  rate_mismatch_count: 0, status: "discrepancy", comparison_mode: "itemized", warnings: ["1 ticket on the invoice is not in TicketFlow."],
  tickets: [], ticket_check: [
    { result: "matched", line: 1, invoice_ticket_number: "11002", billed_tons: "20.500", ticket: { ticket_id: 1002, batch_id: 5, ticket_number: "11002", delivery_date: "2026-10-15", supplier: "Example Aggregates", material: "No. 57 Stone", tons: "20.500", review_status: "verified", project: "24-115" }, candidates: [], verified_tons: "20.500", tons_difference: "0.000", flags: [], notes: [], evidence: "confirmed", link_id: 3 },
    { result: "not_found", line: 2, invoice_ticket_number: "99999", billed_tons: "20.500", ticket: null, candidates: [], verified_tons: null, tons_difference: null, flags: [], notes: ["No ticket with this number in TicketFlow."], evidence: "unmatched", link_id: null },
  ],
  ticket_check_counts: { matched: 1, not_found: 1 }, itemized_billed_tons: "41.000",
  evidence: { listed_tickets: 2, confirmed: 1, candidates: 0, conflicting: 0, invalidated: 0, unmatched: 1, confirmed_tons: "20.500" },
  diagnosis: { headline: "The invoice bills 20.500 tons more than approved deliveries.", tons: { start: "41.000", billed: "61.500", complete: true, rows_without_tons: 0, unexplained: "0.000", items: [{ kind: "not_found", label: "Billed, not in TicketFlow", tons: "20.500", count: 1, tickets: ["99999"] }] }, money: null, money_unresolved_reason: "One billed ticket has no approved record.", next_steps: [{ text: "Ask the supplier for ticket 99999", href: null }] },
  revision: "l9-1", created_at: "2026-11-01T00:00:00Z",
};

/**
 * Every page's API, with fictional data or none (`empty`). Returns 404 for anything
 * else, which is what a server without a newer endpoint answers.
 */
export async function mockWorkspace(page, { role = "owner", empty = false, extra } = {}) {
  const log = [];
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    if (method === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
    log.push(`${method} ${path}${url.search}`);
    const special = extra?.(request, path, method, url);
    if (special) return special(route);
    if (/\/image$/.test(path)) return image(route);
    const answers = {
      "/api/auth/me": profile(role),
      "/api/setup/status": { needed: false },
      "/api/status": { extraction_configured: false, model: "none", worker_running: true },
      "/api/projects": empty ? [] : [PROJECT],
      "/api/suppliers": empty ? [] : [{ id: 1, name: "Example Aggregates", contact: null }],
      "/api/materials": empty ? [] : [{ id: 2, name: "No. 57 Stone", unit: "ton" }],
      "/api/rates": empty ? [] : [{ id: 3, supplier_id: 1, material_id: 2, unit_price: "30.0000" }],
      "/api/pricing/history": [],
      "/api/name-aliases": [],
      "/api/onboarding": { complete: !empty, steps: [{ key: "project", title: "Create a project", detail: "Each upload belongs to one job.", href: "/projects", done: !empty }] },
      "/api/dashboard": { tickets: empty ? 0 : 12, needs_review: empty ? 0 : 4, flagged: 0, verified: empty ? 0 : 8, total_tons: empty ? "0" : "164.000", pending_tons: "82.000", total_cost: empty ? "0" : "4920.00", unpriced_tickets: 0, pending_cost_tickets: 4, by_supplier: empty ? [] : [{ name: "Example Aggregates", tickets: 12, tons: "164.000", cost: "4920.00" }], by_material: [], recent_batches: empty ? [] : [{ id: 5, status: "ready_for_review", created_at: "2026-10-15T11:00:00Z", files: 1, project: "24-115 · Fictional Road Job" }] },
      "/api/batches/queue": empty ? [] : [{ id: 5, project_id: 1, project_code: "24-115", project_name: "Fictional Road Job", status: "ready_for_review", intake_mode: "manual", created_at: "2026-10-15T11:00:00Z", created_by: "Fictional Clerk", files: 1, failed_files: 0, expected_ticket_count: 4, tickets: 4, needs_review: 2, flagged: 0, verified: 2, skipped: 0, failed: 0, faint_slots: 0 }],
      "/api/batches/5": { id: 5, project_id: 1, layout: "grid_2x2", intake_mode: "manual", status: "ready_for_review", created_at: "2026-10-15T11:00:00Z", files: [{ id: 1, original_name: "fictional.pdf", status: "ready_for_review", error: null }] },
      "/api/batches/5/tickets": [searchRow(1001, { status: "needs_review", verified_by: null, verified_at: null, cost: null }), searchRow(1002), searchRow(1003), searchRow(1004, { status: "flagged", flag_note: "Tons smudged" })],
      "/api/records/search": { rows: empty ? [] : [searchRow(1001), searchRow(1002), searchRow(1003)], total: empty ? 0 : 3, page: 1, page_size: 50, summary: { records: empty ? 0 : 3, by_status: empty ? {} : { verified: 2, needs_review: 1 }, verified_tons: empty ? "0" : "41.000", verified_cost: empty ? "0" : "1230.00", verified_unpriced: 0 } },
      "/api/records/name-variants": { field: "supplier", catalog: ["Example Aggregates"], rows: empty ? [] : [{ value: "Example Aggregates", state: "catalog", target: null, tickets: 3, verified: 2, unverified: 1 }] },
      "/api/saved-views": empty ? [] : [{ id: 1, name: "October stone", filters: { material: "stone" }, columns: ["ticket_number", "tons"], labels: { tons: "Net Tons" }, created_by: "Fictional Owner", created_at: "2026-11-01T00:00:00Z" }],
      "/api/invoice-comparisons": empty ? [] : [INVOICE_LINE],
      "/api/invoice-comparisons/9": INVOICE_LINE,
      "/api/invoice-comparisons/9/audit": [],
      "/api/invoice-comparisons/overview": { lines: empty ? 0 : 1, by_status: empty ? {} : { discrepancy: 1 }, billed: "1845.00", verified: "1230.00", lines_unresolved: 0, by_supplier: [], exceptions: [] },
      "/api/invoice-documents": [],
      "/api/reports/pilot": { project_id: null, unresolved_items: 0, intake: { batches: 1, files: 1, failed_files: 0, files_without_scan_check: 0, pages: 1, slots: 4, faint_unclassified: 0, empty: 0, confirmed_blank: 0, batches_with_paper_count: 1, paper_count_mismatches: 0 }, review: { records: 4, needs_review: 1, flagged: 1, failed: 0, verified: 2, skipped: 0, with_unreadable_fields: 0, verified_unpriced: 0, verified_tons: "41.000" }, reconciliation: { invoice_lines: 1, itemized_lines: 1, totals_only_lines: 0, lines_by_status: { discrepancy: 1 }, lines_needing_attention: 1, verified_tickets_linked: 1, verified_tickets_not_linked: 1, tons_linked: "20.500", tons_not_linked: "20.500" }, batches_needing_attention: [], review_timing: { enabled: false, manual: { samples: 0, median_seconds: null, with_corrections: 0 }, extract: { samples: 0, median_seconds: null, with_corrections: 0 } } },
      "/api/reports/project/1": { project: { id: 1, code: "24-115", name: "Fictional Road Job", status: "active" }, first_delivery: "2026-10-01", last_delivery: "2026-10-20", totals: { tickets: 4, verified_tickets: 2, verified_tons: "41.000", cost: "1230.00", unpriced: 0, pending_tickets: 2, pending_tons: "41.000" }, by_supplier_material: [{ supplier: "Example Aggregates", material: "No. 57 Stone", tickets: 4, verified_tickets: 2, verified_tons: "41.000", cost: "1230.00", unpriced: 0, pending_tickets: 2, pending_tons: "41.000" }], by_month: [{ month: "2026-10", tickets: 4, verified_tickets: 2, verified_tons: "41.000", cost: "1230.00", unpriced: 0, pending_tickets: 2, pending_tons: "41.000" }], invoices: { billed_total: "1845.00", verified_total: "1230.00", lines_unresolved: 0, by_status: { discrepancy: 1 }, lines: [{ id: 9, invoice_number: "EA-2026-1107", supplier: "Example Aggregates", material: "No. 57 Stone", date_from: "2026-10-01", date_to: "2026-10-31", status: "discrepancy", comparison_mode: "itemized", billed_tons: "61.500", verified_tons: "41.000", tons_difference: "20.500", billed_subtotal: "1845.00", verified_subtotal: "1230.00", subtotal_difference: "615.00" }] } },
      "/api/reports/project/1/unbilled": { project: { id: 1, code: "24-115", name: "Fictional Road Job" }, total_tickets: 1, total_tons: "20.500", total_cost: "615.00", truncated: false, rows: [{ ticket_id: 1003, batch_id: 5, ticket_number: "11003", delivery_date: "2026-10-18", supplier: "Example Aggregates", material: "No. 57 Stone", tons: "20.500", cost: "615.00" }], gaps: [] },
      "/api/system/status": { healthy: true, checks: [{ name: "Database", state: "ok", message: "Answering.", action: null }], queue: { pending: 0, running: 0, failed_jobs: 0, oldest_pending_seconds: null }, worker: { running: true, last_seen_seconds: 3 }, database: { kind: "sqlite", revision: "x", expected_revision: "x" }, invoice_reader: "local" },
      "/api/team/members": [{ id: 1, username: "owner", display_name: "Fictional Owner", role: "owner", role_label: "Owner", active: true, must_change_password: false, created_at: "2026-10-01T00:00:00Z", is_you: true }],
      "/api/team/events": [],
    };
    if (path in answers) return json(route, answers[path]);
    if (/^\/api\/tickets\/\d+\/audit$/.test(path)) return json(route, []);
    return json(route, { detail: "Not Found" }, 404);
  });
  return log;
}
