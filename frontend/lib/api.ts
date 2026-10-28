export const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type FileOut = { id: number; original_name: string; status: string; error: string | null };
export type Batch = { id: number; layout: string; status: string; created_at: string; files: FileOut[] };
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
  status: "needs_review" | "verified" | "skipped" | "failed";
  tonnage_confirmed: boolean;
  error: string | null;
  unit_price_override: string | null;
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

export async function uploadBatch(files: File[], layout: string): Promise<Batch> {
  const form = new FormData();
  form.append("layout", layout);
  files.forEach((f) => form.append("files", f));
  return (await check(await fetch(`${API}/api/batches`, { method: "POST", body: form }))).json();
}

export async function getBatch(id: number): Promise<Batch> {
  return (await check(await fetch(`${API}/api/batches/${id}`))).json();
}

export async function getTickets(batchId: number): Promise<Ticket[]> {
  return (await check(await fetch(`${API}/api/batches/${batchId}/tickets`))).json();
}

export async function patchTicket(id: number, body: Partial<Record<string, string | null>>): Promise<Ticket> {
  return (
    await check(
      await fetch(`${API}/api/tickets/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    )
  ).json();
}

export async function ticketAction(id: number, action: "verify" | "skip"): Promise<Ticket> {
  return (await check(await fetch(`${API}/api/tickets/${id}/${action}`, { method: "POST" }))).json();
}

export async function retryBatch(id: number): Promise<void> {
  await check(await fetch(`${API}/api/batches/${id}/retry`, { method: "POST" }));
}

export const ticketImageUrl = (id: number) => `${API}/api/tickets/${id}/image`;
export const exportUrl = (batchId: number) => `${API}/api/batches/${batchId}/export.xlsx`;

const json = (method: string, body?: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: body === undefined ? undefined : JSON.stringify(body),
});

export async function getStatus(): Promise<{ extraction_configured: boolean; model: string }> {
  return (await check(await fetch(`${API}/api/status`))).json();
}
export async function listSuppliers(): Promise<Supplier[]> {
  return (await check(await fetch(`${API}/api/suppliers`))).json();
}
export async function listMaterials(): Promise<Material[]> {
  return (await check(await fetch(`${API}/api/materials`))).json();
}
export async function listRates(): Promise<Rate[]> {
  return (await check(await fetch(`${API}/api/rates`))).json();
}
export async function createSupplier(name: string) {
  await check(await fetch(`${API}/api/suppliers`, json("POST", { name })));
}
export async function createMaterial(name: string) {
  await check(await fetch(`${API}/api/materials`, json("POST", { name })));
}
export async function saveRate(supplier_id: number, material_id: number, unit_price: string) {
  await check(await fetch(`${API}/api/rates`, json("PUT", { supplier_id, material_id, unit_price })));
}
export async function deleteItem(kind: "suppliers" | "materials" | "rates", id: number) {
  await check(await fetch(`${API}/api/${kind}/${id}`, { method: "DELETE" }));
}
