"use client";

import { useEffect, useState } from "react";
import { PilotReport, Project, getPilotReport, listProjects, pilotReportCsvUrl } from "../../lib/api";

const num = (v: number | string | undefined) => v ?? 0;

function Row({ label, value, href, attention }: { label: string; value: number | string; href?: string; attention?: boolean }) {
  return <tr>
    <td>{label}</td>
    <td className={"text-right tabular-nums " + (attention && Number(value) > 0 ? "font-bold text-[#a14a1b]" : "font-semibold")}>{value}</td>
    <td className="text-right">{href && Number(value) > 0 && <a className="tf-link" href={href}>Show</a>}</td>
  </tr>;
}
function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return <section className="tf-panel" aria-label={title}>
    <div className="tf-panel-heading"><h2 className="tf-panel-title">{title}</h2></div>
    <div className="tf-table-wrap"><table className="tf-table"><tbody>{children}</tbody></table></div>
  </section>;
}

export default function ReportsPage() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [project, setProject] = useState("");
  const [report, setReport] = useState<PilotReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => { listProjects(true).then(setProjects).catch(() => undefined); }, []);
  useEffect(() => {
    getPilotReport(project || undefined).then((r) => { setReport(r); setError(null); }).catch((e) => setError((e as Error).message));
  }, [project]);
  const suffix = project ? "&project_id=" + project : "";

  return <div className="space-y-5">
    <div className="tf-page-heading">
      <div>
        <p className="tf-eyebrow">PILOT / INTAKE, REVIEW AND RECONCILIATION</p>
        <h1 className="tf-page-title">Pilot report</h1>
        <p className="tf-page-description">Counts of what's on record and what still needs a person. Not a claim that every paper ticket was captured.</p>
      </div>
      <a className="tf-secondary-button" href={pilotReportCsvUrl(project || undefined)}>Download CSV</a>
    </div>
    <label className="tf-field-label block max-w-sm">Project
      <select className="tf-field w-full" value={project} onChange={(e) => setProject(e.target.value)}>
        <option value="">All projects</option>
        {projects.map((p) => <option key={p.id} value={p.id}>{p.code} · {p.name}</option>)}
      </select>
    </label>
    {error && <div role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">{error}</div>}
    {report && <>
      <p className={"rounded border px-4 py-3 text-[13px] font-semibold " + (report.unresolved_items ? "border-[#f0d8ad] bg-[#fff9ed] text-[#754e20]" : "border-[#e2e7ec] bg-white text-[#344352]")} data-testid="unresolved">
        {report.unresolved_items} item{report.unresolved_items === 1 ? "" : "s"} need{report.unresolved_items === 1 ? "s" : ""} a person.
        {!report.unresolved_items && " Totals can still miss paper that was never scanned: compare your paper counts."}
      </p>
      {report.batches_needing_attention.length > 0 && <section className="tf-panel" aria-label="Batches needing attention">
        <div className="tf-panel-heading"><h2 className="tf-panel-title">Batches needing attention</h2></div>
        <ul className="divide-y divide-[#edf0f2]">{report.batches_needing_attention.map((b) => <li key={b.batch_id} className="flex flex-wrap items-center justify-between gap-3 px-5 py-3 text-[12px]">
          <span><strong>Batch #{b.batch_id}</strong> · {b.reasons.join(" · ")}</span>
          <a className="tf-link" href={"/upload?batch=" + b.batch_id}>Open scan check</a>
        </li>)}</ul>
      </section>}
      <div className="grid items-start gap-5 lg:grid-cols-3">
        <Panel title="Intake">
          <Row label="Batches" value={num(report.intake.batches)}/>
          <Row label="Files" value={num(report.intake.files)}/>
          <Row label="Pages" value={num(report.intake.pages)}/>
          <Row label="Physical slots" value={num(report.intake.slots)}/>
          <Row label="Faint, not classified" value={num(report.intake.faint_unclassified)} attention/>
          <Row label="Empty" value={num(report.intake.empty)}/>
          <Row label="Confirmed blank" value={num(report.intake.confirmed_blank)}/>
          <Row label="Failed files" value={num(report.intake.failed_files)} attention/>
          <Row label="Older files without a scan check" value={num(report.intake.files_without_scan_check)} attention/>
          <Row label="Batches with a paper count" value={num(report.intake.batches_with_paper_count)}/>
          <Row label="Paper count mismatches" value={num(report.intake.paper_count_mismatches)} attention/>
        </Panel>
        <Panel title="Review">
          <Row label="Records" value={num(report.review.records)} href={"/records?x=1" + suffix}/>
          <Row label="Needs review" value={num(report.review.needs_review)} attention href={"/records?status=needs_review" + suffix}/>
          <Row label="Flagged" value={num(report.review.flagged)} attention href={"/records?status=flagged" + suffix}/>
          <Row label="Could not be read" value={num(report.review.failed)} attention href={"/records?status=failed" + suffix}/>
          <Row label="With unreadable fields" value={num(report.review.with_unreadable_fields)}/>
          <Row label="Verified" value={num(report.review.verified)} href={"/records?status=verified" + suffix}/>
          <Row label="Verified, no price" value={num(report.review.verified_unpriced)} attention/>
          <Row label="Skipped" value={num(report.review.skipped)} href={"/records?status=skipped" + suffix}/>
          <Row label="Verified tons" value={num(report.review.verified_tons)}/>
        </Panel>
        <Panel title="Invoice reconciliation">
          <Row label="Invoice lines" value={num(report.reconciliation.invoice_lines as number)} href="/invoices"/>
          <Row label="Lines needing attention" value={num(report.reconciliation.lines_needing_attention as number)} attention href="/invoices"/>
          <Row label="Tickets checked" value={num(report.reconciliation.itemized_lines as number)}/>
          <Row label="Totals only, tickets not checked" value={num(report.reconciliation.totals_only_lines as number)} attention/>
          {Object.entries(report.reconciliation.lines_by_status as Record<string, number>).map(([status, count]) =>
            <Row key={status} label={"Lines: " + status.replaceAll("_", " ")} value={count}/>)}
          <Row label="Verified tickets linked to an invoice" value={num(report.reconciliation.verified_tickets_linked as number)}/>
          <Row label="Verified tickets not linked" value={num(report.reconciliation.verified_tickets_not_linked as number)}/>
          <Row label="Tons linked" value={num(report.reconciliation.tons_linked as string)}/>
          <Row label="Tons not linked" value={num(report.reconciliation.tons_not_linked as string)}/>
        </Panel>
      </div>
      {report.review_timing.enabled && <Panel title="Review time (opt-in)">
        {(["manual", "extract"] as const).map((mode) => <Row key={mode}
          label={(mode === "manual" ? "Manual entry" : "AI extraction") + ": median seconds per ticket, " + report.review_timing[mode].samples + " tickets, " + report.review_timing[mode].with_corrections + " corrected"}
          value={report.review_timing[mode].median_seconds ?? "—"}/>)}
      </Panel>}
    </>}
  </div>;
}
