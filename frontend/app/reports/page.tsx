"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { CostReport, CostReportQuery, CostRow, costReportXlsxUrl, getCostReport } from "../../lib/api";
import { useProject } from "../project-context";

// Display only: the server adds every amount up exactly.
const money = (v: string) => `${Number(v) < 0 ? "-" : ""}$${Math.abs(Number(v)).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const tons = (v: string) => Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 });
const hours = (v: string) => `${Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 })} h`;
const TABS = [["costs", "Project costs"], ["material", "Material"], ["trucking", "Trucking"], ["invoices", "Invoices"]] as const;
type Tab = typeof TABS[number][0];
const periodLabel = (key: string, period: string) => period === "month"
  ? new Date(`${key}-01T00:00:00`).toLocaleDateString(undefined, { month: "long", year: "numeric" })
  : `${period === "week" ? "Week of " : ""}${new Date(`${key}T00:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}`;

function Tile({ label, value, note, warn = false }: { label: string; value: string; note: string; warn?: boolean }) {
  return <div className="border-b border-r border-[#dce1e6] px-5 py-4">
    <p className="tf-stat-label">{label}</p><p className={warn ? "text-[#995a18]" : "text-[#041d2f]"}>{value}</p>
    <span className={`text-[11.5px] ${warn ? "font-semibold text-[#995a18]" : "text-[#5f6c7b]"}`}>{note}</span>
  </div>;
}

function Breakdown({ title, label, rows }: { title: string; label: string; rows: CostRow[] }) {
  return <section className="tf-panel" aria-labelledby={`${label}-title`}>
    <div className="tf-panel-heading"><h2 id={`${label}-title`} className="tf-panel-title">{title}</h2></div>
    {rows.length ? <div className="tf-table-wrap"><table className="tf-table" aria-label={title}>
      <thead><tr><th>{label}</th><th className="text-right">Tickets</th><th className="text-right">Verified tons</th><th className="text-right">Material cost</th></tr></thead>
      <tbody>{rows.map((r) => <tr key={r.name}><td className="font-semibold text-[#041d2f]">{r.name}</td><td className="text-right">{r.tickets.toLocaleString()}</td>
        <td className="text-right">{tons(r.tons)}</td>
        <td className="text-right whitespace-nowrap">{money(r.cost)}{r.unpriced > 0 && <span className="block text-[11px] font-semibold text-[#995a18]">+ {r.unpriced} without a rate</span>}</td></tr>)}</tbody>
    </table></div> : <p className="tf-empty">No approved deliveries in these dates.</p>}
  </section>;
}

/**
 * Cost reports for the chosen project (or all), over a date range: approved material,
 * approved trucking, the two together, and how invoices compare. Approved figures only;
 * a missing price is counted, never shown as $0.
 */
export default function ReportsPage() {
  const { projectId, project } = useProject();
  const [tab, setTab] = useState<Tab>("costs");
  const [range, setRange] = useState<{ date_from: string; date_to: string; period: CostReportQuery["period"] }>({ date_from: "", date_to: "", period: "week" });
  const [report, setReport] = useState<CostReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const wanted = new URLSearchParams(window.location.search).get("tab");
    if (TABS.some(([key]) => key === wanted)) setTab(wanted as Tab);
  }, []);
  const query: CostReportQuery = { project_id: projectId || undefined, date_from: range.date_from || undefined, date_to: range.date_to || undefined, period: range.period };
  const key = JSON.stringify(query);
  useEffect(() => {
    let live = true;
    getCostReport(JSON.parse(key)).then((r) => { if (live) { setReport(r); setError(null); } }).catch((e) => { if (live) setError(e.message); });
    return () => { live = false; };
  }, [key]);

  const r = report;
  return <div className="space-y-4">
    <div className="tf-page-heading">
      <div><h1 className="tf-page-title">Reports</h1>
        <p className="tf-page-description">Approved material and trucking costs for {project ? `${project.code} · ${project.name}` : "all projects"}. Not an approval for payment.</p></div>
      <a className="tf-secondary-button" href={costReportXlsxUrl(query)}>Download Excel</a>
    </div>

    <section className="tf-panel" aria-label="Report dates">
      <div className="flex flex-wrap items-end gap-3 p-4">
        <label className="tf-field-label">From<input type="date" className="tf-field" value={range.date_from} onChange={(e) => setRange({ ...range, date_from: e.target.value })} /></label>
        <label className="tf-field-label">To<input type="date" className="tf-field" value={range.date_to} onChange={(e) => setRange({ ...range, date_to: e.target.value })} /></label>
        <div className="tf-field-label">Group by
          <div className="tf-segmented" role="group" aria-label="Group by">
            {(["day", "week", "month"] as const).map((p) => <button key={p} type="button" aria-pressed={range.period === p} onClick={() => setRange({ ...range, period: p })}>{p[0].toUpperCase() + p.slice(1)}</button>)}
          </div>
        </div>
        {(range.date_from || range.date_to) && <button type="button" className="tf-link" onClick={() => setRange({ ...range, date_from: "", date_to: "" })}>All dates</button>}
      </div>
    </section>
    {error && <div role="alert" className="tf-alert tf-alert-bad">{error}</div>}

    <div role="tablist" aria-label="Reports" className="tf-section-tabs">
      {TABS.map(([k, label]) => <button key={k} type="button" role="tab" aria-selected={tab === k} className={tab === k ? "tf-section-tab-active" : ""}
        onClick={() => { setTab(k); window.history.replaceState(null, "", k === "costs" ? "/reports" : `/reports?tab=${k}`); }}>{label}</button>)}
    </div>

    {!r ? <p className="tf-empty" role="status">Loading the report…</p> : <>
      {tab === "costs" && <>
        <section className="tf-panel" aria-label="Verified costs"><div className="grid grid-cols-2 lg:grid-cols-4">
          <Tile label="Material cost" value={money(r.costs.material)} note={r.costs.unpriced ? `Incomplete: ${r.costs.unpriced} approved ticket${r.costs.unpriced === 1 ? "" : "s"} without a rate` : `${tons(r.material.tons)} t approved`} warn={r.costs.unpriced > 0} />
          <Tile label="Trucking cost" value={money(r.costs.trucking)} note={`${hours(r.trucking.hours)} approved, hourly`} />
          <Tile label="Total verified cost" value={money(r.costs.total)} note={r.costs.complete ? "Material + trucking, each counted once" : "Incomplete until every approved ticket has a rate"} warn={!r.costs.complete} />
          <Tile label="Not yet counted" value={r.material.waiting.toLocaleString()} note="Tickets waiting for review, left out of every figure" />
        </div></section>
        {r.costs.unpriced > 0 && <p className="tf-alert tf-alert-warn">Some approved deliveries have no rate, so their cost is missing, not zero. Add the rate in <Link prefetch={false} className="tf-link" href="/pricing">Rates &amp; Pricing</Link>; approved costs never change on their own.</p>}
        <section className="tf-panel" aria-labelledby="period-title">
          <div className="tf-panel-heading"><h2 id="period-title" className="tf-panel-title">By {r.period}</h2></div>
          {r.by_period.length ? <div className="tf-table-wrap"><table className="tf-table" aria-label={`Costs by ${r.period}`}>
            <thead><tr><th>{r.period[0].toUpperCase() + r.period.slice(1)}</th><th className="text-right">Tickets</th><th className="text-right">Verified tons</th>
              <th className="text-right">Material cost</th><th className="text-right">Truck hours</th><th className="text-right">Trucking cost</th><th className="text-right">Total</th></tr></thead>
            <tbody>{r.by_period.map((p) => <tr key={p.period}><td className="whitespace-nowrap font-semibold text-[#041d2f]">{periodLabel(p.period, r.period)}</td>
              <td className="text-right">{p.tickets.toLocaleString()}</td><td className="text-right">{tons(p.tons)}</td>
              <td className="text-right whitespace-nowrap">{money(p.material)}{p.unpriced > 0 && <span className="block text-[11px] font-semibold text-[#995a18]">+ {p.unpriced} without a rate</span>}</td>
              <td className="text-right">{hours(p.hours)}</td><td className="text-right">{money(p.trucking)}</td><td className="text-right font-semibold">{money(p.total)}</td></tr>)}</tbody>
          </table></div> : <p className="tf-empty">No approved deliveries or shifts in these dates.</p>}
          {r.material.undated.tickets > 0 && <p className="border-t border-[#dce1e6] px-4 py-2 text-[12px] text-[#5f6c7b]">{r.material.undated.tickets} approved ticket(s) have no delivery date, so they&apos;re in the totals but in no {r.period}.</p>}
        </section>
      </>}

      {tab === "material" && <div className="grid items-start gap-4 xl:grid-cols-2">
        <Breakdown title="Quantity by material" label="Material" rows={r.material.by_material} />
        <Breakdown title="Quantity by supplier" label="Supplier" rows={r.material.by_supplier} />
      </div>}

      {tab === "trucking" && <section className="tf-panel" aria-labelledby="haulers-title">
        <div className="tf-panel-heading"><div><h2 id="haulers-title" className="tf-panel-title">Trucking by hauler</h2>
          <p className="tf-panel-description">Approved hourly trucking. Per-load and per-ton hauling aren&apos;t tracked yet.</p></div></div>
        {r.trucking.by_hauler.length ? <div className="tf-table-wrap"><table className="tf-table" aria-label="Trucking by hauler">
          <thead><tr><th>Hauler</th><th className="text-right">Approved shifts</th><th className="text-right">Approved hours</th><th className="text-right">Loads</th><th className="text-right">Trucking cost</th></tr></thead>
          <tbody>{r.trucking.by_hauler.map((h) => <tr key={h.name}><td className="font-semibold text-[#041d2f]">{h.name}</td><td className="text-right">{h.shifts}</td>
            <td className="text-right">{hours(h.hours)}</td><td className="text-right">{h.loads}</td><td className="text-right">{money(h.cost)}</td></tr>)}</tbody>
        </table></div> : <p className="tf-empty">No approved truck shifts in these dates.</p>}
      </section>}

      {tab === "invoices" && <>
        <section className="tf-panel" aria-label="Invoice results"><div className="grid grid-cols-2 lg:grid-cols-4">
          <Tile label="Invoices" value={r.invoices.invoices.toLocaleString()} note={`${money(r.invoices.billed)} billed`} />
          <Tile label="Match approved records" value={r.invoices.matched.toLocaleString()} note="Billed as approved" />
          <Tile label="With differences" value={r.invoices.with_differences.toLocaleString()} note={`${money(r.invoices.difference)} billed above approved, net`} warn={r.invoices.with_differences > 0} />
          <Tile label="Can't compare yet" value={r.invoices.incomplete.toLocaleString()} note={r.invoices.totals_only ? `${r.invoices.totals_only} compared in total only` : "Waiting on approvals or a rate"} />
        </div></section>
        <p className="text-[12px] text-[#5f6c7b]">Invoices whose billing period overlaps these dates. Open the <Link prefetch={false} className="tf-link" href="/invoices">invoice register</Link> or <Link prefetch={false} className="tf-link" href="/invoices/exceptions">exceptions</Link> to work through them.</p>
      </>}
    </>}
    <p className="text-[11.5px] text-[#5f6c7b]">One project in detail, with invoice lines and unbilled deliveries: <Link prefetch={false} className="tf-link" href="/reports/project">Project report</Link>.</p>
  </div>;
}
