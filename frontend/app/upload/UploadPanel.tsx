"use client";

import { useRef, useState } from "react";
import { Batch, Project, uploadBatch } from "../../lib/api";

/** New uploads. Every selected file must pass or none are processed. */
export function UploadPanel({ projects, onUploaded, onError }: {
  projects: Project[];
  onUploaded: (batch: Batch) => void | Promise<void>;
  onError: (message: string | null) => void;
}) {
  const [layout, setLayout] = useState<"single" | "grid_2x2">("grid_2x2");
  const [intakeMode, setIntakeMode] = useState<"extract" | "manual">("manual");
  const [files, setFiles] = useState<File[]>([]);
  const [projectId, setProjectId] = useState("");
  const [expected, setExpected] = useState("");
  const [busy, setBusy] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  const addFiles = (list: FileList | null) => {
    if (!list) return;
    // FileList is live. Snapshot it BEFORE clearing the file input; React
    // may run a state updater after the input's FileList has become empty.
    const selected = Array.from(list);
    if (selected.length) setFiles((previous) => [...previous, ...selected]);
  };

  async function submit() {
    onError(null);
    setBusy(true);
    try {
      const batch = await uploadBatch(files, layout, projectId ? Number(projectId) : undefined, intakeMode,
        expected.trim() ? Number(expected) : undefined);
      setFiles([]);
      setExpected("");
      await onUploaded(batch);
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return <section className="tf-panel">
    <div className="tf-panel-heading">
      <div>
        <h2 className="tf-panel-title">Upload delivery tickets</h2>
        <p className="tf-panel-description">PDF, JPG, or PNG · All selected files must be valid; none are processed if any is rejected</p>
      </div>
      <a className="tf-link" href="/projects">Manage projects</a>
    </div>
    <div className="grid gap-5 p-5 lg:grid-cols-[minmax(0,1fr)_320px]">
      <div>
        <div
          role="button"
          tabIndex={0}
          aria-label="Select scanned ticket files"
          onClick={() => input.current?.click()}
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.current?.click(); } }}
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => { e.preventDefault(); setDragOver(false); addFiles(e.dataTransfer.files); }}
          className={`flex min-h-44 cursor-pointer flex-col items-center justify-center rounded border-2 border-dashed p-6 text-center outline-none focus-visible:ring-2 focus-visible:ring-[#cf6837] ${dragOver ? "border-[#e66b2d] bg-[#fff7f0]" : "border-[#cfd6de] bg-[#f7f8f9] hover:border-[#8f9cab] hover:bg-[#f1f3f5]"}`}
        >
          <span className="mb-2 flex h-10 w-10 items-center justify-center rounded border border-[#dce1e6] bg-white text-xl text-[#657182]" aria-hidden="true">↑</span>
          <span className="text-sm font-semibold text-[#263442]">Drop scanned tickets here, or browse files</span>
          <span className="mt-1 text-xs text-[#657182]">Single tickets or four tickets per page</span>
          <input ref={input} type="file" multiple accept=".pdf,.jpg,.jpeg,.png" className="hidden"
            onClick={(e) => e.stopPropagation()}
            onChange={(e) => { addFiles(e.target.files); e.target.value = ""; }} />
        </div>
        {files.length > 0 && <div className="mt-3 space-y-2">
          <div className="flex items-center justify-between text-xs text-[#657182]">
            <span>{files.length} selected file(s)</span>
            <button type="button" className="tf-link" onClick={() => setFiles([])}>Clear all</button>
          </div>
          <ul className="max-h-32 space-y-1 overflow-auto text-xs text-[#354454]">
            {files.map((file, i) => <li key={i} className="flex items-center justify-between gap-3 rounded bg-[#f7f8f9] px-3 py-2">
              <span className="min-w-0 truncate">{file.name}</span>
              <button type="button" className="font-semibold text-[#657182] hover:text-red-700" aria-label={"Remove " + file.name}
                onClick={() => setFiles((previous) => previous.filter((_, index) => index !== i))}>Remove</button>
            </li>)}
          </ul>
        </div>}
      </div>
      <div className="flex flex-col justify-between gap-4">
        <div className="space-y-3">
          <label className="tf-field-label">Project
            <select className="tf-field w-full" value={projectId} onChange={(e) => setProjectId(e.target.value)}>
              <option value="">Select a project...</option>
              {projects.map((p) => <option key={p.id} value={p.id}>{p.code} · {p.name}</option>)}
            </select>
          </label>
          {projects.length === 0 && <p className="text-xs text-amber-800">No projects yet. <a className="underline" href="/projects">Create one first</a>.</p>}
          <fieldset className="rounded border border-[#dce1e6] p-3">
            <legend className="px-1 text-xs font-bold text-[#425060]">How should these tickets be entered?</legend>
            <div className="mt-1 grid gap-2">
              <label className="flex cursor-pointer items-start gap-2 text-xs text-[#334355]">
                <input type="radio" name="intake-mode" value="manual" checked={intakeMode === "manual"} onChange={() => setIntakeMode("manual")} />
                <span><strong>Manual entry</strong><span className="mt-0.5 block text-[#5f6c7b]">Create blank fields beside each scan. No AI calls.</span></span>
              </label>
              <label className="flex cursor-pointer items-start gap-2 text-xs text-[#334355]">
                <input type="radio" name="intake-mode" value="extract" checked={intakeMode === "extract"} onChange={() => setIntakeMode("extract")} />
                <span><strong>AI extraction</strong><span className="mt-0.5 block text-[#5f6c7b]">Requires a configured provider key. Always review before approval.</span></span>
              </label>
            </div>
          </fieldset>
          <label className="tf-field-label">Document layout
            <select className="tf-field w-full" value={layout} onChange={(e) => setLayout(e.target.value as "single" | "grid_2x2")}>
              <option value="grid_2x2">Four tickets per page (2×2)</option>
              <option value="single">One ticket per page</option>
            </select>
          </label>
          <label className="tf-field-label">Paper tickets in this upload (optional)
            <input className="tf-field w-full" inputMode="numeric" pattern="[0-9]*" value={expected} onChange={(e) => setExpected(e.target.value)} />
            <span className="font-normal text-[#657182]">Count the paper. TicketFlow checks it against the tickets it finds.</span>
          </label>
        </div>
        <button type="button" disabled={!files.length || !projectId || busy} onClick={submit} className="tf-primary-button w-full">
          {busy ? "Uploading..." : intakeMode === "manual" ? "Queue for manual entry" : "Queue for AI extraction"}
        </button>
      </div>
    </div>
  </section>;
}
