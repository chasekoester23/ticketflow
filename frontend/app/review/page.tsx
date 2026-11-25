"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { QueueItem, QueueKind, ReviewQueue, getReviewQueue } from "../../lib/api";
import { useAccount } from "../account-context";
import { useProject } from "../project-context";

const KINDS: { kind: QueueKind; label: string; action: string }[] = [
  { kind: "tickets", label: "Delivery tickets", action: "Review" },
  { kind: "failed_files", label: "Couldn't be read", action: "Open batch" },
  { kind: "truck_shifts", label: "Truck shifts", action: "Approve" },
  { kind: "invoice_lines", label: "Invoice lines", action: "Confirm" },
];
const ACTION = Object.fromEntries(KINDS.map((k) => [k.kind, k.action])) as Record<QueueKind, string>;
const LABEL = Object.fromEntries(KINDS.map((k) => [k.kind, k.label])) as Record<QueueKind, string>;

function waiting(iso: string | null) {
  if (!iso) return "—";
  const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (minutes < 60) return minutes <= 1 ? "Just now" : `${minutes} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h`;
  return `${Math.round(hours / 24)} days`;
}

/**
 * One inbox for everything waiting on a person. Each kind of work keeps its own screen
 * (ticket review beside the scan, shift approval, invoice line confirmation); this page
 * only finds it. Nothing is approved here.
 */
export default function ReviewQueuePage() {
  const { can } = useAccount();
  const { projectId, project } = useProject();
  const [kind, setKind] = useState<QueueKind | "">("");
  const [queue, setQueue] = useState<ReviewQueue | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const wanted = new URLSearchParams(window.location.search).get("kind");
    if (KINDS.some((k) => k.kind === wanted)) setKind(wanted as QueueKind);
  }, []);
  useEffect(() => {
    let live = true;
    getReviewQueue(projectId || undefined).then((q) => { if (live) { setQueue(q); setError(null); } })
      .catch((e) => { if (live) setError(e.message); });
    return () => { live = false; };
  }, [projectId]);
  useEffect(() => { window.history.replaceState(null, "", kind ? `/review?kind=${kind}` : "/review"); }, [kind]);

  const items: QueueItem[] = (queue?.items ?? []).filter((i) => !kind || i.kind === kind);
  return <div className="space-y-4">
    <div className="tf-page-heading">
      <div><h1 className="tf-page-title">Review Queue</h1>
        <p className="tf-page-description">Everything waiting for a person{project ? ` on ${project.code}` : ""}, oldest first. Nothing counts toward quantities or costs until it&apos;s approved.</p></div>
      {can("tickets.upload") && <Link prefetch={false} href="/upload?new=1" className="tf-secondary-button">Upload delivery tickets</Link>}
    </div>
    {error && <div role="alert" className="tf-alert tf-alert-bad">{error}</div>}

    <section className="tf-panel" aria-label="Waiting by kind">
      <div className="grid grid-cols-2 lg:grid-cols-5">
        {[{ kind: "" as const, label: "All waiting", count: queue?.summary.total }, ...KINDS.map((k) => ({ kind: k.kind, label: k.label, count: queue?.summary[k.kind] }))].map((k) =>
          <button key={k.kind || "all"} type="button" aria-pressed={kind === k.kind} onClick={() => setKind(k.kind)}
            className={`tf-queue-tile ${kind === k.kind ? "tf-queue-tile-on" : ""}`}>
            <span className="tf-stat-label">{k.label}</span>
            <span className={`tf-queue-count ${k.kind === "failed_files" && (k.count ?? 0) > 0 ? "text-[#a73631]" : ""}`}>{queue ? (k.count ?? 0).toLocaleString() : "…"}</span>
          </button>)}
      </div>
    </section>

    <section className="tf-panel" aria-labelledby="queue-title">
      <div className="tf-panel-heading"><h2 id="queue-title" className="tf-panel-title">{kind ? LABEL[kind] : "All waiting"}</h2>
        <span className="text-[12px] text-[#5f6c7b]">{items.length.toLocaleString()} item{items.length === 1 ? "" : "s"}</span></div>
      {queue === null ? <p className="tf-empty" role="status">Loading the queue…</p>
        : items.length ? <div className="tf-table-wrap"><table className="tf-table" aria-label="Review queue">
          <thead><tr><th>Waiting</th><th>Item</th><th className="tf-hide-narrow">Project</th><th className="text-right">To do</th><th><span className="sr-only">Action</span></th></tr></thead>
          <tbody>{items.map((i) => <tr key={`${i.kind}-${i.id}`}>
            <td className="whitespace-nowrap text-[#5f6c7b]" title={i.created_at ? new Date(i.created_at).toLocaleString() : undefined}>{waiting(i.created_at)}</td>
            <td className="min-w-[240px]"><span className="block font-semibold text-[#041d2f]">{i.title}</span>
              <span className={`block text-[11.5px] ${i.attention ? "font-semibold text-[#995a18]" : "text-[#5f6c7b]"}`}>{LABEL[i.kind]} · {i.detail}</span></td>
            <td className="tf-hide-narrow">{i.project ? <span title={i.project.name}>{i.project.code}</span> : <span className="text-[#995a18]">No project</span>}</td>
            <td className="text-right">{i.count.toLocaleString()}</td>
            <td className="text-right"><Link prefetch={false} href={i.href} className="whitespace-nowrap">{ACTION[i.kind]} →</Link></td>
          </tr>)}</tbody>
        </table></div>
        : <p className="tf-empty" data-testid="queue-empty">{kind ? `No ${LABEL[kind].toLowerCase()} waiting.` : "Nothing is waiting. New uploads appear here as soon as they're read."}</p>}
    </section>
  </div>;
}
