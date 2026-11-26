"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { projectReportUrl, quantityText, UNIT_LABEL } from "../../../lib/api";
import { useAccount } from "../../account-context";
import { CostFigures, PeriodTable, Range, RangeControls, ShareList, TrendChart, hours, money, queryFor, rangeFor, tons, useCostReport } from "../../analytics/report-parts";
import ProjectBar from "../../ProjectBar";
import { useProject } from "../../project-context";
import { Unbilled } from "../../reports/project/Unbilled";

const recordsFor = (filters: Record<string, string | undefined>) =>
  `/records?${new URLSearchParams(Object.entries({ status: "verified", ...filters }).filter(([, v]) => v) as [string, string][])}`;

/**
 * A project's report: what was delivered, what it cost, and how it's billed, for any
 * dates. The same calculation as Overview and Analytics, filtered to this job, with
 * every figure leading to the records behind it.
 */
export default function ProjectReportPage() {
  const params = useParams<{ id: string }>();
  const id = String(params.id);
  const { can } = useAccount();
  const { project, projects, loaded, setProjectId } = useProject();
  const [range, setRange] = useState<Range>(() => rangeFor("all"));
  const { report, error, loading } = useCostReport(queryFor(range, id));

  // Opening a project makes it the one being worked on everywhere.
  useEffect(() => { if (/^\d+$/.test(id)) setProjectId(id); }, [id, setProjectId]);
  const known = projects.find((p) => String(p.id) === id);
  if (loaded && !known) return <div className="space-y-3"><h1 className="tf-page-title">Project not found</h1>
    <p className="tf-page-description">It may belong to another company or no longer exist. <Link prefetch={false} className="tf-link" href="/projects">All projects</Link></p></div>;
  const p = known ?? project;
  const dates = { date_from: range.date_from || undefined, date_to: range.date_to || undefined };

  return <div className="space-y-4">
    <ProjectBar />
    <div className="tf-page-heading">
      <div>
        <h1 className="tf-page-title">{p ? `${p.code} · ${p.name}` : "Project report"}</h1>
        <p className="tf-page-description">{[p?.job_location, p?.status === "active" ? "Active" : p ? "Archived" : null].filter(Boolean).join(" · ")}
          {" "}Approved figures only; not an approval for payment.</p>
      </div>
      <div className="flex flex-wrap gap-2">
        {can("pricing.manage") && <Link prefetch={false} className="tf-secondary-button" href="/pricing">Rates for this project</Link>}
        <a className="tf-primary-button" href={projectReportUrl(id)}>Download project workbook</a>
      </div>
    </div>

    <RangeControls range={range} onChange={setRange} />
    {error && <div role="alert" className="tf-alert tf-alert-bad">{error}</div>}
    {!report ? <p className="tf-empty" role="status">Loading the report…</p> : <div className={`space-y-4 ${loading ? "opacity-60" : ""}`}>
      <CostFigures report={report} />
      {report.costs.unpriced > 0 && <p className="tf-alert tf-alert-warn">{report.costs.unpriced} approved deliver{report.costs.unpriced === 1 ? "y has" : "ies have"} no rate, so the material cost is missing, not zero.{" "}
        <Link prefetch={false} className="tf-link" href="/pricing">Add the rate</Link>; approved costs never change on their own.</p>}

      <section className="tf-panel" aria-labelledby="trend-title">
        <div className="tf-panel-heading"><h2 id="trend-title" className="tf-panel-title">Cost over time</h2>
          <span className="text-[12px] text-[#5f6c7b]">{report.material.waiting > 0 ? `${report.material.waiting} tickets waiting for review are not included` : "Everything in these dates is reviewed"}</span></div>
        <TrendChart rows={report.by_period} period={report.period} label="Project cost over time" />
        <details className="border-t border-[#dce1e6]"><summary className="cursor-pointer px-4 py-2 text-[12.5px] font-semibold text-[#263646]">Figures by {report.period}</summary>
          <PeriodTable rows={report.by_period} period={report.period} /></details>
      </section>

      <div className="grid items-start gap-4 xl:grid-cols-2">
        <section className="tf-panel" aria-labelledby="materials-title">
          <div className="tf-panel-heading"><h2 id="materials-title" className="tf-panel-title">Materials</h2>
            <Link prefetch={false} className="tf-link" href={recordsFor({ project_id: id, ...dates })}>Approved deliveries →</Link></div>
          {report.unit_prices.length || report.material.by_material.length ? <div className="tf-table-wrap"><table className="tf-table" aria-label="Materials by supplier">
            <thead><tr><th>Material</th><th>Supplier</th><th className="text-right">Quantity</th><th className="text-right">Average price</th><th className="text-right">Cost</th></tr></thead>
            <tbody>{report.unit_prices.map((u) => <tr key={u.supplier + u.material + (u.unit ?? "")}>
              <td><Link prefetch={false} href={recordsFor({ project_id: id, material: u.material, supplier: u.supplier, ...dates })}>{u.material}</Link></td>
              <td>{u.supplier}</td><td className="text-right">{tons(u.tons)} {UNIT_LABEL[u.unit ?? "ton"]?.short}</td><td className="text-right">{money(u.average)}/{UNIT_LABEL[u.unit ?? "ton"]?.short}</td><td className="text-right">{money(u.cost)}</td></tr>)}
              {report.material.by_material.filter((m) => m.unpriced > 0).map((m) => <tr key={"u" + m.name}>
                <td>{m.name}</td><td className="text-[#995a18]" colSpan={4}>{m.unpriced} approved without a rate: cost not known yet</td></tr>)}</tbody>
          </table></div> : <p className="tf-empty">No approved deliveries in these dates.</p>}
        </section>

        <section className="tf-panel" aria-labelledby="suppliers-title">
          <div className="tf-panel-heading"><h2 id="suppliers-title" className="tf-panel-title">Suppliers</h2></div>
          <ShareList label="Material cost by supplier" total={report.costs.material}
            rows={report.material.by_supplier.map((s) => ({ name: s.name, value: s.cost, note: quantityText(s.tons, s.other_units), warn: s.unpriced ? `${s.unpriced} without a rate` : undefined }))}
            href={(name) => recordsFor({ project_id: id, supplier: name, ...dates })} empty="No approved deliveries in these dates." />
        </section>
      </div>

      <div className="grid items-start gap-4 xl:grid-cols-2">
        <section className="tf-panel" aria-labelledby="hauling-title">
          <div className="tf-panel-heading"><h2 id="hauling-title" className="tf-panel-title">Hauling</h2>
            <Link prefetch={false} className="tf-link" href="/trucking">Trucking →</Link></div>
          {report.trucking.by_hauler.length ? <div className="tf-table-wrap"><table className="tf-table" aria-label="Hauling by hauler">
            <thead><tr><th>Hauler</th><th className="text-right">Shifts</th><th className="text-right">Hours</th><th className="text-right">Loads</th><th className="text-right">$/hour</th><th className="text-right">Cost</th></tr></thead>
            <tbody>{report.trucking.by_hauler.map((h) => <tr key={h.name}><td className="font-semibold text-[#041d2f]">{h.name}</td><td className="text-right">{h.shifts}</td>
              <td className="text-right">{hours(h.hours)}</td><td className="text-right">{h.loads}</td><td className="text-right">{money(h.per_hour)}</td><td className="text-right">{money(h.cost)}</td></tr>)}</tbody>
          </table></div> : <p className="tf-empty">No approved trucking in these dates.</p>}
        </section>

        <section className="tf-panel" aria-labelledby="invoices-title">
          <div className="tf-panel-heading"><h2 id="invoices-title" className="tf-panel-title">Invoices</h2>
            <Link prefetch={false} className="tf-link" href="/invoices">This project&apos;s invoices →</Link></div>
          <dl className="tf-facts">
            <div><dt>Invoices with billing in these dates</dt><dd>{report.invoices.invoices}</dd></div>
            <div><dt>Billed</dt><dd>{money(report.invoices.billed)}</dd></div>
            <div><dt>Match approved records</dt><dd>{report.invoices.matched}</dd></div>
            <div><dt>With differences</dt><dd className={report.invoices.with_differences ? "text-[#a73631]" : ""}>{report.invoices.with_differences}</dd></div>
            <div><dt>Can&apos;t compare yet</dt><dd>{report.invoices.incomplete}</dd></div>
          </dl>
          <p className="px-4 pb-3 text-[11.5px] text-[#5f6c7b]">Billed amounts are compared with approved records; they are never added to project cost.</p>
        </section>
      </div>
    </div>}

    <Unbilled projectId={id} />
  </div>;
}
