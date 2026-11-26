"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { costReportXlsxUrl, quantityText, UNIT_LABEL } from "../../lib/api";
import { useProject } from "../project-context";
import { PeriodTable, Range, RangeControls, ShareList, TrendChart, hours, money, queryFor, rangeFor, tons, useCostReport } from "./report-parts";

const TABS = [["costs", "Costs"], ["materials", "Materials"], ["suppliers", "Suppliers"], ["trucking", "Trucking"]] as const;
type Tab = typeof TABS[number][0];
const recordsFor = (filters: Record<string, string | undefined>) =>
  `/records?${new URLSearchParams(Object.entries({ status: "verified", ...filters }).filter(([, v]) => v) as [string, string][])}`;

/**
 * Investigation and comparison: across projects, suppliers, materials, haulers and time.
 * The same server calculation as Overview and project reports; approved figures only.
 */
export default function AnalyticsPage() {
  const { projectId, project } = useProject();
  const [tab, setTab] = useState<Tab>("costs");
  const [range, setRange] = useState<Range>(() => rangeFor("ytd"));
  const query = queryFor(range, projectId);
  const { report: r, error, loading } = useCostReport(query);
  const dates = { date_from: range.date_from || undefined, date_to: range.date_to || undefined, project_id: projectId || undefined };

  useEffect(() => {
    const wanted = new URLSearchParams(window.location.search).get("tab");
    if (TABS.some(([key]) => key === wanted)) setTab(wanted as Tab);
  }, []);
  const choose = (key: Tab) => { setTab(key); window.history.replaceState(null, "", key === "costs" ? "/analytics" : `/analytics?tab=${key}`); };

  // Each material's suppliers side by side: same material, same unit (tons), same dates.
  const byMaterial = new Map<string, NonNullable<typeof r>["unit_prices"]>();
  r?.unit_prices.forEach((u) => byMaterial.set(u.material, [...(byMaterial.get(u.material) ?? []), u]));

  return <div className="space-y-4">
    <div className="tf-page-heading">
      <div><h1 className="tf-page-title">Analytics</h1>
        <p className="tf-page-description">Compare spending across {project ? `${project.code}'s` : "your"} suppliers, materials, haulers and periods. Approved figures only.</p></div>
      <a className="tf-secondary-button" href={costReportXlsxUrl(query)}>Download Excel</a>
    </div>
    <RangeControls range={range} onChange={setRange} />
    <div role="tablist" aria-label="Analytics" className="tf-section-tabs">
      {TABS.map(([key, label]) => <button key={key} type="button" role="tab" aria-selected={tab === key} className={tab === key ? "tf-section-tab-active" : ""} onClick={() => choose(key)}>{label}</button>)}
    </div>
    {error && <div role="alert" className="tf-alert tf-alert-bad">{error}</div>}
    {!r ? <p className="tf-empty" role="status">Loading…</p> : <div className={`space-y-4 ${loading ? "opacity-60" : ""}`}>
      {r.costs.unpriced > 0 && <p className="tf-alert tf-alert-warn">{r.costs.unpriced} approved deliver{r.costs.unpriced === 1 ? "y has" : "ies have"} no rate: material cost here is incomplete, not zero.</p>}

      {tab === "costs" && <>
        <section className="tf-panel" aria-labelledby="trend-title">
          <div className="tf-panel-heading"><h2 id="trend-title" className="tf-panel-title">Material and hauling cost</h2>
            <span className="tf-num text-[13px] font-semibold text-[#041d2f]">{money(r.costs.total)} in these dates</span></div>
          <TrendChart rows={r.by_period} period={r.period} label="Cost over time" />
          <details className="border-t border-[#dce1e6]"><summary className="cursor-pointer px-4 py-2 text-[12.5px] font-semibold text-[#263646]">Figures by {r.period}</summary>
            <PeriodTable rows={r.by_period} period={r.period} /></details>
        </section>
        <section className="tf-panel" aria-labelledby="projects-title">
          <div className="tf-panel-heading"><h2 id="projects-title" className="tf-panel-title">By project</h2></div>
          {r.by_project.length ? <div className="tf-table-wrap"><table className="tf-table" aria-label="Cost by project">
            <thead><tr><th>Project</th><th className="text-right">Tons</th><th className="text-right">Material</th><th className="text-right">Truck hours</th>
              <th className="text-right">Hauling</th><th className="text-right">Combined</th><th className="text-right">Share</th></tr></thead>
            <tbody>{r.by_project.map((p) => <tr key={p.id}>
              <td><Link prefetch={false} href={`/projects/${p.id}`}>{p.code}</Link> <span className="text-[#3b4a59]">{p.name}</span></td>
              <td className="text-right">{tons(p.tons)}</td>
              <td className="text-right whitespace-nowrap">{money(p.material)}{p.unpriced > 0 && <span className="block text-[11px] font-semibold text-[#995a18]">+ {p.unpriced} without a rate</span>}</td>
              <td className="text-right">{hours(p.hours)}</td><td className="text-right">{money(p.trucking)}</td>
              <td className="text-right font-semibold">{money(p.total)}</td>
              <td className="text-right text-[#5f6c7b]">{Number(r.costs.total) ? `${((Number(p.total) / Number(r.costs.total)) * 100).toFixed(1)}%` : "—"}</td></tr>)}</tbody>
          </table></div> : <p className="tf-empty">No approved activity in these dates.</p>}
        </section>
      </>}

      {tab === "materials" && <section className="tf-panel" aria-labelledby="prices-title">
        <div className="tf-panel-heading"><div><h2 id="prices-title" className="tf-panel-title">Unit prices by material</h2>
          <p className="tf-panel-description">Average approved price per unit (per ton, per cubic yard…) from each supplier, from priced deliveries only; low and high show the range of locked rates.</p></div></div>
        {byMaterial.size ? <div className="tf-table-wrap"><table className="tf-table" aria-label="Unit prices by material">
          <thead><tr><th>Material</th><th>Supplier</th><th className="text-right">Quantity</th><th className="text-right">Average price</th><th className="text-right">Low</th><th className="text-right">High</th><th className="text-right">Cost</th></tr></thead>
          <tbody>{[...byMaterial.entries()].map(([material, rows]) => rows.map((u, n) => <tr key={material + u.supplier + (u.unit ?? "")}>
            <td className="font-semibold text-[#041d2f]">{n === 0 ? material : ""}</td>
            <td><Link prefetch={false} href={recordsFor({ ...dates, material, supplier: u.supplier })}>{u.supplier}</Link></td>
            <td className="text-right">{tons(u.tons)} {UNIT_LABEL[u.unit ?? "ton"]?.short}</td><td className="text-right font-semibold">{money(u.average)}/{UNIT_LABEL[u.unit ?? "ton"]?.short}</td>
            <td className="text-right">{money(u.low)}</td><td className="text-right">{money(u.high)}</td><td className="text-right">{money(u.cost)}</td></tr>))}</tbody>
        </table></div> : <p className="tf-empty">No priced deliveries in these dates.</p>}
        {r.material.by_material.some((m) => m.unpriced) && <p className="border-t border-[#dce1e6] px-4 py-2 text-[12px] text-[#995a18]">Not compared (no rate yet): {r.material.by_material.filter((m) => m.unpriced).map((m) => `${m.name} (${m.unpriced})`).join(", ")}.</p>}
      </section>}

      {tab === "suppliers" && <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
        <section className="tf-panel" aria-labelledby="share-title">
          <div className="tf-panel-heading"><h2 id="share-title" className="tf-panel-title">Share of material spend</h2></div>
          <ShareList label="Material spend by supplier" total={r.costs.material}
            rows={r.material.by_supplier.map((s) => ({ name: s.name, value: s.cost, note: quantityText(s.tons, s.other_units), warn: s.unpriced ? `${s.unpriced} without a rate` : undefined }))}
            href={(name) => recordsFor({ ...dates, supplier: name })} />
        </section>
        <section className="tf-panel" aria-labelledby="supplier-title">
          <div className="tf-panel-heading"><h2 id="supplier-title" className="tf-panel-title">Suppliers</h2><Link prefetch={false} className="tf-link" href="/suppliers">Vendor directory →</Link></div>
          {r.material.by_supplier.length ? <div className="tf-table-wrap"><table className="tf-table" aria-label="Suppliers">
            <thead><tr><th>Supplier</th><th className="text-right">Deliveries</th><th className="text-right">Tons</th><th className="text-right">Material cost</th></tr></thead>
            <tbody>{r.material.by_supplier.map((s) => <tr key={s.name}><td><Link prefetch={false} href={recordsFor({ ...dates, supplier: s.name })}>{s.name}</Link></td>
              <td className="text-right">{s.tickets.toLocaleString()}</td><td className="text-right">{tons(s.tons)}</td>
              <td className="text-right whitespace-nowrap">{money(s.cost)}{s.unpriced > 0 && <span className="block text-[11px] font-semibold text-[#995a18]">+ {s.unpriced} without a rate</span>}</td></tr>)}</tbody>
          </table></div> : <p className="tf-empty">No approved deliveries in these dates.</p>}
        </section>
      </div>}

      {tab === "trucking" && <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)]">
        <section className="tf-panel" aria-labelledby="haulers-title">
          <div className="tf-panel-heading"><div><h2 id="haulers-title" className="tf-panel-title">Haulers compared</h2>
            <p className="tf-panel-description">Approved trucking. Cost per hour includes minimums and standby as billed.</p></div></div>
          {r.trucking.by_hauler.length ? <div className="tf-table-wrap"><table className="tf-table" aria-label="Haulers compared">
            <thead><tr><th>Hauler</th><th className="text-right">Shifts</th><th className="text-right">Hours</th><th className="text-right">Loads</th><th className="text-right">$/hour</th><th className="text-right">Cost</th></tr></thead>
            <tbody>{r.trucking.by_hauler.map((h) => <tr key={h.name}><td className="font-semibold text-[#041d2f]">{h.name}</td><td className="text-right">{h.shifts}</td>
              <td className="text-right">{hours(h.hours)}</td><td className="text-right">{h.loads}</td><td className="text-right font-semibold">{money(h.per_hour)}</td><td className="text-right">{money(h.cost)}</td></tr>)}</tbody>
          </table></div> : <p className="tf-empty">No approved trucking in these dates.</p>}
        </section>
        <section className="tf-panel" aria-labelledby="methods-title">
          <div className="tf-panel-heading"><h2 id="methods-title" className="tf-panel-title">By billing method</h2></div>
          <ShareList label="Hauling spend by billing method" total={r.costs.trucking}
            rows={r.trucking.billing_methods.map((m) => ({ name: m.method === "hourly" ? "Hourly" : m.method === "per_ton" ? "Per ton" : m.method === "per_load" ? "Per load" : m.method, value: m.cost }))} />
        </section>
      </div>}
    </div>}
  </div>;
}
