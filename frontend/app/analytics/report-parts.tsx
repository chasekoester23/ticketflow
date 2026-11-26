"use client";

import { useEffect, useState, type ReactNode } from "react";
import { CostReport, CostReportQuery, PeriodRow, getCostReport, otherUnitsText } from "../../lib/api";

/*
 * The reporting pieces shared by Overview, a project's report and Analytics. Every figure
 * comes from one server calculation (/api/reports/summary); nothing is added up here.
 */

// Series colours, validated for colour-blind separation and 3:1 contrast on white.
export const SERIES = { material: "#2563a8", hauling: "#d9741f" } as const;

export const money = (v: string | number | null | undefined) => v === null || v === undefined ? "—"
  : `${Number(v) < 0 ? "-" : ""}$${Math.abs(Number(v)).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
export const compactMoney = (v: string | number) => {
  const n = Number(v);
  if (Math.abs(n) >= 1_000_000) return `$${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`;
  if (Math.abs(n) >= 1_000) return `$${(n / 1_000).toFixed(n >= 100_000 ? 0 : 1)}k`;
  return `$${n.toFixed(0)}`;
};
export const tons = (v: string | number) => Number(v).toLocaleString(undefined, { maximumFractionDigits: 1 });
export const hours = (v: string | number) => `${Number(v).toLocaleString(undefined, { maximumFractionDigits: 1 })} h`;
export function periodLabel(key: string, period: string, short = false) {
  if (period === "month") return new Date(`${key}-01T00:00:00`).toLocaleDateString(undefined, { month: short ? "short" : "long", year: short ? "2-digit" : "numeric" });
  const day = new Date(`${key}T00:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric", ...(short ? {} : { year: "numeric" }) });
  return period === "week" && !short ? `Week of ${day}` : day;
}

export type Range = { preset: string; date_from: string; date_to: string; period: CostReportQuery["period"] };
const iso = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
export const PRESETS: { key: string; label: string; range: () => Omit<Range, "preset"> }[] = [
  { key: "30d", label: "Last 30 days", range: () => { const d = new Date(); d.setDate(d.getDate() - 29); return { date_from: iso(d), date_to: "", period: "day" }; } },
  { key: "90d", label: "Last 90 days", range: () => { const d = new Date(); d.setDate(d.getDate() - 89); return { date_from: iso(d), date_to: "", period: "week" }; } },
  { key: "ytd", label: "This year", range: () => ({ date_from: `${new Date().getFullYear()}-01-01`, date_to: "", period: "month" }) },
  { key: "all", label: "All dates", range: () => ({ date_from: "", date_to: "", period: "month" }) },
];
export const rangeFor = (key: string): Range => ({ preset: key, ...(PRESETS.find((p) => p.key === key) ?? PRESETS[3]).range() });

/** Date range and grouping, in one row above the figures. */
export function RangeControls({ range, onChange, children }: { range: Range; onChange: (r: Range) => void; children?: ReactNode }) {
  return <div className="tf-filterbar" role="group" aria-label="Dates">
    <div className="tf-segmented" role="group" aria-label="Date range">
      {PRESETS.map((p) => <button key={p.key} type="button" aria-pressed={range.preset === p.key} onClick={() => onChange(rangeFor(p.key))}>{p.label}</button>)}
      <button type="button" aria-pressed={range.preset === "custom"} onClick={() => onChange({ ...range, preset: "custom" })}>Custom</button>
    </div>
    {range.preset === "custom" && <>
      <label className="tf-inline-field">From<input type="date" className="tf-field" value={range.date_from} onChange={(e) => onChange({ ...range, date_from: e.target.value })} /></label>
      <label className="tf-inline-field">To<input type="date" className="tf-field" value={range.date_to} onChange={(e) => onChange({ ...range, date_to: e.target.value })} /></label>
    </>}
    <div className="tf-segmented" role="group" aria-label="Group by">
      {(["day", "week", "month"] as const).map((p) => <button key={p} type="button" aria-pressed={range.period === p} onClick={() => onChange({ ...range, period: p })}>
        {p === "day" ? "Daily" : p === "week" ? "Weekly" : "Monthly"}</button>)}
    </div>
    {children && <div className="ml-auto flex items-center gap-2">{children}</div>}
  </div>;
}

export function queryFor(range: Range, projectId?: string): CostReportQuery {
  return { project_id: projectId || undefined, date_from: range.date_from || undefined, date_to: range.date_to || undefined, period: range.period };
}

/** The shared report for a query; refetched only when the query changes. */
export function useCostReport(query: CostReportQuery): { report: CostReport | null; error: string | null; loading: boolean } {
  const key = JSON.stringify(query);
  const [state, setState] = useState<{ key: string; report: CostReport | null; error: string | null }>({ key: "", report: null, error: null });
  useEffect(() => {
    let live = true;
    getCostReport(JSON.parse(key)).then((report) => { if (live) setState({ key, report, error: null }); })
      .catch((e) => { if (live) setState({ key, report: null, error: (e as Error).message }); });
    return () => { live = false; };
  }, [key]);
  return { report: state.report, error: state.error, loading: state.key !== key };
}

/** Material and hauling per period, stacked. One axis; the table below the chart carries every value. */
export function TrendChart({ rows, period, label }: { rows: PeriodRow[]; period: string; label: string }) {
  const [hover, setHover] = useState<number | null>(null);
  if (!rows.length) return <p className="tf-empty">No approved deliveries or trucking in these dates.</p>;
  const W = 760, H = 220, left = 56, bottom = 24, top = 10;
  const max = Math.max(...rows.map((r) => Number(r.total)), 1);
  const step = niceStep(max / 4);
  const ceiling = Math.ceil(max / step) * step;
  const band = (W - left) / rows.length;
  const bar = Math.max(3, Math.min(36, band - 4));
  const y = (v: number) => top + (H - top - bottom) * (1 - v / ceiling);
  const every = Math.ceil(rows.length / 10);
  const shown = hover !== null ? rows[hover] : null;
  return <figure className="tf-chart" aria-label={label}>
    <div className="tf-legend" aria-hidden="true">
      <span><i style={{ background: SERIES.material }} />Material</span><span><i style={{ background: SERIES.hauling }} />Hauling</span>
    </div>
    <div className="relative">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="group" aria-label={`${label}: approved material and hauling cost by ${period}`}
        onMouseLeave={() => setHover(null)}>
        {Array.from({ length: Math.round(ceiling / step) + 1 }, (_, i) => i * step).map((v) => <g key={v}>
          <line x1={left} x2={W} y1={y(v)} y2={y(v)} stroke="#e7eaee" strokeWidth="1" />
          <text x={left - 6} y={y(v) + 4} textAnchor="end" className="tf-chart-axis">{compactMoney(v)}</text>
        </g>)}
        {rows.map((r, i) => {
          const x = left + i * band + (band - bar) / 2;
          const m = Number(r.material), h = Number(r.trucking);
          const mTop = y(m), hTop = y(m + h);
          return <g key={r.period} tabIndex={0} role="img" aria-label={`${periodLabel(r.period, period)}: material ${money(r.material)}, hauling ${money(r.trucking)}, total ${money(r.total)}`}
            onMouseEnter={() => setHover(i)} onFocus={() => setHover(i)} onBlur={() => setHover(null)} className="outline-none">
            <rect x={left + i * band} y={top} width={band} height={H - top - bottom} fill="transparent" />
            {m > 0 && <rect x={x} y={mTop} width={bar} height={Math.max(1, y(0) - mTop)} fill={SERIES.material} rx="2" />}
            {h > 0 && <rect x={x} y={hTop} width={bar} height={Math.max(1, mTop - hTop - (m > 0 ? 2 : 0))} fill={SERIES.hauling} rx="2" />}
            {hover === i && <rect x={x - 2} y={hTop - 2} width={bar + 4} height={y(0) - hTop + 2} fill="none" stroke="#041d2f" strokeWidth="1.5" rx="3" />}
            {i % every === 0 && <text x={left + i * band + band / 2} y={H - 6} textAnchor="middle" className="tf-chart-axis">{periodLabel(r.period, period, true)}</text>}
          </g>;
        })}
        <line x1={left} x2={W} y1={y(0)} y2={y(0)} stroke="#9aa5b1" strokeWidth="1" />
      </svg>
      {shown && <div className="tf-chart-tip" role="status" style={{ left: `${((left + (hover! + 0.5) * band) / W) * 100}%` }}>
        <strong>{periodLabel(shown.period, period)}</strong>
        <span><i style={{ background: SERIES.material }} />Material {money(shown.material)}{shown.unpriced > 0 ? ` + ${shown.unpriced} without a rate` : ""}</span>
        <span><i style={{ background: SERIES.hauling }} />Hauling {money(shown.trucking)}</span>
        <span className="font-semibold">Total {money(shown.total)} · {tons(shown.tons)} t</span>
      </div>}
    </div>
  </figure>;
}

function niceStep(raw: number) {
  const power = 10 ** Math.floor(Math.log10(Math.max(raw, 1)));
  const n = raw / power;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * power;
}

/** A ranked list with a share bar: who or what the money went to. */
export function ShareList({ rows, total, label, href, empty = "Nothing in these dates." }: {
  rows: { name: string; value: string; note?: string; warn?: string }[]; total: string; label: string;
  href?: (name: string) => string | undefined; empty?: string;
}) {
  const sum = Number(total) || 1;
  if (!rows.length) return <p className="tf-empty">{empty}</p>;
  return <ul className="tf-share" aria-label={label}>
    {rows.map((r) => {
      const share = Math.max(0, Number(r.value) / sum);
      const link = href?.(r.name);
      return <li key={r.name}>
        <div className="tf-share-line">
          {link ? <a href={link} className="tf-share-name">{r.name}</a> : <span className="tf-share-name">{r.name}</span>}
          <span className="tf-num">{money(r.value)}</span>
        </div>
        <div className="tf-share-bar"><span style={{ width: `${Math.min(100, share * 100)}%` }} /></div>
        <div className="tf-share-note">{(share * 100).toFixed(share < 0.1 ? 1 : 0)}%{r.note ? ` · ${r.note}` : ""}{r.warn && <span className="text-[#995a18]"> · {r.warn}</span>}</div>
      </li>;
    })}
  </ul>;
}

/** The figures behind the chart, by period. */
export function PeriodTable({ rows, period }: { rows: PeriodRow[]; period: string }) {
  if (!rows.length) return null;
  return <div className="tf-table-wrap"><table className="tf-table" aria-label={`Costs by ${period}`}>
    <thead><tr><th>{period === "day" ? "Day" : period === "week" ? "Week" : "Month"}</th><th className="text-right">Tickets</th><th className="text-right">Tons</th>
      <th className="text-right">Material</th><th className="text-right">Truck hours</th><th className="text-right">Hauling</th><th className="text-right">Total</th></tr></thead>
    <tbody>{[...rows].reverse().map((p) => <tr key={p.period}><td className="whitespace-nowrap font-semibold text-[#041d2f]">{periodLabel(p.period, period)}</td>
      <td className="text-right">{p.tickets.toLocaleString()}</td><td className="text-right">{tons(p.tons)}</td>
      <td className="text-right whitespace-nowrap">{money(p.material)}{p.unpriced > 0 && <span className="block text-[11px] font-semibold text-[#995a18]">+ {p.unpriced} without a rate</span>}</td>
      <td className="text-right">{hours(p.hours)}</td><td className="text-right">{money(p.trucking)}</td><td className="text-right font-semibold">{money(p.total)}</td></tr>)}</tbody>
  </table></div>;
}

/** Cost figures with the disclosure that keeps a missing rate from reading as $0. */
export function CostFigures({ report }: { report: CostReport }) {
  const c = report.costs;
  return <section className="tf-figures" aria-label="Approved costs">
    <div><p className="tf-stat-label">Verified material</p><p className="tf-figure">{tons(report.material.tons)} t</p>{report.material.other_units?.length
      ? <p className="text-[11px] text-[#5f6c7b]">plus {otherUnitsText(report.material.other_units)}, not added to tons</p> : null}
      <span>{report.material.tickets.toLocaleString()} approved deliveries</span></div>
    <div><p className="tf-stat-label">Material cost</p><p className="tf-figure">{money(c.material)}</p>
      <span className={c.unpriced ? "font-semibold text-[#995a18]" : ""}>{c.unpriced ? `Incomplete: ${c.unpriced} approved without a rate` : "At locked rates"}</span></div>
    <div><p className="tf-stat-label">Hauling cost</p><p className="tf-figure">{money(c.trucking)}</p>
      <span>{hours(report.trucking.hours)} approved trucking</span></div>
    <div><p className="tf-stat-label">Combined cost</p><p className="tf-figure">{money(c.total)}</p>
      <span className={c.complete ? "" : "font-semibold text-[#995a18]"}>{c.complete ? "Material + hauling, each counted once" : "Incomplete until every rate is set"}</span></div>
  </section>;
}
