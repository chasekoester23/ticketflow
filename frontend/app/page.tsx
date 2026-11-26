"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { Dashboard, ExceptionsSummary, Onboarding, ReviewQueue, getDashboard, getOnboarding, getReviewQueue, listExceptions } from "../lib/api";
import { useAccount } from "./account-context";
import { CostFigures, Range, RangeControls, ShareList, TrendChart, money, queryFor, rangeFor, tons, useCostReport } from "./analytics/report-parts";
import { useProject } from "./project-context";

const date = (v: string) => new Date(v).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });

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

const ACTION_LABEL = { approved: "Approved", flagged: "Flagged", skipped: "Skipped" } as const;

/**
 * Company performance first: what was delivered, what it cost, how it's trending, and
 * which jobs it went to. Work waiting for people lives in the Review Queue; here it is
 * one line. Every figure is the shared server calculation, approved records only.
 */
export default function DashboardPage() {
  const { account, can } = useAccount();
  const canSetUp = can("projects.manage");
  const { projectId, project } = useProject();
  const [range, setRange] = useState<Range>(() => rangeFor("90d"));
  const { report, error, loading } = useCostReport(queryFor(range, projectId));
  const [queue, setQueue] = useState<ReviewQueue | null>(null);
  const [exceptions, setExceptions] = useState<ExceptionsSummary | null>(null);
  const [activity, setActivity] = useState<NonNullable<Dashboard["activity"]>>([]);
  const [onboarding, setOnboarding] = useState<Onboarding | null>(null);
  const [setupHidden, setSetupHidden] = useState(false);

  useEffect(() => {
    if (!canSetUp || !account) return;
    setSetupHidden(storedHidden(account.id));
    getOnboarding().then(setOnboarding).catch(() => setOnboarding(null));
  }, [canSetUp, account]);
  useEffect(() => {
    let live = true;
    // Optional context: each is left out quietly if unavailable to this role.
    getReviewQueue(projectId || undefined).then((q) => { if (live) setQueue(q); }).catch(() => { if (live) setQueue(null); });
    listExceptions({ project_id: projectId || undefined }).then((r) => { if (live) setExceptions(r.summary); }).catch(() => { if (live) setExceptions(null); });
    getDashboard(projectId || undefined).then((d) => { if (live) setActivity(d.activity ?? []); }).catch(() => { if (live) setActivity([]); });
    return () => { live = false; };
  }, [projectId]);

  const established = Boolean(onboarding?.steps.find((step) => step.key === "approve")?.done);
  const setupApplies = canSetUp && onboarding !== null && !onboarding.complete && !established;
  function hideSetup(hidden: boolean) {
    setSetupHidden(hidden);
    try { if (account) localStorage.setItem(setupKey(account.id), hidden ? "1" : "0"); } catch { /* only a convenience */ }
  }
  const waiting = queue?.summary.total ?? 0;

  return <div className="space-y-4">
    <div className="tf-page-heading">
      <div>
        <h1 className="tf-page-title">Overview</h1>
        <p className="tf-page-description" data-testid="overview-scope">{project ? `${project.code} · ${project.name}` : "All projects"} · approved material and hauling costs</p>
      </div>
    </div>
    {setupApplies && !setupHidden && onboarding && <SetupStrip state={onboarding} onDismiss={() => hideSetup(true)} />}
    <RangeControls range={range} onChange={setRange} />
    {error && <div role="alert" className="tf-alert tf-alert-bad">{error}</div>}

    {!report ? <p className="tf-empty" role="status">Loading…</p> : <div className={`space-y-4 ${loading ? "opacity-60" : ""}`}>
      <CostFigures report={report} />
      <p className="tf-waiting" aria-label="Waiting for people">
        <Link prefetch={false} href="/review">{waiting ? `${waiting.toLocaleString()} item${waiting === 1 ? "" : "s"} in the Review Queue` : "Review Queue is clear"}</Link>
        {exceptions && exceptions.open > 0 && <><span aria-hidden="true">·</span><Link prefetch={false} href="/invoices/exceptions">{exceptions.open} open invoice exception{exceptions.open === 1 ? "" : "s"}</Link></>}
        {report.costs.unpriced > 0 && <><span aria-hidden="true">·</span><Link prefetch={false} href="/pricing">{report.costs.unpriced} approved deliver{report.costs.unpriced === 1 ? "y" : "ies"} without a rate (cost incomplete)</Link></>}
      </p>

      <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1.35fr)_minmax(0,1fr)]">
        <section className="tf-panel" aria-labelledby="trend-title">
          <div className="tf-panel-heading"><h2 id="trend-title" className="tf-panel-title">Spending over time</h2>
            <Link prefetch={false} href="/analytics" className="tf-link">Analytics →</Link></div>
          <TrendChart rows={report.by_period} period={report.period} label="Spending over time" />
        </section>
        <section className="tf-panel" aria-labelledby="projects-title">
          <div className="tf-panel-heading"><h2 id="projects-title" className="tf-panel-title">Projects</h2>
            <Link prefetch={false} href="/projects" className="tf-link">All projects →</Link></div>
          {report.by_project.length ? <div className="tf-table-wrap"><table className="tf-table" aria-label="Project performance">
            <thead><tr><th>Project</th><th className="text-right">Tons</th><th className="text-right">Material</th><th className="text-right">Hauling</th><th className="text-right">Total</th></tr></thead>
            <tbody>{report.by_project.slice(0, 8).map((p) => <tr key={p.id}>
              <td className="max-w-[220px]"><Link prefetch={false} href={`/projects/${p.id}`}>{p.code}</Link> <span className="block truncate text-[11.5px] text-[#4b5a69]">{p.name}</span></td>
              <td className="text-right">{tons(p.tons)}</td>
              <td className="text-right whitespace-nowrap">{money(p.material)}{p.unpriced > 0 && <span className="block text-[11px] font-semibold text-[#995a18]">+ {p.unpriced} no rate</span>}</td>
              <td className="text-right whitespace-nowrap">{money(p.trucking)}</td><td className="text-right whitespace-nowrap font-semibold">{money(p.total)}</td></tr>)}</tbody>
          </table></div> : <p className="tf-empty">No approved activity in these dates.{canSetUp && <> <Link prefetch={false} className="tf-link" href="/projects?new=1">Create a project</Link> to start.</>}</p>}
        </section>
      </div>

      <div className="grid items-start gap-4 xl:grid-cols-2">
        <section className="tf-panel" aria-labelledby="material-title">
          <div className="tf-panel-heading"><h2 id="material-title" className="tf-panel-title">Materials</h2></div>
          <ShareList label="Material cost by material" total={report.costs.material}
            rows={report.material.by_material.slice(0, 6).map((m) => ({ name: m.name, value: m.cost, note: `${tons(m.tons)} t`, warn: m.unpriced ? `${m.unpriced} without a rate` : undefined }))}
            empty="No approved deliveries in these dates." />
        </section>
        <section className="tf-panel" aria-labelledby="supplier-title">
          <div className="tf-panel-heading"><h2 id="supplier-title" className="tf-panel-title">Suppliers</h2></div>
          <ShareList label="Material cost by supplier" total={report.costs.material}
            rows={report.material.by_supplier.slice(0, 6).map((m) => ({ name: m.name, value: m.cost, note: `${tons(m.tons)} t`, warn: m.unpriced ? `${m.unpriced} without a rate` : undefined }))}
            empty="No approved deliveries in these dates." />
        </section>
      </div>

      <details className="tf-panel" data-testid="recent-work">
        <summary className="cursor-pointer px-4 py-2.5 text-[13px] font-semibold text-[#263646]">Recent activity</summary>
        <section className="border-t border-[#e3e7ec]" aria-labelledby="activity-title">
          <h2 id="activity-title" className="sr-only">Recent activity</h2>
          {activity.length ? <ul className="divide-y divide-[#eef0f2]">
            {activity.map((event, index) => <li key={index} className="flex items-baseline justify-between gap-3 px-4 py-1.5 text-[12.5px]">
              <span className="min-w-0 truncate">
                <span className={event.action === "approved" ? "text-[#216c43]" : "text-[#9a4a17]"}>{ACTION_LABEL[event.action]}</span>{" "}
                <Link prefetch={false} href={`/upload?batch=${event.batch_id}&ticket=${event.ticket_id}`} className="tf-link text-[12.5px]">ticket {event.ticket_number || `#${event.ticket_id}`}</Link>
                {event.by && <span className="text-[#5f6c7b]"> · {event.by}</span>}
              </span>
              <span className="shrink-0 whitespace-nowrap text-[11.5px] text-[#5f6c7b]">{relative(event.at)}</span>
            </li>)}
          </ul> : <p className="tf-empty !py-6">Approvals, flags and skips will appear here.</p>}
        </section>
      </details>
    </div>}
    <p className="text-[11.5px] text-[#5f6c7b]">Approved deliveries at their locked rates and approved trucking only. Unreviewed tickets, invoices and unpriced deliveries are never counted as cost.
      {setupApplies && setupHidden && <> <button type="button" className="tf-link text-[11px]" onClick={() => hideSetup(false)}>Show the setup checklist</button></>}</p>
  </div>;
}
