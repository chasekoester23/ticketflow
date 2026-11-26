"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { InvoiceRegister, RegisterInvoice, getInvoiceRegister } from "../../lib/api";
import { useAccount } from "../account-context";
import { useProject } from "../project-context";

// Display only: the server adds every amount up exactly.
const money = (v: string | null) => v === null ? "—"
  : `${Number(v) < 0 ? "-" : ""}$${Math.abs(Number(v)).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const day = (iso: string) => new Date(`${iso}T00:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
const STATUS: Record<RegisterInvoice["status"], [string, string]> = {
  matched: ["Matches approved", "tf-status-open"], discrepancy: ["Differences", "tf-status-error"], incomplete: ["Can't compare yet", "tf-status-attention"],
};

/**
 * Every supplier and trucking invoice, with what was billed against what approved
 * records support. A reconciliation, never a payment approval.
 */
export default function InvoiceRegisterPage() {
  const { can } = useAccount();
  const { projectId, project } = useProject();
  const [register, setRegister] = useState<InvoiceRegister | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [kind, setKind] = useState<"" | "supplier" | "trucking">("");
  const [status, setStatus] = useState<"" | RegisterInvoice["status"]>("");
  const [query, setQuery] = useState("");
  const [adding, setAdding] = useState(false);

  useEffect(() => {
    // The line comparison used to live at this address; its old links still open it.
    const line = new URLSearchParams(window.location.search).get("line");
    if (line) window.location.replace(`/invoices/lines?line=${encodeURIComponent(line)}`);
  }, []);
  useEffect(() => {
    let live = true;
    getInvoiceRegister(projectId || undefined).then((r) => { if (live) { setRegister(r); setError(null); } })
      .catch((e) => { if (live) setError(e.message); });
    return () => { live = false; };
  }, [projectId]);

  const rows = useMemo(() => {
    const words = query.trim().toLowerCase();
    return (register?.invoices ?? []).filter((r) => (!kind || r.kind === kind) && (!status || r.status === status)
      && (!words || `${r.invoice_number} ${r.company ?? ""} ${r.project?.code ?? ""}`.toLowerCase().includes(words)));
  }, [register, kind, status, query]);
  const t = register?.totals;
  const tiles: [string, string, string, boolean][] = [
    ["Invoices", t ? t.invoices.toLocaleString() : "…", "Supplier and trucking", false],
    ["Billed", t ? money(t.billed) : "…", "As invoiced", false],
    ["With differences", t ? t.with_differences.toLocaleString() : "…", "Billed differently from approved records", Boolean(t && t.with_differences > 0)],
    ["Can't compare yet", t ? t.incomplete.toLocaleString() : "…", "Waiting on approvals or a rate", false],
  ];

  return <div className="space-y-4">
    <div className="tf-page-heading">
      <div><h1 className="tf-page-title">Invoices</h1>
        <p className="tf-page-description">Supplier and trucking invoices{project ? ` for ${project.code}` : ""}, checked against approved deliveries and shifts. Not an approval for payment.</p></div>
      {can("invoices.manage") && <div className="relative">
        <button type="button" className="tf-primary-button" aria-haspopup="menu" aria-expanded={adding} onClick={() => setAdding((v) => !v)}>+ Add invoice</button>
        {adding && <div role="menu" aria-label="Add invoice" className="tf-upload-menu">
          <Link role="menuitem" prefetch={false} href="/invoices/documents"><strong>Upload supplier invoice PDF</strong><span>Its lines are drafted for you to confirm</span></Link>
          <Link role="menuitem" prefetch={false} href="/invoices/lines"><strong>Type a supplier invoice line</strong><span>For a paper or scanned invoice</span></Link>
          <Link role="menuitem" prefetch={false} href="/trucking?tab=invoices&new=1"><strong>Enter a trucking invoice</strong><span>Checked against approved shifts</span></Link>
        </div>}
      </div>}
    </div>
    {error && <div role="alert" className="tf-alert tf-alert-bad">{error}</div>}

    <section className="tf-panel" aria-label="Invoice totals"><div className="grid grid-cols-2 lg:grid-cols-4">
      {tiles.map(([label, value, note, warn]) => <div key={label} className="border-b border-r border-[#dce1e6] px-5 py-4 lg:border-b-0">
        <p className="tf-stat-label">{label}</p><p className={warn ? "text-[#a73631]" : "text-[#041d2f]"}>{value}</p>
        <span className="text-[11.5px] text-[#5f6c7b]">{note}</span></div>)}
    </div></section>

    <section className="tf-panel" aria-labelledby="register-title">
      <div className="tf-panel-heading flex-wrap">
        <h2 id="register-title" className="tf-panel-title">Invoice register</h2>
        <div className="flex flex-wrap items-center gap-2">
          <input type="search" className="tf-field w-[220px]" placeholder="Invoice #, company" aria-label="Search invoices" value={query} onChange={(e) => setQuery(e.target.value)} />
          <select className="tf-field" aria-label="Invoice type" value={kind} onChange={(e) => setKind(e.target.value as typeof kind)}>
            <option value="">All types</option><option value="supplier">Supplier (material)</option><option value="trucking">Trucking</option></select>
          <select className="tf-field" aria-label="Comparison result" value={status} onChange={(e) => setStatus(e.target.value as typeof status)}>
            <option value="">Any result</option><option value="discrepancy">Differences</option><option value="incomplete">Can&apos;t compare yet</option><option value="matched">Matches</option></select>
        </div>
      </div>
      {register === null ? <p className="tf-empty" role="status">Loading invoices…</p>
        : rows.length ? <div className="tf-table-wrap"><table className="tf-table" aria-label="Invoice register">
          <thead><tr><th>Invoice</th><th>Type</th><th className="tf-hide-narrow">Project</th><th className="tf-hide-narrow">Period</th>
            <th className="text-right">Billed</th><th className="text-right">Approved records</th><th className="text-right">Difference</th><th>Result</th></tr></thead>
          <tbody>{rows.map((r) => <tr key={`${r.kind}-${r.id}`}>
            <td className="min-w-[180px]"><Link prefetch={false} href={r.href}>{r.invoice_number}</Link>
              <span className="block text-[11.5px] text-[#4b5a69]">{r.company ?? "Unknown company"} · {r.lines} line{r.lines === 1 ? "" : "s"}{r.has_document ? " · original attached" : ""}</span></td>
            <td>{r.kind === "supplier" ? "Supplier" : "Trucking"}</td>
            <td className="tf-hide-narrow">{r.project?.code ?? "—"}</td>
            <td className="tf-hide-narrow whitespace-nowrap text-[#5f6c7b]">{day(r.period_from)} – {day(r.period_to)}</td>
            <td className="text-right whitespace-nowrap">{money(r.billed)}</td>
            <td className="text-right whitespace-nowrap">{money(r.compared)}</td>
            <td className={`text-right whitespace-nowrap font-semibold ${r.difference && Number(r.difference) !== 0 ? "text-[#a73631]" : ""}`}>{money(r.difference)}</td>
            <td><span className={`tf-status ${STATUS[r.status][1]}`}>{STATUS[r.status][0]}</span>
              {!r.ticket_level && <span className="block text-[11px] text-[#995a18]" title="No ticket numbers were entered, so deliveries are compared in total, not one by one.">Totals only</span>}</td>
          </tr>)}</tbody>
        </table></div>
        : <p className="tf-empty" data-testid="register-empty">{register.invoices.length ? "No invoice matches these filters."
          : can("invoices.manage") ? "No invoices yet. Use + Add invoice to upload a supplier PDF, type its lines, or enter a trucking invoice."
          : "No invoices yet. A manager or owner records supplier and trucking invoices."}</p>}
    </section>
    <p className="text-[11.5px] text-[#5f6c7b]">&ldquo;Approved records&rdquo; is what approved tickets and shifts support at their locked rates. &ldquo;Totals only&rdquo; means the invoice listed no ticket numbers, so its deliveries are compared in total, not one by one.</p>
  </div>;
}
