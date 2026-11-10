"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { Dashboard, Group, InvoiceOverview, Onboarding, Project, getDashboard, getInvoiceOverview, getOnboarding, listProjects } from "../lib/api";
import { useAccount } from "./account-context";

const money = (v: string | number) => `$${Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const tons = (v: string | number) => Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 });
const date = (v: string) => new Date(v).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });

function Metric({ label, value, note, attention = false }: { label: string; value: string | number; note: string; attention?: boolean }) {
  return <div className={`min-w-0 border-r border-[#e1e5e9] px-4 py-2.5 last:border-r-0 ${attention ? "bg-[#fffaf5]" : "bg-white"}`}>
    <p className="tf-stat-label">{label}</p>
    <p className={`mt-2 text-[27px] font-bold leading-none tracking-[-0.045em] tabular-nums ${attention ? "text-[#a3511d]" : "text-[#192632]"}`}>{value}</p>
    <p className="mt-1 text-[11px] leading-4 text-[#5f6c7b]">{note}</p>
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

type Attention = { label: string; hint: string; count: number; href: string; tone?: "bad" };

function relative(iso: string) {
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  if (minutes < 24 * 60) return `${Math.round(minutes / 60)} h ago`;
  return date(iso);
}

const setupKey = (accountId: number) => `tf-setup-hidden:${accountId}`;
function storedHidden(accountId: number) {
  try { return localStorage.getItem(setupKey(accountId)) === "1"; } catch { return false; }
}

/** First-run steps for a company that hasn't approved a ticket yet. One line, and it can be put away. */
function SetupStrip({ state, onDismiss }: { state: Onboarding; onDismiss: () => void }) {
  const [open, setOpen] = useState(false);
  const done = state.steps.filter((step) => step.done).length;
  const next = state.steps.find((step) => !step.done);
  return <section className="tf-panel" aria-label="Setup checklist">
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2 text-[12.5px]">
      <h2 className="tf-panel-title">Setup</h2>
      <span className="tabular-nums text-[#5f6c7b]">{done} of {state.steps.length} done</span>
      {next && <span className="text-[#3b4a59]">Next: <Link prefetch={false} href={next.href} className="tf-link text-[12.5px]">{next.title}</Link></span>}
      <span className="ml-auto flex items-center gap-3">
        <button type="button" className="tf-link text-[12px]" aria-expanded={open} aria-controls="setup-steps" onClick={() => setOpen((v) => !v)}>{open ? "Hide steps" : "All steps"}</button>
        <button type="button" className="text-[12px] font-medium text-[#5f6c7b] hover:text-[#1d2b38]" onClick={onDismiss}>Dismiss</button>
      </span>
    </div>
    {open && <ol id="setup-steps" className="grid border-t border-[#e8ebee] sm:grid-cols-2 xl:grid-cols-3">
      {state.steps.map((step) => <li key={step.key} className="flex items-baseline gap-2 border-b border-[#eef0f2] px-4 py-1.5 text-[12.5px]">
        <span aria-hidden="true" className={step.done ? "text-[#2f855a]" : "text-[#9aa5b1]"}>{step.done ? "✓" : "○"}</span>
        {step.done ? <span className="text-[#5f6c7b]">{step.title}<span className="sr-only"> (done)</span></span>
          : <Link prefetch={false} href={step.href} className="font-semibold text-[#253342] hover:underline">{step.title}</Link>}
      </li>)}
    </ol>}
  </section>;
}

function AttentionList({ items }: { items: Attention[] }) {
  const open = items.reduce((sum, item) => sum + item.count, 0);
  return <section className="tf-panel" aria-labelledby="attention-title">
    <div className="tf-panel-heading">
      <h2 id="attention-title" className="tf-panel-title">Needs attention</h2>
      <span className="text-[11.5px] text-[#5f6c7b]">{open ? `${open.toLocaleString()} open` : "Nothing waiting"}</span>
    </div>
    <ul className="divide-y divide-[#eef0f2]">
      {items.map((item) => <li key={item.label}>
        <Link prefetch={false} href={item.href} className="flex items-center justify-between gap-3 px-4 py-1.5 hover:bg-[#fafbfc]">
          <span className="min-w-0">
            <span className={`block text-[13px] font-semibold ${item.count ? "text-[#1f2c39]" : "text-[#5f6c7b]"}`}>{item.label}</span>
            <span className="block text-[11.5px] text-[#5f6c7b]">{item.hint}</span>
          </span>
          <span className={`min-w-[2.5rem] rounded-[2px] border px-2 py-0.5 text-center text-[14px] font-semibold tabular-nums ${!item.count ? "border-[#e1e5e9] bg-[#f7f8f9] text-[#5f6c7b]"
            : item.tone === "bad" ? "border-[#f0c4c2] bg-[#fdf1f0] text-[#9b2c26]" : "border-[#efcb9b] bg-[#fff5e6] text-[#9a4a17]"}`}>{item.count.toLocaleString()}</span>
        </Link>
      </li>)}
    </ul>
  </section>;
}

const BATCH_STATUS: Record<string, [string, string]> = {
  uploaded: ["Uploaded", ""], processing: ["Processing", "tf-status-attention"],
  ready_for_review: ["Ready for review", "tf-status-open"], failed: ["Failed", "tf-status-error"],
};
const ACTION_LABEL = { approved: "Approved", flagged: "Flagged", skipped: "Skipped" } as const;

export default function DashboardPage() {
  const { account, can } = useAccount();
  const canUpload = can("tickets.upload");
  const canSetUp = can("projects.manage");
  const [data, setData] = useState<Dashboard | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [onboarding, setOnboarding] = useState<Onboarding | null>(null);
  const [setupHidden, setSetupHidden] = useState(false);
  const [invoices, setInvoices] = useState<InvoiceOverview | null>(null);

  useEffect(() => {
    listProjects().then(setProjects).catch(() => {});
    // Not every role reads invoice comparisons; without it, that row is simply left out.
    getInvoiceOverview().then(setInvoices).catch(() => setInvoices(null));
  }, []);
  useEffect(() => {
    if (!canSetUp || !account) return;
    setSetupHidden(storedHidden(account.id));
    getOnboarding().then(setOnboarding).catch(() => setOnboarding(null));
  }, [canSetUp, account]);
  useEffect(() => {
    let active = true;
    setError(null);
    getDashboard(projectId || undefined)
      .then((result) => { if (active) setData(result); })
      .catch((e) => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [projectId]);

  // A company that has approved a ticket is up and running: the checklist has done its job.
  const established = Boolean(onboarding?.steps.find((step) => step.key === "approve")?.done);
  const setupApplies = canSetUp && onboarding !== null && !onboarding.complete && !established;
  function hideSetup(hidden: boolean) {
    setSetupHidden(hidden);
    try { if (account) localStorage.setItem(setupKey(account.id), hidden ? "1" : "0"); } catch { /* only a convenience */ }
  }

  const failed = data?.failed ?? 0;
  const attention: Attention[] = data ? [
    { label: "Awaiting verification", hint: "Read, not yet checked against the scan", count: Math.max(0, data.needs_review - data.flagged - failed), href: "/records?status=needs_review" },
    { label: "Flagged for follow-up", hint: "Set aside with a question", count: data.flagged, href: "/records?status=flagged" },
    { label: "Couldn't be read", hint: "Enter by hand, or retry the batch", count: failed, href: "/records?status=failed", tone: "bad" },
    { label: "Approved without a rate", hint: "No cost until a rate is set", count: data.unpriced_tickets, href: "/pricing" },
    ...(invoices ? [{ label: "Invoice lines with differences", hint: "Billed differently from approved tickets", count: invoices.by_status.discrepancy ?? 0, href: "/invoices", tone: "bad" as const }] : []),
  ] : [];

  return <div className="space-y-4">
    <div className="tf-page-heading">
      <div>
        <h1 className="tf-page-title">Delivery operations</h1>
        <p className="tf-page-description">Tickets, review status and approved quantities across your jobs.</p>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <label className="sr-only" htmlFor="dashboard-project">Filter overview by project</label>
        <select id="dashboard-project" className="tf-field min-w-[170px]" value={projectId} onChange={(e) => setProjectId(e.target.value)}>
          <option value="">All projects</option>
          {projects.map((p) => <option key={p.id} value={p.id}>{p.code} · {p.name}</option>)}
        </select>
        {canUpload && <Link prefetch={false} href="/upload" className="tf-primary-button"><span aria-hidden="true" className="text-lg leading-none">+</span> New ticket upload</Link>}
      </div>
    </div>

    {error && <div role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">{error}</div>}
    {setupApplies && !setupHidden && onboarding && <SetupStrip state={onboarding} onDismiss={() => hideSetup(true)} />}

    {data ? <>
      <section className="tf-panel" aria-label="Totals">
        <div className="grid grid-cols-2 lg:grid-cols-5">
          <Metric label="Tickets received" value={data.tickets.toLocaleString()} note="All uploaded ticket records" />
          <Metric label="Needs review" value={data.needs_review.toLocaleString()} note={`${data.flagged.toLocaleString()} flagged, ${failed.toLocaleString()} unreadable`} attention={data.needs_review > 0} />
          <Metric label="Accepted tickets" value={data.verified.toLocaleString()} note="Individually approved records" />
          <Metric label="Verified tonnage" value={tons(data.total_tons)} note={`${tons(data.pending_tons)} unverified tons excluded`} />
          <Metric label="Material cost" value={money(data.total_cost)} note={data.unpriced_tickets ? `${data.unpriced_tickets} accepted tickets missing rates` : "Verified, priced deliveries only"} />
        </div>
      </section>

      <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.45fr)]">
        <AttentionList items={attention} />
        <section className="tf-panel" aria-labelledby="projects-title">
          <div className="tf-panel-heading">
            <h2 id="projects-title" className="tf-panel-title">Active projects</h2>
            <Link prefetch={false} href="/projects" className="tf-link">All projects →</Link>
          </div>
          {data.projects?.length ? <div className="tf-table-wrap"><table className="tf-table" aria-label="Active projects">
            <thead><tr><th>Project</th><th className="tf-hide-narrow text-right">Tickets</th><th className="text-right">Open</th><th className="text-right">Verified tons</th><th className="tf-hide-narrow">Last upload</th></tr></thead>
            <tbody>{data.projects.map((p) => <tr key={p.id}>
              <td><Link prefetch={false} href={`/records?project_id=${p.id}`}>{p.code}</Link> <span className="text-[#3b4a59]">{p.name}</span></td>
              <td className="tf-hide-narrow text-right">{p.tickets.toLocaleString()}</td>
              <td className={`text-right ${p.open ? "font-semibold text-[#9a4a17]" : "text-[#5f6c7b]"}`}>{p.open.toLocaleString()}</td>
              <td className="text-right">{tons(p.verified_tons)}</td>
              <td className="tf-hide-narrow whitespace-nowrap text-[#5f6c7b]">{p.last_upload ? relative(p.last_upload) : "No uploads"}</td>
            </tr>)}</tbody>
          </table></div> : <p className="tf-empty !py-6">No active projects.{canSetUp && <> <Link prefetch={false} className="tf-link" href="/projects">Create one</Link>, so uploads are filed by job.</>}</p>}
        </section>
      </div>

      <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1.45fr)_minmax(0,1fr)]">
        <section className="tf-panel" aria-labelledby="uploads-title">
          <div className="tf-panel-heading">
            <h2 id="uploads-title" className="tf-panel-title">Recent uploads</h2>
            <Link prefetch={false} href="/upload" className="tf-link">Open intake →</Link>
          </div>
          {data.recent_batches.length ? <div className="tf-table-wrap"><table className="tf-table" aria-label="Recent uploads">
            <thead><tr><th>Batch</th><th>Project</th><th className="tf-hide-narrow">Uploaded</th><th className="tf-hide-narrow text-right">Files</th><th>Status</th></tr></thead>
            <tbody>{data.recent_batches.map((b) => {
              const [label, tone] = BATCH_STATUS[b.status] ?? [b.status, ""];
              return <tr key={b.id}>
                <td><Link prefetch={false} href={`/upload?batch=${b.id}`}>#{b.id}</Link></td>
                <td>{b.project ?? "Unassigned"}</td>
                <td className="tf-hide-narrow whitespace-nowrap">{relative(b.created_at)}</td>
                <td className="tf-hide-narrow text-right">{b.files}</td>
                <td><span className={`tf-status ${tone}`}>{label}</span></td>
              </tr>;
            })}</tbody>
          </table></div> : <p className="tf-empty !py-6">No uploads yet.{canUpload && <> <Link prefetch={false} className="tf-link" href="/upload">Upload scanned tickets</Link>.</>}</p>}
        </section>

        <section className="tf-panel" aria-labelledby="activity-title">
          <div className="tf-panel-heading"><h2 id="activity-title" className="tf-panel-title">Recent activity</h2></div>
          {data.activity?.length ? <ul className="divide-y divide-[#eef0f2]">
            {data.activity.map((event, index) => <li key={index} className="flex items-baseline justify-between gap-3 px-4 py-1.5 text-[12.5px]">
              <span className="min-w-0 truncate">
                <span className={event.action === "approved" ? "text-[#216c43]" : "text-[#9a4a17]"}>{ACTION_LABEL[event.action]}</span>{" "}
                <Link prefetch={false} href={`/upload?batch=${event.batch_id}&ticket=${event.ticket_id}`} className="tf-link text-[12.5px]">ticket {event.ticket_number || `#${event.ticket_id}`}</Link>
                {event.by && <span className="text-[#5f6c7b]"> · {event.by}</span>}
              </span>
              <span className="shrink-0 whitespace-nowrap text-[11.5px] text-[#5f6c7b]">{relative(event.at)}</span>
            </li>)}
          </ul> : <p className="tf-empty !py-6">Approvals, flags and skips will appear here.</p>}
        </section>
      </div>

      <div className="grid items-start gap-4 xl:grid-cols-2">
        <Breakdown title="Deliveries by supplier" description="Tickets received, with verified quantities and costs" rows={data.by_supplier} />
        <Breakdown title="Deliveries by material" description="Approved tonnage and material cost by type" rows={data.by_material} />
      </div>
      <p className="text-[11px] leading-5 text-[#5f6c7b]">
        Quantity and cost summaries exclude unverified deliveries. Material cost excludes taxes and delivery charges.
        {data.pending_cost_tickets > 0 ? ` ${data.pending_cost_tickets} tickets are not yet eligible for cost reporting.` : ""}
        {setupApplies && setupHidden && <> <button type="button" className="tf-link text-[11px]" onClick={() => hideSetup(false)}>Show the setup checklist</button></>}
      </p>
    </> : !error && <div role="status" className="tf-panel p-8 text-sm text-[#5f6c7b]">Loading operations overview…</div>}
  </div>;
}
