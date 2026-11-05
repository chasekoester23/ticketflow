"use client";

import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import { ApiError, HistoryEntry, Ticket, getTicketHistory, patchTicket, ticketAction } from "../../lib/api";
import { useAccount } from "../account-context";

export const STATUS_STYLE: Record<string, string> = {
  needs_review: "tf-status-attention",
  flagged: "tf-status-attention",
  verified: "tf-status-open",
  skipped: "",
  failed: "tf-status-error",
};
export const STATUS_LABEL: Record<string, string> = {
  needs_review: "Needs review",
  flagged: "Flagged",
  verified: "Verified",
  skipped: "Skipped",
  failed: "Failed",
};

type FieldKey = "ticket_number" | "delivery_date" | "material_type" | "tonnage" | "supplier" | "job_location";
const FIELDS: { key: FieldKey; label: string; type?: string; flagWords: string[]; critical?: boolean; repeats?: boolean }[] = [
  { key: "ticket_number", label: "Ticket number", flagWords: ["Ticket number"], critical: true },
  { key: "delivery_date", label: "Delivery date", type: "date", flagWords: ["Date"], critical: true, repeats: true },
  { key: "material_type", label: "Material", flagWords: ["Material"], repeats: true },
  { key: "tonnage", label: "Tonnage (tons)", type: "number", flagWords: ["Tonnage"], critical: true },
  { key: "supplier", label: "Supplier", flagWords: ["Supplier"], repeats: true },
  { key: "job_location", label: "Job location", flagWords: ["Job location"], repeats: true },
];
// Filled together by Alt+D. Date, ticket number and tons need a look at each scan.
const CARRIED: FieldKey[] = ["material_type", "supplier", "job_location"];
const CARRY_LABEL = "Material, supplier and job";

export type PreviousValues = Partial<Record<FieldKey, string>>;

/** The nearest earlier value of each field in this batch, for typing help only. */
export function previousValues(tickets: Ticket[], index: number): PreviousValues {
  const found: PreviousValues = {};
  for (let i = index - 1; i >= 0; i -= 1) {
    for (const field of FIELDS) {
      if (!field.repeats || found[field.key]) continue;
      const value = tickets[i][field.key];
      if (value) found[field.key] = String(value);
    }
  }
  return found;
}

export function ReviewForm({
  ticket, manual, catalog, previous, onChange, onAdvance, onAccepted, onDirtyChange, onError, onStale,
}: {
  ticket: Ticket;
  manual: boolean;
  catalog: { suppliers: string[]; materials: string[] };
  previous: PreviousValues;
  onChange: (t: Ticket) => void;
  onAdvance: () => void;
  onAccepted: (ticketId: number) => void;
  onDirtyChange: (dirty: boolean) => void;
  onError: (m: string | null) => void;
  onStale: (message: string) => void;
}) {
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(FIELDS.map((f) => [f.key, (ticket[f.key] as string | null) ?? ""])),
  );
  const [saving, setSaving] = useState(false);
  const acceptButton = useRef<HTMLButtonElement>(null);
  const form = useRef<HTMLElement>(null);
  const { can } = useAccount();
  const canEdit = can("tickets.edit");
  const canVerify = can("tickets.verify");
  const canFlag = can("tickets.flag");
  const canSkip = can("tickets.skip");
  const canOverride = can("tickets.price_override");
  const [override, setOverride] = useState(ticket.unit_price_override ?? "");
  const [note, setNote] = useState(ticket.flag_note ?? "");
  const overrideDirty = override !== (ticket.unit_price_override ?? "");

  const dirty = overrideDirty || FIELDS.some((f) => values[f.key] !== ((ticket[f.key] as string | null) ?? ""));
  // Before paint, so the unsaved-work guard is never a keystroke behind.
  useLayoutEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);

  // Put the cursor where work starts, unless the person is typing somewhere else.
  useEffect(() => {
    if (!canEdit) return;
    const active = document.activeElement;
    if (active && active !== document.body && !form.current?.contains(active) && active.closest("input, textarea, select, [role=dialog]")) return;
    const flagged = FIELDS.find((f) => ticket.flags.some((fl) => f.flagWords.some((w) => fl.startsWith(w))));
    const target = FIELDS.find((f) => !values[f.key]) ?? flagged;
    if (target) document.getElementById("review-" + target.key)?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once per ticket shown; the form remounts per ticket
  }, []);

  function fail(e: unknown): null {
    if (e instanceof ApiError && e.code === "ticket_changed") onStale(e.message);
    else onError((e as Error).message);
    return null;
  }

  async function save(): Promise<Ticket | null> {
    const body: Record<string, string | null> = {};
    FIELDS.forEach((f) => {
      const orig = (ticket[f.key] as string | null) ?? "";
      if (values[f.key] !== orig) body[f.key] = values[f.key].trim() === "" ? null : values[f.key];
    });
    if (overrideDirty) body.unit_price_override = override.trim() === "" ? null : override;
    if (!Object.keys(body).length) return ticket;
    try {
      const t = await patchTicket(ticket.id, body, ticket.revision);
      onChange(t);
      return t;
    } catch (e) {
      return fail(e);
    }
  }

  async function run(action: () => Promise<Ticket | null>, advance = false) {
    onError(null);
    setSaving(true);
    const t = await action();
    setSaving(false);
    if (t && advance) onAdvance();
  }

  const act = (name: "verify" | "flag" | "skip", noteText?: string) => async (): Promise<Ticket | null> => {
    const saved = await save();
    if (!saved) return null;
    try {
      const t = await ticketAction(ticket.id, name, saved.revision, noteText);
      onChange(t);
      if (name === "verify") onAccepted(t.id);
      return t;
    } catch (e) {
      return fail(e);
    }
  };

  const filled = FIELDS.filter((field) => values[field.key].trim() !== "").length;
  // A blank field on a manual ticket isn't news: the counter above already says
  // so. Keep every other warning, and all of them for AI-read tickets.
  const isMissingNotice = (flag: string) => /\bmissing\b/i.test(flag);
  const flags = manual ? ticket.flags.filter((flag) => !isMissingNotice(flag)) : ticket.flags;
  const flaggedFor = (words: string[]) => flags.some((fl) => words.some((w) => fl.startsWith(w)));
  const carry = CARRIED.filter((key) => !values[key] && previous[key]);
  const tonsProblem = values.tonnage.trim() !== "" && !/^\d*\.?\d{0,3}$/.test(values.tonnage.trim())
    ? "Enter tons as a number with up to 3 decimals, like 24.50. Don't convert pounds or yards here." : null;

  function fillRepeats() {
    if (!canEdit) return;
    setValues((current) => {
      const next = { ...current };
      CARRIED.forEach((key) => { if (!next[key] && previous[key]) next[key] = previous[key] as string; });
      return next;
    });
  }

  // Enter moves down the form, like a spreadsheet; the last field lands on Accept.
  function nextOnEnter(event: KeyboardEvent<HTMLInputElement>, index: number) {
    if (event.key !== "Enter" || event.metaKey || event.ctrlKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    const next = FIELDS[index + 1];
    if (next) document.getElementById("review-" + next.key)?.focus();
    else acceptButton.current?.focus();
  }

  useEffect(() => {
    function onShortcut(event: globalThis.KeyboardEvent) {
      if (event.target instanceof HTMLElement && event.target.closest("[role=dialog]")) return;
      // Cmd/Ctrl+Enter is a deliberate approval shortcut. It invokes the same
      // guarded button as a click, never bypassing server verification.
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey) && !event.repeat && !event.isComposing) {
        event.preventDefault();
        acceptButton.current?.click();
      }
      if (event.altKey && event.code === "KeyD" && !event.repeat) {
        event.preventDefault();
        document.getElementById("fill-repeats")?.click();
      }
    }
    window.addEventListener("keydown", onShortcut);
    return () => window.removeEventListener("keydown", onShortcut);
  }, []);

  return (
    <aside ref={form} className="tf-review-pane rounded-md border border-[#dce1e6] bg-white">
      <div className="tf-review-pane-scroll space-y-2.5 p-3.5">
        <div className="border-b border-[#e9edf0] pb-2">
          <div className="flex items-center justify-between gap-2">
            <h3 className="text-[14px] font-bold text-[#1d2b38]">{manual ? "Manual ticket entry" : "Extracted ticket information"}</h3>
            <span className={`tf-status ${STATUS_STYLE[ticket.status]}`}>{STATUS_LABEL[ticket.status]}</span>
          </div>
          <p className="mt-1 text-[11px] text-[#657182]">
            {manual ? <>No AI populated these values. Required fields entered: <strong className="tabular-nums text-[#293948]">{filled} / 6</strong></> : "Compare every value with the scan, then accept."}
          </p>
        </div>
        {ticket.error && <p className="text-sm text-red-700">{ticket.error}</p>}
        {ticket.flag_note && <p className="rounded border border-[#e5b993] bg-[#fff7ee] px-3 py-2 text-xs text-[#7c4518]" data-testid="flag-note">
          <strong>Set aside:</strong> {ticket.flag_note}
        </p>}
        {flags.length > 0 && <ul className="rounded border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900">
          {flags.map((f) => <li key={f}>• {f}</li>)}
        </ul>}
        {ticket.duplicates.length > 0 && <ul className="rounded border border-red-200 bg-red-50 p-3 text-xs text-red-900">
          <li className="font-bold">Possible duplicate</li>
          {ticket.duplicates.map((d) => <li key={d}>• {d}</li>)}
        </ul>}
        {canEdit && carry.length > 0 && <button id="fill-repeats" type="button" onClick={fillRepeats}
          className="flex w-full items-center justify-between gap-2 rounded border border-[#dce1e6] bg-[#f8f9fa] px-2.5 py-1.5 text-left text-[11px] font-semibold text-[#39495b] hover:bg-[#f1f3f5]">
          <span>{CARRY_LABEL}: same as previous ticket</span><kbd className="tf-kbd">Alt+D</kbd>
        </button>}
        {FIELDS.map((f, index) => <div key={f.key}>
          <label htmlFor={"review-" + f.key} className={"mb-0.5 block text-[11px] text-[#526273] " + (f.critical ? "font-bold" : "font-medium")}>
            {f.label}{f.critical && <span className="ml-1 font-normal text-[#7a8796]">· check against the scan</span>}
          </label>
          <input
            id={"review-" + f.key}
            list={f.key === "supplier" ? "catalog-suppliers" : f.key === "material_type" ? "catalog-materials" : undefined}
            aria-describedby={ticket.as_read?.[f.key] !== undefined && ticket.as_read[f.key] !== values[f.key] ? "as-read-" + f.key : undefined}
            aria-invalid={f.key === "tonnage" && tonsProblem ? true : undefined}
            type={f.type === "number" ? "text" : f.type ?? "text"}
            inputMode={f.type === "number" ? "decimal" : undefined}
            value={values[f.key]}
            readOnly={!canEdit}
            onKeyDown={(event) => nextOnEnter(event, index)}
            onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}
            className={`w-full rounded border px-2.5 py-1.5 text-sm text-[#19232e] outline-none focus:ring-2 focus:ring-[#e6a97b] read-only:bg-[#f6f7f8] ${
              flaggedFor(f.flagWords) ? "border-amber-400 bg-amber-50" : "border-[#cfd6de]"
            }`}
          />
          {f.key === "tonnage" && tonsProblem && <p className="mt-1 text-[11px] font-semibold text-red-700">{tonsProblem}</p>}
          {ticket.as_read?.[f.key] !== undefined && ticket.as_read[f.key] !== values[f.key] && (
            <p id={"as-read-" + f.key} className="mt-1 text-[11px] text-[#657182]">Read from the ticket: {ticket.as_read[f.key]}</p>
          )}
          {canEdit && ticket.suggestions?.[f.key] && ticket.suggestions[f.key] !== values[f.key] && (
            <button type="button" className="tf-link mt-1 text-[11px]"
              onClick={() => setValues({ ...values, [f.key]: ticket.suggestions?.[f.key] ?? values[f.key] })}>
              Use your catalog name: {ticket.suggestions[f.key]}
            </button>
          )}
          {canEdit && f.key === "delivery_date" && !values[f.key] && previous[f.key] && (
            <button type="button" className="tf-link mt-1 text-[11px]"
              onClick={() => setValues({ ...values, [f.key]: previous[f.key] as string })}>
              Same date as previous ticket: {previous[f.key]}
            </button>
          )}
        </div>)}
        <datalist id="catalog-suppliers">{catalog.suppliers.map((name) => <option key={name} value={name} />)}</datalist>
        <datalist id="catalog-materials">{catalog.materials.map((name) => <option key={name} value={name} />)}</datalist>
        <div className="rounded border border-[#e2e7eb] bg-[#f7f8f9] p-3 text-xs">
          {ticket.status === "verified" && ticket.price_snapshot_state === "priced" && (
            <p className="mb-2 text-emerald-800">Unit price locked at verification. Later changes to supplier rates will not change this ticket.</p>
          )}
          {ticket.status === "verified" && ticket.price_snapshot_state === null && (
            <p className="mb-2 font-medium text-amber-800">Legacy record: the historical rate is unknown. Enter the documented original rate as an override, then save and verify again.</p>
          )}
          {ticket.status === "verified" && ticket.price_snapshot_state === "unpriced" && (
            <p className="mb-2 font-medium text-amber-800">Verified without a price. Enter the correct rate as an override, then save and verify again.</p>
          )}
          {ticket.unit_price !== null ? (
            <p>
              Rate: ${ticket.unit_price}/ton ({ticket.price_source === "override" ? "manual override" : "supplier rate"})
              {ticket.cost ? <> · Cost: <strong>${ticket.cost}</strong></> : " · cost appears after you verify the tonnage"}
            </p>
          ) : (
            <p className="text-amber-800">{ticket.pricing_notes.join(". ")}. Add it under Suppliers &amp; Pricing, or enter an override below.</p>
          )}
          <label htmlFor="review-rate-override" className="mt-2 block font-medium text-[#526273]">Override rate for this ticket ($/ton, optional)</label>
          <input
            id="review-rate-override"
            inputMode="decimal"
            value={override}
            readOnly={!canOverride}
            aria-describedby={canOverride ? undefined : "review-rate-override-note"}
            onChange={(e) => setOverride(e.target.value)}
            className="mt-1 w-full rounded border border-[#cfd6de] px-3 py-2 text-sm read-only:bg-[#f6f7f8]"
          />
          {!canOverride && <p id="review-rate-override-note" className="mt-1 text-[#657182]">Only a manager or owner can change a ticket’s rate.</p>}
        </div>
        {ticket.status === "verified" && (
          <p className="rounded border border-[#cce4d2] bg-[#f3f9f4] px-3 py-2 text-xs text-[#216c43]" data-testid="verified-by">
            {ticket.verified_by
              ? <>Approved by <strong>{ticket.verified_by}</strong>{ticket.verified_at ? <> on {new Date(ticket.verified_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}</> : null}</>
              : <>Approved before individual accounts were introduced; the approver was not recorded.</>}
          </p>
        )}
        {canVerify ? (
          <p className="rounded border border-[#e2e7eb] bg-[#f7f8f9] px-3 py-2 text-xs leading-relaxed text-[#526273]">
            <strong className="text-[#263442]">Your acceptance is required.</strong> Confirm that the ticket number, date, material, tons, supplier and job match the original. Only accepted tickets count toward verified totals. Your name is recorded with the approval.
          </p>
        ) : canEdit ? (
          <p className="rounded border border-[#e2e7eb] bg-[#f7f8f9] px-3 py-2 text-xs leading-relaxed text-[#526273]">
            <strong className="text-[#263442]">Enter and check each field, then save.</strong> A manager or owner approves quantities before they count toward project totals.
          </p>
        ) : (
          <p className="tf-notice">View-only access. Ask an owner if you need to enter or approve tickets.</p>
        )}
        {canFlag && <label className="tf-field-label">Note if you set this aside (optional)
          <input className="tf-field w-full font-normal" maxLength={300} value={note} onChange={(e) => setNote(e.target.value)}
            placeholder="e.g. tons smudged, ask the driver" />
        </label>}
        <TicketHistory ticketId={ticket.id} version={JSON.stringify([ticket.status, ticket.verified_at, ticket.unit_price_override, ...FIELDS.map((f) => ticket[f.key])])} />
      </div>
      {dirty && <p className="border-t border-[#e9edf0] bg-[#fff8ec] px-4 py-1.5 text-xs font-semibold text-amber-800" role="status">Unsaved changes — save or accept before leaving this ticket.</p>}
      {canEdit && <div className="flex flex-wrap gap-1.5 border-t border-[#dce1e6] bg-white p-2.5">
        {canVerify ? <button
          ref={acceptButton}
          title="Review and accept ticket (Cmd/Ctrl + Enter)"
          onClick={() => run(act("verify"), true)}
          className="w-full rounded bg-[#dc6c30] px-4 py-2.5 text-sm font-bold text-white hover:bg-[#c45922] disabled:opacity-40"
          disabled={saving || Boolean(tonsProblem) || (ticket.status === "verified" && !dirty)}
        >
          {ticket.status === "verified" && !dirty ? "Accepted" : "Accept & next"}
        </button> : <button
          ref={acceptButton}
          title="Save and go to the next ticket (Cmd/Ctrl + Enter)"
          onClick={() => run(save, true)}
          className="w-full rounded bg-[#dc6c30] px-4 py-2.5 text-sm font-bold text-white hover:bg-[#c45922] disabled:opacity-40"
          disabled={saving || Boolean(tonsProblem)}
        >
          Save &amp; next
        </button>}
        <button disabled={saving || !dirty || Boolean(tonsProblem)} onClick={() => run(save)} className="rounded border border-[#cfd6dc] px-2.5 py-1.5 text-xs font-semibold text-[#39495b] disabled:opacity-40">
          Save changes
        </button>
        {canFlag && <button disabled={saving} onClick={() => run(act("flag", note), true)}
          className="rounded border border-[#e5b993] bg-[#fff7ee] px-2.5 py-1.5 text-xs font-semibold text-[#97511e] hover:bg-[#fff0dc] disabled:opacity-40">
          Flag for later
        </button>}
        {canSkip && <button disabled={saving} onClick={() => {
          if (!window.confirm("Skip this ticket? Skipped tickets are excluded from exports and financial totals.")) return;
          run(act("skip"), true);
        }} className="rounded px-2 py-1.5 text-xs font-medium text-[#657182] hover:text-[#19232e] disabled:opacity-40">
          Skip non-ticket
        </button>}
      </div>}
    </aside>
  );
}

const HISTORY_FIELD: Record<string, string> = {
  status: "Status", verified_unit_price: "Locked unit price", ticket_number: "Ticket number",
  delivery_date: "Delivery date", material_type: "Material", tonnage: "Tons", supplier: "Supplier",
  job_location: "Job location", unit_price_override: "Rate override", flag_note: "Note",
};

/** Who changed what on this ticket. Loaded only when opened. */
function TicketHistory({ ticketId, version }: { ticketId: number; version: string }) {
  const [open, setOpen] = useState(false);
  const [entries, setEntries] = useState<HistoryEntry[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setFailed(false);
    getTicketHistory(ticketId).then((rows) => { if (!cancelled) setEntries(rows); })
      .catch(() => { if (!cancelled) setFailed(true); });
    return () => { cancelled = true; };
  }, [open, ticketId, version]);
  useEffect(() => { setEntries(null); }, [ticketId]);

  const show = (value: string | null) => value === null || value === "" ? "—" : (STATUS_LABEL[value] ?? value);
  return (
    <details className="rounded border border-[#e2e7eb] text-xs" onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary className="cursor-pointer select-none px-3 py-2 font-semibold text-[#46566a]">Change history</summary>
      <div className="border-t border-[#e9edf0] px-3 py-2">
        {failed ? <p className="text-red-700">Could not load history.</p>
          : entries === null ? <p className="text-[#657182]">Loading…</p>
          : entries.length === 0 ? <p className="text-[#657182]">No changes recorded yet.</p>
          : <ol className="space-y-1.5">
            {[...entries].reverse().map((e, i) => <li key={i} className="leading-5 text-[#3d4c5d]">
              <span className="font-semibold">{HISTORY_FIELD[e.field] ?? e.field}</span>: {show(e.old)} → {show(e.new)}
              <span className="block text-[11px] text-[#7a8796]">
                {e.actor ?? "Before individual accounts"} · {new Date(e.at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}
              </span>
            </li>)}
          </ol>}
      </div>
    </details>
  );
}
