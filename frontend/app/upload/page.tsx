"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  Batch,
  Ticket,
  exportUrl,
  getBatch,
  getManifest,
  Manifest,
  getStatus,
  listMaterials,
  listProjects,
  listSuppliers,
  Project,
  getTickets,
  moveBatch,
  recordReviewTiming,
  retryBatch,
  ticketImageUrl,
} from "../../lib/api";
import { isSigningOut, removeTicketDrafts } from "../../lib/drafts";
import { lastPlace, rememberPlace } from "../../lib/places";
import { useAccount } from "../account-context";
import { Dialog } from "../Dialog";
import { BatchQueue } from "./BatchQueue";
import { ActionFailure, ReviewForm, STATUS_LABEL, previousValues } from "./ReviewForm";
import { ScanCheck } from "./ScanCheck";
import { ScanViewer, zoomIn, zoomOut } from "./ScanViewer";
import { QueueFilter, TicketQueue, pendingStatus } from "./TicketQueue";
import { UploadPanel } from "./UploadPanel";

const BATCH_LABEL: Record<string, string> = {
  uploaded: "Uploaded",
  processing: "Processing",
  ready_for_review: "Ready for review",
  failed: "Failed",
};
const DISCARD = "Discard unsaved changes to this ticket? Select Save changes to keep your corrections.";
const isTyping = (target: EventTarget | null) => {
  const element = target as HTMLElement | null;
  return Boolean(element?.isContentEditable || element?.closest?.("input, textarea, select, [contenteditable], [role=dialog]"));
};
/** The next ticket after `from` that passes `test`, wrapping round to the start; -1 if none. */
const nextMatching = (tickets: Ticket[], from: number, test: (t: Ticket) => boolean) => {
  const later = tickets.findIndex((t, i) => i > from && test(t));
  return later >= 0 ? later : tickets.findIndex((t, i) => i < from && test(t));
};

/** What the scan check depends on. It's reloaded when this changes. */
const statesOf = (tickets: Ticket[]) => tickets.map((t) => `${t.id}:${t.status}:${t.ticket_number ?? ""}`).join("|");
/** Keep the objects of unchanged tickets, so reloading a list doesn't re-render every row of it. */
const reuseUnchanged = (old: Ticket[], fresh: Ticket[]) => {
  const before = new Map(old.map((t) => [t.id, t]));
  return fresh.map((t) => {
    const same = before.get(t.id);
    return same && JSON.stringify(same) === JSON.stringify(t) ? same : t;
  });
};
// On Windows and Linux, Alt+Left is the browser's Back button. On a Mac it moves
// the cursor by a word, which people use while correcting a value.
const altArrowsGoBack = () => !/Mac|iPhone|iPad/.test(navigator.platform);

const SHORTCUTS: [string, string][] = [
  ["⌘/Ctrl + Enter", "Accept (or save) and go to the next ticket"],
  ["Enter", "Next field, then the Accept button"],
  ["Alt + D", "Material, supplier and job: same as previous ticket"],
  ["Alt + ↑ / ↓", "Previous or next ticket, even while typing"],
  ["J / K, Alt + ← / →", "Next or previous ticket"],
  ["N", "Next ticket that still needs review"],
  ["F", "Next flagged ticket"],
  ["+ / −  ·  0", "Zoom the scan in or out  ·  fit to width"],
  ["R", "Rotate the view (the stored scan isn't changed)"],
  ["/", "Search the ticket queue"],
  ["Esc", "Leave the current field"],
  ["?", "Show or hide this list"],
];

export default function Home() {
  const { account, can } = useAccount();
  const accountId = account?.id ?? 0;
  const canUpload = can("tickets.upload");
  const [batch, setBatch] = useState<Batch | null>(null);
  const [opening, setOpening] = useState(false);
  const [showUpload, setShowUpload] = useState(false);
  const [tickets, setTickets] = useState<Ticket[]>([]);
  const [selected, setSelected] = useState(0);
  const [formDirty, setFormDirty] = useState(false);
  // What the keyboard handler reads. Set before the browser can deliver another
  // key, so a fast key press can never act on the previous ticket or miss unsaved work.
  const dirtyNow = useRef(false);
  const reportDirty = useCallback((dirty: boolean) => {
    dirtyNow.current = dirty;
    setFormDirty(dirty);
  }, []);
  // Leaving a ticket with unsaved work takes a person's say-so. Choosing to
  // discard it also drops its crash-recovery copy, or it would come back later.
  const shownTicket = useRef<number | undefined>(undefined);
  const busyNow = useRef(false);
  const reportBusy = useCallback((busy: boolean) => { busyNow.current = busy; }, []);
  const mayLeaveTicket = useCallback((question: string = DISCARD) => {
    // A save or approval is on its way. Its answer decides what happens next.
    if (busyNow.current) return false;
    if (!dirtyNow.current) return true;
    if (!window.confirm(question)) return false;
    if (shownTicket.current !== undefined) removeTicketDrafts(shownTicket.current);
    return true;
  }, []);
  const [reviewFilter, setReviewFilter] = useState<QueueFilter>("pending");
  const [queueOpen, setQueueOpen] = useState(true);
  const [zoom, setZoom] = useState(1);
  const [rotation, setRotation] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [failure, setFailure] = useState<ActionFailure | null>(null);
  const [jumpStatus, setJumpStatus] = useState<string | null>(null);
  const [offline, setOffline] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [imageVersion, setImageVersion] = useState(0);
  const [timingEnabled, setTimingEnabled] = useState(false);
  const [workerRunning, setWorkerRunning] = useState<boolean | undefined>(undefined);
  const [help, setHelp] = useState(false);
  const [session, setSession] = useState({ accepted: 0, seconds: 0 });
  const [moving, setMoving] = useState(false);
  const canMove = can("projects.manage");
  const openedAt = useRef<Record<number, number>>({});
  const [catalog, setCatalog] = useState<{ suppliers: string[]; materials: string[] }>({ suppliers: [], materials: [] });

  useEffect(() => {
    listProjects().then(setProjects).catch((e) => setError(e.message));
  }, []);

  // Typing help only: free text stays allowed, because the ticket may truly differ.
  useEffect(() => {
    Promise.all([listSuppliers(), listMaterials()])
      .then(([s, m]) => setCatalog({ suppliers: s.map((x) => x.name), materials: m.map((x) => x.name) }))
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    getStatus().then((s) => {
      setConfigured(s.extraction_configured);
      setTimingEnabled(Boolean(s.review_timing_enabled));
      setWorkerRunning(s.worker_running);
    }).catch(() => setConfigured(null));
  }, []);

  const manifestStates = useRef("");
  const refresh = useCallback(async (id: number) => {
    const [b, t] = await Promise.all([getBatch(id), getTickets(id)]);
    setBatch(b);
    setTickets((old) => reuseUnchanged(old, t));
    // The scan check adds evidence; failing to load it must not stop review.
    manifestStates.current = statesOf(t);
    getManifest(id).then(setManifest).catch(() => setManifest(null));
    return { batch: b, tickets: t };
  }, []);

  // Edits and approvals happen in the review form, so keep the scan check in
  // step with them. A stale count here would defeat its purpose.
  const batchId = batch?.id;
  const ticketStates = useMemo(() => statesOf(tickets), [tickets]);
  useEffect(() => {
    if (batchId === undefined || !ticketStates || manifestStates.current === ticketStates) return;
    // A brief pause, so someone working quickly through a stack doesn't reload it per ticket.
    const timer = setTimeout(() => {
      manifestStates.current = ticketStates;
      getManifest(batchId).then(setManifest).catch(() => undefined);
    }, 400);
    return () => clearTimeout(timer);
  }, [batchId, ticketStates]);

  // Open the ticket in the address. Failing that, pick up where this person left
  // off in the batch, or at the next ticket to review after it. Failing that, the
  // first ticket needing work.
  const [resumedAt, setResumedAt] = useState<number | null>(null);
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const id = Number(params.get("batch"));
    const ticketId = Number(params.get("ticket"));
    if (!id) return;
    setOpening(true);
    refresh(id).then(({ tickets: loaded }) => {
      const wanted = loaded.findIndex((t) => t.id === ticketId);
      const firstOpen = loaded.findIndex(pendingStatus);
      if (wanted >= 0) return setSelected(wanted);
      const place = lastPlace(accountId, id);
      const left = place === null ? -1 : loaded.findIndex((t) => t.id === place);
      const resume = left < 0 ? -1 : pendingStatus(loaded[left]) ? left : nextMatching(loaded, left, pendingStatus);
      if (resume >= 0 && resume !== firstOpen) {
        setSelected(resume);
        setResumedAt(loaded[resume].id);
      } else if (firstOpen >= 0) {
        setSelected(firstOpen);
      }
    }).catch((e) => setError(e.message)).finally(() => setOpening(false));
  }, [refresh]); // eslint-disable-line react-hooks/exhaustive-deps -- the account is set before this page renders

  // Keep the address on the current ticket, so a reload or a shared link lands here.
  const current = tickets[selected];
  shownTicket.current = current?.id;
  useEffect(() => {
    if (batchId === undefined) return;
    const suffix = current ? `&ticket=${current.id}` : "";
    window.history.replaceState(null, "", `/upload?batch=${batchId}${suffix}`);
    if (current) rememberPlace(accountId, batchId, current.id);
  }, [batchId, current?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  // Poll the real backend status until processing actually finishes.
  useEffect(() => {
    if (!batch || !["uploaded", "processing"].includes(batch.status)) return;
    const timer = setInterval(() => refresh(batch.id).catch((e) => setError(e.message)), 1500);
    return () => clearInterval(timer);
  }, [batch, refresh]);

  async function onUploaded(uploaded: Batch) {
    if (!mayLeaveTicket("You have unsaved ticket corrections. Discard them and open the new upload?")) return;
    setSelected(0);
    reportDirty(false);
    setReviewFilter("pending");
    setShowUpload(false);
    setBatch(uploaded);
    await refresh(uploaded.id);
  }

  async function onRetry() {
    if (!batch || retrying) return;
    setRetrying(true);
    setError(null);
    try {
      await retryBatch(batch.id);
      await refresh(batch.id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRetrying(false);
    }
  }

  async function onMove(projectId: number) {
    if (!batch) return;
    try {
      setBatch(await moveBatch(batch.id, projectId));
      setMoving(false);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  useEffect(() => {
    if (current && openedAt.current[current.id] === undefined) openedAt.current[current.id] = Date.now();
    setNotice(null);
    setFailure(null);
    setJumpStatus(null);
  }, [current?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const update = () => setOffline(!navigator.onLine);
    update();
    window.addEventListener("online", update);
    window.addEventListener("offline", update);
    return () => {
      window.removeEventListener("online", update);
      window.removeEventListener("offline", update);
    };
  }, []);

  // Fetch the next scans early, so moving on feels instant.
  useEffect(() => {
    const upcoming = [
      tickets[selected + 1],
      tickets.find((t, i) => i > selected && pendingStatus(t)),
    ];
    upcoming.forEach((t) => {
      if (t) new Image().src = ticketImageUrl(t.id);
    });
  }, [selected, tickets]);

  const timingOn = useRef(false);
  timingOn.current = timingEnabled;
  const onAccepted = useCallback((ticketId: number) => {
    const started = openedAt.current[ticketId];
    if (started === undefined) return;
    const seconds = Math.min(3600, Math.max(0.1, Math.round((Date.now() - started) / 100) / 10));
    delete openedAt.current[ticketId];
    setSession((s) => ({ accepted: s.accepted + 1, seconds: s.seconds + seconds }));
    // Opt-in pilot measurement. It can never block or fail an approval.
    if (timingOn.current) recordReviewTiming(ticketId, seconds).catch(() => undefined);
  }, []);
  const onTicketChange = useCallback((updated: Ticket) => {
    setTickets((previous) => previous.map((t) => t.id === updated.id ? updated : t));
  }, []);

  // Someone else changed this ticket. Show them the latest values rather than
  // quietly applying the person's stale view on top.
  const shownBatch = useRef<number | undefined>(undefined);
  shownBatch.current = batchId;
  const onStale = useCallback((message: string) => {
    reportDirty(false);
    setNotice(message);
    if (shownBatch.current !== undefined) refresh(shownBatch.current).catch((e) => setError(e.message));
  }, [refresh, reportDirty]);
  const onScanCheckChanged = useCallback((next: Manifest) => {
    setManifest(next);
    setImageVersion((v) => v + 1);
    if (shownBatch.current !== undefined) refresh(shownBatch.current).catch((e) => setError(e.message));
  }, [refresh]);

  const counts = useMemo(() => {
    const tally = { review: 0, flagged: 0, verified: 0, skipped: 0 };
    tickets.forEach((t) => {
      if (pendingStatus(t)) tally.review += 1;
      else if (t.status === "flagged") tally.flagged += 1;
      else if (t.status === "verified") tally.verified += 1;
      else if (t.status === "skipped") tally.skipped += 1;
    });
    return tally;
  }, [tickets]);
  const done = counts.verified + counts.skipped;
  const progress = tickets.length ? (done / tickets.length) * 100 : 0;
  // One listener for the life of the page, reading the current state from here.
  // Re-subscribing on every change left moments when a key saw the old ticket.
  // Callbacks read it too, so they stay the same from one render to the next.
  const keyState = useRef({ selected, tickets, help });
  useLayoutEffect(() => {
    keyState.current = { selected, tickets, help };
  });

  const selectTicket = useCallback((index: number) => {
    if (index === keyState.current.selected) return;
    if (!mayLeaveTicket()) return;
    setSelected(index);
    reportDirty(false);
  }, [mayLeaveTicket, reportDirty]);
  const hideQueue = useCallback(() => setQueueOpen(false), []);

  // N and F: straight to the next ticket that needs a person. Never approves anything.
  const jumpTo = useCallback((kind: "review" | "flagged") => {
    const { tickets, selected } = keyState.current;
    const test = kind === "review" ? pendingStatus : (t: Ticket) => t.status === "flagged";
    const next = nextMatching(tickets, selected, test);
    if (next < 0) {
      setJumpStatus(kind === "review" ? "No other ticket in this batch needs review."
        : tickets[selected] && test(tickets[selected]) ? "This is the only flagged ticket in this batch." : "No flagged tickets in this batch.");
      return;
    }
    if (!mayLeaveTicket()) return;
    setJumpStatus(null);
    setReviewFilter(kind === "review" ? "pending" : "flagged");
    setSelected(next);
    reportDirty(false);
  }, [mayLeaveTicket, reportDirty]);
  const onOpenFromScanCheck = useCallback((ticketId: number) => {
    const index = keyState.current.tickets.findIndex((t) => t.id === ticketId);
    if (index >= 0) selectTicket(index);
  }, [selectTicket]);
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.ctrlKey || event.metaKey) return;
      const { selected, tickets, help } = keyState.current;
      const count = tickets.length;
      if (event.key === "Escape") {
        if (help) setHelp(false);
        else if (isTyping(event.target)) (event.target as HTMLElement).blur();
        return;
      }
      if (!count) return;
      const typing = isTyping(event.target);
      const move = (step: number) => {
        event.preventDefault();
        const next = selected + step;
        if (next < 0 || next >= count) return;
        if (!mayLeaveTicket()) return;
        setSelected(next);
        reportDirty(false);
      };
      if (event.altKey) {
        // Alt+↑/↓ means nothing in a one-line field, so they work while typing.
        // Alt+←/→ only give way to typing on a Mac, where they move by word.
        // Elsewhere they'd leave the page, so they always move between tickets.
        if (event.key === "ArrowDown") move(1);
        else if (event.key === "ArrowUp") move(-1);
        else if ((!typing || altArrowsGoBack()) && event.key === "ArrowRight") move(1);
        else if ((!typing || altArrowsGoBack()) && event.key === "ArrowLeft") move(-1);
        return;
      }
      if (typing) return;
      if (event.key === "j") move(1);
      else if (event.key === "n" || event.key === "N") { event.preventDefault(); jumpTo("review"); }
      else if (event.key === "f" || event.key === "F") { event.preventDefault(); jumpTo("flagged"); }
      else if (event.key === "k") move(-1);
      else if (event.key === "+" || event.key === "=") setZoom(zoomIn);
      else if (event.key === "-") setZoom(zoomOut);
      else if (event.key === "0") setZoom(1);
      else if (event.key === "r") setRotation((v) => (v + 90) % 360);
      else if (event.key === "?") setHelp((v) => !v);
      else if (event.key === "/") {
        event.preventDefault();
        setQueueOpen(true);
        requestAnimationFrame(() => document.getElementById("queue-search")?.focus());
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [reportDirty, mayLeaveTicket, jumpTo]);

  // Complete the pending queue first, then automatically revisit flagged tickets.
  // A ticket is never auto-accepted; only the explicit Accept action verifies.
  const advance = useCallback(() => {
    const { tickets, selected } = keyState.current;
    reportDirty(false);
    const findNext = (predicate: (ticket: Ticket) => boolean): number => {
      const later = tickets.findIndex((t, i) => i > selected && predicate(t));
      return later >= 0 ? later : tickets.findIndex((t, i) => i < selected && predicate(t));
    };
    let next = findNext(pendingStatus);
    if (next < 0) {
      next = findNext((t) => t.status === "flagged");
      if (next >= 0) setReviewFilter("flagged");
    } else {
      setReviewFilter("pending");
    }
    if (next >= 0) setSelected(next);
  }, [reportDirty]);

  // Everything for one ticket fits on one screen: the panes take whatever height
  // is left under the batch summary, so Accept and the scan are always in view.
  const workspace = useRef<HTMLElement>(null);
  const hasWorkspace = tickets.length > 0;
  useEffect(() => {
    if (!hasWorkspace) return;
    const update = () => {
      const element = workspace.current;
      if (!element) return;
      const top = element.getBoundingClientRect().top + window.scrollY;
      element.style.setProperty("--tf-pane-height", `${Math.max(540, window.innerHeight - top - 12)}px`);
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(document.body);
    window.addEventListener("resize", update);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", update);
    };
  }, [hasWorkspace]);

  useEffect(() => {
    if (!formDirty) return;
    const warn = (event: BeforeUnloadEvent) => {
      if (isSigningOut()) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [formDirty]);

  const previous = useMemo(() => previousValues(tickets, selected), [tickets, selected]);
  const allResolved = tickets.length > 0 && counts.review === 0 && counts.flagged === 0;
  const project = batch ? projects.find((p) => p.id === batch.project_id) : undefined;
  const failedFiles = batch?.files.filter((f) => f.error) ?? [];
  const pace = session.accepted >= 3 ? Math.round(session.seconds / session.accepted) : null;

  return (
    <div className="space-y-3 pb-6">
      {!batch && !opening && <div className="tf-page-heading">
        <div>
          <h1 className="tf-page-title">Ticket intake & review</h1>
          <p className="tf-page-description">Upload delivery scans, compare extracted information with the original, and approve each ticket.</p>
        </div>
        <div className="tf-status tf-status-attention">Human verification required</div>
      </div>}

      {configured === false && (!batch || showUpload) && (
        <p className="tf-notice" role="note">
          AI extraction is not configured. <strong>Manual entry works without an API key.</strong> Select “Manual entry” below, then run the local processing worker to prepare your scans. AI extraction requires a provider key in backend/.env.
        </p>
      )}
      {error && (
        <div role="alert" className="rounded border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-900">
          {error} <button type="button" className="ml-3 font-semibold underline" onClick={() => setError(null)}>Dismiss</button>
        </div>
      )}
      {resumedAt !== null && current?.id === resumedAt && (
        <p role="status" data-testid="resumed" className="tf-notice flex flex-wrap items-center gap-x-3 gap-y-1">
          <span>Picked up where you left off in this batch.</span>
          <button type="button" className="tf-link" onClick={() => {
            const first = tickets.findIndex(pendingStatus);
            setResumedAt(null);
            if (first >= 0) selectTicket(first);
          }}>Go to the first ticket to review</button>
        </p>
      )}
      {notice && (
        <div role="alert" data-testid="stale-notice" className="rounded border border-amber-300 bg-amber-50 px-4 py-3 text-sm font-medium text-amber-900">
          {notice}
        </div>
      )}

      {offline && <p role="status" data-testid="offline-notice" className="rounded border border-amber-300 bg-amber-50 px-4 py-2 text-sm font-medium text-amber-900">
        This computer has lost its connection. You can keep typing: it&apos;s kept here until you can save.
      </p>}

      {!canUpload && !opening && (
        <p className="tf-notice" role="note">Your role is view-only. You can open batches and ticket images, but uploads and edits are disabled.</p>
      )}
      {!batch && !opening && <BatchQueue />}
      {canUpload && !opening && (!batch || showUpload) && <UploadPanel projects={projects} onUploaded={onUploaded} onError={setError} />}

      {batch && (
        <section className="tf-panel px-4 py-3" aria-label="Batch summary">
          <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
            <div className="min-w-0">
              <a href="/upload" className="tf-link" onClick={(event) => { if (!mayLeaveTicket()) event.preventDefault(); }}>← All uploads</a>
              <h1 className="mt-0.5 text-[15px] font-bold text-[#1d2b38]">
                Batch #{batch.id} · {BATCH_LABEL[batch.status] ?? batch.status}
                {project && <span className="ml-2 font-medium text-[#657182]">{project.code} · {project.name}</span>}
                {canMove && !moving && <button type="button" className="tf-link ml-2 text-[11px]" onClick={() => setMoving(true)}>Change project</button>}
              </h1>
              {moving && <label className="mt-1 flex flex-wrap items-center gap-2 text-xs font-semibold text-[#49586a]">Move this batch to
                <select className="tf-field !min-h-0 !py-1 !text-xs" aria-label="Move this batch to project" defaultValue=""
                  onChange={(e) => { if (e.target.value) void onMove(Number(e.target.value)); }}>
                  <option value="" disabled>Choose a project…</option>
                  {projects.filter((p) => p.id !== batch.project_id).map((p) => <option key={p.id} value={p.id}>{p.code} · {p.name}</option>)}
                </select>
                <button type="button" className="tf-link text-[11px]" onClick={() => setMoving(false)}>Cancel</button>
              </label>}
              <p className="mt-0.5 text-xs text-[#657182]">{tickets.length} tickets · {counts.verified} accepted · {counts.flagged} flagged · {counts.review} awaiting review · {batch.intake_mode === "manual" ? "Manual entry" : "AI extraction"}</p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <button type="button" className="tf-secondary-button" onClick={() => setHelp(true)} aria-label="Keyboard shortcuts"><kbd className="tf-kbd">?</kbd> Shortcuts</button>
              {batch.status === "failed" && canUpload && (
                <button type="button" onClick={onRetry} disabled={retrying} className="tf-secondary-button disabled:opacity-40">
                  {retrying ? "Retrying…" : "Retry failed files"}
                </button>
              )}
              <a className="tf-secondary-button" href={exportUrl(batch.id)}>Export Excel</a>
              {canUpload && <button type="button" className="tf-secondary-button" aria-expanded={showUpload} onClick={() => setShowUpload((v) => !v)}>New upload</button>}
            </div>
          </div>
          {tickets.length > 0 && <div className="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1.5">
            <div className="tf-progress min-w-[160px] flex-1" role="progressbar" aria-label="Tickets accepted or skipped" aria-valuenow={done} aria-valuemax={tickets.length} aria-valuemin={0}>
              <div style={{ width: progress + "%" }} />
            </div>
            <span className="text-xs font-bold tabular-nums text-[#293948]">{done} / {tickets.length} resolved</span>
            <span className="text-xs text-[#657182]"><strong className="text-[#216c43]">{counts.verified}</strong> accepted · <strong className="text-[#995a18]">{counts.review}</strong> to review · <strong className="text-[#995a18]">{counts.flagged}</strong> flagged for later{counts.skipped > 0 && <> · <strong>{counts.skipped}</strong> skipped</>}</span>
            {pace !== null && <span className="text-xs text-[#657182]" data-testid="session-pace">This session: {session.accepted} accepted, about {pace} s each</span>}
          </div>}
          {batch.status === "uploaded" && (
            <p className={workerRunning === false ? "mt-3 rounded border border-amber-300 bg-amber-50 px-3 py-2 text-xs font-medium text-amber-900" : "tf-notice mt-3"} role="status" data-testid="queue-status">
              {workerRunning === false && <strong>The processing worker isn&apos;t running, so this upload is waiting. </strong>}
              {workerRunning === true && <strong>The worker is running, and will pick this up shortly. </strong>}
              Your files are saved in the processing queue. A separate TicketFlow worker must be running
              to {batch.intake_mode === "manual" ? "prepare ticket images for manual entry" : "extract ticket fields"}.
              From the backend folder, run <code>python scripts/run_worker.py</code>.
              The upload remains queued even if you close this page.
            </p>
          )}
          {batch.status === "failed" && (
            <p className="mt-3 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
              Some files failed to process. Any successfully extracted tickets are still available for review.
              Retrying will preserve accepted tickets and manually corrected values.
            </p>
          )}
          {(failedFiles.length > 0 || tickets.length === 0) && <ul className="mt-2 space-y-1 text-xs text-[#657182]">
            {batch.files.map((file) => <li key={file.id}>
              <span className="font-medium text-[#354454]">{file.original_name}</span>: {BATCH_LABEL[file.status] ?? file.status}
              {file.error && <span className="ml-1 text-red-700">({file.error})</span>}
            </li>)}
          </ul>}
          {manifest && manifest.batch_id === batch.id && (
            <ScanCheck key={batch.id} manifest={manifest} onError={setError}
              onChanged={onScanCheckChanged} onOpenTicket={onOpenFromScanCheck} />
          )}
        </section>
      )}

      {allResolved && batch && (
        <section className="tf-panel flex flex-wrap items-center justify-between gap-3 border-[#cce4d2] bg-[#f3f9f4] px-4 py-3" data-testid="batch-complete">
          <p className="text-sm font-semibold text-[#216c43]">All tickets have been resolved.
            {manifest?.status === "count_not_confirmed" && <span className="ml-1 font-normal">Enter your paper count in the scan check to confirm nothing was missed.</span>}
          </p>
          <div className="flex flex-wrap gap-2">
            <a className="tf-secondary-button" href="/invoices">Compare an invoice</a>
            <a className="tf-secondary-button" href="/upload">All uploads</a>
          </div>
        </section>
      )}
      {!allResolved && counts.review === 0 && counts.flagged > 0 && (
        <p className="tf-notice text-[#7c4518]">Review flagged tickets before finishing.</p>
      )}

      {tickets.length > 0 && current && (
        <section ref={workspace} className={`tf-review-grid ${queueOpen ? "" : "tf-review-grid-noqueue"}`} aria-label="Ticket review workspace">
          <p className="sr-only" aria-live="polite" data-testid="position-announcement">
            Showing {selected + 1} of {tickets.length}: ticket number {current.ticket_number || "not entered"}, {(STATUS_LABEL[current.status] ?? current.status).toLowerCase()}.
          </p>
          {queueOpen && <TicketQueue key={batch?.id} tickets={tickets} selected={selected} filter={reviewFilter}
            onFilter={setReviewFilter} onSelect={selectTicket} onHide={hideQueue} />}

          <section className="tf-panel tf-review-pane flex flex-col">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[#e7eaee] px-3 py-2">
              <div className="min-w-0">
                <h2 className="text-[13px] font-bold text-[#1d2b38]">Original scanned ticket</h2>
                <p className="text-[11px] text-[#657182]" data-testid="position"><strong className="font-bold text-[#293948]">Ticket {selected + 1} of {tickets.length}</strong> · {counts.review} to review · Page {current.source_page}, crop {current.position + 1}</p>
                <div role="status" data-testid="jump-status" className="text-[11px] font-semibold text-[#8a4f14]">{jumpStatus}</div>
              </div>
              <div className="flex flex-wrap items-center gap-1.5">
                {!queueOpen && <button type="button" className="tf-secondary-button !min-h-0 !px-2 !py-1" onClick={() => setQueueOpen(true)}>Show queue</button>}
                <button type="button" onClick={() => setZoom(zoomOut)} disabled={zoom <= 1} className="tf-secondary-button !min-h-0 !px-2.5 !py-1 disabled:opacity-40" aria-label="Zoom out">−</button>
                <span className="w-10 text-center text-[11px] tabular-nums text-[#526273]" data-testid="zoom-level">{Math.round(zoom * 100)}%</span>
                <button type="button" onClick={() => setZoom(zoomIn)} disabled={zoom >= 4} className="tf-secondary-button !min-h-0 !px-2.5 !py-1 disabled:opacity-40" aria-label="Zoom in">+</button>
                <button type="button" onClick={() => setRotation((v) => (v + 90) % 360)} className="tf-secondary-button !min-h-0 !px-2.5 !py-1">Rotate</button>
                <a href={ticketImageUrl(current.id)} target="_blank" rel="noreferrer" className="tf-link px-1 text-[11px]">Open full image</a>
              </div>
            </div>
            <ScanViewer src={ticketImageUrl(current.id) + (imageVersion ? "?v=" + imageVersion : "")}
              alt={"Original scanned delivery ticket " + (current.ticket_number || String(current.id))}
              zoom={zoom} rotation={rotation} onZoom={setZoom} />
            <div className="flex flex-wrap items-center justify-between gap-2 border-t border-[#e7eaee] px-3 py-1.5">
              <p className="text-[11px] text-[#5f6c7b]">Click to zoom on a spot, drag to pan. Zoom and rotation carry to the next ticket.</p>
              <div className="flex items-center gap-1.5">
                <button type="button" disabled={selected === 0} onClick={() => selectTicket(selected - 1)} className="tf-secondary-button !min-h-0 !px-2.5 !py-1 disabled:opacity-40">Previous</button>
                <button type="button" disabled={selected >= tickets.length - 1} onClick={() => selectTicket(selected + 1)} className="tf-secondary-button !min-h-0 !px-2.5 !py-1 disabled:opacity-40">Next</button>
              </div>
            </div>
          </section>

          <ReviewForm
            key={current.id + "-" + (current.revision ?? `${current.status}-${current.tonnage}-${current.ticket_number}-${current.unit_price}-${current.cost}`)}
            ticket={current}
            manual={batch?.intake_mode === "manual"}
            catalog={catalog}
            previous={previous}
            failure={failure?.ticketId === current.id ? failure : null}
            onChange={onTicketChange}
            onAdvance={advance}
            onAccepted={onAccepted}
            onDirtyChange={reportDirty}
            onBusyChange={reportBusy}
            onError={setError}
            onFailure={setFailure}
            onStale={onStale}
          />
        </section>
      )}

      {help && <Dialog label="Keyboard shortcuts" onClose={() => setHelp(false)}>
        <div className="tf-panel-heading">
          <h2 className="tf-panel-title">Keyboard shortcuts</h2>
          <button type="button" className="tf-link" onClick={() => setHelp(false)}>Close</button>
        </div>
        <table className="tf-table"><tbody>
          {SHORTCUTS.map(([keys, what]) => <tr key={keys}><td className="whitespace-nowrap"><kbd className="tf-kbd">{keys}</kbd></td><td>{what}</td></tr>)}
        </tbody></table>
        <p className="border-t border-[#e9edf0] px-4 py-2.5 text-[11px] text-[#5f6c7b]">Single keys work when the cursor isn&apos;t in a field. No key approves more than the ticket in front of you.</p>
      </Dialog>}
    </div>
  );
}
