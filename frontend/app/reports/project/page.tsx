"use client";

import { useEffect, useState } from "react";
import { Project, ProjectReport, ReportGroup, getProjectReport, listProjects, projectReportUrl } from "../../../lib/api";
import { Unbilled } from "./Unbilled";

const money = (v: string | null) => v === null ? "Unresolved" : `$${Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const tons = (v: string) => Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 3 });
const RESULT: Record<string, { label: string; tone: string }> = {
  matches: { label: "Figures match", tone: "tf-status-open" },
  discrepancy: { label: "Difference found", tone: "tf-status-error" },
  incomplete: { label: "Incomplete", tone: "tf-status-attention" },
  no_verified_tickets: { label: "No verified tickets", tone: "tf-status-attention" },
};

const NOT_ENTERED = "(not entered)"; // How the server names a blank supplier, material or month.
/** The register, filtered to the approved tickets behind a row. Names match as "contains". */
function approvedLink(projectId: string, filters: Record<string, string | undefined>) {
  const params = new URLSearchParams({ project_id: projectId, status: "verified" });
  Object.entries(filters).forEach(([key, value]) => { if (value && value !== NOT_ENTERED) params.set(key, value); });
  return `/records?${params}`;
}
const monthRange = (month: string) => {
  const [year, number] = month.split("-").map(Number);
  if (!year || !number) return {};
  const last = new Date(year, number, 0).getDate();
  return { date_from: `${month}-01`, date_to: `${month}-${String(last).padStart(2, "0")}` };
};

function GroupCells({ row, href }: { row: ReportGroup; href?: string }) {
  return <>
    <td className="text-right tabular-nums">{href && row.verified_tickets > 0 ? <a href={href}>{row.verified_tickets}<span className="sr-only"> approved tickets, open in records</span></a> : row.verified_tickets}</td>
    <td className="text-right tabular-nums">{tons(row.verified_tons)}</td>
    <td className="text-right tabular-nums">{money(row.cost)}{row.unpriced > 0 && <span className="block text-[10px] font-bold text-[#a14c1c]">{row.unpriced} with no locked price</span>}</td>
    <td className="text-right tabular-nums text-[#5f6c7b]">{row.pending_tickets}</td>
    <td className="text-right tabular-nums text-[#5f6c7b]">{tons(row.pending_tons)}</td>
  </>;
}
const GROUP_HEADERS = <>
  <th className="text-right">Approved tickets</th><th className="text-right">Approved tons</th><th className="text-right">Material cost</th>
  <th className="text-right">Not yet approved</th><th className="text-right">Tons not yet approved</th>
</>;

function Tile({ label, value, note, attention }: { label: string; value: string; note: string; attention?: boolean }) {
  return <div className="min-w-0 px-5 py-4">
    <p className="text-[11px] font-bold uppercase tracking-[.055em] text-[#637181]">{label}</p>
    <p className={`mt-1.5 text-[22px] font-bold tabular-nums ${attention ? "text-[#a3511d]" : "text-[#192632]"}`}>{value}</p>
    <p className="mt-1 text-[11px] text-[#5f6c7b]">{note}</p>
  </div>;
}

export default function ProjectReportPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [project, setProject] = useState("");
  const [report, setReport] = useState<ProjectReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listProjects(true).then((found) => {
      setProjects(found);
      const wanted = new URLSearchParams(window.location.search).get("project_id");
      const first = wanted ?? (found[0] ? String(found[0].id) : "");
      setProject(first);
    }).catch((e) => setError(e.message));
  }, []);
  useEffect(() => {
    if (!project) return;
    let active = true;
    setReport(null);
    getProjectReport(project).then((found) => { if (active) { setReport(found); setError(null); } })
      .catch((e) => { if (active) setError(e.message); });
    window.history.replaceState(null, "", `/reports/project?project_id=${project}`);
    return () => { active = false; };
  }, [project]);

  const totals = report?.totals;
  return <div className="space-y-5">
    <div className="tf-page-heading">
      <div>
        <h1 className="tf-page-title">Project report</h1>
        <p className="tf-page-description">Approved material by supplier and month, the invoice lines against it, and approved deliveries no invoice covers. Not an approval for payment.</p>
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <label className="tf-field-label">Project
          <select className="tf-field min-w-[220px]" value={project} onChange={(e) => setProject(e.target.value)}>
            {!project && <option value="">No projects yet</option>}
            {projects.map((p) => <option key={p.id} value={p.id}>{p.code} · {p.name}</option>)}
          </select>
        </label>
        {project && <a className="tf-secondary-button" href={projectReportUrl(project)}>Download Excel</a>}
      </div>
    </div>
    {error && <div role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">{error}</div>}
    {report && totals && <>
      <section className="tf-panel grid divide-y divide-[#e7eaee] sm:grid-cols-2 sm:divide-x sm:divide-y-0 lg:grid-cols-4" aria-label="Project totals">
        <Tile label="Approved tons" value={tons(totals.verified_tons)} note={`${totals.verified_tickets.toLocaleString()} of ${totals.tickets.toLocaleString()} tickets`} />
        <Tile label="Material cost" value={money(totals.cost)} note={totals.unpriced ? `${totals.unpriced} approved with no locked price` : "Tons × locked rate"} attention={totals.unpriced > 0} />
        <Tile label="Not yet approved" value={tons(totals.pending_tons) + " t"} note={`${totals.pending_tickets.toLocaleString()} tickets, excluded from both`} attention={totals.pending_tickets > 0} />
        <Tile label="Tickets on record" value={totals.tickets.toLocaleString()} note={report.last_delivery ? `Delivered ${report.first_delivery} to ${report.last_delivery}` : "No dated tickets yet"} />
      </section>

      <section className="tf-panel" aria-label="By supplier and material">
        <div className="tf-panel-heading"><h2 className="tf-panel-title">By supplier and material</h2>
          <a className="tf-link" href={`/records?project_id=${project}`}>Open records</a></div>
        {report.by_supplier_material.length ? <div className="tf-table-wrap"><table className="tf-table">
          <thead><tr><th>Supplier</th><th>Material</th>{GROUP_HEADERS}</tr></thead>
          <tbody>{report.by_supplier_material.map((row) => <tr key={row.supplier + "|" + row.material}>
            <td className="font-semibold">{row.supplier}</td><td>{row.material}</td>
            <GroupCells row={row} href={row.supplier === NOT_ENTERED || row.material === NOT_ENTERED ? undefined : approvedLink(project, { supplier: row.supplier, material: row.material })} />
          </tr>)}</tbody>
        </table></div> : <p className="tf-empty">No tickets on this project yet.</p>}
      </section>

      <section className="tf-panel" aria-label="By delivery month">
        <div className="tf-panel-heading"><h2 className="tf-panel-title">By delivery month</h2></div>
        {report.by_month.length ? <div className="tf-table-wrap"><table className="tf-table">
          <thead><tr><th>Month</th>{GROUP_HEADERS}</tr></thead>
          <tbody>{report.by_month.map((row) => <tr key={row.month}><td className="font-semibold tabular-nums">{row.month}</td>
            <GroupCells row={row} href={row.month === NOT_ENTERED ? undefined : approvedLink(project, monthRange(row.month))} /></tr>)}</tbody>
        </table></div> : <p className="tf-empty">No tickets on this project yet.</p>}
      </section>

      <section className="tf-panel" aria-label="Invoice lines">
        <div className="tf-panel-heading flex-wrap">
          <div><h2 className="tf-panel-title">Invoice lines</h2>
            <p className="tf-panel-description">Billed {money(report.invoices.billed_total)} · approved cost {money(report.invoices.verified_total)}
              {report.invoices.lines_unresolved > 0 && <span className="ml-1 font-semibold text-[#a14c1c]">· {report.invoices.lines_unresolved} line(s) with an unresolved figure are left out of the approved cost</span>}</p></div>
          <a className="tf-link" href="/invoices">Open invoices</a>
        </div>
        {report.invoices.lines.length ? <div className="tf-table-wrap"><table className="tf-table">
          <thead><tr><th>Invoice</th><th>Supplier</th><th>Material</th><th>Result</th><th className="text-right">Billed tons</th><th className="text-right">Approved tons</th><th className="text-right">Billed</th><th className="text-right">Approved cost</th><th className="text-right">Difference</th></tr></thead>
          <tbody>{report.invoices.lines.map((line) => <tr key={line.id}>
            <td className="font-semibold tabular-nums"><a href={`/invoices?line=${line.id}`}>{line.invoice_number}<span className="sr-only">, open this invoice line</span></a></td><td>{line.supplier}</td><td>{line.material}</td>
            <td><span className={`tf-status ${RESULT[line.status]?.tone ?? ""}`}>{RESULT[line.status]?.label ?? line.status}</span></td>
            <td className="text-right tabular-nums">{tons(line.billed_tons)}</td><td className="text-right tabular-nums">{tons(line.verified_tons)}</td>
            <td className="text-right tabular-nums">{money(line.billed_subtotal)}</td><td className="text-right tabular-nums">{money(line.verified_subtotal)}</td>
            <td className="text-right tabular-nums">{money(line.subtotal_difference)}</td>
          </tr>)}</tbody>
        </table></div> : <p className="tf-empty">No invoice lines on this project yet.</p>}
      </section>

      <Unbilled projectId={project} />
    </>}
  </div>;
}
