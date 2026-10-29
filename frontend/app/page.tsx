"use client";

import { useEffect, useState } from "react";
import { Dashboard, Group, Project, getDashboard, listProjects } from "../lib/api";

const money = (v: string | number) => `$${Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const tons = (v: string | number) => Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 });

export default function DashboardPage() {
  const [data, setData] = useState<Dashboard | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listProjects().then(setProjects).catch(() => {});
  }, []);
  useEffect(() => {
    getDashboard(projectId || undefined).then(setData).catch((e) => setError(e.message));
  }, [projectId]);

  const Card = ({ label, value, sub }: { label: string; value: string | number; sub?: string }) => (
    <div className="rounded-lg border border-slate-200 bg-white p-4">
      <div className="text-xs uppercase text-slate-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold">{value}</div>
      {sub && <div className="mt-1 text-xs text-slate-500">{sub}</div>}
    </div>
  );
  const Table = ({ title, rows }: { title: string; rows: Group[] }) => (
    <section className="rounded-lg border border-slate-200 bg-white p-5">
      <h2 className="font-semibold">{title}</h2>
      {rows.length === 0 ? (
        <p className="mt-2 text-sm text-slate-500">No data yet.</p>
      ) : (
        <table className="mt-3 w-full text-left text-sm">
          <thead className="text-xs uppercase text-slate-500">
            <tr><th className="py-1">Name</th><th>Tickets</th><th>Tons</th><th>Cost</th></tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.name} className="border-t border-slate-100">
                <td className="py-1">{r.name}</td><td>{r.tickets}</td><td>{tons(r.tons)}</td><td>{money(r.cost)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );

  return (
    <div className="space-y-6">
      {error && <div className="rounded border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-800">{error}</div>}
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Dashboard</h1>
        <select value={projectId} onChange={(e) => setProjectId(e.target.value)} className="rounded border border-slate-300 bg-white px-3 py-2 text-sm">
          <option value="">All projects</option>
          {projects.map((p) => <option key={p.id} value={p.id}>{p.code} · {p.name}</option>)}
        </select>
      </div>
      {data && (
        <>
          <div className="grid grid-cols-2 gap-4 md:grid-cols-5">
            <Card label="Tickets" value={data.tickets} />
            <Card label="Need review" value={data.needs_review} sub={data.flagged ? `${data.flagged} flagged for later` : undefined} />
            <Card label="Verified" value={data.verified} />
            <Card label="Verified tons" value={tons(data.total_tons)} sub={`${tons(data.pending_tons)} unverified tons excluded`} />
            <Card label="Verified material cost" value={money(data.total_cost)} sub={data.unpriced_tickets ? `${data.unpriced_tickets} verified tickets lack a rate` : "Cost shown only for verified, priced tickets"} />
          </div>
          <p className="text-xs text-slate-500">Totals include only verified quantities. {data.pending_cost_tickets} tickets still need verification; material cost excludes delivery charges and taxes. The supplier and material tables count all tickets, but tons and costs include verified records only.</p>
          <div className="grid gap-6 md:grid-cols-2">
            <Table title="By supplier" rows={data.by_supplier} />
            <Table title="By material" rows={data.by_material} />
          </div>
          <section className="rounded-lg border border-slate-200 bg-white p-5">
            <h2 className="font-semibold">Recent uploads</h2>
            {data.recent_batches.length === 0 ? (
              <p className="mt-2 text-sm text-slate-500">Nothing uploaded yet. <a href="/upload" className="underline">Upload tickets</a>.</p>
            ) : (
              <ul className="mt-3 space-y-1 text-sm">
                {data.recent_batches.map((b) => (
                  <li key={b.id}>
                    <a href={`/upload?batch=${b.id}`} className="underline">Batch #{b.id}</a> · {b.project ?? "No project"} · {b.files} file(s) · {b.status.replace(/_/g, " ")} · {new Date(b.created_at).toLocaleString()}
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      )}
    </div>
  );
}
