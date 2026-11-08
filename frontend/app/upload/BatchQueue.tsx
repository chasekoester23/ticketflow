"use client";

import { useEffect, useState } from "react";
import { BatchQueueRow, getBatchQueue } from "../../lib/api";

const when = (value: string) => new Date(value).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

function nextStep(row: BatchQueueRow): { text: string; tone: string } {
  if (row.status === "uploaded" || row.status === "processing") return { text: "Being prepared", tone: "" };
  if (row.failed_files) return { text: `${row.failed_files} file(s) failed: retry`, tone: "tf-status-error" };
  if (row.faint_slots) return { text: `${row.faint_slots} faint slot(s) to check`, tone: "tf-status-attention" };
  const open = row.needs_review + row.failed;
  if (open) return { text: `${open} to review`, tone: "tf-status-attention" };
  if (row.flagged) return { text: `${row.flagged} flagged to resolve`, tone: "tf-status-attention" };
  return { text: "All resolved", tone: "tf-status-open" };
}

/** Where people pick their work back up. Nothing here needs remembering a link. */
export function BatchQueue() {
  const [rows, setRows] = useState<BatchQueueRow[] | null>(null);
  const [openOnly, setOpenOnly] = useState(true);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    setFailed(false);
    getBatchQueue(openOnly).then((result) => { if (active) setRows(result); })
      .catch(() => { if (active) { setRows([]); setFailed(true); } });
    return () => { active = false; };
  }, [openOnly]);

  return <section className="tf-panel" aria-label="Uploads">
    <div className="tf-panel-heading flex-wrap">
      <div>
        <h2 className="tf-panel-title">Uploads</h2>
        <p className="tf-panel-description">Pick up where you or a teammate left off.</p>
      </div>
      <div className="tf-segmented" role="group" aria-label="Which uploads to show">
        <button type="button" aria-pressed={openOnly} onClick={() => setOpenOnly(true)}>Needs work</button>
        <button type="button" aria-pressed={!openOnly} onClick={() => setOpenOnly(false)}>All</button>
      </div>
    </div>
    {failed ? <p className="tf-empty">The upload list couldn&apos;t be loaded. You can still upload below.</p>
      : rows === null ? <p className="tf-empty" role="status">Loading uploads…</p>
      : rows.length === 0 ? <p className="tf-empty">{openOnly ? "Nothing is waiting on a person." : "Nothing uploaded yet. Your first upload starts below."}</p>
      : <div className="tf-table-wrap"><table className="tf-table">
        <thead><tr><th>Batch</th><th>Project</th><th>Next step</th><th className="tf-hide-narrow">Uploaded</th><th className="tf-hide-narrow text-right">Tickets</th><th className="tf-hide-narrow">Accepted</th><th><span className="sr-only">Open</span></th></tr></thead>
        <tbody>{rows.map((row) => {
          const step = nextStep(row);
          const resolved = row.verified + row.skipped;
          return <tr key={row.id}>
            <td className="whitespace-nowrap font-bold tabular-nums">#{row.id}</td>
            <td className="min-w-[120px]">{row.project_code ? `${row.project_code} · ${row.project_name}` : <span className="text-[#a14c1c]">No project</span>}</td>
            <td><span className={`tf-status ${step.tone}`}>{step.text}</span></td>
            <td className="tf-hide-narrow whitespace-nowrap">{when(row.created_at)}{row.created_by && <span className="block text-[11px] text-[#5f6c7b]">{row.created_by}</span>}</td>
            <td className="tf-hide-narrow text-right tabular-nums">{row.tickets}</td>
            <td className="tf-hide-narrow min-w-[110px]">
              <div className="tf-progress" role="progressbar" aria-label={`Batch ${row.id} resolved`} aria-valuenow={resolved} aria-valuemin={0} aria-valuemax={row.tickets || 0}>
                <div style={{ width: `${row.tickets ? (resolved / row.tickets) * 100 : 0}%` }} />
              </div>
              <span className="mt-1 block text-[11px] tabular-nums text-[#5f6c7b]">{resolved} of {row.tickets}</span>
            </td>
            <td className="whitespace-nowrap text-right"><a href={`/upload?batch=${row.id}`} aria-label={`Open batch ${row.id}`}>Open →</a></td>
          </tr>;
        })}</tbody>
      </table></div>}
  </section>;
}
