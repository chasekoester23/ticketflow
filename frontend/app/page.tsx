"use client";

import { useEffect, useState } from "react";
import { Dashboard, Group, Project, getDashboard, listProjects } from "../lib/api";

const money = (v: string | number) => `$${Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const tons = (v: string | number) => Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 });
const date = (v: string) => new Date(v).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });

function Metric({ label, value, note, attention = false }: { label: string; value: string | number; note: string; attention?: boolean }) {
  return <div className={`min-w-0 border-r border-[#e1e5e9] px-5 py-5 last:border-r-0 ${attention ? "bg-[#fffaf5]" : "bg-white"}`}>
    <p className="text-[11px] font-bold tracking-[0.055em] text-[#637181] uppercase">{label}</p>
    <p className={`mt-2 text-[27px] font-bold leading-none tracking-[-0.045em] tabular-nums ${attention ? "text-[#b85c2b]" : "text-[#192632]"}`}>{value}</p>
    <p className="mt-3 text-[11px] leading-5 text-[#7b8794]">{note}</p>
  </div>;
}

function Breakdown({ title, rows, description }: { title: string; rows: Group[]; description: string }) {
  return <section className="tf-panel">
    <div className="tf-panel-heading">
      <div><h2 className="tf-panel-title">{title}</h2><p className="tf-panel-description">{description}</p></div>
    </div>
    {rows.length ? <div className="tf-table-wrap"><table className="tf-table">
      <thead><tr><th>Name</th><th className="text-right">Tickets</th><th className="text-right">Verified tons</th><th className="text-right">Material cost</th></tr></thead>
      <tbody>{rows.map((r) => <tr key={r.name}>
        <td className="font-semibold">{r.name}</td><td className="text-right tabular-nums">{r.tickets}</td>
        <td className="text-right tabular-nums">{tons(r.tons)}</td><td className="text-right tabular-nums">{money(r.cost)}</td>
      </tr>)}</tbody>
    </table></div> : <p className="tf-empty">No delivery records to summarize yet.</p>}
  </section>;
}

export default function DashboardPage() {
  const [data, setData] = useState<Dashboard | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listProjects().then(setProjects).catch(() => {});
  }, []);
  useEffect(() => {
    let active = true;
    setError(null);
    getDashboard(projectId || undefined)
      .then((result) => { if (active) setData(result); })
      .catch((e) => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [projectId]);

  return <div className="space-y-5">
    <div className="tf-page-heading">
      <div>
        <p className="tf-eyebrow">OPERATIONS / OVERVIEW</p>
        <h1 className="tf-page-title">Delivery operations</h1>
        <p className="tf-page-description">Material ticket volume, review status, and verified quantities across your projects.</p>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <label className="sr-only" htmlFor="dashboard-project">Filter overview by project</label>
        <select id="dashboard-project" className="tf-field min-w-[170px]" value={projectId} onChange={(e) => setProjectId(e.target.value)}>
          <option value="">All projects</option>
          {projects.map((p) => <option key={p.id} value={p.id}>{p.code} · {p.name}</option>)}
        </select>
        <a href="/upload" className="tf-primary-button"><span aria-hidden="true" className="text-lg leading-none">+</span> New ticket upload</a>
      </div>
    </div>

    {error && <div role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">{error}</div>}

    {data ? <>
      <section className="tf-panel">
        <div className="border-b border-[#dce1e6] px-5 py-3.5 flex items-center justify-between gap-3">
          <h2 className="text-[12px] font-bold text-[#344352]">Portfolio snapshot</h2>
          <span className="text-[11px] text-[#788493]">Confirmed figures only where indicated</span>
        </div>
        <div className="grid grid-cols-2 lg:grid-cols-5">
          <Metric label="Tickets received" value={data.tickets} note="All uploaded ticket records" />
          <Metric label="Needs review" value={data.needs_review} note={`${data.flagged} flagged separately`} attention={data.needs_review > 0} />
          <Metric label="Accepted tickets" value={data.verified} note="Individually approved records" />
          <Metric label="Verified tonnage" value={tons(data.total_tons)} note={`${tons(data.pending_tons)} unverified tons excluded`} />
          <Metric label="Material cost" value={money(data.total_cost)} note={data.unpriced_tickets ? `${data.unpriced_tickets} accepted tickets missing rates` : "Verified, priced deliveries only"} />
        </div>
      </section>

      <section className="grid gap-5 lg:grid-cols-[minmax(0,1.7fr)_minmax(260px,1fr)]">
        <div className="tf-panel">
          <div className="tf-panel-heading">
            <div><h2 className="tf-panel-title">Recent ticket batches</h2><p className="tf-panel-description">Latest documents received into TicketFlow</p></div>
            <a href="/upload" className="tf-link">Open intake →</a>
          </div>
          {data.recent_batches.length ? <div className="tf-table-wrap"><table className="tf-table">
            <thead><tr><th>Batch</th><th>Project</th><th>Uploaded</th><th>Files</th><th>Status</th></tr></thead>
            <tbody>{data.recent_batches.map((b) => <tr key={b.id}>
              <td><a href={`/upload?batch=${b.id}`}>#{b.id}</a></td>
              <td className="font-semibold">{b.project ?? "Unassigned"}</td>
              <td className="whitespace-nowrap">{date(b.created_at)}</td>
              <td className="tabular-nums">{b.files}</td>
              <td><span className={`tf-status ${b.status === "failed" ? "tf-status-error" : b.status === "ready_for_review" ? "tf-status-open" : b.status === "processing" ? "tf-status-attention" : ""}`}>
                {b.status.replaceAll("_", " ")}
              </span></td>
            </tr>)}</tbody>
          </table></div> : <div className="tf-empty">
            <p>No uploads recorded yet.</p>
            <a className="tf-link mt-3 inline-block" href="/upload">Upload your first ticket batch →</a>
          </div>}
        </div>

        <div className="tf-panel">
          <div className="tf-panel-heading">
            <div><h2 className="tf-panel-title">Review desk</h2><p className="tf-panel-description">Items needing human attention</p></div>
          </div>
          <div className="divide-y divide-[#e8ebee]">
            <a href="/records?status=needs_review" className="flex items-center justify-between gap-4 px-5 py-5 hover:bg-[#fafbfc]">
              <div><p className="text-[13px] font-bold text-[#253342]">Awaiting verification</p><p className="mt-1 text-[11px] text-[#7c8996]">Confirm each extracted delivery</p></div>
              <span className={`rounded border px-2.5 py-1 text-[15px] font-bold tabular-nums ${data.needs_review ? "border-[#efcb9b] bg-[#fff5e6] text-[#b26828]" : "border-[#dce1e6] bg-[#f5f6f7] text-[#657182]"}`}>{data.needs_review}</span>
            </a>
            <a href="/records?status=flagged" className="flex items-center justify-between gap-4 px-5 py-5 hover:bg-[#fafbfc]">
              <div><p className="text-[13px] font-bold text-[#253342]">Flagged for follow-up</p><p className="mt-1 text-[11px] text-[#7c8996]">Questions or missing information</p></div>
              <span className="rounded border border-[#dce1e6] bg-[#f5f6f7] px-2.5 py-1 text-[15px] font-bold tabular-nums text-[#455263]">{data.flagged}</span>
            </a>
          </div>
          <div className="border-t border-[#e4e8eb] bg-[#f8f9fa] px-5 py-4">
            <a className="tf-secondary-button w-full" href="/records">View all delivery records <span aria-hidden="true">→</span></a>
          </div>
        </div>
      </section>

      <div className="grid gap-5 xl:grid-cols-2">
        <Breakdown title="Deliveries by supplier" description="Tickets received, with verified quantities and costs" rows={data.by_supplier} />
        <Breakdown title="Deliveries by material" description="Approved tonnage and material cost by type" rows={data.by_material} />
      </div>
      <p className="text-[11px] leading-5 text-[#718091]">
        Quantity and cost summaries exclude unverified deliveries. Material cost excludes taxes and delivery charges.
        {data.pending_cost_tickets > 0 ? ` ${data.pending_cost_tickets} tickets are not yet eligible for cost reporting.` : ""}
      </p>
    </> : !error && <div role="status" className="tf-panel p-8 text-sm text-[#718091]">Loading operations overview…</div>}
  </div>;
}
