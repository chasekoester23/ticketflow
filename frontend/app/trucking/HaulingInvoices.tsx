"use client";

import Link from "next/link";
import { Fragment, useState } from "react";
import {
  HaulerRow, HaulingInvoice, HaulingInvoiceLine, Project, attachHaulingInvoice, haulingInvoiceUrl, saveHaulingInvoice,
} from "../../lib/api";

const RESULT: Record<string, [string, string]> = {
  matched: ["Matches approved shift", "tf-status-open"], differs: ["Differs", "tf-status-attention"], repeat: ["Billed again", "tf-status-error"],
  no_shift: ["No shift recorded", "tf-status-error"], not_approved: ["Shift not approved", "tf-status-attention"],
  ambiguous: ["Which truck?", "tf-status-attention"], fee: ["Fee: not in an agreement", "tf-status-attention"],
};
const KIND = { hours: "Working hours", standby: "Standby", minimum: "Minimum charge", fee: "Fee" } as const;
// Display only: server decimals are shown, never added up here.
const money = (v: string | null | undefined) => v == null ? "—" : (v.startsWith("-") ? "−$" + Number(v.slice(1)).toLocaleString(undefined, { minimumFractionDigits: 2 })
  : "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const blankLine = (): HaulingInvoiceLine => ({ kind: "hours", truck_identifier: "", work_date: "", hours: "", rate: "", amount: "", description: "" });

function InvoiceForm({ haulers, projects, start, onSaved, onCancel }: {
  haulers: HaulerRow[]; projects: Project[]; start: HaulingInvoice | null; onSaved: () => Promise<void>; onCancel: () => void;
}) {
  const [head, setHead] = useState({
    hauler_id: start ? String(start.hauler_id) : "", project_id: start?.project_id ? String(start.project_id) : "",
    invoice_number: start?.invoice_number ?? "", invoice_date: start?.invoice_date ?? "", period_from: start?.period_from ?? "", period_to: start?.period_to ?? "",
  });
  const [lines, setLines] = useState<HaulingInvoiceLine[]>(start ? start.lines.map((l) => ({ ...l })) : [blankLine()]);
  const [error, setError] = useState<string | null>(null);
  const set = (n: number, patch: Partial<HaulingInvoiceLine>) => setLines(lines.map((l, i) => i === n ? { ...l, ...patch } : l));
  const text = (v: string | null) => v && v.trim() ? v.trim() : null;
  async function save() {
    setError(null);
    try {
      await saveHaulingInvoice({
        hauler_id: Number(head.hauler_id), project_id: head.project_id ? Number(head.project_id) : null, invoice_number: head.invoice_number,
        invoice_date: head.invoice_date || null, period_from: head.period_from, period_to: head.period_to,
        lines: lines.map((l) => ({ kind: l.kind, truck_identifier: text(l.truck_identifier), work_date: l.work_date || null, hours: text(l.hours),
          rate: text(l.rate), amount: l.amount, description: text(l.description) })),
      }, start ? { id: start.id, revision: start.revision } : undefined);
      await onSaved();
    } catch (e) { setError((e as Error).message); }
  }
  return <form className="space-y-3 border-b border-[#e9edf0] p-5" aria-label={start ? "Correct hauling invoice" : "Enter hauling invoice"}
    onSubmit={(e) => { e.preventDefault(); void save(); }}>
    <div className="grid gap-3 sm:grid-cols-3 xl:grid-cols-6">
      <label className="tf-field-label">Hauler<select required className="tf-field w-full" value={head.hauler_id} onChange={(e) => setHead({ ...head, hauler_id: e.target.value })}>
        <option value="">Select</option>{haulers.map((h) => <option key={h.id} value={h.id}>{h.name}</option>)}</select></label>
      <label className="tf-field-label">Project<select className="tf-field w-full" value={head.project_id} onChange={(e) => setHead({ ...head, project_id: e.target.value })}>
        <option value="">From each shift</option>{projects.map((p) => <option key={p.id} value={p.id}>{p.code}</option>)}</select></label>
      <label className="tf-field-label">Invoice number<input required className="tf-field w-full" value={head.invoice_number} onChange={(e) => setHead({ ...head, invoice_number: e.target.value })} /></label>
      <label className="tf-field-label">Invoice date<input type="date" className="tf-field w-full" value={head.invoice_date} onChange={(e) => setHead({ ...head, invoice_date: e.target.value })} /></label>
      <label className="tf-field-label">Period from<input required type="date" className="tf-field w-full" value={head.period_from} onChange={(e) => setHead({ ...head, period_from: e.target.value })} /></label>
      <label className="tf-field-label">Period to<input required type="date" className="tf-field w-full" value={head.period_to} onChange={(e) => setHead({ ...head, period_to: e.target.value })} /></label>
    </div>
    <div className="tf-table-wrap"><table className="tf-table" aria-label="Invoice lines">
      <thead><tr><th>Charge</th><th>Truck</th><th>Date worked</th><th className="text-right">Hours</th><th className="text-right">Rate</th><th className="text-right">Amount</th><th>Description</th><th /></tr></thead>
      <tbody>{lines.map((l, n) => <tr key={n}>
        <td><select aria-label="Charge" className="tf-field" value={l.kind} onChange={(e) => set(n, { kind: e.target.value as HaulingInvoiceLine["kind"] })}>
          {Object.entries(KIND).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></td>
        <td><input aria-label="Truck" className="tf-field w-24" value={l.truck_identifier ?? ""} onChange={(e) => set(n, { truck_identifier: e.target.value })} /></td>
        <td><input aria-label="Date worked" type="date" className="tf-field" value={l.work_date ?? ""} required={l.kind !== "fee"} onChange={(e) => set(n, { work_date: e.target.value })} /></td>
        <td><input aria-label="Hours" inputMode="decimal" className="tf-field w-20 text-right" value={l.hours ?? ""} onChange={(e) => set(n, { hours: e.target.value })} /></td>
        <td><input aria-label="Rate" inputMode="decimal" className="tf-field w-24 text-right" value={l.rate ?? ""} onChange={(e) => set(n, { rate: e.target.value })} /></td>
        <td><input aria-label="Amount" required inputMode="decimal" className="tf-field w-28 text-right" value={l.amount} onChange={(e) => set(n, { amount: e.target.value })} /></td>
        <td><input aria-label="Description" className="tf-field w-full" value={l.description ?? ""} onChange={(e) => set(n, { description: e.target.value })} /></td>
        <td>{lines.length > 1 && <button type="button" className="tf-link text-[12px]" onClick={() => setLines(lines.filter((_, i) => i !== n))}>Remove</button>}</td>
      </tr>)}</tbody>
    </table></div>
    {error && <p role="alert" className="tf-alert tf-alert-bad">{error}</p>}
    <div className="flex flex-wrap gap-2">
      <button type="button" className="tf-secondary-button" onClick={() => setLines([...lines, blankLine()])}>+ Add line</button>
      <button type="submit" className="tf-primary-button">{start ? "Save corrections" : "Save and check"}</button>
      <button type="button" className="tf-secondary-button" onClick={onCancel}>Cancel</button>
    </div>
  </form>;
}

export function HaulingInvoices({ invoices, haulers, projects, canManage, onChanged, onOpenShift }: {
  invoices: HaulingInvoice[]; haulers: HaulerRow[]; projects: Project[]; canManage: boolean;
  onChanged: () => Promise<void>; onOpenShift: (id: number) => void;
}) {
  const [editing, setEditing] = useState<HaulingInvoice | "new" | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  return <section className="tf-panel" aria-label="Hauling invoices">
    <div className="tf-panel-heading">
      <div><h2 className="tf-panel-title">Hauling invoices</h2>
        <p className="tf-panel-description">Each line is checked against the approved shift for that truck and day. Differences, repeats, fees and missing shifts go to <Link prefetch={false} className="tf-link" href="/invoices/exceptions?category=hours">Invoice exceptions</Link>.</p></div>
      {canManage && editing === null && <button type="button" className="tf-secondary-button" onClick={() => setEditing("new")}>+ Enter an invoice</button>}
    </div>
    {editing !== null && <InvoiceForm haulers={haulers} projects={projects} start={editing === "new" ? null : editing}
      onSaved={async () => { setEditing(null); await onChanged(); }} onCancel={() => setEditing(null)} />}
    {error && <p role="alert" className="tf-alert tf-alert-bad m-4">{error}</p>}
    {invoices.length === 0 ? <p className="tf-empty">No hauling invoices yet. Enter one from the hauler&apos;s bill to check it against approved shifts.</p>
      : <div className="tf-table-wrap"><table className="tf-table" aria-label="Hauling invoice list">
        <thead><tr><th>Invoice</th><th>Hauler</th><th>Period</th><th className="text-right">Billed</th><th className="text-right">Supported</th><th className="text-right">Difference</th><th>To check</th><th /></tr></thead>
        <tbody>{invoices.map((inv) => <Fragment key={inv.id}>
          <tr className="cursor-pointer" onClick={() => setOpen(open === inv.id ? null : inv.id)}>
            <td className="font-semibold"><button type="button" aria-expanded={open === inv.id} className="hover:underline">{inv.invoice_number}</button></td>
            <td>{inv.hauler}</td><td className="whitespace-nowrap tabular-nums">{inv.period_from} – {inv.period_to}</td>
            <td className="text-right tabular-nums">{money(inv.billed)}</td>
            <td className="text-right tabular-nums">{inv.supported === null ? <span className="text-[#5f6c7b]">Not yet known</span> : money(inv.supported)}</td>
            <td className={`text-right font-semibold tabular-nums ${inv.difference && Number(inv.difference) > 0 ? "text-[#a3511d]" : ""}`}>{money(inv.difference)}</td>
            <td>{inv.exceptions ? <span className="tf-status tf-status-attention">{inv.exceptions}</span> : <span className="tf-status tf-status-open">None</span>}</td>
            <td className="text-right">{inv.document ? <a className="tf-link text-[12px]" href={haulingInvoiceUrl(inv.id)} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>Original</a> : null}</td>
          </tr>
          {open === inv.id && <tr><td colSpan={8} className="bg-[#fafbfc]">
            <table className="tf-table" aria-label={`Lines of ${inv.invoice_number}`}><tbody>{inv.lines.map((l) => <tr key={l.line_no}>
              <td className="tabular-nums">{l.line_no}</td><td>{KIND[l.kind]}{l.description ? ` · ${l.description}` : ""}</td>
              <td>{l.truck_identifier ?? "—"}</td><td className="tabular-nums">{l.work_date ?? "—"}</td>
              <td className="text-right tabular-nums">{l.hours ? `${l.hours} h × ${money(l.rate)}` : "—"}</td>
              <td className="text-right tabular-nums">{money(l.amount)}</td>
              <td>{l.result && <span className={`tf-status ${RESULT[l.result][1]}`}>{RESULT[l.result][0]}</span>}
                {l.difference && Number(l.difference) !== 0 && <span className="ml-1 tabular-nums text-[11px]">{Number(l.difference) > 0 ? "+" : ""}{money(l.difference)}</span>}</td>
              <td>{l.shift_id ? <button type="button" className="tf-link text-[12px]" onClick={() => onOpenShift(l.shift_id!)}>Shift</button> : null}</td>
            </tr>)}</tbody></table>
            {canManage && <div className="flex flex-wrap items-center gap-3 p-2">
              <button type="button" className="tf-secondary-button" onClick={() => setEditing(inv)}>Correct this invoice</button>
              <label className="tf-link cursor-pointer text-[12px]">{inv.document ? "Replace the original" : "Attach the original (PDF or photo)"}
                <input type="file" className="sr-only" accept=".pdf,.jpg,.jpeg,.png" onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) attachHaulingInvoice(inv.id, file).then(onChanged).then(() => setError(null)).catch((err) => setError((err as Error).message));
                }} /></label>
            </div>}
          </td></tr>}
        </Fragment>)}</tbody>
      </table></div>}
  </section>;
}
