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

export type Owner = { id: number; username: string };

export async function getAuthMe(): Promise<Owner> {
  return (await check(await apiFetch(`${API}/api/auth/me`))).json();
}

export async function logIn(username: string, password: string): Promise<Owner> {
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
export type Batch = { id: number; project_id: number | null; layout: string; status: string; created_at: string; files: FileOut[] };
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
};
export type Supplier = { id: number; name: string; contact: string | null };
export type Material = { id: number; name: string; unit: string };
export type Rate = { id: number; supplier_id: number; material_id: number; unit_price: string };

async function check(res: Response) {
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {}
    throw new Error(detail);
  }
  return res;
}

export async function uploadBatch(files: File[], layout: string, projectId?: number): Promise<Batch> {
  const form = new FormData();
  form.append("layout", layout);
  if (projectId !== undefined) form.append("project_id", String(projectId));
  files.forEach((f) => form.append("files", f));
  return (await check(await apiFetch(`${API}/api/batches`, { method: "POST", body: form }))).json();
}

export async function getBatch(id: number): Promise<Batch> {
  return (await check(await apiFetch(`${API}/api/batches/${id}`))).json();
}

export async function getTickets(batchId: number): Promise<Ticket[]> {
  return (await check(await apiFetch(`${API}/api/batches/${batchId}/tickets`))).json();
}

export async function patchTicket(id: number, body: Partial<Record<string, string | null>>): Promise<Ticket> {
  return (
    await check(
      await apiFetch(`${API}/api/tickets/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    )
  ).json();
}

export async function ticketAction(id: number, action: "verify" | "flag" | "skip"): Promise<Ticket> {
  return (await check(await apiFetch(`${API}/api/tickets/${id}/${action}`, { method: "POST" }))).json();
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

export async function getStatus(): Promise<{ extraction_configured: boolean; model: string }> {
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
export async function deleteItem(kind: "suppliers" | "materials" | "rates", id: number) {
  await check(await apiFetch(`${API}/api/${kind}/${id}`, { method: "DELETE" }));
}

export type Filters = { project_id?: string; supplier?: string; material?: string; status?: string; date_from?: string; date_to?: string };
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
export const recordsExportUrl = (f: Filters) => `${API}/api/records/export.xlsx${qs(f)}`;

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
