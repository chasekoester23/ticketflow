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
