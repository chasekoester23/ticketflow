"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  confirmTicketLinks, downloadInvoiceComparisonReport, getInvoiceComparisons, getInvoiceOverview, releaseTicketLink, getInvoiceHistory,
  InvoiceDiagnosis, InvoiceLineComparison, InvoiceLineInput, InvoiceOverview, listMaterials, listProjects, listSuppliers, Material,
  Project, saveInvoiceComparison, Supplier, CheckedTicket, TicketCheckRow, ticketImageUrl,
} from "../../lib/api";
import { useAccount } from "../account-context";

const EMPTY: Omit<InvoiceLineInput, "project_id"> & { project_id: number | "" } = {
  project_id: "", invoice_number: "", supplier_name: "", material_name: "",
  date_from: "", date_to: "", billed_tons: "", billed_unit_price: "",
  billed_subtotal: "", ticket_list: "",
};
const TOTALS_ONLY = "Totals-only comparison — individual tickets not checked.";

function money(value: string | null) {
  if (value === null) return "Unresolved";
  return "$" + Number(value).toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
}
function tons(value: string) {
  return Number(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 3 });
}
function signed(value: string) {
  return Number(value) > 0 ? "+" + value : value;
}
function statusLabel(row: InvoiceLineComparison) {
  switch (row.status) {
    // Matching totals never imply every ticket was billed correctly.
    case "matches": return row.comparison_mode === "itemized" ? "Tickets and totals match" : "Totals match";
    case "discrepancy": return "Difference found";
    case "incomplete": return "Incomplete review";
    case "no_verified_tickets": return "No verified tickets";
  }
}
function statusStyle(status: InvoiceLineComparison["status"]) {
  return status === "matches" ? "tf-status-open" : status === "discrepancy" ?
    "tf-status-error" : "tf-status-attention";
}
const RESULT_LABELS: Record<TicketCheckRow["result"], string> = {
  matched: "Matched",
  tons_differ: "Tons differ",
  not_found: "Not in TicketFlow",
  outside_scope: "Outside this invoice line",
  ambiguous: "Several records — not matched",
  needs_review: "Not verified or incomplete",
  not_on_invoice: "Delivered, not on invoice",
};
const EVIDENCE_LABELS: Record<NonNullable<TicketCheckRow["evidence"]>, string> = {
  unmatched: "—",
  candidate: "Not confirmed",
  confirmed: "Confirmed",
  conflicting: "Conflict",
  invalidated: "No longer holds",
};
const FLAG_LABELS = {
  duplicate_on_invoice: "Repeated on this invoice",
  on_another_invoice: "Also on another invoice",
};
function resultStyle(row: TicketCheckRow) {
  if (row.result === "matched" && !row.flags.length) return "tf-status-open";
  if (row.result === "ambiguous" || row.result === "needs_review") return "tf-status-attention";
  if (row.result === "not_on_invoice" && row.verified_tons === null) return "tf-status-attention";
  return "tf-status-error";
}
/** Rebuild the pasted list (ticket TAB tons) so a saved line can be corrected. */
function ticketListText(row: InvoiceLineComparison) {
  return row.ticket_check.filter((r) => r.line !== null)
    .map((r) => r.invoice_ticket_number + (r.billed_tons ? "\t" + r.billed_tons : ""))
    .join("\n");
}
function RecordLinks({ ticket }: { ticket: Pick<CheckedTicket, "ticket_id" | "batch_id"> }) {
  return <span className="whitespace-nowrap">
    <a href={ticketImageUrl(ticket.ticket_id)} target="_blank" rel="noreferrer">Scan</a>
    {" · "}
    <a href={"/upload?batch=" + ticket.batch_id + "&ticket=" + ticket.ticket_id}>Review &amp; history</a>
  </span>;
}
function RecordSummary({ ticket }: { ticket: CheckedTicket }) {
  return <span>
    <span className="font-semibold">{ticket.ticket_number ?? "Unnumbered"}</span>
    <span className="block text-[11px] text-[#778493]">
      {[ticket.delivery_date ?? "no date", ticket.material ?? "no material",
        ticket.project ? "project " + ticket.project : "no project",
        ticket.review_status === "verified" ? "verified" : ticket.review_status.replaceAll("_", " ")].join(" · ")}
    </span>
  </span>;
}

const signedMoney = (value: string) => (Number(value) > 0 ? "+" : "") + money(value);

/** Why this result, in figures that add up exactly to what was billed. */
function WhyThisResult({ diagnosis, status }: { diagnosis: InvoiceDiagnosis; status: InvoiceLineComparison["status"] }) {
  const { tons: bridge, money: cost } = diagnosis;
  return <section aria-label="Why this result" className="border-b border-[#e2e7ec] p-5">
    <p className={"text-[14px] font-bold " + (status === "matches" ? "text-[#216c43]" : status === "discrepancy" ? "text-[#a73631]" : "text-[#995a18]")} data-testid="diagnosis-headline">{diagnosis.headline}</p>
    <div className="mt-4 grid gap-5 lg:grid-cols-2">
      {bridge && <div>
        <p className="mb-1.5 text-[12px] font-bold text-[#344352]">From approved tons to billed tons</p>
        <table className="tf-table" aria-label="From approved tons to billed tons"><tbody>
          <tr><td>Approved tons in this line&apos;s scope</td><td className="text-right font-semibold tabular-nums">{tons(bridge.start)}</td></tr>
          {bridge.items.map((item) => <tr key={item.kind}>
            <td>{item.label} <span className="text-[#7a8796]">({item.count})</span>
              {item.tickets.length > 0 && <span className="block font-mono text-[11px] text-[#657182]">{item.tickets.join(", ")}{item.count > item.tickets.length && " …"}</span>}
            </td>
            <td className="whitespace-nowrap text-right font-semibold tabular-nums text-[#a14c1c]">{signed(item.tons)}</td>
          </tr>)}
          {bridge.complete && Number(bridge.unexplained) !== 0 && <tr>
            <td>Not explained by the ticket list <span className="block text-[11px] text-[#657182]">The line&apos;s billed tons differ from the sum of its own tickets.</span></td>
            <td className="whitespace-nowrap text-right font-semibold tabular-nums text-[#a73631]">{signed(bridge.unexplained as string)}</td>
          </tr>}
          <tr className="border-t-2 border-[#cfd6de]"><td className="font-bold">Billed tons</td><td className="text-right font-bold tabular-nums">{tons(bridge.billed)}</td></tr>
        </tbody></table>
        {!bridge.complete && <p className="mt-1.5 text-[11px] text-[#995a18]">{bridge.rows_without_tons} ticket(s) have no billed tons on this invoice, so the tons can&apos;t be broken down exactly.</p>}
      </div>}
      <div>
        <p className="mb-1.5 text-[12px] font-bold text-[#344352]">From approved cost to billed subtotal</p>
        {cost ? <table className="tf-table" aria-label="From approved cost to billed subtotal"><tbody>
          <tr><td>Approved cost at locked rates</td><td className="text-right font-semibold tabular-nums">{money(cost.start)}</td></tr>
          {Number(cost.quantity_effect) !== 0 && <tr><td>Billed tons differ from approved tons</td><td className="text-right font-semibold tabular-nums text-[#a14c1c]">{signedMoney(cost.quantity_effect)}</td></tr>}
          {cost.rate_groups.map((group) => <tr key={group.locked_rate}>
            <td>Invoice rate differs from locked {money(group.locked_rate)} <span className="block text-[11px] text-[#657182]">{group.tickets} ticket(s), {tons(group.tons)} tons</span></td>
            <td className="text-right font-semibold tabular-nums text-[#a14c1c]">{signedMoney(group.effect)}</td>
          </tr>)}
          {Number(cost.arithmetic_effect) !== 0 && <tr><td>Invoice&apos;s own math <span className="block text-[11px] text-[#657182]">Subtotal differs from tons × rate on the invoice.</span></td><td className="text-right font-semibold tabular-nums text-[#a73631]">{signedMoney(cost.arithmetic_effect)}</td></tr>}
          {Number(cost.rounding) !== 0 && <tr><td>Rounding <span className="block text-[11px] text-[#657182]">Each ticket&apos;s cost is rounded to the cent.</span></td><td className="text-right font-semibold tabular-nums">{signedMoney(cost.rounding)}</td></tr>}
          <tr className="border-t-2 border-[#cfd6de]"><td className="font-bold">Billed material subtotal</td><td className="text-right font-bold tabular-nums">{money(cost.billed)}</td></tr>
        </tbody></table> : <p className="text-[12px] text-[#995a18]">Unresolved. {diagnosis.money_unresolved_reason}</p>}
      </div>
    </div>
    {diagnosis.next_steps.length > 0 && <div className="mt-4">
      <p className="mb-1 text-[12px] font-bold text-[#344352]">What to do next</p>
      <ol className="list-decimal space-y-1 pl-5 text-[12px] text-[#354454]">
        {diagnosis.next_steps.map((step) => <li key={step.text}>{step.href ? <a className="tf-link" href={step.href}>{step.text}</a> : step.text}</li>)}
      </ol>
    </div>}
  </section>;
}

export default function InvoiceComparisonPage() {
  const canManage = useAccount().can("invoices.manage");
  const [projects, setProjects] = useState<Project[]>([]);
  const [suppliers, setSuppliers] = useState<Supplier[]>([]);
  const [materials, setMaterials] = useState<Material[]>([]);
  const [lines, setLines] = useState<InvoiceLineComparison[]>([]);
  const [form, setForm] = useState<typeof EMPTY>({ ...EMPTY });
  const [editing, setEditing] = useState<number | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [history, setHistory] = useState<string[] | null>(null);
  const [saving, setSaving] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [overview, setOverview] = useState<InvoiceOverview | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [resultFilter, setResultFilter] = useState<"all" | "open" | "discrepancy" | "incomplete" | "matches">("all");
  const [lineSearch, setLineSearch] = useState("");
  const detail = useRef<HTMLElement>(null);

  const loadOverview = useCallback(() => {
    // A summary on top of the list: failing to load it must not stop anything.
    getInvoiceOverview().then(setOverview).catch(() => setOverview(null));
  }, []);
  const reload = useCallback(async () => {
    const data = await getInvoiceComparisons();
    setLines(data);
    loadOverview();
  }, [loadOverview]);

  useEffect(() => {
    Promise.all([
      listProjects(true), listSuppliers(), listMaterials(), getInvoiceComparisons(),
    ]).then(([p, s, m, rows]) => {
      setProjects(p); setSuppliers(s); setMaterials(m); setLines(rows);
    }).catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false));
    loadOverview();
  }, [loadOverview]);

  // Details sit below the list, so bring them into view when opened.
  useEffect(() => {
    if (selected !== null) detail.current?.scrollIntoView?.({ block: "start", behavior: "auto" });
  }, [selected]);

  function field<K extends keyof typeof EMPTY>(key: K, value: (typeof EMPTY)[K]) {
    setForm((old) => ({ ...old, [key]: value }));
  }

  function reset() {
    setEditing(null);
    setForm({ ...EMPTY });
    setError(null);
    setShowForm(false);
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
    setShowForm(true);
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
      ticket_list: ticketListText(row),
    });
    setError(null);
  }

  async function showHistory(id: number) {
    setError(null);
    try {
      const events = await getInvoiceHistory(id);
      setHistory(events.length ? events.map((event) =>
        event.field + ": " + (event.old ?? "—") + " → " + (event.new ?? "—")
        + " · " + (event.actor ?? "before individual accounts")
        + " · " + new Date(event.at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })
      ) : ["No corrections recorded."]);
      setSelected(id);
    } catch (exc) {
      setError((exc as Error).message);
    }
  }

  async function evidence(action: () => Promise<InvoiceLineComparison>) {
    setError(null);
    try {
      await action();
      await reload();
      setHistory(null);
    } catch (exc) {
      setError((exc as Error).message);
    }
  }

  async function downloadReport(id: number) {
    if (exporting) return;
    setExporting(true);
    setError(null);
    try {
      await downloadInvoiceComparisonReport(id);
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setExporting(false);
    }
  }

  const chosen = lines.find((line) => line.id === selected) ?? null;
  const search = lineSearch.trim().toLowerCase();
  const shownLines = lines.filter((line) => {
    if (resultFilter === "open" && line.status === "matches") return false;
    if (resultFilter === "discrepancy" && line.status !== "discrepancy") return false;
    if (resultFilter === "incomplete" && line.status !== "incomplete" && line.status !== "no_verified_tickets") return false;
    if (resultFilter === "matches" && line.status !== "matches") return false;
    return !search || [line.invoice_number, line.supplier_name, line.material_name].some((value) => value.toLowerCase().includes(search));
  });
  const formOpen = showForm || editing !== null || (!loading && lines.length === 0);
  const pastedRows = form.ticket_list.split(/\r?\n/).filter((line) => line.trim()).length;
  const required = form.project_id !== "" && form.invoice_number.trim() &&
    form.supplier_name.trim() && form.material_name.trim() && form.date_from &&
    form.date_to && form.billed_tons && form.billed_unit_price && form.billed_subtotal;

  return <div className="space-y-5">
    <div className="tf-page-heading">
      <div>
        <h1 className="tf-page-title">Invoice comparison</h1>
        <p className="tf-page-description">
          Compare supplier invoice lines with verified material deliveries by project, supplier, material and delivery dates.
        </p>
      </div>
      <div className="flex flex-wrap gap-2">
        <a href="/invoices/documents" className="tf-secondary-button">Invoice PDFs →</a>
        <a href="/records" className="tf-secondary-button">View ticket records →</a>
      </div>
    </div>

    <p className="rounded border border-[#e2e7ec] bg-white px-4 py-3 text-[12px] leading-5 text-[#47576a]">
      <strong>Comparison only — not approval for payment.</strong> Each line is compared with approved deliveries
      using the rates locked when they were approved. Tax, freight and other charges stay out of the material subtotal.
      Without the invoice&apos;s ticket numbers, only totals are compared.
    </p>
    {error && <div role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">{error}</div>}
    {!canManage && <p className="tf-notice" role="note">Invoice lines are recorded and corrected by managers and owners. You can review comparisons and download reports.</p>}

    {overview && overview.lines > 0 && <section className="tf-panel" aria-label="Invoice overview">
      <div className="grid divide-y divide-[#e7eaee] sm:grid-cols-2 sm:divide-x sm:divide-y-0 lg:grid-cols-4">
        <div className="px-5 py-4"><p className="text-[11px] font-bold uppercase tracking-[.055em] text-[#637181]">Billed material</p>
          <p className="mt-1.5 text-[22px] font-bold tabular-nums text-[#192632]">{money(overview.billed)}</p><p className="mt-1 text-[11px] text-[#7b8794]">{overview.lines} lines</p></div>
        <div className="px-5 py-4"><p className="text-[11px] font-bold uppercase tracking-[.055em] text-[#637181]">Approved cost</p>
          <p className="mt-1.5 text-[22px] font-bold tabular-nums text-[#192632]">{money(overview.verified)}</p>
          <p className="mt-1 text-[11px] text-[#7b8794]">{overview.lines_unresolved ? overview.lines_unresolved + " line(s) unresolved and left out" : "At locked rates"}</p></div>
        <div className="px-5 py-4"><p className="text-[11px] font-bold uppercase tracking-[.055em] text-[#637181]">Differences found</p>
          <p className={"mt-1.5 text-[22px] font-bold tabular-nums " + (overview.by_status.discrepancy ? "text-[#a73631]" : "text-[#192632]")}>{overview.by_status.discrepancy ?? 0}</p><p className="mt-1 text-[11px] text-[#7b8794]">Lines to take up with a supplier</p></div>
        <div className="px-5 py-4"><p className="text-[11px] font-bold uppercase tracking-[.055em] text-[#637181]">Can&apos;t settle yet</p>
          <p className={"mt-1.5 text-[22px] font-bold tabular-nums " + ((overview.by_status.incomplete || overview.by_status.no_verified_tickets) ? "text-[#b85c2b]" : "text-[#192632]")}>{(overview.by_status.incomplete ?? 0) + (overview.by_status.no_verified_tickets ?? 0)}</p><p className="mt-1 text-[11px] text-[#7b8794]">Waiting on deliveries or review</p></div>
      </div>
      {overview.by_supplier.length > 1 && <details className="border-t border-[#e7eaee]">
        <summary className="cursor-pointer select-none px-5 py-2 text-[12px] font-semibold text-[#46566a]">By supplier</summary>
        <div className="tf-table-wrap"><table className="tf-table" aria-label="Invoices by supplier">
          <thead><tr><th>Supplier</th><th className="text-right">Lines</th><th className="text-right">Billed</th><th className="text-right">Approved cost</th><th className="text-right">Still open</th></tr></thead>
          <tbody>{overview.by_supplier.map((group) => <tr key={group.supplier}>
            <td className="font-semibold">{group.supplier}</td><td className="text-right tabular-nums">{group.lines}</td><td className="text-right tabular-nums">{money(group.billed)}</td>
            <td className="text-right tabular-nums">{money(group.verified)}{group.lines_unresolved > 0 && <span className="block text-[10px] font-bold text-[#a14c1c]">{group.lines_unresolved} unresolved</span>}</td>
            <td className="text-right tabular-nums">{group.open}</td>
          </tr>)}</tbody>
        </table></div>
      </details>}
      {overview.exceptions.length > 0 && <div className="border-t border-[#f0d8ad] bg-[#fff9ed] px-5 py-3" role="note" aria-label="Billing patterns to check">
        <p className="text-[12px] font-bold text-[#754e20]">Billing patterns worth a question ({overview.exceptions.length})</p>
        <ul className="mt-1 space-y-1.5 text-[12px] text-[#754e20]">
          {overview.exceptions.slice(0, 10).map((item) => <li key={item.kind + item.title + item.line_ids.join()}>
            <strong>{item.title}.</strong> {item.detail}
            {item.line_ids.slice(0, 4).map((id) => {
              const line = lines.find((row) => row.id === id);
              return line && <button key={id} type="button" className="tf-link ml-2" onClick={() => { setSelected(id); setHistory(null); }}>Open {line.invoice_number}</button>;
            })}
          </li>)}
        </ul>
      </div>}
    </section>}

    {canManage && !formOpen && <div><button type="button" className="tf-primary-button" onClick={() => setShowForm(true)}><span aria-hidden="true">+</span> Record an invoice line</button></div>}
    {canManage && formOpen && <section className="tf-panel">
      <div className="tf-panel-heading">
        <div>
          <h2 className="tf-panel-title">{editing === null ? "Record an invoice material line" : "Correct invoice line #" + editing}</h2>
          <p className="tf-panel-description">Typed by a person. To draft from a PDF, use <a className="tf-link" href="/invoices/documents">Invoice PDFs</a>.</p>
        </div>
        {(editing !== null || lines.length > 0) && <button type="button" className="tf-secondary-button" onClick={reset}>{editing !== null ? "Cancel editing" : "Close"}</button>}
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
        <div className="sm:col-span-2 xl:col-span-3">
          <label className="tf-field-label" htmlFor="invoice-ticket-list">
            Ticket numbers listed on this invoice (optional)
          </label>
          <textarea id="invoice-ticket-list" className="tf-field mt-1 h-32 w-full font-mono"
            aria-describedby="invoice-ticket-list-help" spellCheck={false} maxLength={200000}
            value={form.ticket_list} placeholder={"10482\t24.50\n10483\t22.10"}
            onChange={(e) => field("ticket_list", e.target.value)}/>
          <p id="invoice-ticket-list-help" className="mt-1 text-[11px] text-[#718091]">
            Paste two columns from Excel: ticket number, then billed tons (tons optional). One ticket per row;
            a header row is fine. Leading zeros count. Leave blank if the invoice shows totals only.
            {pastedRows > 0 && <strong> {pastedRows} row{pastedRows === 1 ? "" : "s"} entered.</strong>}
          </p>
        </div>
        <div className="sm:col-span-2 xl:col-span-3 flex items-center gap-3">
          <button type="submit" className="tf-primary-button" disabled={!required || saving}>
            {saving ? "Saving…" : editing === null ? "Compare invoice line" : "Save corrected line"}
          </button>
          <span className="text-[11px] text-[#718091]">All comparisons update if the underlying ticket verification changes.</span>
        </div>
      </form>
    </section>}

    <section className="tf-panel">
      <div className="tf-panel-heading flex-wrap">
        <div>
          <h2 className="tf-panel-title">Saved invoice comparisons</h2>
          <p className="tf-panel-description">Matches use verified ticket quantities and locked historical prices only.</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label className="sr-only" htmlFor="line-search">Search invoice lines</label>
          <input id="line-search" type="search" className="tf-field !min-h-0 !py-1.5 !text-xs" placeholder="Invoice, supplier, material…" value={lineSearch} onChange={(e) => setLineSearch(e.target.value)} />
          <div className="tf-segmented" role="group" aria-label="Filter invoice lines">
            {([["all", "All"], ["open", "Open"], ["discrepancy", "Differences"], ["incomplete", "Can't settle"], ["matches", "Match"]] as const).map(([key, label]) =>
              <button type="button" key={key} aria-pressed={resultFilter === key} onClick={() => setResultFilter(key)}>{label}</button>)}
          </div>
          <span className="tf-status">{shownLines.length === lines.length ? lines.length : `${shownLines.length} of ${lines.length}`} material lines</span>
        </div>
      </div>
      <div className="tf-table-wrap">
        <table className="tf-table">
          <thead><tr>
            {["Invoice", "Result", "Supplier / material", "Dates", "Billed tons", "Verified tons", "Tons diff.", "Amount diff.", ""].map((h, i) => <th key={h} className={i >= 4 && i <= 7 ? "text-right" : ""}>{h}</th>)}
          </tr></thead>
          <tbody>{shownLines.map((row) => <tr key={row.id} aria-selected={selected === row.id} className={selected === row.id ? "bg-[#fff8f2]" : ""}>
            <td className="max-w-[150px] [overflow-wrap:anywhere]"><span className="font-semibold">{row.invoice_number}</span><span className="block text-[11px] text-[#778493]">{projects.find((p) => p.id === row.project_id)?.code ?? row.project_id}</span></td>
            <td>
              <span className={"tf-status " + statusStyle(row.status)}>{statusLabel(row)}</span>
              <span className="mt-0.5 block max-w-[150px] text-[11px] leading-4 text-[#778493]">
                {row.comparison_mode === "itemized" ? "Tickets checked" : "Totals only — tickets not checked"}
              </span>
            </td>
            <td><span className="font-semibold">{row.supplier_name}</span><span className="block text-[11px] text-[#778493]">{row.material_name}</span></td>
            <td className="whitespace-nowrap text-[11px]">{row.date_from}<span className="block">{row.date_to}</span></td>
            <td className="text-right tabular-nums">{tons(row.billed_tons)}</td>
            <td className="text-right tabular-nums">{tons(row.verified_tons)}</td>
            <td className="text-right tabular-nums">{signed(row.quantity_difference)}</td>
            <td className="text-right tabular-nums">{row.subtotal_difference === null ? "Unresolved" : money(row.subtotal_difference)}</td>
            <td className="whitespace-nowrap text-right">
              <button type="button" onClick={() => { setSelected(row.id); setHistory(null); }} className="tf-link block w-full text-right">Details</button>
              {canManage && <button type="button" onClick={() => edit(row)} className="tf-link mt-1 block w-full text-right">Correct</button>}
            </td>
          </tr>)}</tbody>
        </table>
        {!loading && !lines.length && <p className="tf-empty">No invoice lines entered yet. Add one above to compare it with approved tickets.</p>}
        {!loading && lines.length > 0 && !shownLines.length && <p className="tf-empty">No invoice lines match this view.</p>}
        {loading && <p className="tf-empty">Loading invoice comparisons…</p>}
      </div>
    </section>

    {chosen && <section ref={detail} className="tf-panel scroll-mt-2">
      <div className="tf-panel-heading">
        <div>
          <h2 className="tf-panel-title">Invoice {chosen.invoice_number} · {chosen.material_name}</h2>
          <p className="tf-panel-description">
            {chosen.comparison_mode === "itemized" ? "Each invoice ticket number checked against verified deliveries." : TOTALS_ONLY}
            {" "}Current comparison, not an invoice approval or payment recommendation.
            {chosen.source_document_id && <> <a className="tf-link" href={"/invoices/documents?id=" + chosen.source_document_id}>View original invoice, page {chosen.source_page}</a></>}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button type="button" className="tf-secondary-button" disabled={exporting}
            onClick={() => void downloadReport(chosen.id)}>
            {exporting ? "Preparing report…" : "Download CSV report"}
          </button>
          <button type="button" className="tf-secondary-button" onClick={() => void showHistory(chosen.id)}>
            Correction history
          </button>
        </div>
      </div>
      {chosen.diagnosis && <WhyThisResult diagnosis={chosen.diagnosis} status={chosen.status} />}
      <details className="border-b border-[#e2e7ec]">
      <summary className="cursor-pointer select-none px-5 py-2 text-[12px] font-semibold text-[#46566a]">All figures on this line</summary>
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
      {chosen.warnings.length > 0 && <div className="border-t border-[#f0d8ad] bg-[#fff9ed] px-5 py-3 text-[12px] text-[#754e20]">
        <p className="font-bold">Every check that raised something</p>
        <ul className="mt-1 list-disc space-y-1 pl-5">
          {chosen.warnings.map((message) => <li key={message}>{message}</li>)}
        </ul>
      </div>}
      </details>
      {chosen.comparison_mode === "totals_only" && <div role="note" className="border-t border-[#e2e7ec] bg-[#f6f8fa] px-5 py-3 text-[12px] text-[#344352]">
        <p className="font-bold">{TOTALS_ONLY}</p>
        <p>Matching totals do not show that every ticket was billed correctly. Enter the ticket numbers from the invoice to check each delivery.</p>
      </div>}
      {chosen.comparison_mode === "itemized" && <div className="px-5 pt-5">
        <p className="text-[12px] font-bold text-[#344352]">Ticket-by-ticket check</p>
        <p className="mb-3 text-[12px] text-[#526273]">
          {Object.entries(RESULT_LABELS).filter(([key]) => chosen.ticket_check_counts[key])
            .map(([key, label]) => chosen.ticket_check_counts[key] + " " + label.toLowerCase()).join(" · ")}
          {Object.entries(FLAG_LABELS).filter(([key]) => chosen.ticket_check_counts[key])
            .map(([key, label]) => " · " + chosen.ticket_check_counts[key] + " " + label.toLowerCase()).join("")}
          {chosen.itemized_billed_tons !== null && <> · itemized tons {tons(chosen.itemized_billed_tons)}</>}
        </p>
        {chosen.evidence && <div className="mb-3 flex flex-wrap items-center justify-between gap-3 rounded border border-[#e2e7ec] bg-[#f6f8fa] px-3 py-2 text-[12px] text-[#344352]">
          <p data-testid="evidence-summary">
            <strong>{chosen.evidence.confirmed} of {chosen.evidence.listed_tickets} invoice tickets confirmed by a person</strong>
            {" "}({tons(chosen.evidence.confirmed_tons)} tons).
            {chosen.evidence.candidates > 0 && <> {chosen.evidence.candidates} clean match{chosen.evidence.candidates === 1 ? "" : "es"} not yet confirmed.</>}
            {chosen.evidence.invalidated > 0 && <> {chosen.evidence.invalidated} no longer hold.</>}
            {chosen.evidence.conflicting > 0 && <> {chosen.evidence.conflicting} in conflict.</>}
          </p>
          {canManage && chosen.evidence.candidates > 0 && <button type="button" className="tf-secondary-button"
            onClick={() => void evidence(() => confirmTicketLinks(chosen.id, chosen.ticket_check
              .filter((r) => r.evidence === "candidate" && r.ticket).map((r) => r.ticket!.ticket_id)))}>
            Confirm {chosen.evidence.candidates} clean match{chosen.evidence.candidates === 1 ? "" : "es"}
          </button>}
        </div>}
        <div className="tf-table-wrap"><table className="tf-table" aria-label="Ticket-by-ticket check">
          <thead><tr><th>Line</th><th>Invoice ticket</th><th className="text-right">Billed tons</th><th>Result</th><th>TicketFlow record</th><th className="text-right">Verified tons</th><th className="text-right">Difference</th><th>Evidence</th></tr></thead>
          <tbody>{chosen.ticket_check.map((row, index) => <tr key={index}>
            <td className="tabular-nums">{row.line ?? "—"}</td>
            <td className="font-mono">{row.invoice_ticket_number ?? <span className="font-sans text-[#778493]">Not listed</span>}</td>
            <td className="text-right tabular-nums">{row.billed_tons ? tons(row.billed_tons) : "—"}</td>
            <td>
              <span className={"tf-status " + resultStyle(row)}>{RESULT_LABELS[row.result]}</span>
              {row.flags.map((flag) => <span key={flag} className="tf-status tf-status-error ml-1">{FLAG_LABELS[flag]}</span>)}
              {row.notes.map((note) => <span key={note} className="block max-w-[28rem] text-[11px] text-[#5d6b7a]">{note}</span>)}
            </td>
            <td className="min-w-[210px]">{row.ticket ? <><RecordSummary ticket={row.ticket}/><RecordLinks ticket={row.ticket}/></> : row.candidates.length ?
              row.candidates.map((c) => <span key={c.ticket_id} className="block"><RecordSummary ticket={c}/><RecordLinks ticket={c}/></span>) : "—"}</td>
            <td className="text-right tabular-nums">{row.verified_tons ? tons(row.verified_tons) : "—"}</td>
            <td className="text-right tabular-nums">{row.tons_difference ? signed(row.tons_difference) : "—"}</td>
            <td className="whitespace-nowrap">
              {EVIDENCE_LABELS[row.evidence ?? "unmatched"]}
              {canManage && row.evidence === "invalidated" && row.result === "matched" && row.ticket && <button type="button" className="tf-link ml-2"
                onClick={() => void evidence(() => confirmTicketLinks(chosen.id, [row.ticket!.ticket_id]))}>Re-confirm</button>}
              {canManage && row.link_id && <button type="button" className="tf-link ml-2"
                onClick={() => void evidence(() => releaseTicketLink(chosen.id, row.link_id as number))}>Release</button>}
            </td>
          </tr>)}</tbody>
        </table></div>
      </div>}
      <div className="p-5">
        {(() => {
          // Itemized lines already list every in-scope delivery above; only
          // unclassified tickets (unknown supplier, material or date) remain.
          const shown = chosen.comparison_mode === "itemized"
            ? chosen.tickets.filter((t) => t.scope_match === "unclassified") : chosen.tickets;
          if (chosen.comparison_mode === "itemized" && !shown.length) return null;
          return <>
        <p className="mb-3 text-[12px] font-bold text-[#344352]">{chosen.comparison_mode === "itemized"
          ? "Unreviewed tickets that might belong to this invoice line"
          : "Tickets in this supplier, material and date range"}</p>
        {shown.length ? <div className="tf-table-wrap"><table className="tf-table">
          <thead><tr><th>Ticket</th><th>Review status</th><th>Scope</th><th>Tons</th><th>Locked rate</th><th>Record</th></tr></thead>
          <tbody>{shown.map((ticket) => <tr key={ticket.ticket_id}>
            <td>{ticket.ticket_number ?? "Unnumbered"}</td>
            <td>{ticket.review_status === "verified" ? "Verified" : "Needs review (" + ticket.review_status.replaceAll("_", " ") + ")"}</td>
            <td>{ticket.scope_match === "exact" ? "In scope" : "Potential match — classification needed"}</td>
            <td>{ticket.tons ?? "—"}</td>
            <td>{money(ticket.confirmed_price)}</td>
            <td><RecordLinks ticket={ticket}/></td>
          </tr>)}</tbody>
        </table></div> : <p className="text-[12px] text-[#718091]">No matching tickets in this scope. Verify supplier spelling and delivery dates.</p>}
          </>;
        })()}
        {history && <div className="mt-4 border-t border-[#e2e7ec] pt-4">
          <p className="mb-2 text-[12px] font-bold">Invoice entry corrections</p>
          {history.map((entry, index) => <p key={index} className="text-[12px] text-[#526273]">{entry}</p>)}
        </div>}
      </div>
    </section>}
  </div>;
}
