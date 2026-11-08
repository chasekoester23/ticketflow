"use client";

import { memo, useDeferredValue, useMemo, useState } from "react";
import type { Ticket } from "../../lib/api";
import { STATUS_LABEL, STATUS_STYLE } from "./ReviewForm";

export type QueueFilter = "pending" | "flagged" | "all";
export const pendingStatus = (t: Ticket) => t.status === "needs_review" || t.status === "failed";

const QueueRow = memo(function QueueRow({ ticket, index, isCurrent, onSelect }: {
  ticket: Ticket;
  index: number;
  isCurrent: boolean;
  onSelect: (index: number) => void;
}) {
  return <button type="button" onClick={() => onSelect(index)}
    aria-current={isCurrent ? "true" : undefined}
    className={`tf-queue-row block w-full border-l-[3px] px-2.5 py-2 text-left ${isCurrent ? "border-[#df702f] bg-[#fff8f2]" : "border-transparent hover:bg-[#f8f9fa]"}`}>
    <span className="flex items-center justify-between gap-1.5">
      <span className="min-w-0 truncate text-xs font-bold tabular-nums text-[#1d2b38]">#{index + 1} · {ticket.ticket_number || "No number yet"}</span>
      <span className={`tf-status shrink-0 !px-1 !py-0.5 !text-[9px] ${STATUS_STYLE[ticket.status] ?? ""}`}>{STATUS_LABEL[ticket.status] ?? ticket.status}</span>
    </span>
    <span className="mt-0.5 block truncate text-[11px] text-[#7a8794]">{ticket.flag_note || `${ticket.material_type || "Material not entered"} · ${ticket.tonnage ?? "—"} t`}</span>
  </button>;
});

/**
 * The list of tickets beside the scan. Search stays in here, and only rows whose
 * ticket or place changed render again, so a batch of thousands costs little per key.
 */
export const TicketQueue = memo(function TicketQueue({ tickets, selected, filter, onFilter, onSelect, onHide }: {
  tickets: Ticket[];
  selected: number;
  filter: QueueFilter;
  onFilter: (filter: QueueFilter) => void;
  onSelect: (index: number) => void;
  onHide: () => void;
}) {
  const [search, setSearch] = useState("");
  // The box answers straight away. The list follows as soon as the browser has time.
  const needle = useDeferredValue(search).trim().toLowerCase();
  const rows = useMemo(() => {
    const found: { ticket: Ticket; index: number }[] = [];
    tickets.forEach((ticket, index) => {
      if (filter === "pending" && !pendingStatus(ticket)) return;
      if (filter === "flagged" && ticket.status !== "flagged") return;
      if (needle && ![ticket.ticket_number, ticket.material_type, ticket.supplier, ticket.job_location]
        .some((field) => (field ?? "").toLowerCase().includes(needle))) return;
      found.push({ ticket, index });
    });
    return found;
  }, [tickets, filter, needle]);

  return <section className="tf-panel tf-review-pane" aria-label="Ticket queue">
    <div className="border-b border-[#e7eaee] p-2.5">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-xs font-bold text-[#3c4b5b]">Ticket queue</h2>
        <button type="button" className="tf-link text-[11px]" onClick={onHide}>Hide</button>
      </div>
      <div className="tf-segmented mt-2 flex" role="group" aria-label="Filter ticket queue">
        {(["pending", "flagged", "all"] as const).map((name) => (
          <button type="button" key={name} className="flex-1" aria-pressed={filter === name} onClick={() => onFilter(name)}>
            {name === "pending" ? "To review" : name === "flagged" ? "Flagged" : "All"}
          </button>
        ))}
      </div>
      <label htmlFor="queue-search" className="sr-only">Search tickets in queue</label>
      <input id="queue-search" type="search" value={search} onChange={(e) => setSearch(e.target.value)}
        placeholder="Number, supplier, material…" className="tf-field mt-2 w-full !min-h-0 !py-1.5 !text-xs" />
      <p className="mt-1.5 text-[11px] text-[#7a8794]">{rows.length} matching ticket(s) · {tickets.length} total</p>
    </div>
    {rows.length === 0 ? (
      <div className="p-3 text-xs text-[#526273]">
        No tickets match the current view.
        {search && <button type="button" className="tf-link ml-2" onClick={() => setSearch("")}>Clear search</button>}
        <button type="button" className="tf-link ml-2" onClick={() => onFilter("all")}>Show all</button>
      </div>
    ) : (
      <div className="tf-review-pane-scroll max-h-72 divide-y divide-[#edf0f2] overflow-auto xl:max-h-none">
        {rows.map(({ ticket, index }) => <QueueRow key={ticket.id} ticket={ticket} index={index} isCurrent={selected === index} onSelect={onSelect} />)}
      </div>
    )}
  </section>;
});
