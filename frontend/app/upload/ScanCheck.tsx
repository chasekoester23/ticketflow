"use client";

import { useState } from "react";
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
export function ScanCheck({ manifest, onChanged, onOpenTicket, onError }: {
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

  return <section className="mt-4 rounded border border-[#e2e7ec]" aria-label="Scan check">
    <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[#e2e7ec] px-4 py-3">
      <div>
        <h3 className="text-[13px] font-bold text-[#344352]">Scan check</h3>
        <p className="mt-0.5 text-[12px] text-[#526273]">
          {counts.pages} page{counts.pages === 1 ? "" : "s"} · {counts.slots} slots · {counts.records} tickets on record
          · {counts.suspected_blank} faint · {counts.empty} empty · {counts.confirmed_blank} confirmed blank
          {counts.skipped > 0 && <> · {counts.skipped} skipped</>}
          {counts.failed_files > 0 && <> · {counts.failed_files} failed file{counts.failed_files === 1 ? "" : "s"}</>}
        </p>
      </div>
      <span className={"tf-status " + status.style}>{status.label}</span>
    </div>

    {manifest.blockers.length > 0 && <ul className="list-disc space-y-1 border-b border-[#f0d8ad] bg-[#fff9ed] py-3 pl-8 pr-4 text-[12px] text-[#754e20]">
      {manifest.blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}
    </ul>}

    <div className="flex flex-wrap items-end gap-3 px-4 py-3">
      {canCount ? <form className="flex items-end gap-2" onSubmit={saveCount}>
        <label className="tf-field-label">Paper tickets you scanned
          <input className="tf-field w-28" inputMode="numeric" pattern="[0-9]*" value={count}
            onChange={(e) => setCount(e.target.value)}/>
        </label>
        <button type="submit" className="tf-secondary-button" disabled={busy}>Save count</button>
      </form> : <p className="text-[12px] text-[#526273]">Paper tickets entered: {manifest.expected_ticket_count ?? "not entered"}</p>}
      {counts.legacy_files > 0 && canCount && <button type="button" className="tf-secondary-button" disabled={busy}
        onClick={() => void run(() => buildManifest(manifest.batch_id))}>Build scan check</button>}
    </div>

    <details open={counts.suspected_blank > 0 || undefined} className="border-t border-[#e2e7ec]">
      <summary className="cursor-pointer select-none px-4 py-2 text-[12px] font-semibold text-[#46566a]">Pages and slots</summary>
      {manifest.files.flatMap((file) => file.pages.map((page) => <div key={file.id + "-" + page.page}
        className="grid gap-4 border-t border-[#eef0f3] p-4 lg:grid-cols-[220px_minmax(0,1fr)]">
        <figure>
          <img src={pageImageUrl(file.id, page.page)} alt={"Original page " + page.page + " of " + file.original_name}
            loading="lazy" className="w-full rounded border border-[#dce1e6] bg-white"/>
          <figcaption className="mt-1 break-words text-[11px] text-[#718091]">{file.original_name} · page {page.page}</figcaption>
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
      {counts.slots === 0 && <p className="px-4 pb-3 text-[12px] text-[#718091]">No slots on record for this batch yet.</p>}
    </details>
  </section>;
}
