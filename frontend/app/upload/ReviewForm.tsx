"use client";

import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { ApiError, Failure, HistoryEntry, Ticket, describeFailure, getTicketHistory, patchTicket, ticketAction } from "../../lib/api";
import { TicketDraft, draftsFor, removeDraft, removeTicketDrafts, saveDraft } from "../../lib/drafts";
import { useAccount } from "../account-context";
import { FieldMessage, NEEDED_LABEL, REQUIRED_TEXT, dateMessage, rateMessage, tonsMessage } from "./validation";

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

const OVERRIDE_KEY = "unit_price_override";
const NOTE_KEY = "flag_note";
const DRAFT_LABEL: Record<string, string> = {
  ...Object.fromEntries(FIELDS.map((f) => [f.key, f.label])),
  [OVERRIDE_KEY]: "Override rate",
  [NOTE_KEY]: "Note",
};

export type PreviousValues = Partial<Record<FieldKey, string>>;

/** The saved values of a ticket as the form holds them, with blanks as empty text. */
function savedValues(ticket: Ticket): Record<string, string> {
  return {
    ...Object.fromEntries(FIELDS.map((f) => [f.key, (ticket[f.key] as string | null) ?? ""])),
    [OVERRIDE_KEY]: ticket.unit_price_override ?? "",
    [NOTE_KEY]: ticket.flag_note ?? "",
  };
}

/**
 * Anything still unsaved from before this form opened. A draft at this revision
 * was typed over these exact values, so it comes back. A draft at another revision
 * was typed over values that have since changed: a person decides what to keep.
 */
function recoverDrafts(ticket: Ticket, accountId: number, allowed: (key: string) => boolean) {
  const saved = savedValues(ticket);
  const revision = ticket.revision ?? "";
  const usable = (draft: TicketDraft) => {
    const typed = Object.fromEntries(Object.entries(draft.typed).filter(([key, text]) => allowed(key) && text !== saved[key]));
    if (!Object.keys(typed).length) removeDraft(draft.ticketId, draft.revision);
    return { ...draft, typed };
  };
  const drafts = draftsFor(ticket.id, accountId).map(usable).filter((draft) => Object.keys(draft.typed).length > 0);
  return {
    same: drafts.find((draft) => draft.revision === revision) ?? null,
    older: drafts.find((draft) => draft.revision !== revision) ?? null,
  };
}

type ActionName = "save" | "verify" | "flag" | "skip";
const ACTION_NOUN: Record<ActionName, string> = { save: "save", verify: "approval", flag: "flag", skip: "skip" };
/**
 * A save or approval that failed. It lives with the page, because a form remounts
 * when its ticket's revision changes, and a save can succeed before an approval fails.
 */
export type ActionFailure = Failure & { ticketId: number; action: ActionName; note?: string; advance: boolean };

const clockTime = (iso: string) => new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });

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

export const ReviewForm = memo(function ReviewForm({
  ticket, manual, catalog, previous, failure, onChange, onAdvance, onAccepted, onDirtyChange, onBusyChange, onError, onFailure, onStale,
}: {
  ticket: Ticket;
  manual: boolean;
  catalog: { suppliers: string[]; materials: string[] };
  previous: PreviousValues;
  failure: ActionFailure | null;
  onChange: (t: Ticket) => void;
  onAdvance: () => void;
  onAccepted: (ticketId: number) => void;
  onDirtyChange: (dirty: boolean) => void;
  onBusyChange: (busy: boolean) => void;
  onError: (m: string | null) => void;
  onFailure: (failure: ActionFailure | null) => void;
  onStale: (message: string) => void;
}) {
  const { account, can } = useAccount();
  const accountId = account?.id ?? 0;
  const canEdit = can("tickets.edit");
  const canVerify = can("tickets.verify");
  const canFlag = can("tickets.flag");
  const canSkip = can("tickets.skip");
  const canOverride = can("tickets.price_override");
  const saved = savedValues(ticket);
  const revision = ticket.revision ?? "";

  const [recovered] = useState(() => recoverDrafts(ticket, accountId, (key) =>
    canEdit && (key !== OVERRIDE_KEY || canOverride) && (key !== NOTE_KEY || canFlag)));
  const [restoredAt, setRestoredAt] = useState(recovered.same?.savedAt ?? null);
  const [olderDraft, setOlderDraft] = useState(recovered.older);
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(FIELDS.map((f) => [f.key, recovered.same?.typed[f.key] ?? saved[f.key]])),
  );
  const [override, setOverride] = useState(recovered.same?.typed[OVERRIDE_KEY] ?? saved[OVERRIDE_KEY]);
  const [note, setNote] = useState(recovered.same?.typed[NOTE_KEY] ?? failure?.note ?? saved[NOTE_KEY]);
  const [saving, setSaving] = useState(false);
  const [leftBlank, setLeftBlank] = useState<Record<string, boolean>>({});
  const [triedAccept, setTriedAccept] = useState(false);
  // Read and set in the same moment as a click. Disabling the button waits for a
  // render, which is too late for a double click to be stopped by it alone.
  const inFlight = useRef(false);
  const acceptButton = useRef<HTMLButtonElement>(null);
  const form = useRef<HTMLElement>(null);
  const overrideDirty = override !== saved[OVERRIDE_KEY];

  const dirty = overrideDirty || FIELDS.some((f) => values[f.key] !== saved[f.key]);
  // Before paint, so the unsaved-work guard is never a keystroke behind.
  useLayoutEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);

  // Keep what's been typed but not saved, on every change, for a crash or a closed tab.
  useEffect(() => {
    if (!canEdit) return;
    const typed: Record<string, string> = {};
    FIELDS.forEach((f) => { if (values[f.key] !== saved[f.key]) typed[f.key] = values[f.key]; });
    if (overrideDirty) typed[OVERRIDE_KEY] = override;
    if (note !== saved[NOTE_KEY]) typed[NOTE_KEY] = note;
    if (Object.keys(typed).length) saveDraft(ticket.id, revision, accountId, typed);
    else removeDraft(ticket.id, revision);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- saved values only change by remounting this form
  }, [values, override, note]);

  function discardRestored() {
    setValues(Object.fromEntries(FIELDS.map((f) => [f.key, saved[f.key]])));
    setOverride(saved[OVERRIDE_KEY]);
    setNote(saved[NOTE_KEY]);
    setRestoredAt(null);
  }

  function applyTyped(keys: string[]) {
    if (!olderDraft) return;
    const pick = (key: string) => olderDraft.typed[key];
    setValues((current) => ({ ...current, ...Object.fromEntries(keys.filter((key) => key in current).map((key) => [key, pick(key)])) }));
    if (keys.includes(OVERRIDE_KEY)) setOverride(pick(OVERRIDE_KEY));
    if (keys.includes(NOTE_KEY)) setNote(pick(NOTE_KEY));
    const left = Object.fromEntries(Object.entries(olderDraft.typed).filter(([key]) => !keys.includes(key)));
    if (Object.keys(left).length) {
      setOlderDraft({ ...olderDraft, typed: left });
      saveDraft(olderDraft.ticketId, olderDraft.revision, accountId, left);
    } else {
      dropOlderDraft();
    }
  }
  function dropOlderDraft() {
    if (olderDraft) removeDraft(olderDraft.ticketId, olderDraft.revision);
    setOlderDraft(null);
  }

  // Put the cursor where work starts, unless the person is typing somewhere else.
  // Before paint: once a ticket is on screen the cursor is settled, so the next
  // key press can't land in a field the person didn't choose.
  useLayoutEffect(() => {
    if (!canEdit) return;
    const active = document.activeElement;
    if (active && active !== document.body && !form.current?.contains(active) && active.closest("input, textarea, select, [role=dialog]")) return;
    const flagged = FIELDS.find((f) => ticket.flags.some((fl) => f.flagWords.some((w) => fl.startsWith(w))));
    const target = FIELDS.find((f) => !values[f.key]) ?? flagged;
    if (target) document.getElementById("review-" + target.key)?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once per ticket shown; the form remounts per ticket
  }, []);

  function fail(e: unknown, action: ActionName, advance: boolean, savedFirst = false): null {
    if (e instanceof ApiError && (e.code === "ticket_changed" || e.code === "record_changed")) {
      // A form on the latest values opens next; what was typed stays in its draft for a person to compare.
      onStale(e.message);
    } else {
      onFailure({ ...describeFailure(e, ACTION_NOUN[action], savedFirst), ticketId: ticket.id, action, advance, note: action === "flag" ? note : undefined });
    }
    return null;
  }

  /** Send what's been typed, if anything. The form stays open until the whole action is done. */
  async function write(action: ActionName, advance: boolean): Promise<Ticket | null> {
    const body: Record<string, string | null> = {};
    FIELDS.forEach((f) => {
      if (values[f.key] !== saved[f.key]) body[f.key] = values[f.key].trim() === "" ? null : values[f.key];
    });
    if (overrideDirty) body.unit_price_override = override.trim() === "" ? null : override;
    if (!Object.keys(body).length) return ticket;
    try {
      const t = await patchTicket(ticket.id, body, ticket.revision);
      removeTicketDrafts(ticket.id);
      return t;
    } catch (e) {
      return fail(e, action, advance);
    }
  }

  async function perform(action: ActionName, noteText: string | undefined, advance: boolean): Promise<Ticket | null> {
    const written = await write(action, advance);
    if (!written) return null;
    const wroteChanges = written !== ticket;
    if (action === "save") {
      if (wroteChanges) onChange(written);
      return written;
    }
    try {
      let t: Ticket;
      try {
        t = await ticketAction(ticket.id, action, written.revision, noteText === undefined ? undefined : { note: noteText });
      } catch (e) {
        // Possibly the same delivery twice: a person decides, and it's recorded.
        if (!(e instanceof ApiError) || e.code !== "possible_duplicate") throw e;
        if (!window.confirm(`${e.message}\n\nApprove this ticket anyway? Both will count toward totals, and your decision is recorded.`)) {
          if (wroteChanges) onChange(written);
          return null;
        }
        t = await ticketAction(ticket.id, action, written.revision, { confirm_duplicate: true });
      }
      removeTicketDrafts(ticket.id);
      onChange(t);
      if (action === "verify") onAccepted(t.id);
      return t;
    } catch (e) {
      fail(e, action, advance, wroteChanges);
      if (wroteChanges) onChange(written);
      return null;
    }
  }

  async function attempt(action: ActionName, noteText?: string, advance = false) {
    if (inFlight.current) return;
    inFlight.current = true;
    onError(null);
    onFailure(null);
    onBusyChange(true);
    setSaving(true);
    let done: Ticket | null = null;
    try {
      done = await perform(action, noteText, advance);
    } finally {
      inFlight.current = false;
      onBusyChange(false);
      setSaving(false);
    }
    if (done && advance) onAdvance();
  }

  const filled = FIELDS.filter((field) => values[field.key].trim() !== "").length;
  // A blank field on a manual ticket isn't news: the counter above already says
  // so. Keep every other warning, and all of them for AI-read tickets.
  const isMissingNotice = (flag: string) => /\bmissing\b/i.test(flag);
  const flags = manual ? ticket.flags.filter((flag) => !isMissingNotice(flag)) : ticket.flags;
  const flaggedFor = (words: string[]) => flags.some((fl) => words.some((w) => fl.startsWith(w)));
  const carry = CARRIED.filter((key) => !values[key] && previous[key]);
  const tons = tonsMessage(values.tonnage);
  const rate = rateMessage(override);
  const cannotSave = Boolean(tons?.blocksSaving || rate?.blocksSaving);
  const message = (key: FieldKey): FieldMessage | null => {
    if (!canEdit) return null;
    const about = key === "tonnage" ? tons : key === "delivery_date" ? dateMessage(values[key]) : null;
    if (about) return about;
    return !values[key].trim() && (leftBlank[key] || triedAccept) ? { tone: "problem", text: REQUIRED_TEXT[key] } : null;
  };
  // What the server would refuse to approve. Saying so here saves the round trip,
  // and the server still checks everything itself.
  const stopsApproval = (key: FieldKey) => !values[key].trim() || (key === "tonnage" && tons?.tone === "problem");
  const needed = FIELDS.filter((f) => !values[f.key].trim()).map((f) => NEEDED_LABEL[f.key]);

  // A message appearing pushes the form down. If that happened mid-click, the
  // click would land on something else, so it waits for the pointer to come up.
  const pointerDown = useRef(false);
  useEffect(() => {
    const press = () => { pointerDown.current = true; };
    const release = () => { pointerDown.current = false; };
    window.addEventListener("pointerdown", press, true);
    window.addEventListener("pointerup", release, true);
    window.addEventListener("pointercancel", release, true);
    return () => {
      window.removeEventListener("pointerdown", press, true);
      window.removeEventListener("pointerup", release, true);
      window.removeEventListener("pointercancel", release, true);
    };
  }, []);
  function leftField(key: string) {
    const mark = () => setLeftBlank((old) => old[key] ? old : { ...old, [key]: true });
    if (pointerDown.current) window.addEventListener("pointerup", () => setTimeout(mark, 0), { once: true });
    else mark();
  }

  function accept() {
    const first = FIELDS.find((f) => stopsApproval(f.key));
    if (first) {
      setTriedAccept(true);
      document.getElementById("review-" + first.key)?.focus();
      return;
    }
    void attempt("verify", undefined, true);
  }

  // A long catalog needn't be rebuilt for every key press.
  const catalogLists = useMemo(() => <>
    <datalist id="catalog-suppliers">{catalog.suppliers.map((name) => <option key={name} value={name} />)}</datalist>
    <datalist id="catalog-materials">{catalog.materials.map((name) => <option key={name} value={name} />)}</datalist>
  </>, [catalog]);

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
        {restoredAt && <div role="status" data-testid="draft-restored" className="rounded border border-[#e5b993] bg-[#fff7ee] px-3 py-2 text-xs leading-relaxed text-[#7c4518]">
          <strong>Restored unsaved work</strong> you typed on {clockTime(restoredAt)}. It isn&apos;t saved yet: check it against the scan, then save or accept.
          <button type="button" className="tf-link ml-2" onClick={discardRestored}>Discard it</button>
        </div>}
        {olderDraft && <section aria-label="Values you typed on an earlier version" data-testid="draft-conflict" className="rounded border border-amber-300 bg-amber-50 p-3 text-xs text-amber-950">
          <p className="font-bold">This ticket changed after you typed these values.</p>
          <p className="mt-0.5 leading-relaxed">You typed them on {clockTime(olderDraft.savedAt)}. They haven&apos;t been applied. Compare them with the current values and the scan.</p>
          <table className="mt-2 w-full text-left text-[11px]">
            <thead><tr><th className="pb-1 pr-2 font-semibold">Field</th><th className="pb-1 pr-2 font-semibold">You typed</th><th className="pb-1 pr-2 font-semibold">Current</th><th><span className="sr-only">Choose</span></th></tr></thead>
            <tbody>{Object.entries(olderDraft.typed).map(([key, text]) => <tr key={key} className="border-t border-amber-200 align-top">
              <td className="py-1 pr-2">{DRAFT_LABEL[key] ?? key}</td>
              <td className="py-1 pr-2 font-semibold [overflow-wrap:anywhere]">{text || "blank"}</td>
              <td className="py-1 pr-2 [overflow-wrap:anywhere]">{saved[key] || "blank"}</td>
              <td className="py-1 text-right"><button type="button" className="tf-link whitespace-nowrap text-[11px]" onClick={() => applyTyped([key])}>Use mine<span className="sr-only"> for {DRAFT_LABEL[key] ?? key}</span></button></td>
            </tr>)}</tbody>
          </table>
          <div className="mt-2 flex flex-wrap gap-2">
            <button type="button" className="tf-secondary-button !min-h-0 !py-1" onClick={() => applyTyped(Object.keys(olderDraft.typed))}>Use all I typed</button>
            <button type="button" className="tf-secondary-button !min-h-0 !py-1" onClick={dropOlderDraft}>Discard what I typed</button>
          </div>
        </section>}
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
        {FIELDS.map((f, index) => {
          const said = message(f.key);
          const showsAsRead = ticket.as_read?.[f.key] !== undefined && ticket.as_read[f.key] !== values[f.key];
          const describedBy = [said && "message-" + f.key, showsAsRead && "as-read-" + f.key].filter(Boolean).join(" ") || undefined;
          return <div key={f.key}>
          <label htmlFor={"review-" + f.key} className={"mb-0.5 block text-[11px] text-[#526273] " + (f.critical ? "font-bold" : "font-medium")}>
            {f.label}{f.critical && <span className="ml-1 font-normal text-[#5f6c7b]">· check against the scan</span>}
          </label>
          <input
            id={"review-" + f.key}
            list={f.key === "supplier" ? "catalog-suppliers" : f.key === "material_type" ? "catalog-materials" : undefined}
            aria-describedby={describedBy}
            aria-invalid={said?.tone === "problem" ? true : undefined}
            type={f.type === "number" ? "text" : f.type ?? "text"}
            inputMode={f.type === "number" ? "decimal" : undefined}
            value={values[f.key]}
            readOnly={!canEdit || saving}
            onKeyDown={(event) => nextOnEnter(event, index)}
            onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}
            onBlur={() => leftField(f.key)}
            className={`w-full rounded border px-2.5 py-1.5 text-sm text-[#19232e] outline-none focus:ring-2 focus:ring-[#e6a97b] read-only:bg-[#f6f7f8] ${
              said?.tone === "problem" ? "border-red-400" : said?.tone === "warning" || flaggedFor(f.flagWords) ? "border-amber-400 bg-amber-50" : "border-[#cfd6de]"
            }`}
          />
          <div aria-live="polite">{said && <p id={"message-" + f.key} data-testid={"message-" + f.key}
            className={"mt-1 text-[11px] font-semibold leading-snug " + (said.tone === "problem" ? "text-red-700" : "text-amber-800")}>{said.text}</p>}</div>
          {showsAsRead && (
            <p id={"as-read-" + f.key} className="mt-1 text-[11px] text-[#657182]">Read from the ticket: {ticket.as_read?.[f.key]}</p>
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
        </div>;
        })}
        {catalogLists}
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
            readOnly={!canOverride || saving}
            aria-describedby={canOverride ? (rate ? "message-rate" : undefined) : "review-rate-override-note"}
            aria-invalid={rate ? true : undefined}
            onChange={(e) => setOverride(e.target.value)}
            className="mt-1 w-full rounded border border-[#cfd6de] px-3 py-2 text-sm read-only:bg-[#f6f7f8]"
          />
          {rate && <p id="message-rate" className="mt-1 text-[11px] font-semibold text-red-700">{rate.text}</p>}
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
      {failure && <div role="alert" data-testid="action-failure" className="border-t border-red-200 bg-red-50 px-3 py-2 text-xs leading-relaxed text-red-900">
        <p>{failure.message}</p>
        {failure.reference && <p className="mt-0.5">Reference: <code className="font-mono font-semibold">{failure.reference}</code></p>}
        <div className="mt-1.5 flex flex-wrap items-center gap-2">
          {failure.canRetry && <button type="button" className="tf-secondary-button !min-h-0 !py-1" disabled={saving}
            onClick={() => void attempt(failure.action, failure.note, failure.advance)}>{saving ? "Trying…" : "Try again"}</button>}
          {failure.signIn && <a className="tf-secondary-button !min-h-0 !py-1" href="/login">Sign in again</a>}
          <button type="button" className="font-semibold underline" onClick={() => onFailure(null)}>Dismiss</button>
        </div>
      </div>}
      {dirty && <p className="border-t border-[#e9edf0] bg-[#fff8ec] px-4 py-1.5 text-xs font-semibold text-amber-800" role="status">Unsaved changes — save or accept before leaving this ticket.</p>}
      {canEdit && <div className="flex flex-wrap gap-1.5 border-t border-[#dce1e6] bg-white p-2.5">
        {canVerify && needed.length > 0 && <p id="accept-needs" data-testid="accept-needs" className="w-full text-[11px] leading-snug text-[#526273]">Still needed to accept: {needed.join(", ")}.</p>}
        {canVerify ? <button
          ref={acceptButton}
          title="Review and accept ticket (Cmd/Ctrl + Enter)"
          aria-describedby={needed.length > 0 ? "accept-needs" : undefined}
          onClick={accept}
          className="w-full rounded bg-[#c2541c] px-4 py-2.5 text-sm font-bold text-white hover:bg-[#a64716] disabled:opacity-40"
          disabled={saving || cannotSave || (ticket.status === "verified" && !dirty)}
        >
          {ticket.status === "verified" && !dirty ? "Accepted" : "Accept & next"}
        </button> : <button
          ref={acceptButton}
          title="Save and go to the next ticket (Cmd/Ctrl + Enter)"
          onClick={() => void attempt("save", undefined, true)}
          className="w-full rounded bg-[#c2541c] px-4 py-2.5 text-sm font-bold text-white hover:bg-[#a64716] disabled:opacity-40"
          disabled={saving || cannotSave}
        >
          Save &amp; next
        </button>}
        <button disabled={saving || !dirty || cannotSave} onClick={() => void attempt("save")} className="rounded border border-[#cfd6dc] px-2.5 py-1.5 text-xs font-semibold text-[#39495b] disabled:opacity-40">
          Save changes
        </button>
        {canFlag && <button disabled={saving} onClick={() => void attempt("flag", note, true)}
          className="rounded border border-[#e5b993] bg-[#fff7ee] px-2.5 py-1.5 text-xs font-semibold text-[#97511e] hover:bg-[#fff0dc] disabled:opacity-40">
          Flag for later
        </button>}
        {canSkip && <button disabled={saving} onClick={() => {
          if (!window.confirm("Skip this ticket? Skipped tickets are excluded from exports and financial totals.")) return;
          void attempt("skip", undefined, true);
        }} className="rounded px-2 py-1.5 text-xs font-medium text-[#657182] hover:text-[#19232e] disabled:opacity-40">
          Skip non-ticket
        </button>}
      </div>}
    </aside>
  );
});

const HISTORY_FIELD: Record<string, string> = {
  status: "Status", verified_unit_price: "Locked unit price", ticket_number: "Ticket number",
  delivery_date: "Delivery date", material_type: "Material", tonnage: "Tons", supplier: "Supplier",
  job_location: "Job location", unit_price_override: "Rate override", flag_note: "Note",
  duplicate_override: "Approved despite matching ticket",
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
              <span className="block text-[11px] text-[#5f6c7b]">
                {e.actor ?? "Before individual accounts"} · {new Date(e.at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}
              </span>
            </li>)}
          </ol>}
      </div>
    </details>
  );
}
