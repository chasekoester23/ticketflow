"use client";

import { memo, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import type { Ticket } from "../../lib/api";
import { STATUS_LABEL, STATUS_STYLE } from "./ReviewForm";

export type QueueFilter = "pending" | "flagged" | "all";
export const pendingStatus = (t: Ticket) => t.status === "needs_review" || t.status === "failed";
/** What the server's bulk edit will change (backend/app/routers/records.py BULK_EDITABLE). */
export const bulkEditable = (t: Ticket) => t.status === "needs_review" || t.status === "flagged";

/** Kinds of problem a ticket can carry, for narrowing the queue. Advice only: nothing here decides a ticket. */
export type Issue = "warnings" | "missing_tons" | "duplicate" | "no_rate" | "not_tons";
export const ISSUES: { key: Issue; label: string; test: (t: Ticket) => boolean }[] = [
  { key: "warnings", label: "Has warnings", test: (t) => t.flags.length > 0 },
  { key: "missing_tons", label: "Missing tons", test: (t) => !t.tonnage },
  { key: "duplicate", label: "Possible duplicate", test: (t) => t.duplicates.length > 0 },
  { key: "no_rate", label: "No rate", test: (t) => t.unit_price === null },
  { key: "not_tons", label: "Unit not tons", test: (t) => (t.unit ?? "ton") !== "ton" },
];
export const hasIssue = (t: Ticket, issue: Issue | "") =>
  issue ? ISSUES.find((i) => i.key === issue)!.test(t) : ISSUES.some((i) => i.test(t));

const QueueRow = memo(function QueueRow({ ticket, index, isCurrent, onSelect, picking, picked, onPick }: {
  ticket: Ticket;
  index: number;
  isCurrent: boolean;
  onSelect: (index: number) => void;
  picking: boolean;
  picked: boolean;
  onPick: (id: number) => void;
}) {
  const open = <button type="button" onClick={() => onSelect(index)}
    aria-current={isCurrent ? "true" : undefined}
    className={`tf-queue-row block w-full min-w-0 border-l-[3px] px-2.5 py-2 text-left ${isCurrent ? "border-[#df702f] bg-[#fff8f2]" : "border-transparent hover:bg-[#f8f9fa]"}`}>
    <span className="flex items-center justify-between gap-1.5">
      <span className="min-w-0 truncate text-xs font-bold tabular-nums text-[#1d2b38]">#{index + 1} · {ticket.ticket_number || "No number yet"}</span>
      <span className={`tf-status shrink-0 !px-1 !py-0.5 !text-[9px] ${STATUS_STYLE[ticket.status] ?? ""}`}>{STATUS_LABEL[ticket.status] ?? ticket.status}</span>
    </span>
    <span className="mt-0.5 block truncate text-[11px] text-[#5f6c7b]">{ticket.flag_note || `${ticket.material_type || "Material not entered"} · ${ticket.tonnage ?? "—"} t`}</span>
  </button>;
  if (!picking) return open;
  const editable = bulkEditable(ticket);
  return <div className="tf-queue-pick">
    <input type="checkbox" checked={picked} disabled={!editable} onChange={() => onPick(ticket.id)}
      aria-label={`Select ticket ${index + 1}${editable ? "" : " (approved or skipped tickets can't be changed together)"}`} />
    {open}
  </div>;
});

/**
 * The list of tickets beside the scan. Search stays in here, and only rows whose
 * ticket or place changed render again, so a batch of thousands costs little per key.
 */
export const TicketQueue = memo(function TicketQueue({ tickets, selected, filter, onFilter, onSelect, onHide, issue, onIssue, picking, picked, onPick, onPickMany, canPick }: {
  tickets: Ticket[];
  selected: number;
  filter: QueueFilter;
  onFilter: (filter: QueueFilter) => void;
  onSelect: (index: number) => void;
  onHide: () => void;
  issue: Issue | "";
  onIssue: (issue: Issue | "") => void;
  /** Choosing tickets to correct together. */
  picking: boolean;
  picked: ReadonlySet<number>;
  onPick: (id: number) => void;
  onPickMany: (ids: number[] | null) => void;
  canPick: boolean;
}) {
  const [search, setSearch] = useState("");
  // The box answers straight away. The list follows as soon as the browser has time.
  const needle = useDeferredValue(search).trim().toLowerCase();
  const rows = useMemo(() => {
    const found: { ticket: Ticket; index: number }[] = [];
    tickets.forEach((ticket, index) => {
      if (filter === "pending" && !pendingStatus(ticket)) return;
      if (filter === "flagged" && ticket.status !== "flagged") return;
      if (issue && !hasIssue(ticket, issue)) return;
      if (needle && ![ticket.ticket_number, ticket.material_type, ticket.supplier, ticket.job_location]
        .some((field) => (field ?? "").toLowerCase().includes(needle))) return;
      found.push({ ticket, index });
    });
    return found;
  }, [tickets, filter, issue, needle]);

  // Keep the open ticket in sight in a long list. Only the list scrolls, never the
  // page, so the keyboard can't pull a phone's screen away from the scan.
  const list = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const box = list.current;
    const row = box?.querySelector<HTMLElement>("[aria-current=true]");
    if (!box || !row) return;
    const top = (row.closest(".tf-queue-pick") as HTMLElement | null ?? row).offsetTop;
    if (top < box.scrollTop) box.scrollTop = top;
    else if (top + row.offsetHeight > box.scrollTop + box.clientHeight) box.scrollTop = top + row.offsetHeight - box.clientHeight;
  }, [selected, rows]);

  const pendingCount = useMemo(() => tickets.filter(pendingStatus).length, [tickets]);
  const flaggedCount = useMemo(() => tickets.filter((ticket) => ticket.status === "flagged").length, [tickets]);
  // Counts for the tickets still open, which is where an issue costs time.
  const issueCounts = useMemo(() => Object.fromEntries(ISSUES.map((i) =>
    [i.key, tickets.filter((t) => (pendingStatus(t) || t.status === "flagged") && i.test(t)).length])), [tickets]);
  const editableShown = rows.filter((row) => bulkEditable(row.ticket)).map((row) => row.ticket.id);
  const resolvedShown = rows.filter((row) => row.ticket.status === "verified" || row.ticket.status === "skipped").length;
  return <section className="tf-panel tf-review-pane" aria-label="Ticket queue">
    <div className="border-b border-[#e7eaee] p-2.5">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-xs font-bold text-[#3c4b5b]">Ticket queue</h2>
        <button type="button" className="tf-link text-[11px]" onClick={onHide}>Hide</button>
      </div>
      <div className="tf-segmented mt-2 flex" role="group" aria-label="Filter ticket queue">
        {(["pending", "flagged", "all"] as const).map((name) => {
          // "All" has no count: the total is just below, and the queue is narrow.
          const count = name === "pending" ? pendingCount : name === "flagged" ? flaggedCount : null;
          return <button type="button" key={name} className="flex-1" aria-pressed={filter === name} onClick={() => onFilter(name)}>
            {name === "pending" ? "To review" : name === "flagged" ? "Flagged" : "All"}
            {/* The batch summary says these counts in words; here they're for the eye. */}
            {count !== null && <span aria-hidden="true" className={`ml-1 tabular-nums ${name === "flagged" && count ? "rounded-[2px] bg-[#fff1dc] px-1 font-semibold text-[#8f4a12]" : "text-[#5f6c7b]"}`}>{count}</span>}
          </button>;
        })}
      </div>
      <label htmlFor="queue-issue" className="sr-only">Show only tickets with an issue</label>
      <select id="queue-issue" data-testid="queue-issue" value={issue} onChange={(e) => onIssue(e.target.value as Issue | "")}
        className="tf-field mt-2 w-full !min-h-0 !py-1 !text-xs">
        <option value="">Any issue or none</option>
        {ISSUES.map((i) => <option key={i.key} value={i.key}>{i.label} ({issueCounts[i.key]} open)</option>)}
      </select>
      <label htmlFor="queue-search" className="sr-only">Search tickets in queue</label>
      <input id="queue-search" type="search" value={search} onChange={(e) => setSearch(e.target.value)}
        placeholder="Number, supplier, material…" className="tf-field mt-2 w-full !min-h-0 !py-1.5 !text-xs" />
      <p className="mt-1.5 text-[11px] text-[#5f6c7b]" data-testid="queue-count">{rows.length} matching ticket(s) · {tickets.length} total
        {rows.length > 0 && filter === "all" && <> · {resolvedShown} of {rows.length} resolved</>}</p>
      {canPick && <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px]">
        <button type="button" className="tf-link" aria-pressed={picking} onClick={() => onPickMany(picking ? null : [])}>
          {picking ? "Done choosing" : "Correct several…"}
        </button>
        {picking && editableShown.length > 0 && <button type="button" className="tf-link" onClick={() => onPickMany(editableShown)}>
          Choose all {editableShown.length} shown</button>}
      </div>}
    </div>
    {rows.length === 0 ? (
      <div className="p-3 text-xs text-[#526273]">
        No tickets match the current view.
        {search && <button type="button" className="tf-link ml-2" onClick={() => setSearch("")}>Clear search</button>}
        <button type="button" className="tf-link ml-2" onClick={() => { onIssue(""); onFilter("all"); }}>Show all</button>
      </div>
    ) : (
      <div ref={list} className="tf-review-pane-scroll relative max-h-72 divide-y divide-[#edf0f2] overflow-auto xl:max-h-none">
        {rows.map(({ ticket, index }) => <QueueRow key={ticket.id} ticket={ticket} index={index} isCurrent={selected === index} onSelect={onSelect}
          picking={picking} picked={picked.has(ticket.id)} onPick={onPick} />)}
      </div>
    )}
  </section>;
});
