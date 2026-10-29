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
  verified: "bg-emerald-100 text-emerald-800",
  skipped: "bg-slate-200 text-slate-600",
  failed: "bg-red-100 text-red-800",
};
const STATUS_LABEL: Record<string, string> = {
  needs_review: "Needs review",
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
    setError(null);
    setBusy(true);
    try {
      const b = await uploadBatch(files, layout, projectId ? Number(projectId) : undefined);
      setFiles([]);
      setSelected(0);
      setBatch(b);
      await refresh(b.id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const current = tickets[selected];
  const counts = {
    review: tickets.filter((t) => t.status === "needs_review" || t.status === "failed").length,
    verified: tickets.filter((t) => t.status === "verified").length,
  };

  return (
    <div className="space-y-6">
      {configured === false && (
        <div className="rounded border border-amber-300 bg-amber-50 px-4 py-2 text-sm text-amber-900">
          Extraction is not configured: set ANTHROPIC_API_KEY in backend/.env and restart the backend. Uploads will fail until then.
        </div>
      )}
      {error && <div className="rounded border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-800">{error}</div>}

      <section className="rounded-lg border border-slate-200 bg-white p-5">
        <h2 className="text-base font-semibold">1. Upload scanned tickets</h2>
        <div className="mt-4 flex flex-wrap items-start gap-6">
          <div
            onDragOver={(e) => {
              e.preventDefault();
              setDragOver(true);
            }}
            onDragLeave={() => setDragOver(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragOver(false);
              addFiles(e.dataTransfer.files);
            }}
            onClick={() => inputRef.current?.click()}
            className={`flex h-32 w-96 cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed text-sm ${
              dragOver ? "border-slate-700 bg-slate-100" : "border-slate-300 text-slate-500"
            }`}
          >
            <span>Drag PDFs or images here, or click to browse</span>
            <span className="mt-1 text-xs">PDF, JPG, PNG</span>
            <input
              ref={inputRef}
              type="file"
              multiple
              accept=".pdf,.jpg,.jpeg,.png"
              className="hidden"
              onChange={(e) => addFiles(e.target.files)}
            />
          </div>
          <div className="space-y-3">
            <label className="block text-sm font-medium">Project</label>
            <select
              value={projectId}
              onChange={(e) => setProjectId(e.target.value)}
              className="rounded border border-slate-300 bg-white px-3 py-2 text-sm"
            >
              <option value="">Select a project…</option>
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.code} · {p.name}
                </option>
              ))}
            </select>
            {projects.length === 0 && (
              <p className="text-xs text-amber-800">
                No projects yet. <a href="/projects" className="underline">Create one</a> first.
              </p>
            )}
            <label className="block text-sm font-medium">Page layout</label>
            <select
              value={layout}
              onChange={(e) => setLayout(e.target.value as "single" | "grid_2x2")}
              className="rounded border border-slate-300 bg-white px-3 py-2 text-sm"
            >
              <option value="grid_2x2">Four tickets per page (2×2)</option>
              <option value="single">One ticket per page</option>
            </select>
            <div className="text-sm text-slate-600">
              {files.length === 0 ? "No files selected" : `${files.length} file(s): ${files.map((f) => f.name).join(", ")}`}
            </div>
            <button
              disabled={!files.length || !projectId || busy}
              onClick={onUpload}
              className="rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
            >
              {busy ? "Uploading…" : "Upload and process"}
            </button>
          </div>
        </div>
      </section>

      {batch && (
        <section className="rounded-lg border border-slate-200 bg-white p-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-base font-semibold">
                Batch #{batch.id} · {BATCH_LABEL[batch.status] ?? batch.status}
              </h2>
              <p className="text-sm text-slate-500">
                {tickets.length} tickets · {counts.verified} verified · {counts.review} need review
              </p>
            </div>
            <div className="flex gap-2">
              {batch.status === "failed" && (
                <button
                  onClick={() => retryBatch(batch.id).then(() => refresh(batch.id)).catch((e) => setError(e.message))}
                  className="rounded border border-slate-300 px-3 py-2 text-sm"
                >
                  Retry failed
                </button>
              )}
              <a
                href={exportUrl(batch.id)}
                className="rounded bg-emerald-700 px-4 py-2 text-sm font-medium text-white"
              >
                Download Excel
              </a>
            </div>
          </div>
          <ul className="mt-3 space-y-1 text-sm">
            {batch.files.map((f) => (
              <li key={f.id} className="text-slate-600">
                {f.original_name}: {BATCH_LABEL[f.status] ?? f.status}
                {f.error && <span className="ml-2 text-red-700">({f.error})</span>}
              </li>
            ))}
          </ul>
        </section>
      )}

      {tickets.length > 0 && current && (
        <section className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_420px]">
          <div className="space-y-4">
            <div className="rounded-lg border border-slate-200 bg-white p-4">
              <div className="mb-2 flex items-center justify-between text-sm text-slate-500">
                <span>
                  Ticket {selected + 1} of {tickets.length} · page {current.source_page}, position {current.position + 1}
                </span>
                <span className="space-x-2">
                  <button disabled={selected === 0} onClick={() => setSelected(selected - 1)} className="rounded border px-2 py-1 disabled:opacity-40">
                    Prev
                  </button>
                  <button
                    disabled={selected >= tickets.length - 1}
                    onClick={() => setSelected(selected + 1)}
                    className="rounded border px-2 py-1 disabled:opacity-40"
                  >
                    Next
                  </button>
                </span>
              </div>
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={ticketImageUrl(current.id)} alt="Cropped delivery ticket" className="max-h-[70vh] w-full object-contain" />
            </div>

            <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
              <table className="w-full text-left text-sm">
                <thead className="bg-slate-100 text-xs uppercase text-slate-500">
                  <tr>
                    {["#", "Ticket", "Date", "Material", "Tons", "Supplier", "Job", "$/ton", "Cost", "Status"].map((h) => (
                      <th key={h} className="px-3 py-2">
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {tickets.map((t, i) => (
                    <tr
                      key={t.id}
                      onClick={() => setSelected(i)}
                      className={`cursor-pointer border-t border-slate-100 ${i === selected ? "bg-slate-100" : "hover:bg-slate-50"}`}
                    >
                      <td className="px-3 py-2">{i + 1}</td>
                      <td className="px-3 py-2">{t.ticket_number ?? "—"}</td>
                      <td className="px-3 py-2">{t.delivery_date ?? "—"}</td>
                      <td className="px-3 py-2">{t.material_type ?? "—"}</td>
                      <td className="px-3 py-2">{t.tonnage ?? "—"}</td>
                      <td className="px-3 py-2">{t.supplier ?? "—"}</td>
                      <td className="px-3 py-2">{t.job_location ?? "—"}</td>
                      <td className="px-3 py-2">{t.unit_price ?? "—"}</td>
                      <td className="px-3 py-2">{t.cost ? `$${t.cost}` : "—"}</td>
                      <td className="px-3 py-2">
                        <span className={`rounded px-2 py-0.5 text-xs font-medium ${STATUS_STYLE[t.status]}`}>{STATUS_LABEL[t.status]}</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <ReviewForm
            key={`${current.id}-${current.status}-${current.tonnage}-${current.ticket_number}-${current.unit_price}-${current.cost}`}
            ticket={current}
            onChange={(t) => setTickets((prev) => prev.map((x) => (x.id === t.id ? t : x)))}
            onAdvance={() => setSelected((s) => Math.min(s + 1, tickets.length - 1))}
            onError={setError}
          />
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
