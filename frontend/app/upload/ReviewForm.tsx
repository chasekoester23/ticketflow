"use client";

import { Fragment, memo, useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { ApiError, Failure, HistoryEntry, Ticket, UNIT_LABEL, describeFailure, getTicketHistory, patchTicket, ticketAction } from "../../lib/api";
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
// Each row of the form adds up to six columns. The order is also the Enter and Tab order.
const FIELD_SPAN: Record<FieldKey, string> = {
  ticket_number: "tf-rv-span3", delivery_date: "tf-rv-span3", material_type: "tf-rv-span4", tonnage: "tf-rv-span2 tf-rv-tons",
  supplier: "", job_location: "",
};
const FIELDS: { key: FieldKey; label: string; type?: string; flagWords: string[]; critical?: boolean; repeats?: boolean }[] = [
  { key: "ticket_number", label: "Ticket number", flagWords: ["Ticket number"], critical: true },
  { key: "delivery_date", label: "Delivery date", type: "date", flagWords: ["Date"], critical: true, repeats: true },
  { key: "material_type", label: "Material", flagWords: ["Material"], repeats: true },
  { key: "tonnage", label: "Tonnage (tons)", type: "number", flagWords: ["Tonnage", "Quantity", "Weights"], critical: true },
  { key: "supplier", label: "Supplier", flagWords: ["Supplier"], repeats: true },
  { key: "job_location", label: "Job location", flagWords: ["Job location"], repeats: true },
];
// Filled together by Alt+D. Date, ticket number and tons need a look at each scan.
const CARRIED: FieldKey[] = ["material_type", "supplier", "job_location"];
const CARRY_LABEL = "Material, supplier and job";

const OVERRIDE_KEY = "unit_price_override";
const NOTE_KEY = "flag_note";
const HAULER_KEY = "hauler_id";
const TRUCK_KEY = "truck_number";
const UNIT_KEY = "unit";
const GROSS_KEY = "gross_weight";
const TARE_KEY = "tare_weight";
const WEIGHT_UNIT_KEY = "weight_unit";
/** What a quantity can be entered in. Pounds are saved as tons (2,000 lb = 1 t) and the pounds are kept. */
const UNIT_CHOICES: [string, string][] = [["ton", "Tons"], ["lb", "Pounds (saved as tons)"], ["cy", "Cubic yards"], ["load", "Loads"],
  ["each", "Each"], ["gal", "Gallons"]];
const DRAFT_LABEL: Record<string, string> = {
  ...Object.fromEntries(FIELDS.map((f) => [f.key, f.label])),
  [OVERRIDE_KEY]: "Override rate", [HAULER_KEY]: "Hauled by", [TRUCK_KEY]: "Truck",
  [NOTE_KEY]: "Note",
};

export type PreviousValues = Partial<Record<FieldKey, string>>;
const HAUL_TEXT: Record<string, string> = {
  per_ton: "billed per ton", per_load: "billed per load", freight_included: "included in the material price",
  hourly: "paid by its hourly truck shift", unpriced: "no hauling rate yet (cost missing, not zero)", ambiguous: "two hauling rates apply; remove one",
};

/** The saved values of a ticket as the form holds them, with blanks as empty text. */
function savedValues(ticket: Ticket): Record<string, string> {
  return {
    ...Object.fromEntries(FIELDS.map((f) => [f.key, (ticket[f.key] as string | null) ?? ""])),
    [OVERRIDE_KEY]: ticket.unit_price_override ?? "",
    [NOTE_KEY]: ticket.flag_note ?? "",
    [HAULER_KEY]: ticket.hauler_id != null ? String(ticket.hauler_id) : "",
    [TRUCK_KEY]: ticket.truck_number ?? "",
    [UNIT_KEY]: ticket.unit ?? "ton",
    [GROSS_KEY]: ticket.gross_weight ?? "",
    [TARE_KEY]: ticket.tare_weight ?? "",
    [WEIGHT_UNIT_KEY]: ticket.weight_unit ?? "lb",
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

const SUBMIT_KEYS = typeof navigator !== "undefined" && /Mac/.test(navigator.platform) ? "⌘↵" : "Ctrl ↵";

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

const missingNotice = (flag: string) => /\bmissing\b/i.test(flag);
/**
 * Fields holding a value that arrived with a reason to doubt it. Blank fields aren't
 * here: those already have to be filled before Accept. The server still checks it all.
 */
export function attentionFields(ticket: Ticket, saved: Record<string, string>, manual: boolean): FieldKey[] {
  const flags = manual ? ticket.flags.filter((flag) => !missingNotice(flag)) : ticket.flags;
  return FIELDS.filter((f) => {
    const value = saved[f.key];
    if (!value.trim()) return false;
    if (flags.some((flag) => f.flagWords.some((word) => flag.startsWith(word)))) return true;
    if (f.key === "tonnage") return tonsMessage(value, ticket.unit ?? "ton")?.tone === "warning";
    if (f.key === "delivery_date") return dateMessage(value) !== null;
    if (f.key === "ticket_number") return Boolean(ticket.number_warning);
    return false;
  }).map((f) => f.key);
}

export const ReviewForm = memo(function ReviewForm({
  ticket, manual, catalog, previous, failure, onChange, onAdvance, onAccepted, onDirtyChange, onBusyChange, onError, onFailure, onStale,
  haulers = [],
}: {
  ticket: Ticket;
  /** The company's haulers, for deliveries billed per ton or per load. None: the field isn't shown. */
  haulers?: { id: number; name: string }[];
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
  const [hauler, setHauler] = useState(recovered.same?.typed[HAULER_KEY] ?? saved[HAULER_KEY]);
  const [truck, setTruck] = useState(recovered.same?.typed[TRUCK_KEY] ?? saved[TRUCK_KEY]);
  // Unit and scale weights: not kept in drafts, so a restored draft can't be read in the wrong unit.
  const [unit, setUnit] = useState(saved[UNIT_KEY]);
  const [gross, setGross] = useState(saved[GROSS_KEY]);
  const [tare, setTare] = useState(saved[TARE_KEY]);
  const [weightUnit, setWeightUnit] = useState(saved[WEIGHT_UNIT_KEY]);
  const [saving, setSaving] = useState(false);
  // Values that arrived with a warning: the reader's flag, an implausible figure, or a
  // number that may be the job's. Each needs a person to look at it before Accept works.
  // Typing in the field, pressing Enter or Tab in it, clicking into it, or "Matches the
  // scan" counts as looking; the cursor merely landing there doesn't.
  const [attention] = useState<FieldKey[]>(() => canEdit && ticket.status !== "verified" ? attentionFields(ticket, saved, manual) : []);
  const [checked, setChecked] = useState<Partial<Record<FieldKey, boolean>>>({});
  const markChecked = (key: FieldKey) => setChecked((old) => old[key] ? old : { ...old, [key]: true });
  const openedAt = useRef(0);
  const [leftBlank, setLeftBlank] = useState<Record<string, boolean>>({});
  const [triedAccept, setTriedAccept] = useState(false);
  // Read and set in the same moment as a click. Disabling the button waits for a
  // render, which is too late for a double click to be stopped by it alone.
  const inFlight = useRef(false);
  const acceptButton = useRef<HTMLButtonElement>(null);
  const form = useRef<HTMLElement>(null);
  const overrideDirty = override !== saved[OVERRIDE_KEY];
  const haulDirty = hauler !== saved[HAULER_KEY] || truck !== saved[TRUCK_KEY];

  const weightsDirty = gross !== saved[GROSS_KEY] || tare !== saved[TARE_KEY] || ((gross || tare) !== "" && weightUnit !== saved[WEIGHT_UNIT_KEY]);
  const unitDirty = unit !== saved[UNIT_KEY];
  const dirty = overrideDirty || haulDirty || unitDirty || weightsDirty || FIELDS.some((f) => values[f.key] !== saved[f.key]);
  // Before paint, so the unsaved-work guard is never a keystroke behind.
  useLayoutEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);

  // Keep what's been typed but not saved, on every change, for a crash or a closed tab.
  useEffect(() => {
    if (!canEdit) return;
    const typed: Record<string, string> = {};
    FIELDS.forEach((f) => { if (values[f.key] !== saved[f.key]) typed[f.key] = values[f.key]; });
    if (overrideDirty) typed[OVERRIDE_KEY] = override;
    if (note !== saved[NOTE_KEY]) typed[NOTE_KEY] = note;
    if (hauler !== saved[HAULER_KEY]) typed[HAULER_KEY] = hauler;
    if (truck !== saved[TRUCK_KEY]) typed[TRUCK_KEY] = truck;
    if (Object.keys(typed).length) saveDraft(ticket.id, revision, accountId, typed);
    else removeDraft(ticket.id, revision);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- saved values only change by remounting this form
  }, [values, override, note, hauler, truck]);

  function discardRestored() {
    setValues(Object.fromEntries(FIELDS.map((f) => [f.key, saved[f.key]])));
    setOverride(saved[OVERRIDE_KEY]);
    setNote(saved[NOTE_KEY]);
    setHauler(saved[HAULER_KEY]);
    setTruck(saved[TRUCK_KEY]);
    setUnit(saved[UNIT_KEY]);
    setGross(saved[GROSS_KEY]);
    setTare(saved[TARE_KEY]);
    setWeightUnit(saved[WEIGHT_UNIT_KEY]);
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
    openedAt.current = performance.now();
    if (active && active !== document.body && !form.current?.contains(active) && active.closest("input, textarea, select, [role=dialog]")) return;
    // The first field needing a person, in form order: blank, or read with a warning.
    // A clean ticket opens on Accept, so Enter approves what's on screen.
    const target = FIELDS.find((f) => !values[f.key] || attention.includes(f.key));
    if (target) document.getElementById("review-" + target.key)?.focus();
    else if (ticket.status !== "verified") acceptButton.current?.focus({ preventScroll: true });
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
    const body: Record<string, string | number | null> = {};
    FIELDS.forEach((f) => {
      if (values[f.key] !== saved[f.key]) body[f.key] = values[f.key].trim() === "" ? null : values[f.key];
    });
    if (overrideDirty) body.unit_price_override = override.trim() === "" ? null : override;
    if (hauler !== saved[HAULER_KEY]) body.hauler_id = hauler ? Number(hauler) : null;
    if (truck !== saved[TRUCK_KEY]) body.truck_number = truck.trim() === "" ? null : truck.trim();
    if (unitDirty) {
      body.unit = unit;
      // Pounds are converted from the figure typed, so it goes with them.
      if (unit === "lb" && values.tonnage.trim()) body.tonnage = values.tonnage.trim();
    }
    if (weightsDirty) {
      body.gross_weight = gross.trim() === "" ? null : gross.trim();
      body.tare_weight = tare.trim() === "" ? null : tare.trim();
      body.weight_unit = gross.trim() || tare.trim() ? weightUnit : null;
    }
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
  const tons = tonsMessage(values.tonnage, unit);
  const unitText = UNIT_LABEL[ticket.unit ?? "ton"] ?? UNIT_LABEL.ton;
  const rate = rateMessage(override);
  const cannotSave = Boolean(tons?.blocksSaving || rate?.blocksSaving);
  const message = (key: FieldKey): FieldMessage | null => {
    if (!canEdit) return null;
    const about = key === "tonnage" ? tons : key === "delivery_date" ? dateMessage(values[key]) : null;
    if (about) return about;
    // Worked out by the server for the saved number, so it shows until that number is changed.
    if (key === "ticket_number" && ticket.number_warning && values[key] === saved[key] && values[key]) return { tone: "warning", text: ticket.number_warning };
    return !values[key].trim() && (leftBlank[key] || triedAccept) ? { tone: "problem", text: REQUIRED_TEXT[key] } : null;
  };
  // What the server would refuse to approve. Saying so here saves the round trip,
  // and the server still checks everything itself.
  const stopsApproval = (key: FieldKey) => !values[key].trim() || (key === "tonnage" && tons?.tone === "problem");
  const needed = FIELDS.filter((f) => !values[f.key].trim()).map((f) => NEEDED_LABEL[f.key]);
  // Changing a value is looking at it.
  const unchecked = (key: FieldKey) => attention.includes(key) && !checked[key] && values[key] === saved[key];
  const toCheck = canVerify ? FIELDS.filter((f) => unchecked(f.key)).map((f) => NEEDED_LABEL[f.key]) : [];

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
    const first = FIELDS.find((f) => stopsApproval(f.key)) ?? FIELDS.find((f) => unchecked(f.key));
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

  // Enter confirms this field and moves down to the next one still needing a person
  // (blank, or read with a warning), then to Accept. Tab still visits every field.
  function nextOnEnter(event: KeyboardEvent<HTMLInputElement>, index: number) {
    if (event.key === "Tab" && !event.shiftKey) markChecked(FIELDS[index].key);
    if (event.key !== "Enter" || event.metaKey || event.ctrlKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    if (event.repeat) return;
    markChecked(FIELDS[index].key);
    const next = FIELDS.slice(index + 1).find((f) => !values[f.key].trim() || unchecked(f.key));
    if (next) document.getElementById("review-" + next.key)?.focus();
    else acceptButton.current?.focus();
  }
  // A held Enter repeats. Without this it would approve the next ticket the moment it opened.
  function guardAcceptKey(event: KeyboardEvent<HTMLButtonElement>) {
    if (event.key === "Enter" && (event.repeat || performance.now() - openedAt.current < 350)) event.preventDefault();
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
    <aside ref={form} className="tf-panel tf-review-pane tf-review-form">
      <div className="tf-review-pane-scroll tf-rv-body">
        <div className="tf-rv-head">
          <h3>{manual ? "Manual ticket entry" : "Extracted ticket information"}</h3>
          {manual && <span className="tf-rv-count">{filled} of 6 entered</span>}
          <span className={`tf-status ${STATUS_STYLE[ticket.status]}`}>{STATUS_LABEL[ticket.status]}</span>
        </div>
        {ticket.status === "verified" && (
          <p className="tf-alert tf-alert-good" data-testid="verified-by">
            {ticket.verified_by
              ? <>Approved by <strong>{ticket.verified_by}</strong>{ticket.verified_at ? <> on {new Date(ticket.verified_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}</> : null}</>
              : <>Approved before individual accounts were introduced; the approver was not recorded.</>}
          </p>
        )}
        {restoredAt && <div role="status" data-testid="draft-restored" className="tf-alert tf-alert-warn">
          <strong>Restored unsaved work</strong> you typed on {clockTime(restoredAt)}. It isn&apos;t saved yet: check it against the scan, then save or accept.
          <button type="button" className="tf-link ml-2" onClick={discardRestored}>Discard it</button>
        </div>}
        {olderDraft && <section aria-label="Values you typed on an earlier version" data-testid="draft-conflict" className="tf-alert tf-alert-warn">
          <p className="font-semibold">This ticket changed after you typed these values.</p>
          <p className="mt-0.5">You typed them on {clockTime(olderDraft.savedAt)}. They haven&apos;t been applied. Compare them with the current values and the scan.</p>
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
            <button type="button" className="tf-tool" onClick={() => applyTyped(Object.keys(olderDraft.typed))}>Use all I typed</button>
            <button type="button" className="tf-tool" onClick={dropOlderDraft}>Discard what I typed</button>
          </div>
        </section>}
        {ticket.error && <p className="tf-alert tf-alert-bad">{ticket.error}</p>}
        {ticket.duplicates.length > 0 && <ul className="tf-alert tf-alert-bad">
          <li className="font-semibold">Possible duplicate</li>
          {ticket.duplicates.map((d) => <li key={d}>{d}</li>)}
        </ul>}
        {ticket.flag_note && <p className="tf-alert tf-alert-warn" data-testid="flag-note"><strong>Set aside:</strong> {ticket.flag_note}</p>}
        {flags.length > 0 && <ul className="tf-alert tf-alert-warn">
          {flags.map((f) => <li key={f}>{f}</li>)}
        </ul>}
        {canEdit && carry.length > 0 && <button id="fill-repeats" type="button" onClick={fillRepeats} className="tf-rv-carry">
          <span>{CARRY_LABEL}: same as previous ticket</span><kbd className="tf-kbd">Alt+D</kbd>
        </button>}
        <div className="tf-rv-fields">
        {FIELDS.map((f, index) => {
          const said = message(f.key);
          const showsAsRead = ticket.as_read?.[f.key] !== undefined && ticket.as_read[f.key] !== values[f.key];
          const describedBy = [said && "message-" + f.key, showsAsRead && "as-read-" + f.key, canVerify && unchecked(f.key) && "check-" + f.key]
            .filter(Boolean).join(" ") || undefined;
          const notes = <>
            <div aria-live="polite">{said && <p id={"message-" + f.key} data-testid={"message-" + f.key}
              className={"tf-rv-note " + (said.tone === "problem" ? "text-red-700" : "text-amber-800")}>{said.text}</p>}</div>
            {showsAsRead && <p id={"as-read-" + f.key} className="tf-rv-note tf-rv-asread">Read from the ticket: {ticket.as_read?.[f.key]}</p>}
            {canEdit && ticket.suggestions?.[f.key] && ticket.suggestions[f.key] !== values[f.key] && (
              <button type="button" className="tf-link tf-rv-note"
                onClick={() => setValues({ ...values, [f.key]: ticket.suggestions?.[f.key] ?? values[f.key] })}>
                Use your catalog name: {ticket.suggestions[f.key]}
              </button>
            )}
            {canEdit && !ticket.suggestions?.[f.key] && (f.key === "supplier" || f.key === "material_type") && ticket.similar?.[f.key]
              && ticket.similar[f.key] !== values[f.key] && values[f.key] === saved[f.key] && (
              <button type="button" className="tf-link tf-rv-note"
                onClick={() => setValues({ ...values, [f.key]: ticket.similar?.[f.key as "supplier" | "material_type"] ?? values[f.key] })}>
                Did you mean {ticket.similar[f.key]}? Use the catalog name
              </button>
            )}
            {canVerify && attention.includes(f.key) && values[f.key] === saved[f.key] && (unchecked(f.key)
              ? <p className={"tf-rv-note tf-rv-check" + (triedAccept ? " tf-rv-check-asked" : "")} id={"check-" + f.key} data-testid={"check-" + f.key}>
                Check this against the scan, then press Enter.{" "}
                {/* Named without the field's label, so the label still names only its field. It sits just under that field. */}
                <button type="button" className="tf-link" data-testid={"confirm-" + f.key} onClick={() => markChecked(f.key)}>Matches the scan</button>
              </p>
              : <p className="tf-rv-note tf-rv-muted" data-testid={"checked-" + f.key}>Checked against the scan.</p>)}
            {canEdit && f.key === "delivery_date" && !values[f.key] && previous[f.key] && (
              <button type="button" className="tf-link tf-rv-note"
                onClick={() => setValues({ ...values, [f.key]: previous[f.key] as string })}>
                Same date as previous ticket: {previous[f.key]}
              </button>
            )}
          </>;
          // The tons box is narrow, so what's said about it runs the full width beneath its row.
          const wideNotes = f.key === "tonnage";
          return <Fragment key={f.key}>
            <div className={`tf-rv-field ${FIELD_SPAN[f.key]} ${f.critical ? "tf-rv-critical" : ""}`}>
              <label htmlFor={"review-" + f.key}>{f.key === "tonnage" && unit !== "ton"
                ? `Quantity (${unit === "lb" ? "pounds" : UNIT_LABEL[unit]?.long ?? unit})` : f.label}</label>
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
                onPointerDown={() => markChecked(f.key)}
                onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}
                onBlur={() => leftField(f.key)}
                data-state={said?.tone === "problem" ? "problem" : said?.tone === "warning" || flaggedFor(f.flagWords) ? "warn" : undefined}
                className="tf-rv-input"
              />
              {!wideNotes && notes}
            </div>
            {wideNotes && <div className="tf-rv-field tf-rv-widenotes">{notes}</div>}
          </Fragment>;
        })}
        </div>
        <details className="tf-rv-units" open={saved[UNIT_KEY] !== "ton" || Boolean(saved[GROSS_KEY] || saved[TARE_KEY]) || undefined}>
          <summary>Unit and scale weights <span className="tf-rv-muted">
            {unitText.long === "tons" ? "· in tons" : `· in ${unitText.long}`}
            {ticket.source_quantity && ticket.source_unit ? ` · written as ${Number(ticket.source_quantity).toLocaleString()} ${ticket.source_unit}` : ""}</span></summary>
          <div className="tf-rv-fields" role="group" aria-label="Unit and scale weights">
            <div className="tf-rv-field">
              <label htmlFor="review-unit">Measured in</label>
              <select id="review-unit" className="tf-rv-input" value={unit} disabled={!canEdit || saving} onChange={(e) => setUnit(e.target.value)}>
                {UNIT_CHOICES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
              {unit === "lb" && <p className="tf-rv-note tf-rv-muted">Type the pounds above. They&apos;re saved as tons (2,000 lb = 1 t) and the pounds stay on the record.</p>}
              {unit !== "ton" && unit !== "lb" && <p className="tf-rv-note tf-rv-muted">Priced only by a rate per {UNIT_LABEL[unit]?.per ?? unit}, and never added to tons.</p>}
            </div>
            <div className="tf-rv-field tf-rv-span2">
              <label htmlFor="review-gross">Gross weight</label>
              <input id="review-gross" className="tf-rv-input" inputMode="decimal" value={gross} readOnly={!canEdit || saving} onChange={(e) => setGross(e.target.value)} />
            </div>
            <div className="tf-rv-field tf-rv-span2">
              <label htmlFor="review-tare">Tare weight</label>
              <input id="review-tare" className="tf-rv-input" inputMode="decimal" value={tare} readOnly={!canEdit || saving} onChange={(e) => setTare(e.target.value)} />
            </div>
            <div className="tf-rv-field tf-rv-span2">
              <label htmlFor="review-weight-unit">Weights in</label>
              <select id="review-weight-unit" className="tf-rv-input" value={weightUnit} disabled={!canEdit || saving} onChange={(e) => setWeightUnit(e.target.value)}>
                <option value="lb">Pounds</option><option value="ton">Tons</option>
              </select>
            </div>
            <p className="tf-rv-field tf-rv-note tf-rv-muted">Optional. Gross minus tare is checked against the tons; it never replaces them.</p>
          </div>
        </details>
        {haulers.length > 0 && <div className="tf-rv-fields" role="group" aria-label="Hauling">
          <div className="tf-rv-field tf-rv-span4">
            <label htmlFor="review-hauler">Hauled by <span className="tf-rv-muted">(if a hauler bills per ton or per load)</span></label>
            <select id="review-hauler" className="tf-rv-input" value={hauler} disabled={!canEdit || saving} onChange={(e) => setHauler(e.target.value)}>
              <option value="">Supplier delivered / not recorded</option>
              {haulers.map((h) => <option key={h.id} value={h.id}>{h.name}</option>)}
            </select>
          </div>
          <div className="tf-rv-field tf-rv-span2">
            <label htmlFor="review-truck">Truck</label>
            <input id="review-truck" className="tf-rv-input" value={truck} readOnly={!canEdit || saving} maxLength={50} onChange={(e) => setTruck(e.target.value)} />
          </div>
          {ticket.status === "verified" && ticket.haul_billing && <p className="tf-rv-field tf-rv-note tf-rv-muted">
            Hauling: {HAUL_TEXT[ticket.haul_billing] ?? ticket.haul_billing}{ticket.haul_cost ? ` · $${ticket.haul_cost} locked at approval` : ""}</p>}
        </div>}
        {catalogLists}
        <div className="tf-rv-cost">
          {ticket.status === "verified" && ticket.price_snapshot_state === "priced" && (
            <p className="tf-rv-muted">Unit price locked at verification. Later changes to supplier rates will not change this ticket.</p>
          )}
          {ticket.status === "verified" && ticket.price_snapshot_state === null && (
            <p className="font-medium text-amber-800">Legacy record: the historical rate is unknown. Enter the documented original rate as an override, then save and verify again.</p>
          )}
          {ticket.status === "verified" && ticket.price_snapshot_state === "unpriced" && (
            <p className="font-medium text-amber-800">Verified without a price. Enter the correct rate as an override, then save and verify again.</p>
          )}
          {ticket.unit_price !== null ? (
            <div className="tf-rv-cost-line">
              <span><strong>${ticket.unit_price}</strong>/{unitText.per} <span className="tf-rv-muted">{ticket.price_source === "override" ? "manual override" : ticket.price_source === "project_rate" ? "project agreement" : "company rate"}</span></span>
              {ticket.cost ? <strong className="tf-rv-total">${ticket.cost}</strong> : <span className="tf-rv-muted">cost appears after you verify the tonnage</span>}
            </div>
          ) : (
            <p className="text-amber-800">{ticket.pricing_notes.join(". ")}. Add it under Suppliers &amp; Pricing, or enter an override below.</p>
          )}
          {canOverride ? <>
            <div className="tf-rv-override">
              <label htmlFor="review-rate-override">Override rate</label>
              <input
                id="review-rate-override"
                aria-label={`Override rate for this ticket ($/${unitText.per}, optional)`}
                inputMode="decimal"
                placeholder="$/ton"
                value={override}
                readOnly={saving}
                aria-describedby={rate ? "message-rate" : undefined}
                aria-invalid={rate ? true : undefined}
                onChange={(e) => setOverride(e.target.value)}
                className="tf-rv-input"
              />
            </div>
            {rate && <p id="message-rate" className="tf-rv-note text-red-700">{rate.text}</p>}
          </> : <p className="tf-rv-muted">{ticket.unit_price_override ? <>Rate override on this ticket: ${ticket.unit_price_override}/ton. </> : null}Only a manager or owner can change a ticket’s rate.</p>}
        </div>
        {!canEdit && <p className="tf-notice">View-only access. Ask an owner if you need to enter or approve tickets.</p>}
        {canFlag && <div className="tf-rv-field">
          <label htmlFor="review-flag-note">Note if you set this aside (optional)</label>
          <input id="review-flag-note" className="tf-rv-input" maxLength={300} value={note} onChange={(e) => setNote(e.target.value)}
            placeholder="e.g. tons smudged, ask the driver" />
        </div>}
        <TicketHistory ticketId={ticket.id} version={JSON.stringify([ticket.status, ticket.verified_at, ticket.unit_price_override, ...FIELDS.map((f) => ticket[f.key])])} />
      </div>
      {/* Below laptop width the form is part of a long page, so Accept and its messages stay at the screen's foot. */}
      <div className="tf-review-actions">
      {failure && <div role="alert" data-testid="action-failure" className="border-t border-red-200 bg-red-50 px-3 py-2 text-xs leading-relaxed text-red-900">
        <p>{failure.message}</p>
        {failure.reference && <p className="mt-0.5">Reference: <code className="font-mono font-semibold">{failure.reference}</code></p>}
        <div className="mt-1.5 flex flex-wrap items-center gap-2">
          {failure.canRetry && <button type="button" className="tf-tool" disabled={saving}
            onClick={() => void attempt(failure.action, failure.note, failure.advance)}>{saving ? "Trying…" : "Try again"}</button>}
          {failure.signIn && <a className="tf-tool" href="/login">Sign in again</a>}
          <button type="button" className="font-semibold underline" onClick={() => onFailure(null)}>Dismiss</button>
        </div>
      </div>}
      {dirty && <p className="tf-rv-unsaved" role="status">Unsaved changes — save or accept before leaving this ticket.</p>}
      {canEdit && <div className="tf-rv-actions">
        {canVerify && needed.length > 0 && <p id="accept-needs" data-testid="accept-needs" className="tf-rv-needs">Still needed to accept: {needed.join(", ")}.</p>}
        {canVerify && needed.length === 0 && toCheck.length > 0 && <p id="accept-checks" data-testid="accept-checks" className="tf-rv-needs">
          Check against the scan before accepting: {toCheck.join(", ")}.</p>}
        <div className="tf-rv-buttons">
          <button disabled={saving || !dirty || cannotSave} onClick={() => void attempt("save")} className="tf-secondary-button">Save changes</button>
          {canFlag && <button id="review-flag-button" disabled={saving} onClick={() => void attempt("flag", note, true)} className="tf-secondary-button">Flag for later</button>}
          {canVerify ? <button
            ref={acceptButton}
            title="Review and accept ticket (Enter here, or Cmd/Ctrl + Enter anywhere)"
            aria-describedby={needed.length > 0 ? "accept-needs" : toCheck.length > 0 ? "accept-checks" : undefined}
            onKeyDown={guardAcceptKey}
            onClick={accept}
            className="tf-primary-button tf-rv-accept"
            disabled={saving || cannotSave || (ticket.status === "verified" && !dirty)}
          >
            {ticket.status === "verified" && !dirty ? "Accepted" : <>Accept &amp; next<kbd aria-hidden="true">{SUBMIT_KEYS}</kbd></>}
          </button> : <button
            ref={acceptButton}
            title="Save and go to the next ticket (Cmd/Ctrl + Enter)"
            onClick={() => void attempt("save", undefined, true)}
            className="tf-primary-button tf-rv-accept"
            disabled={saving || cannotSave}
          >
            Save &amp; next<kbd aria-hidden="true">{SUBMIT_KEYS}</kbd>
          </button>}
        </div>
        <div className="tf-rv-foot">
          <span>{canVerify ? "Accepting records your name and counts these values toward totals." : "A manager or owner approves these before they count."}</span>
          {canSkip && <button id="review-skip-button" disabled={saving} onClick={() => {
            if (!window.confirm("Skip this ticket? Skipped tickets are excluded from exports and financial totals.")) return;
            void attempt("skip", undefined, true);
          }} className="tf-link">Skip non-ticket</button>}
        </div>
      </div>}
      </div>
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
    <details className="tf-rv-history" onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary>Change history</summary>
      <div className="pt-1.5">
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
