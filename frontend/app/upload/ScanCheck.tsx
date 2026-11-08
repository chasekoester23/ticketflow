"use client";

import { memo, useEffect, useState } from "react";
import {
  Manifest, ManifestSlot, buildManifest, confirmSlotBlank, markSlotTicket, pageImageUrl,
  rotateSlot, setExpectedCount, slotImageUrl,
} from "../../lib/api";
import { useAccount } from "../account-context";

const STATUS: Record<Manifest["status"], { label: string; style: string }> = {
  processing: { label: "Still processing", style: "tf-status-attention" },
  needs_attention: { label: "Needs attention", style: "tf-status-error" },
  count_not_confirmed: { label: "Paper count not entered", style: "tf-status-attention" },
  count_matches: { label: "Matches your paper count", style: "tf-status-open" },
};
const SLOT_LABEL: Record<ManifestSlot["state"], string> = {
  ticket: "Ticket on record",
  suspected_blank: "Faint — is this a ticket?",
  empty: "Looks empty",
  confirmed_blank: "Confirmed blank",
};

/** Whole page plus every physical slot, so people can check scans against paper. */
export const ScanCheck = memo(function ScanCheck({ manifest, onChanged, onOpenTicket, onError }: {
  manifest: Manifest;
  onChanged: (next: Manifest) => void;
  onOpenTicket: (ticketId: number) => void;
  onError: (message: string) => void;
}) {
  const { can } = useAccount();
  const [busy, setBusy] = useState(false);
  const [count, setCount] = useState(manifest.expected_ticket_count?.toString() ?? "");
  const canResolve = can("scans.resolve");
  const canConfirm = can("scans.confirm_blank");
  const canCount = can("tickets.upload");
  const { counts } = manifest;
  // Opens by itself when a slot needs a decision, and stays open after one is
  // made, so people don't lose their place.
  const [open, setOpen] = useState(counts.suspected_blank > 0);
  useEffect(() => {
    if (counts.suspected_blank > 0) setOpen(true);
  }, [counts.suspected_blank]);
  const status = STATUS[manifest.status];

  async function run(action: () => Promise<Manifest>) {
    if (busy) return;
    setBusy(true);
    try {
      onChanged(await action());
    } catch (exc) {
      onError((exc as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function saveCount(event: React.FormEvent) {
    event.preventDefault();
    const value = count.trim();
    void run(() => setExpectedCount(manifest.batch_id, value === "" ? null : Number(value)));
  }

  // One compact row while nothing needs a decision, so the scan and the Accept
  // button stay on screen. Anything needing a person expands below it.
  return <section className="mt-2.5 rounded border border-[#e2e7ec]" aria-label="Scan check">
    <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 px-3 py-2">
      <div className="min-w-0">
        <h3 className="text-[12px] font-bold text-[#344352]">Scan check</h3>
        <p className="text-[11px] text-[#526273]">
          {counts.pages} page{counts.pages === 1 ? "" : "s"} · {counts.slots} slots · {counts.records} tickets on record
          · {counts.suspected_blank} faint · {counts.empty} empty · {counts.confirmed_blank} confirmed blank
          {counts.skipped > 0 && <> · {counts.skipped} skipped</>}
          {counts.failed_files > 0 && <> · {counts.failed_files} failed file{counts.failed_files === 1 ? "" : "s"}</>}
        </p>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {canCount ? <form className="flex items-center gap-1.5" onSubmit={saveCount}>
          <label className="flex items-center gap-1.5 text-[11px] font-bold text-[#49586a]">Paper tickets you scanned
            <input className="tf-field !min-h-0 w-16 !py-1 !text-xs" inputMode="numeric" pattern="[0-9]*" value={count}
              onChange={(e) => setCount(e.target.value)}/>
          </label>
          <button type="submit" className="tf-secondary-button !min-h-0 !py-1" disabled={busy}>Save count</button>
        </form> : <p className="text-[11px] text-[#526273]">Paper tickets entered: {manifest.expected_ticket_count ?? "not entered"}</p>}
        {counts.legacy_files > 0 && canCount && <button type="button" className="tf-secondary-button !min-h-0 !py-1" disabled={busy}
          onClick={() => void run(() => buildManifest(manifest.batch_id))}>Build scan check</button>}
        <span className={"tf-status " + status.style}>{status.label}</span>
      </div>
    </div>

    {manifest.blockers.length > 0 && <ul className="list-disc space-y-0.5 border-t border-[#f0d8ad] bg-[#fff9ed] py-2 pl-7 pr-4 text-[11px] text-[#754e20]">
      {manifest.blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}
    </ul>}

    <details open={open} onToggle={(e) => setOpen(e.currentTarget.open)} className="border-t border-[#e2e7ec]">
      <summary className="cursor-pointer select-none px-3 py-1 text-[11px] font-semibold text-[#46566a]">Pages and slots</summary>
      {/* Only built once opened. A large batch has thousands of slots nobody is looking at. */}
      {open && manifest.files.flatMap((file) => file.pages.map((page) => <div key={file.id + "-" + page.page}
        className="grid gap-4 border-t border-[#eef0f3] p-4 lg:grid-cols-[220px_minmax(0,1fr)]">
        <figure>
          <img src={pageImageUrl(file.id, page.page)} alt={"Original page " + page.page + " of " + file.original_name}
            loading="lazy" className="w-full rounded border border-[#dce1e6] bg-white"/>
          <figcaption className="mt-1 break-words text-[11px] text-[#5f6c7b]">{file.original_name} · page {page.page}</figcaption>
        </figure>
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {page.slots.map((slot) => <article key={slot.id} aria-label={"Page " + page.page + " slot " + (slot.position + 1)}
            className={"rounded border p-2 text-[11px] " + (slot.state === "suspected_blank" ? "border-[#e0a458] bg-[#fff9ed]" : "border-[#e2e7ec]")}>
            <img src={slotImageUrl(slot.id) + "?r=" + slot.rotation} alt={"Scan slot " + (slot.position + 1)} loading="lazy"
              className="mb-2 h-32 w-full rounded border border-[#eef0f3] bg-white object-contain"/>
            <p className="font-bold text-[#344352]">Slot {slot.position + 1}: {SLOT_LABEL[slot.state]}</p>
            {slot.state === "ticket" && <p className="text-[#5d6b7a]">{slot.ticket_number ?? "Number not entered"} · {(slot.ticket_status ?? "").replaceAll("_", " ")}</p>}
            <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1">
              {slot.ticket_id !== null && <button type="button" className="tf-link" onClick={() => onOpenTicket(slot.ticket_id as number)}>Open ticket</button>}
              {slot.ticket_id === null && canResolve && <button type="button" className="tf-link" disabled={busy}
                onClick={() => void run(() => markSlotTicket(slot.id))}>This is a ticket</button>}
              {slot.ticket_id === null && slot.state !== "confirmed_blank" && canConfirm && <button type="button" className="tf-link" disabled={busy}
                onClick={() => void run(() => confirmSlotBlank(slot.id))}>Confirm blank</button>}
              {canResolve && slot.ticket_status !== "verified" && <button type="button" className="tf-link" disabled={busy}
                onClick={() => void run(() => rotateSlot(slot.id, (slot.rotation + 90) % 360))}>Rotate 90°</button>}
            </div>
          </article>)}
        </div>
      </div>))}
      {open && counts.slots === 0 && <p className="px-4 pb-3 text-[12px] text-[#5f6c7b]">No slots on record for this batch yet.</p>}
    </details>
  </section>;
});
