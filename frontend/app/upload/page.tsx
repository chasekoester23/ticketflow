"use client";

import { useCallback, useEffect, useRef, useState } from "react";
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
import { useAccount } from "../account-context";
import { BatchQueue } from "./BatchQueue";
import { ReviewForm, STATUS_LABEL, STATUS_STYLE, previousValues } from "./ReviewForm";
import { ScanCheck } from "./ScanCheck";
import { ScanViewer, zoomIn, zoomOut } from "./ScanViewer";
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
const pendingStatus = (t: Ticket) => t.status === "needs_review" || t.status === "failed";

const SHORTCUTS: [string, string][] = [
  ["⌘/Ctrl + Enter", "Accept (or save) and go to the next ticket"],
  ["Enter", "Next field, then the Accept button"],
  ["Alt + D", "Material, supplier and job: same as previous ticket"],
  ["Alt + ↑ / ↓", "Previous or next ticket, even while typing"],
  ["J / K, Alt + ← / →", "Next or previous ticket, outside a field"],
  ["+ / −  ·  0", "Zoom the scan in or out  ·  fit to width"],
  ["R", "Rotate the view (the stored scan isn't changed)"],
  ["/", "Search the ticket queue"],
  ["Esc", "Leave the current field"],
  ["?", "Show or hide this list"],
];

export default function Home() {
  const { can } = useAccount();
  const canUpload = can("tickets.upload");
  const [batch, setBatch] = useState<Batch | null>(null);
  const [opening, setOpening] = useState(false);
  const [showUpload, setShowUpload] = useState(false);
  const [tickets, setTickets] = useState<Ticket[]>([]);
  const [selected, setSelected] = useState(0);
  const [formDirty, setFormDirty] = useState(false);
  const [reviewFilter, setReviewFilter] = useState<"pending" | "flagged" | "all">("pending");
  const [queueSearch, setQueueSearch] = useState("");
  const [queueOpen, setQueueOpen] = useState(true);
  const [zoom, setZoom] = useState(1);
  const [rotation, setRotation] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [imageVersion, setImageVersion] = useState(0);
  const [timingEnabled, setTimingEnabled] = useState(false);
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
    }).catch(() => setConfigured(null));
  }, []);

  const refresh = useCallback(async (id: number) => {
    const [b, t] = await Promise.all([getBatch(id), getTickets(id)]);
    setBatch(b);
    setTickets(t);
    // The scan check adds evidence; failing to load it must not stop review.
    getManifest(id).then(setManifest).catch(() => setManifest(null));
    return { batch: b, tickets: t };
  }, []);

  // Edits and approvals happen in the review form, so keep the scan check in
  // step with them. A stale count here would defeat its purpose.
  const batchId = batch?.id;
  const ticketStates = tickets.map((t) => `${t.id}:${t.status}:${t.ticket_number ?? ""}`).join("|");
  useEffect(() => {
    if (batchId === undefined || !ticketStates) return;
    getManifest(batchId).then(setManifest).catch(() => undefined);
  }, [batchId, ticketStates]);

  // Open the batch and ticket in the address, or the first one needing work.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const id = Number(params.get("batch"));
    const ticketId = Number(params.get("ticket"));
    if (!id) return;
    setOpening(true);
    refresh(id).then(({ tickets: loaded }) => {
      const wanted = loaded.findIndex((t) => t.id === ticketId);
      const firstOpen = loaded.findIndex(pendingStatus);
      if (wanted >= 0) setSelected(wanted);
      else if (firstOpen >= 0) setSelected(firstOpen);
    }).catch((e) => setError(e.message)).finally(() => setOpening(false));
  }, [refresh]);

  // Keep the address on the current ticket, so a reload or a shared link lands here.
  const current = tickets[selected];
  useEffect(() => {
    if (batchId === undefined) return;
    const suffix = current ? `&ticket=${current.id}` : "";
    window.history.replaceState(null, "", `/upload?batch=${batchId}${suffix}`);
  }, [batchId, current?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  // Poll the real backend status until processing actually finishes.
  useEffect(() => {
    if (!batch || !["uploaded", "processing"].includes(batch.status)) return;
    const timer = setInterval(() => refresh(batch.id).catch((e) => setError(e.message)), 1500);
    return () => clearInterval(timer);
  }, [batch, refresh]);

  async function onUploaded(uploaded: Batch) {
    if (formDirty && !window.confirm("You have unsaved ticket corrections. Discard them and open the new upload?")) return;
    setSelected(0);
    setFormDirty(false);
    setReviewFilter("pending");
    setQueueSearch("");
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
  }, [current?.id]); // eslint-disable-line react-hooks/exhaustive-deps

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

  function onAccepted(ticketId: number) {
    const started = openedAt.current[ticketId];
    if (started === undefined) return;
    const seconds = Math.min(3600, Math.max(0.1, Math.round((Date.now() - started) / 100) / 10));
    delete openedAt.current[ticketId];
    setSession((s) => ({ accepted: s.accepted + 1, seconds: s.seconds + seconds }));
    // Opt-in pilot measurement. It can never block or fail an approval.
    if (timingEnabled) recordReviewTiming(ticketId, seconds).catch(() => undefined);
  }

  // Someone else changed this ticket. Show them the latest values rather than
  // quietly applying the person's stale view on top.
  const onStale = useCallback((message: string) => {
    setFormDirty(false);
    setNotice(message);
    if (batchId !== undefined) refresh(batchId).catch((e) => setError(e.message));
  }, [batchId, refresh]);

  const counts = {
    review: tickets.filter(pendingStatus).length,
    flagged: tickets.filter((t) => t.status === "flagged").length,
    verified: tickets.filter((t) => t.status === "verified").length,
    skipped: tickets.filter((t) => t.status === "skipped").length,
  };
  const done = counts.verified + counts.skipped;
  const progress = tickets.length ? (done / tickets.length) * 100 : 0;
  const queue = tickets.map((t, index) => ({ t, index })).filter(({ t }) => {
    if (reviewFilter === "pending" && !pendingStatus(t)) return false;
    if (reviewFilter === "flagged" && t.status !== "flagged") return false;
    const search = queueSearch.trim().toLowerCase();
    if (!search) return true;
    return [t.ticket_number, t.material_type, t.supplier, t.job_location]
      .some((field) => (field ?? "").toLowerCase().includes(search));
  });

  function selectTicket(index: number) {
    if (index === selected) return;
    if (formDirty && !window.confirm(DISCARD)) return;
    setSelected(index);
    setFormDirty(false);
  }

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.ctrlKey || event.metaKey) return;
      if (event.key === "Escape") {
        if (help) setHelp(false);
        else if (isTyping(event.target)) (event.target as HTMLElement).blur();
        return;
      }
      if (!tickets.length) return;
      const typing = isTyping(event.target);
      const move = (step: number) => {
        event.preventDefault();
        const next = selected + step;
        if (next < 0 || next >= tickets.length) return;
        if (formDirty && !window.confirm(DISCARD)) return;
        setSelected(next);
        setFormDirty(false);
      };
      if (event.altKey) {
        // Alt+↑/↓ means nothing in a one-line field, so they work while typing.
        // Alt+←/→ move by word there, so those never steal from typing.
        if (event.key === "ArrowDown") move(1);
        else if (event.key === "ArrowUp") move(-1);
        else if (!typing && event.key === "ArrowRight") move(1);
        else if (!typing && event.key === "ArrowLeft") move(-1);
        return;
      }
      if (typing) return;
      if (event.key === "j") move(1);
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
  }, [selected, tickets.length, formDirty, help]);

  // Complete the pending queue first, then automatically revisit flagged tickets.
  // A ticket is never auto-accepted; only the explicit Accept action verifies.
  function advance() {
    setFormDirty(false);
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
  }

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
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [formDirty]);

  const allResolved = tickets.length > 0 && counts.review === 0 && counts.flagged === 0;
  const project = batch ? projects.find((p) => p.id === batch.project_id) : undefined;
  const failedFiles = batch?.files.filter((f) => f.error) ?? [];
  const pace = session.accepted >= 3 ? Math.round(session.seconds / session.accepted) : null;

  return (
    <div className="space-y-3 pb-6">
      {!batch && !opening && <div className="tf-page-heading">
        <div>
          <p className="tf-eyebrow">DELIVERY DOCUMENTATION / TICKET INTAKE</p>
          <h1 className="tf-page-title">Ticket intake & review</h1>
          <p className="tf-page-description">Upload delivery scans, compare extracted information with the original, and approve each ticket.</p>
        </div>
        <div className="tf-status tf-status-attention">Human verification required</div>
      </div>}

      {configured === false && (!batch || showUpload) && (
        <div role="alert" className="rounded border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
          AI extraction is not configured. <strong>Manual entry works without an API key.</strong> Select “Manual entry” below, then run the local processing worker to prepare your scans. AI extraction requires a provider key in backend/.env.
        </div>
      )}
      {error && (
        <div role="alert" className="rounded border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-900">
          {error} <button type="button" className="ml-3 font-semibold underline" onClick={() => setError(null)}>Dismiss</button>
        </div>
      )}
      {notice && (
        <div role="alert" data-testid="stale-notice" className="rounded border border-amber-300 bg-amber-50 px-4 py-3 text-sm font-medium text-amber-900">
          {notice}
        </div>
      )}

      {!canUpload && !opening && (
        <p className="tf-notice" role="note">Your role is view-only. You can open batches and ticket images, but uploads and edits are disabled.</p>
      )}
      {!batch && !opening && <BatchQueue />}
      {canUpload && !opening && (!batch || showUpload) && <UploadPanel projects={projects} onUploaded={onUploaded} onError={setError} />}

      {batch && (
        <section className="tf-panel px-4 py-3" aria-live="polite">
          <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
            <div className="min-w-0">
              <a href="/upload" className="tf-link" onClick={(event) => { if (formDirty && !window.confirm(DISCARD)) event.preventDefault(); }}>← All uploads</a>
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
            <p className="tf-notice mt-3" role="status">
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
              onChanged={(next) => {
                setManifest(next);
                setImageVersion((v) => v + 1);
                refresh(batch.id).catch((e) => setError(e.message));
              }}
              onOpenTicket={(ticketId) => {
                const index = tickets.findIndex((t) => t.id === ticketId);
                if (index >= 0) selectTicket(index);
              }} />
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
          {queueOpen && <section className="tf-panel tf-review-pane" aria-label="Ticket queue">
            <div className="border-b border-[#e7eaee] p-2.5">
              <div className="flex items-center justify-between gap-2">
                <h2 className="text-xs font-bold text-[#3c4b5b]">Ticket queue</h2>
                <button type="button" className="tf-link text-[11px]" onClick={() => setQueueOpen(false)}>Hide</button>
              </div>
              <div className="tf-segmented mt-2 flex" role="group" aria-label="Filter ticket queue">
                {(["pending", "flagged", "all"] as const).map((filter) => (
                  <button type="button" key={filter} className="flex-1" aria-pressed={reviewFilter === filter} onClick={() => setReviewFilter(filter)}>
                    {filter === "pending" ? "To review" : filter === "flagged" ? "Flagged" : "All"}
                  </button>
                ))}
              </div>
              <label htmlFor="queue-search" className="sr-only">Search tickets in queue</label>
              <input id="queue-search" type="search" value={queueSearch} onChange={(e) => setQueueSearch(e.target.value)}
                placeholder="Number, supplier, material…" className="tf-field mt-2 w-full !min-h-0 !py-1.5 !text-xs" />
              <p className="mt-1.5 text-[11px] text-[#7a8794]">{queue.length} matching ticket(s) · {tickets.length} total</p>
            </div>
            {queue.length === 0 ? (
              <div className="p-3 text-xs text-[#526273]">
                No tickets match the current view.
                {queueSearch && <button type="button" className="tf-link ml-2" onClick={() => setQueueSearch("")}>Clear search</button>}
                <button type="button" className="tf-link ml-2" onClick={() => setReviewFilter("all")}>Show all</button>
              </div>
            ) : (
              <div className="tf-review-pane-scroll max-h-72 divide-y divide-[#edf0f2] overflow-auto xl:max-h-none">
                {queue.map(({ t, index }) => (
                  <button type="button" key={t.id} onClick={() => selectTicket(index)}
                    aria-current={selected === index ? "true" : undefined}
                    className={`tf-queue-row block w-full border-l-[3px] px-2.5 py-2 text-left ${selected === index ? "border-[#df702f] bg-[#fff8f2]" : "border-transparent hover:bg-[#f8f9fa]"}`}>
                    <span className="flex items-center justify-between gap-1.5">
                      <span className="min-w-0 truncate text-xs font-bold tabular-nums text-[#1d2b38]">#{index + 1} · {t.ticket_number || "No number yet"}</span>
                      <span className={`tf-status shrink-0 !px-1 !py-0.5 !text-[9px] ${STATUS_STYLE[t.status] ?? ""}`}>{STATUS_LABEL[t.status] ?? t.status}</span>
                    </span>
                    <span className="mt-0.5 block truncate text-[11px] text-[#7a8794]">{t.flag_note || `${t.material_type || "Material not entered"} · ${t.tonnage ?? "—"} t`}</span>
                  </button>
                ))}
              </div>
            )}
          </section>}

          <section className="tf-panel tf-review-pane flex flex-col">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[#e7eaee] px-3 py-2">
              <div className="min-w-0">
                <h2 className="text-[13px] font-bold text-[#1d2b38]">Original scanned ticket</h2>
                <p className="text-[11px] text-[#657182]">Ticket {selected + 1} of {tickets.length} · Page {current.source_page}, crop {current.position + 1}</p>
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
              <p className="text-[11px] text-[#7a8794]">Click to zoom on a spot, drag to pan. Zoom and rotation carry to the next ticket.</p>
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
            previous={previousValues(tickets, selected)}
            onChange={(updated) => setTickets((previous) => previous.map((t) => t.id === updated.id ? updated : t))}
            onAdvance={advance}
            onAccepted={onAccepted}
            onDirtyChange={setFormDirty}
            onError={setError}
            onStale={onStale}
          />
        </section>
      )}

      {help && <div className="tf-dialog-scrim" onClick={() => setHelp(false)}>
        <div role="dialog" aria-modal="true" aria-label="Keyboard shortcuts" className="tf-dialog" onClick={(e) => e.stopPropagation()}>
          <div className="tf-panel-heading">
            <h2 className="tf-panel-title">Keyboard shortcuts</h2>
            <button type="button" className="tf-link" onClick={() => setHelp(false)} autoFocus>Close</button>
          </div>
          <table className="tf-table"><tbody>
            {SHORTCUTS.map(([keys, what]) => <tr key={keys}><td className="whitespace-nowrap"><kbd className="tf-kbd">{keys}</kbd></td><td>{what}</td></tr>)}
          </tbody></table>
        </div>
      </div>}
    </div>
  );
}
