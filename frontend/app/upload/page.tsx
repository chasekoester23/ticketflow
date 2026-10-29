"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Batch,
  Ticket,
  exportUrl,
  getBatch,
  getStatus,
  listProjects,
  Project,
  getTickets,
  patchTicket,
  retryBatch,
  ticketAction,
  ticketImageUrl,
  uploadBatch,
} from "../../lib/api";

const STATUS_STYLE: Record<string, string> = {
  needs_review: "bg-amber-100 text-amber-800",
  flagged: "bg-orange-100 text-orange-800",
  verified: "bg-emerald-100 text-emerald-800",
  skipped: "bg-slate-200 text-slate-600",
  failed: "bg-red-100 text-red-800",
};
const STATUS_LABEL: Record<string, string> = {
  needs_review: "Needs review",
  flagged: "Flagged",
  verified: "Verified",
  skipped: "Skipped",
  failed: "Failed",
};
const BATCH_LABEL: Record<string, string> = {
  uploaded: "Uploaded",
  processing: "Processing",
  ready_for_review: "Ready for review",
  failed: "Failed",
};

export default function Home() {
  const [layout, setLayout] = useState<"single" | "grid_2x2">("grid_2x2");
  const [files, setFiles] = useState<File[]>([]);
  const [batch, setBatch] = useState<Batch | null>(null);
  const [tickets, setTickets] = useState<Ticket[]>([]);
  const [selected, setSelected] = useState(0);
  const [formDirty, setFormDirty] = useState(false);
  const [reviewFilter, setReviewFilter] = useState<"pending" | "flagged" | "all">("pending");
  const [zoom, setZoom] = useState(1);
  const [rotation, setRotation] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");

  useEffect(() => {
    listProjects().then(setProjects).catch((e) => setError(e.message));
  }, []);

  useEffect(() => {
    getStatus().then((s) => setConfigured(s.extraction_configured)).catch(() => setConfigured(null));
  }, []);

  const refresh = useCallback(async (id: number) => {
    const [b, t] = await Promise.all([getBatch(id), getTickets(id)]);
    setBatch(b);
    setTickets(t);
    return b;
  }, []);

  // Reopen a previous batch via /upload?batch=ID (linked from the dashboard).
  useEffect(() => {
    const id = Number(new URLSearchParams(window.location.search).get("batch"));
    if (id) refresh(id).catch((e) => setError(e.message));
  }, [refresh]);

  // Poll the real backend status until processing actually finishes.
  useEffect(() => {
    if (!batch || !["uploaded", "processing"].includes(batch.status)) return;
    const timer = setInterval(() => refresh(batch.id).catch((e) => setError(e.message)), 1500);
    return () => clearInterval(timer);
  }, [batch, refresh]);

  const addFiles = (list: FileList | null) => list && setFiles((prev) => [...prev, ...Array.from(list)]);

  async function onUpload() {
    if (formDirty && !window.confirm("You have unsaved ticket corrections. Discard them and start a new upload?")) return;
    setError(null);
    setBusy(true);
    try {
      const b = await uploadBatch(files, layout, projectId ? Number(projectId) : undefined);
      setFiles([]);
      setSelected(0);
      setFormDirty(false);
      setReviewFilter("pending");
      setBatch(b);
      await refresh(b.id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const current = tickets[selected];
  const pending = (t: Ticket) => t.status === "needs_review" || t.status === "failed";
  const counts = {
    review: tickets.filter(pending).length,
    flagged: tickets.filter((t) => t.status === "flagged").length,
    verified: tickets.filter((t) => t.status === "verified").length,
    skipped: tickets.filter((t) => t.status === "skipped").length,
  };
  const done = counts.verified + counts.skipped;
  const progress = tickets.length ? (done / tickets.length) * 100 : 0;
  const queue = tickets.map((t, index) => ({ t, index })).filter(({ t }) =>
    reviewFilter === "all" || (reviewFilter === "flagged" ? t.status === "flagged" : pending(t))
  );

  function selectTicket(index: number) {
    if (index === selected) return;
    if (formDirty && !window.confirm("Discard unsaved changes to this ticket? Select Save changes to keep your corrections.")) return;
    setSelected(index);
    setFormDirty(false);
    setZoom(1);
    setRotation(0);
  }

  // After a successful server-side action, work through unreviewed tickets first.
  // Flagged items stay in their own queue to be revisited deliberately.
  function advance() {
    setFormDirty(false);
    const later = tickets.findIndex((t, i) => i > selected && pending(t));
    const earlier = later === -1 ? tickets.findIndex((t, i) => i < selected && pending(t)) : -1;
    if (later >= 0 || earlier >= 0) {
      setSelected(later >= 0 ? later : earlier);
      setZoom(1);
      setRotation(0);
    }
  }

  useEffect(() => {
    if (!formDirty) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [formDirty]);


  return (
    <div className="space-y-6 pb-14">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.15em] text-slate-500">Delivery operations / Ticket intake</p>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight text-slate-950">Upload & review tickets</h1>
          <p className="mt-1 text-sm text-slate-600">AI enters the information. You verify every delivery against the original ticket.</p>
        </div>
        <div className="rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-600">
          Human approval required
        </div>
      </div>

      {configured === false && (
        <div role="alert" className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
          Extraction is not configured. Set ANTHROPIC_API_KEY in backend/.env and restart the backend before uploading.
        </div>
      )}
      {error && (
        <div role="alert" className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-900">
          {error} <button type="button" className="ml-3 font-semibold underline" onClick={() => setError(null)}>Dismiss</button>
        </div>
      )}

      <section className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 px-5 py-4">
          <div>
            <h2 className="font-semibold text-slate-900">1. Upload delivery tickets</h2>
            <p className="mt-0.5 text-xs text-slate-500">PDF, JPG, or PNG · Documents are not approved automatically</p>
          </div>
          <a className="text-sm font-medium text-slate-700 underline-offset-2 hover:underline" href="/projects">Manage projects</a>
        </div>
        <div className="grid gap-5 p-5 lg:grid-cols-[minmax(0,1fr)_320px]">
          <div>
            <div
              role="button"
              tabIndex={0}
              aria-label="Select scanned ticket files"
              onClick={() => inputRef.current?.click()}
              onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); inputRef.current?.click(); } }}
              onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
              onDragLeave={() => setDragOver(false)}
              onDrop={(e) => { e.preventDefault(); setDragOver(false); addFiles(e.dataTransfer.files); }}
              className={dragOver ? "flex min-h-44 cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed border-orange-500 bg-orange-50 p-6 text-center outline-none focus-visible:ring-2 focus-visible:ring-orange-500" : "flex min-h-44 cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed border-slate-300 bg-slate-50 p-6 text-center outline-none transition hover:border-slate-500 hover:bg-slate-100 focus-visible:ring-2 focus-visible:ring-slate-800"}
            >
              <span className="mb-2 flex h-10 w-10 items-center justify-center rounded-lg border border-slate-200 bg-white text-xl text-slate-500" aria-hidden="true">↑</span>
              <span className="text-sm font-semibold text-slate-800">Drop scanned tickets here, or browse files</span>
              <span className="mt-1 text-xs text-slate-500">Single tickets or four tickets per page</span>
              <input ref={inputRef} type="file" multiple accept=".pdf,.jpg,.jpeg,.png" className="hidden"
                onChange={(e) => { addFiles(e.target.files); e.target.value = ""; }} />
            </div>
            {files.length > 0 && (
              <div className="mt-3 space-y-2">
                <div className="flex items-center justify-between text-xs text-slate-500">
                  <span>{files.length} selected file(s)</span>
                  <button type="button" className="font-medium text-slate-700 hover:underline" onClick={() => setFiles([])}>Clear all</button>
                </div>
                <ul className="max-h-32 space-y-1 overflow-auto text-xs text-slate-700">
                  {files.map((file, i) => (
                    <li key={i} className="flex items-center justify-between gap-3 rounded-md bg-slate-50 px-3 py-2">
                      <span className="min-w-0 truncate">{file.name}</span>
                      <button type="button" className="font-semibold text-slate-500 hover:text-red-700" aria-label={"Remove " + file.name}
                        onClick={() => setFiles((previous) => previous.filter((_, index) => index !== i))}>Remove</button>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
          <div className="flex flex-col justify-between gap-4">
            <div className="space-y-3">
              <label className="block text-sm font-medium text-slate-700">Project
                <select value={projectId} onChange={(e) => setProjectId(e.target.value)}
                  className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm text-slate-900 focus:border-slate-700 focus:outline-none">
                  <option value="">Select a project...</option>
                  {projects.map((p) => <option key={p.id} value={p.id}>{p.code} · {p.name}</option>)}
                </select>
              </label>
              {projects.length === 0 && <p className="text-xs text-amber-800">No projects yet. <a className="underline" href="/projects">Create one first</a>.</p>}
              <label className="block text-sm font-medium text-slate-700">Document layout
                <select value={layout} onChange={(e) => setLayout(e.target.value as "single" | "grid_2x2")}
                  className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm text-slate-900 focus:border-slate-700 focus:outline-none">
                  <option value="grid_2x2">Four tickets per page (2×2)</option>
                  <option value="single">One ticket per page</option>
                </select>
              </label>
            </div>
            <button type="button" disabled={!files.length || !projectId || busy} onClick={onUpload}
              className="w-full rounded-lg bg-slate-900 px-4 py-3 text-sm font-semibold text-white transition hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-40">
              {busy ? "Uploading..." : "Upload & process"}
            </button>
          </div>
        </div>
      </section>

      {batch && (
        <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm" aria-live="polite">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h2 className="font-semibold text-slate-900">Batch #{batch.id} · {BATCH_LABEL[batch.status] ?? batch.status}</h2>
              <p className="mt-1 text-sm text-slate-600">{tickets.length} tickets · {counts.verified} accepted · {counts.flagged} flagged · {counts.review} awaiting review</p>
            </div>
            <div className="flex flex-wrap gap-2">
              {batch.status === "failed" && (
                <button type="button" onClick={() => retryBatch(batch.id).then(() => refresh(batch.id)).catch((e) => setError(e.message))}
                  className="rounded-lg border border-slate-300 px-3 py-2 text-sm font-medium">Retry failed</button>
              )}
              <a className="rounded-lg border border-slate-300 px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50"
                href={exportUrl(batch.id)}>Export Excel</a>
            </div>
          </div>
          <ul className="mt-3 space-y-1 text-xs text-slate-500">
            {batch.files.map((file) => <li key={file.id}>
              <span className="font-medium text-slate-700">{file.original_name}</span>: {BATCH_LABEL[file.status] ?? file.status}
              {file.error && <span className="ml-1 text-red-700">({file.error})</span>}
            </li>)}
          </ul>
        </section>
      )}

      {tickets.length > 0 && current && (
        <section className="space-y-4" aria-label="Ticket review workspace">
          <div className="rounded-2xl border border-slate-200 bg-white px-5 py-4 shadow-sm">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <h2 className="text-lg font-semibold text-slate-900">2. Compare & accept each ticket</h2>
                <p className="mt-1 text-sm text-slate-600">The original scan stays beside the extracted fields. Nothing is verified until you accept it.</p>
              </div>
              <span className="text-sm font-semibold tabular-nums text-slate-800">{done} / {tickets.length} resolved</span>
            </div>
            <div className="mt-4 h-2 overflow-hidden rounded-full bg-slate-100" role="progressbar"
              aria-label="Tickets accepted or skipped" aria-valuenow={done} aria-valuemax={tickets.length} aria-valuemin={0}>
              <div className="h-full rounded-full bg-emerald-600 transition-all" style={{ width: progress + "%" }} />
            </div>
            <div className="mt-3 flex flex-wrap gap-x-5 gap-y-2 text-xs text-slate-600">
              <span><strong className="text-emerald-700">{counts.verified}</strong> accepted</span>
              <span><strong className="text-amber-700">{counts.review}</strong> to review</span>
              <span><strong className="text-orange-700">{counts.flagged}</strong> flagged for later</span>
              {counts.skipped > 0 && <span><strong>{counts.skipped}</strong> skipped</span>}
              {counts.review === 0 && counts.flagged > 0 && <span className="font-medium text-orange-800">Review flagged tickets before finishing.</span>}
              {counts.review === 0 && counts.flagged === 0 && <span className="font-medium text-emerald-800">All tickets have been resolved.</span>}
            </div>
          </div>

          <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1.15fr)_minmax(360px,0.85fr)]">
            <section className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm">
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 px-4 py-3">
                <div>
                  <h3 className="font-semibold text-slate-900">Original scanned ticket</h3>
                  <p className="mt-0.5 text-xs text-slate-500">
                    Ticket {selected + 1} of {tickets.length} · Page {current.source_page}, crop {current.position + 1}
                  </p>
                </div>
                <span className={"rounded-full px-2.5 py-1 text-xs font-semibold " + (STATUS_STYLE[current.status] ?? "bg-slate-100")}>
                  {STATUS_LABEL[current.status] ?? current.status}
                </span>
              </div>
              <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 bg-slate-50 px-4 py-2">
                <div className="flex items-center gap-2">
                  <button type="button" onClick={() => setZoom((v) => Math.max(1, Math.round((v - .25) * 100) / 100))}
                    disabled={zoom <= 1} className="rounded-md border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium disabled:opacity-40" aria-label="Zoom out">−</button>
                  <span className="w-12 text-center text-xs tabular-nums text-slate-600">{Math.round(zoom * 100)}%</span>
                  <button type="button" onClick={() => setZoom((v) => Math.min(3, Math.round((v + .25) * 100) / 100))}
                    disabled={zoom >= 3} className="rounded-md border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium disabled:opacity-40" aria-label="Zoom in">+</button>
                  <button type="button" onClick={() => setRotation((v) => (v + 90) % 360)}
                    className="rounded-md border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium">Rotate</button>
                </div>
                <a href={ticketImageUrl(current.id)} target="_blank" rel="noreferrer"
                  className="text-xs font-medium text-slate-700 underline-offset-2 hover:underline">Open full image</a>
              </div>
              <div className="flex min-h-[350px] max-h-[72vh] items-start justify-center overflow-auto bg-slate-100 p-4">
                <img src={ticketImageUrl(current.id)} alt={"Original scanned delivery ticket " + (current.ticket_number || String(current.id))}
                  className="h-auto max-h-none rounded-md border border-slate-200 bg-white shadow-sm"
                  style={{ width: zoom * 100 + "%", maxWidth: "none", transform: "rotate(" + rotation + "deg)" }} />
              </div>
              <div className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-100 px-4 py-3">
                <p className="text-xs text-slate-500">Zoom or open the original to inspect handwriting.</p>
                <div className="flex items-center gap-2">
                  <button type="button" disabled={selected === 0} onClick={() => selectTicket(selected - 1)}
                    className="rounded-lg border border-slate-300 px-3 py-1.5 text-sm disabled:opacity-40">Previous</button>
                  <button type="button" disabled={selected >= tickets.length - 1} onClick={() => selectTicket(selected + 1)}
                    className="rounded-lg border border-slate-300 px-3 py-1.5 text-sm disabled:opacity-40">Next</button>
                </div>
              </div>
            </section>

            <ReviewForm
              key={current.id + "-" + current.status + "-" + current.tonnage + "-" + current.ticket_number + "-" + current.unit_price + "-" + current.cost}
              ticket={current}
              onChange={(updated) => setTickets((previous) => previous.map((t) => t.id === updated.id ? updated : t))}
              onAdvance={advance}
              onDirtyChange={setFormDirty}
              onError={setError}
            />
          </div>

          <section className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm">
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 px-5 py-4">
              <div>
                <h3 className="font-semibold text-slate-900">Ticket queue</h3>
                <p className="mt-1 text-xs text-slate-500">Choose a ticket to inspect. Unsaved changes are protected.</p>
              </div>
              <div className="flex gap-1 rounded-lg bg-slate-100 p-1" role="group" aria-label="Filter ticket queue">
                {(["pending", "flagged", "all"] as const).map((filter) => (
                  <button type="button" key={filter} onClick={() => setReviewFilter(filter)}
                    className={reviewFilter === filter ? "rounded-md bg-white px-3 py-1.5 text-xs font-semibold text-slate-900 shadow-sm" : "rounded-md px-3 py-1.5 text-xs font-medium text-slate-600 hover:text-slate-900"}>
                    {filter === "pending" ? "To review" : filter === "flagged" ? "Flagged" : "All"}
                  </button>
                ))}
              </div>
            </div>
            {queue.length === 0 ? (
              <p className="p-6 text-sm text-slate-600">No tickets in this view. Select <strong>All</strong> to revisit accepted tickets.</p>
            ) : (
              <div className="max-h-80 overflow-auto divide-y divide-slate-100">
                {queue.map(({ t, index }) => (
                  <button type="button" key={t.id} onClick={() => selectTicket(index)}
                    aria-current={selected === index ? "true" : undefined}
                    className={selected === index ? "flex w-full flex-wrap items-center justify-between gap-3 border-l-4 border-slate-900 bg-slate-50 px-4 py-3 text-left" : "flex w-full flex-wrap items-center justify-between gap-3 border-l-4 border-transparent px-4 py-3 text-left transition hover:bg-slate-50"}>
                    <span className="min-w-0">
                      <span className="block text-sm font-semibold text-slate-900">#{index + 1} · {t.ticket_number || "Unrecognized number"}</span>
                      <span className="mt-0.5 block text-xs text-slate-500">{t.material_type || "Material not identified"} · {t.tonnage ?? "—"} tons</span>
                    </span>
                    <span className={"shrink-0 rounded-full px-2.5 py-1 text-xs font-semibold " + (STATUS_STYLE[t.status] ?? "bg-slate-100")}>
                      {STATUS_LABEL[t.status] ?? t.status}
                    </span>
                  </button>
                ))}
              </div>
            )}
          </section>
        </section>
      )}
    </div>
  );
}

const FIELDS: { key: keyof Ticket; label: string; type?: string; flagWords: string[] }[] = [
  { key: "ticket_number", label: "Ticket number", flagWords: ["Ticket number"] },
  { key: "delivery_date", label: "Delivery date", type: "date", flagWords: ["Date"] },
  { key: "material_type", label: "Material", flagWords: ["Material"] },
  { key: "tonnage", label: "Tonnage (tons)", type: "number", flagWords: ["Tonnage"] },
  { key: "supplier", label: "Supplier", flagWords: ["Supplier"] },
  { key: "job_location", label: "Job location", flagWords: ["Job location"] },
];

function ReviewForm({
  ticket,
  onChange,
  onAdvance,
  onError,
}: {
  ticket: Ticket;
  onChange: (t: Ticket) => void;
  onAdvance: () => void;
  onError: (m: string | null) => void;
}) {
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(FIELDS.map((f) => [f.key, (ticket[f.key] as string | null) ?? ""])),
  );
  const [saving, setSaving] = useState(false);
  const [override, setOverride] = useState(ticket.unit_price_override ?? "");
  const overrideDirty = override !== (ticket.unit_price_override ?? "");

  const dirty = overrideDirty || FIELDS.some((f) => values[f.key] !== ((ticket[f.key] as string | null) ?? ""));

  async function save(): Promise<Ticket | null> {
    const body: Record<string, string | null> = {};
    FIELDS.forEach((f) => {
      const orig = (ticket[f.key] as string | null) ?? "";
      if (values[f.key] !== orig) body[f.key] = values[f.key].trim() === "" ? null : values[f.key];
    });
    if (overrideDirty) body.unit_price_override = override.trim() === "" ? null : override;
    if (!Object.keys(body).length) return ticket;
    try {
      const t = await patchTicket(ticket.id, body);
      onChange(t);
      return t;
    } catch (e) {
      onError((e as Error).message);
      return null;
    }
  }

  async function run(fn: () => Promise<Ticket | null>, advance = false) {
    onError(null);
    setSaving(true);
    const t = await fn();
    setSaving(false);
    if (t && advance) onAdvance();
  }

  const flaggedFor = (words: string[]) => ticket.flags.some((fl) => words.some((w) => fl.startsWith(w)));

  return (
    <aside className="space-y-4 rounded-lg border border-slate-200 bg-white p-5 lg:sticky lg:top-6 lg:self-start">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold">Review ticket</h3>
        <span className={`rounded px-2 py-0.5 text-xs font-medium ${STATUS_STYLE[ticket.status]}`}>{STATUS_LABEL[ticket.status]}</span>
      </div>
      {ticket.error && <p className="text-sm text-red-700">{ticket.error}</p>}
      {ticket.flags.length > 0 && (
        <ul className="rounded border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
          {ticket.flags.map((f) => (
            <li key={f}>• {f}</li>
          ))}
        </ul>
      )}
      {ticket.duplicates.length > 0 && (
        <ul className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-900">
          <li className="font-medium">Possible duplicate</li>
          {ticket.duplicates.map((d) => (
            <li key={d}>• {d}</li>
          ))}
        </ul>
      )}
      {FIELDS.map((f) => (
        <div key={f.key}>
          <label className="mb-1 block text-xs font-medium text-slate-600">{f.label}</label>
          <input
            type={f.type === "number" ? "text" : f.type ?? "text"}
            inputMode={f.type === "number" ? "decimal" : undefined}
            value={values[f.key]}
            onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}
            className={`w-full rounded border px-3 py-2 text-sm ${
              flaggedFor(f.flagWords) ? "border-amber-400 bg-amber-50" : "border-slate-300"
            }`}
          />
        </div>
      ))}
      <div className="rounded bg-slate-50 p-3 text-sm">
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
        <label className="mt-2 block text-xs font-medium text-slate-600">Override rate for this ticket ($/ton, optional)</label>
        <input
          inputMode="decimal"
          value={override}
          onChange={(e) => setOverride(e.target.value)}
          className="mt-1 w-full rounded border border-slate-300 px-3 py-2 text-sm"
        />
      </div>
      <p className="text-xs text-slate-500">
        Verifying confirms the tonnage above matches the ticket image. Cost calculation requires confirmed tonnage.
      </p>
      <div className="flex flex-wrap gap-2">
        <button
          disabled={saving}
          onClick={() =>
            run(async () => {
              const saved = await save();
              return saved ? ticketAction(ticket.id, "verify").then((t) => (onChange(t), t)).catch((e) => (onError(e.message), null)) : null;
            }, true)
          }
          className="rounded bg-emerald-700 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
        >
          Verify{dirty ? " & save" : ""}
        </button>
        <button disabled={saving || !dirty} onClick={() => run(save)} className="rounded border border-slate-300 px-3 py-2 text-sm disabled:opacity-40">
          Save changes
        </button>
        <button
          disabled={saving}
          onClick={() => run(() => ticketAction(ticket.id, "skip").then((t) => (onChange(t), t)).catch((e) => (onError(e.message), null)), true)}
          className="rounded border border-slate-300 px-3 py-2 text-sm"
        >
          Skip
        </button>
      </div>
    </aside>
  );
}
