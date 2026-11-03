"use client";

import { useCallback, useEffect, useState } from "react";
import {
  getInvoiceComparisons, getInvoiceHistory, InvoiceLineComparison,
  InvoiceLineInput, listMaterials, listProjects, listSuppliers, Material,
  Project, saveInvoiceComparison, Supplier,
} from "../../lib/api";

const EMPTY: Omit<InvoiceLineInput, "project_id"> & { project_id: number | "" } = {
  project_id: "", invoice_number: "", supplier_name: "", material_name: "",
  date_from: "", date_to: "", billed_tons: "", billed_unit_price: "",
  billed_subtotal: "",
};

function money(value: string | null) {
  if (value === null) return "Unresolved";
  return "$" + Number(value).toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
}
function tons(value: string) {
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: 3 });
}
function signed(value: string) {
  return Number(value) > 0 ? "+" + value : value;
}
function statusLabel(status: InvoiceLineComparison["status"]) {
  switch (status) {
    case "matches": return "Figures match";
    case "discrepancy": return "Difference found";
    case "incomplete": return "Incomplete review";
    case "no_verified_tickets": return "No verified tickets";
  }
}
function statusStyle(status: InvoiceLineComparison["status"]) {
  return status === "matches" ? "tf-status-open" : status === "discrepancy" ?
    "tf-status-error" : "tf-status-attention";
}

export default function InvoiceComparisonPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [suppliers, setSuppliers] = useState<Supplier[]>([]);
  const [materials, setMaterials] = useState<Material[]>([]);
  const [lines, setLines] = useState<InvoiceLineComparison[]>([]);
  const [form, setForm] = useState<typeof EMPTY>({ ...EMPTY });
  const [editing, setEditing] = useState<number | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [history, setHistory] = useState<string[] | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(async () => {
    const data = await getInvoiceComparisons();
    setLines(data);
  }, []);

  useEffect(() => {
    Promise.all([
      listProjects(true), listSuppliers(), listMaterials(), getInvoiceComparisons(),
    ]).then(([p, s, m, rows]) => {
      setProjects(p); setSuppliers(s); setMaterials(m); setLines(rows);
    }).catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false));
  }, []);

  function field<K extends keyof typeof EMPTY>(key: K, value: (typeof EMPTY)[K]) {
    setForm((old) => ({ ...old, [key]: value }));
  }

  function reset() {
    setEditing(null);
    setForm({ ...EMPTY });
    setError(null);
  }

  async function save(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (saving || form.project_id === "") return;
    setSaving(true);
    setError(null);
    try {
      const row = await saveInvoiceComparison(form as InvoiceLineInput, editing ?? undefined);
      await reload();
      setSelected(row.id);
      setHistory(null);
      reset();
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setSaving(false);
    }
  }

  function edit(row: InvoiceLineComparison) {
    setEditing(row.id);
    setForm({
      project_id: row.project_id,
      invoice_number: row.invoice_number,
      supplier_name: row.supplier_name,
      material_name: row.material_name,
      date_from: row.date_from,
      date_to: row.date_to,
      billed_tons: row.billed_tons,
      billed_unit_price: row.billed_unit_price,
      billed_subtotal: row.billed_subtotal,
    });
    setError(null);
  }

  async function showHistory(id: number) {
    setError(null);
    try {
      const events = await getInvoiceHistory(id);
      setHistory(events.length ? events.map((event) =>
        event.field + ": " + (event.old ?? "—") + " → " + (event.new ?? "—")
      ) : ["No corrections recorded."]);
      setSelected(id);
    } catch (exc) {
      setError((exc as Error).message);
    }
  }

  const chosen = lines.find((line) => line.id === selected) ?? null;
  const required = form.project_id !== "" && form.invoice_number.trim() &&
    form.supplier_name.trim() && form.material_name.trim() && form.date_from &&
    form.date_to && form.billed_tons && form.billed_unit_price && form.billed_subtotal;

  return <div className="space-y-5">
    <div className="tf-page-heading">
      <div>
        <p className="tf-eyebrow">MATERIAL ACCOUNTING / RECONCILIATION</p>
        <h1 className="tf-page-title">Invoice comparison</h1>
        <p className="tf-page-description">
          Compare supplier invoice lines with verified material deliveries by project, supplier, material and delivery dates.
        </p>
      </div>
      <a href="/records" className="tf-secondary-button">View ticket records →</a>
    </div>

    <p className="rounded border border-[#e2e7ec] bg-white px-4 py-3 text-[12px] leading-5 text-[#47576a]">
      <strong>Comparison only — not approval for payment.</strong> Enter the material line as shown on
      the original supplier invoice. Exclude tax, freight and unrelated charges from the material subtotal.
      This first version does not read invoice PDFs or allocate partial deliveries between invoices.
    </p>
    {error && <div role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">{error}</div>}

    <section className="tf-panel">
      <div className="tf-panel-heading">
        <div>
          <h2 className="tf-panel-title">{editing === null ? "Record an invoice material line" : "Correct invoice line #" + editing}</h2>
          <p className="tf-panel-description">Manual entry. No Gemini calls, invoice uploads or payment actions.</p>
        </div>
        {editing !== null && <button type="button" className="tf-secondary-button" onClick={reset}>Cancel editing</button>}
      </div>
      <form onSubmit={(e) => void save(e)} className="grid items-end gap-4 p-5 sm:grid-cols-2 xl:grid-cols-3">
        <label className="tf-field-label">Project
          <select className="tf-field w-full" required value={form.project_id}
            onChange={(e) => field("project_id", e.target.value ? Number(e.target.value) : "")}>
            <option value="">Select project</option>
            {projects.map((p) => <option key={p.id} value={p.id}>{p.code} · {p.name}</option>)}
          </select>
        </label>
        <label className="tf-field-label">Invoice number
          <input className="tf-field w-full" maxLength={100} required value={form.invoice_number}
            placeholder="INV-2026-1042" onChange={(e) => field("invoice_number", e.target.value)}/>
        </label>
        <label className="tf-field-label">Supplier name (match ticket supplier)
          <input className="tf-field w-full" list="invoice-suppliers" maxLength={200} required
            value={form.supplier_name} placeholder="Example Aggregates"
            onChange={(e) => field("supplier_name", e.target.value)}/>
          <datalist id="invoice-suppliers">
            {suppliers.map((s) => <option key={s.id} value={s.name}/>)}
          </datalist>
        </label>
        <label className="tf-field-label">Material (one material per line)
          <input className="tf-field w-full" list="invoice-materials" maxLength={200} required
            value={form.material_name} placeholder="No. 53 Stone"
            onChange={(e) => field("material_name", e.target.value)}/>
          <datalist id="invoice-materials">
            {materials.map((m) => <option key={m.id} value={m.name}/>)}
          </datalist>
        </label>
        <label className="tf-field-label">Delivery date from
          <input className="tf-field w-full" type="date" required value={form.date_from}
            onChange={(e) => field("date_from", e.target.value)}/>
        </label>
        <label className="tf-field-label">Delivery date through (inclusive)
          <input className="tf-field w-full" type="date" required min={form.date_from || undefined}
            value={form.date_to} onChange={(e) => field("date_to", e.target.value)}/>
        </label>
        <label className="tf-field-label">Billed quantity (tons)
          <input className="tf-field w-full" inputMode="decimal" required
            value={form.billed_tons} placeholder="46.600"
            onChange={(e) => field("billed_tons", e.target.value)}/>
        </label>
        <label className="tf-field-label">Billed rate ($ per ton)
          <input className="tf-field w-full" inputMode="decimal" required
            value={form.billed_unit_price} placeholder="30.00"
            onChange={(e) => field("billed_unit_price", e.target.value)}/>
        </label>
        <label className="tf-field-label">Invoice MATERIAL subtotal ($)
          <input className="tf-field w-full" inputMode="decimal" required
            value={form.billed_subtotal} placeholder="1398.00"
            onChange={(e) => field("billed_subtotal", e.target.value)}/>
        </label>
        <div className="sm:col-span-2 xl:col-span-3 flex items-center gap-3">
          <button type="submit" className="tf-primary-button" disabled={!required || saving}>
            {saving ? "Saving…" : editing === null ? "Compare invoice line" : "Save corrected line"}
          </button>
          <span className="text-[11px] text-[#718091]">All comparisons update if the underlying ticket verification changes.</span>
        </div>
      </form>
    </section>

    <section className="tf-panel">
      <div className="tf-panel-heading">
        <div>
          <h2 className="tf-panel-title">Saved invoice comparisons</h2>
          <p className="tf-panel-description">Matches use verified ticket quantities and locked historical prices only.</p>
        </div>
        <span className="tf-status">{lines.length} material lines</span>
      </div>
      <div className="tf-table-wrap">
        <table className="tf-table">
          <thead><tr>
            {["Invoice", "Project", "Supplier / material", "Dates", "Billed tons", "Verified tons", "Tons diff.", "Amount diff.", "Result", ""].map((h) => <th key={h}>{h}</th>)}
          </tr></thead>
          <tbody>{lines.map((row) => <tr key={row.id}>
            <td className="font-semibold">{row.invoice_number}</td>
            <td>{projects.find((p) => p.id === row.project_id)?.code ?? row.project_id}</td>
            <td><span className="font-semibold">{row.supplier_name}</span><span className="block text-[11px] text-[#778493]">{row.material_name}</span></td>
            <td className="whitespace-nowrap">{row.date_from} – {row.date_to}</td>
            <td className="tabular-nums">{tons(row.billed_tons)}</td>
            <td className="tabular-nums">{tons(row.verified_tons)}</td>
            <td className="tabular-nums">{signed(row.quantity_difference)}</td>
            <td className="tabular-nums">{row.subtotal_difference === null ? "Unresolved" : money(row.subtotal_difference)}</td>
            <td><span className={"tf-status " + statusStyle(row.status)}>{statusLabel(row.status)}</span></td>
            <td className="whitespace-nowrap">
              <button type="button" onClick={() => { setSelected(row.id); setHistory(null); }} className="tf-link">Details</button>
              <button type="button" onClick={() => edit(row)} className="tf-link ml-3">Correct</button>
            </td>
          </tr>)}</tbody>
        </table>
        {!loading && !lines.length && <p className="tf-empty">No invoice lines entered yet. Add one above to compare it with approved tickets.</p>}
        {loading && <p className="tf-empty">Loading invoice comparisons…</p>}
      </div>
    </section>

    {chosen && <section className="tf-panel">
      <div className="tf-panel-heading">
        <div>
          <h2 className="tf-panel-title">Invoice {chosen.invoice_number} · {chosen.material_name}</h2>
          <p className="tf-panel-description">Current comparison, not an invoice approval or payment recommendation.</p>
        </div>
        <button type="button" className="tf-secondary-button" onClick={() => void showHistory(chosen.id)}>Correction history</button>
      </div>
      <div className="grid gap-5 p-5 md:grid-cols-2">
        <div className="space-y-2 text-[13px]">
          <p className="font-bold text-[#344352]">Supplier invoice line</p>
          <p>Billed quantity: <strong>{tons(chosen.billed_tons)} tons</strong></p>
          <p>Invoice unit rate: <strong>{money(chosen.billed_unit_price)}</strong></p>
          <p>Quantity × rate: <strong>{money(chosen.billed_extension)}</strong></p>
          <p>Entered material subtotal: <strong>{money(chosen.billed_subtotal)}</strong></p>
          <p>Invoice arithmetic difference: <strong>{money(chosen.invoice_arithmetic_difference)}</strong></p>
        </div>
        <div className="space-y-2 text-[13px]">
          <p className="font-bold text-[#344352]">TicketFlow verified deliveries</p>
          <p>Verified quantity: <strong>{tons(chosen.verified_tons)} tons</strong></p>
          <p>Verified material subtotal: <strong>{money(chosen.verified_subtotal)}</strong></p>
          <p>Billed minus verified tons: <strong>{signed(chosen.quantity_difference)}</strong></p>
          <p>Billed minus verified material cost: <strong>{money(chosen.subtotal_difference)}</strong></p>
          <p>{chosen.verified_count} verified · {chosen.pending_count} pending · {chosen.unclassified_count} not yet classified · {chosen.unpriced_count} unpriced</p>
        </div>
      </div>
      {chosen.warnings.length > 0 && <div className="border-y border-[#f0d8ad] bg-[#fff9ed] px-5 py-3 text-[12px] text-[#754e20]">
        <p className="font-bold">Items to investigate</p>
        <ul className="mt-1 list-disc space-y-1 pl-5">
          {chosen.warnings.map((message) => <li key={message}>{message}</li>)}
        </ul>
      </div>}
      <div className="p-5">
        <p className="mb-3 text-[12px] font-bold text-[#344352]">Tickets in this supplier, material and date range</p>
        {chosen.tickets.length ? <div className="tf-table-wrap"><table className="tf-table">
          <thead><tr><th>Ticket</th><th>Tons</th><th>Locked rate</th><th>Record</th></tr></thead>
          <tbody>{chosen.tickets.map((ticket) => <tr key={ticket.ticket_id}>
            <td>{ticket.ticket_number ?? "Unnumbered"}</td>
            <td>{ticket.tons ?? "—"}</td>
            <td>{money(ticket.confirmed_price)}</td>
            <td><a href={"/upload?batch=" + ticket.batch_id + "&ticket=" + ticket.ticket_id}>Review ticket →</a></td>
          </tr>)}</tbody>
        </table></div> : <p className="text-[12px] text-[#718091]">No matching tickets in this scope. Verify supplier spelling and delivery dates.</p>}
        {history && <div className="mt-4 border-t border-[#e2e7ec] pt-4">
          <p className="mb-2 text-[12px] font-bold">Invoice entry corrections</p>
          {history.map((entry, index) => <p key={index} className="text-[12px] text-[#526273]">{entry}</p>)}
        </div>}
      </div>
    </section>}
  </div>;
}
