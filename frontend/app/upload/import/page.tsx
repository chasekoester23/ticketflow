"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  DeliveryImport, ImportField, ImportHistoryRow, ImportPreviewRow, ImportUnit, Project, UNIT_LABEL,
  commitDeliveryImport, deliveryImportFileUrl, getDeliveryImport, listDeliveryImports, listProjects, mapDeliveryImport,
  uploadDeliverySpreadsheet,
} from "../../../lib/api";
import { useAccount } from "../../account-context";
import { useProject } from "../../project-context";

type Step = "file" | "map" | "preview" | "done";
const STEPS: { key: Step; label: string }[] = [
  { key: "file", label: "Choose file" }, { key: "map", label: "Map columns" }, { key: "preview", label: "Check rows" }, { key: "done", label: "Imported" },
];
const UNITS: { value: ImportUnit; label: string }[] = [
  ...Object.entries(UNIT_LABEL).map(([value, u]) => ({ value: value as ImportUnit, label: u.long })),
  { value: "lb", label: "pounds (converted to tons, the pounds kept)" },
];
const ACTION_LABEL: Record<ImportPreviewRow["action"], { text: string; style: string }> = {
  import: { text: "Ready", style: "tf-status-open" },
  duplicate: { text: "Duplicate", style: "tf-status-attention" },
  problem: { text: "Problem", style: "tf-status-error" },
};

const stepOf = (imp: DeliveryImport | null): Step =>
  !imp ? "file" : imp.status === "imported" ? "done" : imp.status === "mapped" ? "preview" : "map";

/**
 * A supplier's delivery export (CSV or Excel) into the normal review: each row becomes an
 * unapproved ticket that a person checks and approves as usual. Nothing is approved,
 * priced or totalled here.
 */
export default function ImportSpreadsheetPage() {
  const { can } = useAccount();
  const chosen = useProject();
  const [imp, setImp] = useState<DeliveryImport | null>(null);
  const [step, setStep] = useState<Step>("file");
  const [projects, setProjects] = useState<Project[]>([]);
  const [history, setHistory] = useState<ImportHistoryRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const show = useCallback((next: DeliveryImport, at?: Step) => {
    setImp(next);
    setStep(at ?? stepOf(next));
    window.history.replaceState(null, "", `/upload/import?id=${next.id}`);
  }, []);
  const reloadHistory = useCallback(() => listDeliveryImports().then(setHistory).catch(() => setHistory([])), []);

  useEffect(() => {
    listProjects().then(setProjects).catch((e) => setError(e.message));
    reloadHistory();
    const id = Number(new URLSearchParams(window.location.search).get("id"));
    if (id) getDeliveryImport(id).then((found) => show(found)).catch((e) => setError(e.message));
  }, [reloadHistory, show]);

  async function run<T>(action: () => Promise<T>): Promise<T | undefined> {
    setBusy(true);
    setError(null);
    try {
      return await action();
    } catch (e) {
      setError((e as Error).message);
      return undefined;
    } finally {
      setBusy(false);
    }
  }

  const startOver = () => {
    setImp(null);
    setStep("file");
    setError(null);
    window.history.replaceState(null, "", "/upload/import");
  };

  return <div className="space-y-3 pb-6">
    <div className="tf-page-heading">
      <div>
        <a href="/upload" className="tf-link text-xs">← Document Intake</a>
        <h1 className="tf-page-title">Import a delivery spreadsheet</h1>
        <p className="tf-page-description">A supplier&apos;s CSV or Excel export of deliveries. Each row becomes a ticket waiting for review, checked and approved like a scanned one.</p>
      </div>
      <div className="tf-status tf-status-attention">Human verification required</div>
    </div>

    <ol className="flex flex-wrap gap-2 text-xs" aria-label="Import steps">
      {STEPS.map((s, i) => <li key={s.key} aria-current={s.key === step ? "step" : undefined}
        className={`rounded border px-2.5 py-1 ${s.key === step ? "border-[#e66b2d] bg-[#fff7f0] font-semibold text-[#8a3f14]" : "border-[#dce1e6] bg-white text-[#657182]"}`}>
        {i + 1}. {s.label}
      </li>)}
    </ol>

    {error && <div role="alert" className="rounded border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-900">
      {error} <button type="button" className="ml-3 font-semibold underline" onClick={() => setError(null)}>Dismiss</button>
    </div>}
    {!can("tickets.upload") && <p className="tf-notice" role="note">Your role is view-only. You can see past imports and download their files, but not import.</p>}

    {step === "file" && can("tickets.upload") &&
      <ChooseFile busy={busy} onFile={(file) => run(async () => { show(await uploadDeliverySpreadsheet(file), "map"); reloadHistory(); })} />}
    {step === "map" && imp && <MapColumns imp={imp} projects={projects} defaultProject={chosen.project?.status === "active" ? chosen.projectId : ""}
      busy={busy} onCancel={startOver}
      onPreview={(body) => run(async () => show(await mapDeliveryImport(imp.id, body), "preview"))} />}
    {step === "preview" && imp?.preview && <Preview imp={imp} busy={busy} canImport={can("tickets.edit")}
      onBack={() => setStep("map")}
      onImport={(body) => run(async () => { show(await commitDeliveryImport(imp.id, body), "done"); reloadHistory(); })} />}
    {step === "done" && imp?.result && <Done imp={imp} onAnother={startOver} />}

    <History rows={history} onOpen={(id) => run(async () => show(await getDeliveryImport(id)))} />
  </div>;
}

function ChooseFile({ busy, onFile }: { busy: boolean; onFile: (file: File) => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);
  return <section className="tf-panel">
    <div className="tf-panel-heading">
      <div>
        <h2 className="tf-panel-title">1. Choose the supplier&apos;s file</h2>
        <p className="tf-panel-description">CSV or Excel (.xlsx), up to 10 MB and 2,000 rows · The first sheet of a workbook is read · Formulas are never calculated</p>
      </div>
    </div>
    <div className="p-5">
      <div role="button" tabIndex={0} aria-label="Select a delivery spreadsheet"
        onClick={() => input.current?.click()}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.current?.click(); } }}
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => { e.preventDefault(); setDragOver(false); const f = e.dataTransfer.files[0]; if (f) onFile(f); }}
        className={`flex min-h-36 cursor-pointer flex-col items-center justify-center rounded border-2 border-dashed p-6 text-center outline-none focus-visible:ring-2 focus-visible:ring-[#cf6837] ${dragOver ? "border-[#e66b2d] bg-[#fff7f0]" : "border-[#cfd6de] bg-[#f7f8f9] hover:border-[#8f9cab] hover:bg-[#f1f3f5]"}`}>
        <span className="mb-2 flex h-10 w-10 items-center justify-center rounded border border-[#dce1e6] bg-white text-xl text-[#657182]" aria-hidden="true">↑</span>
        <span className="text-sm font-semibold text-[#263442]">{busy ? "Reading the file…" : "Drop the spreadsheet here, or browse files"}</span>
        <span className="mt-1 text-xs text-[#657182]">The original file is kept with the import</span>
        <input ref={input} type="file" accept=".csv,.txt,.tsv,.xlsx" className="hidden" data-testid="import-file"
          onClick={(e) => e.stopPropagation()}
          onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; if (f) onFile(f); }} />
      </div>
    </div>
  </section>;
}

function MapColumns({ imp, projects, defaultProject, busy, onCancel, onPreview }: {
  imp: DeliveryImport; projects: Project[]; defaultProject: string; busy: boolean; onCancel: () => void;
  onPreview: (body: Parameters<typeof mapDeliveryImport>[1]) => void;
}) {
  const start = imp.mapping ?? imp.suggested?.mapping ?? {};
  const [columns, setColumns] = useState<Partial<Record<ImportField, number>>>(start);
  const [projectId, setProjectId] = useState(imp.project_id ? String(imp.project_id) : defaultProject);
  const [supplier, setSupplier] = useState(imp.defaults?.supplier ?? "");
  const [unit, setUnit] = useState<string>(imp.defaults?.unit ?? imp.suggested?.unit ?? "");
  const project = projects.find((p) => String(p.id) === projectId);
  const [job, setJob] = useState(imp.defaults?.job_location ?? "");
  const [headerRow, setHeaderRow] = useState(imp.header_row);
  const used = new Map(Object.entries(columns).map(([field, index]) => [index, field]));
  const missing = imp.fields.filter((f) => f.required && columns[f.key] === undefined).map((f) => f.label);
  const needsUnit = columns.unit === undefined && !unit;
  const needsSupplier = columns.supplier === undefined && !supplier.trim();

  function choose(field: ImportField, value: string) {
    setColumns((previous) => {
      const next = { ...previous };
      if (value === "") delete next[field];
      else next[field] = Number(value);
      return next;
    });
  }

  return <section className="tf-panel" aria-label="Map columns">
    <div className="tf-panel-heading">
      <div>
        <h2 className="tf-panel-title">2. Which column is which?</h2>
        <p className="tf-panel-description"><strong>{imp.original_name}</strong> · {imp.rows} row{imp.rows === 1 ? "" : "s"} under the header in row {imp.header_row} · Suggested from the column names: check each one</p>
      </div>
      <button type="button" className="tf-link" onClick={onCancel}>Choose another file</button>
    </div>
    <div className="grid gap-5 p-5 lg:grid-cols-[minmax(0,1fr)_320px]">
      <div className="space-y-4">
        <div className="grid gap-3 sm:grid-cols-2">
          {imp.fields.map((f) => <label key={f.key} className="tf-field-label">{f.label}{f.required ? " (required)" : ""}
            <select className="tf-field w-full" aria-label={`Column for ${f.label}`} value={columns[f.key] ?? ""}
              onChange={(e) => choose(f.key, e.target.value)}>
              <option value="">{f.required ? "Choose a column…" : "Not in this file"}</option>
              {(imp.columns ?? []).map((c) => <option key={c.index} value={c.index}
                disabled={used.has(c.index) && used.get(c.index) !== f.key}>{c.header}</option>)}
            </select>
          </label>)}
        </div>
        {imp.sample && imp.sample.length > 0 && <div className="tf-table-wrap rounded border border-[#e3e7eb]">
          <table className="tf-table" aria-label="First rows of the file">
            <thead><tr>{(imp.columns ?? []).map((c) => <th key={c.index}>{c.header}</th>)}</tr></thead>
            <tbody>{imp.sample.map((row, i) => <tr key={i}>{(imp.columns ?? []).map((c) => <td key={c.index}>{row[c.index] ?? ""}</td>)}</tr>)}</tbody>
          </table>
        </div>}
        {(imp.header_candidates?.length ?? 0) > 1 && <label className="tf-field-label">Header row
          <select className="tf-field w-full sm:w-auto" value={headerRow} onChange={(e) => setHeaderRow(Number(e.target.value))}>
            {imp.header_candidates!.map((c) => <option key={c.row} value={c.row}>Row {c.row}: {c.cells.filter(Boolean).join(", ").slice(0, 80)}</option>)}
          </select>
          {headerRow !== imp.header_row && <span className="font-normal text-[#8a5a12]">The columns change with the header row: preview to see them.</span>}
        </label>}
      </div>
      <div className="flex flex-col justify-between gap-4">
        <div className="space-y-3">
          <label className="tf-field-label">Project
            <select className="tf-field w-full" value={projectId} onChange={(e) => setProjectId(e.target.value)}>
              <option value="">Select a project...</option>
              {projects.filter((p) => p.status === "active").map((p) => <option key={p.id} value={p.id}>{p.code} · {p.name}</option>)}
            </select>
          </label>
          <label className="tf-field-label">Supplier{columns.supplier !== undefined ? " (when a row leaves it blank)" : ""}
            <input className="tf-field w-full" value={supplier} onChange={(e) => setSupplier(e.target.value)} maxLength={200} placeholder="Who sent this file" />
          </label>
          <label className="tf-field-label">Unit{columns.unit !== undefined ? " (when a row leaves it blank)" : ""}
            <select className="tf-field w-full" value={unit} onChange={(e) => setUnit(e.target.value)}>
              <option value="">{columns.unit !== undefined ? "From the unit column" : "Choose the unit…"}</option>
              {UNITS.map((u) => <option key={u.value} value={u.value}>{u.label}</option>)}
            </select>
          </label>
          <label className="tf-field-label">Job{columns.job_location !== undefined ? " (when a row leaves it blank)" : ""}
            <input className="tf-field w-full" value={job} onChange={(e) => setJob(e.target.value)} maxLength={200}
              placeholder={project?.job_location ?? project?.name ?? "Job or delivery location"} />
            {project && !job && (project.job_location || project.name) && <button type="button" className="tf-link mt-1 text-left text-xs font-normal"
              onClick={() => setJob(project.job_location || project.name)}>Use “{project.job_location || project.name}”</button>}
          </label>
          {(missing.length > 0 || needsUnit || needsSupplier) && <p className="text-xs text-[#8a5a12]" role="note">
            {missing.length > 0 && <>Choose the column for {missing.join(" and ")}. </>}
            {needsUnit && <>Choose the unit, or the column that says it. </>}
            {needsSupplier && <>No supplier yet: rows without one will need it entered.</>}
          </p>}
        </div>
        <button type="button" className="tf-primary-button w-full" disabled={busy || missing.length > 0 || needsUnit}
          onClick={() => onPreview({
            header_row: headerRow, columns, project_id: projectId ? Number(projectId) : null,
            supplier: supplier.trim() || null, unit: (unit || null) as ImportUnit | null, job_location: job.trim() || null,
          })}>{busy ? "Checking rows…" : "Preview the rows"}</button>
      </div>
    </div>
  </section>;
}

function quantityOf(row: ImportPreviewRow) {
  if (row.quantity === null) return "—";
  const shown = `${Number(row.quantity).toLocaleString(undefined, { maximumFractionDigits: 3 })} ${UNIT_LABEL[row.unit]?.short ?? row.unit}`;
  return row.source_unit === "lb" ? `${shown} (${Number(row.source_quantity).toLocaleString()} lb)` : shown;
}

function Preview({ imp, busy, canImport, onBack, onImport }: {
  imp: DeliveryImport; busy: boolean; canImport: boolean; onBack: () => void;
  onImport: (body: Parameters<typeof commitDeliveryImport>[1]) => void;
}) {
  const preview = imp.preview!;
  const [choices, setChoices] = useState<Record<number, "skip" | "import">>({});
  const [withProblems, setWithProblems] = useState(false);
  const [filter, setFilter] = useState<"all" | ImportPreviewRow["action"]>("all");
  const coming = useMemo(() => preview.rows.filter((r) => (r.errors.length ? withProblems : true)
    && (!r.duplicate || choices[r.row] === "import")).length, [preview.rows, choices, withProblems]);
  const shown = preview.rows.filter((r) => filter === "all" || r.action === filter);
  const { counts } = preview;
  const duplicates = counts.duplicates_existing + counts.duplicates_in_file;

  return <section className="tf-panel" aria-label="Preview">
    <div className="tf-panel-heading">
      <div>
        <h2 className="tf-panel-title">3. Check the rows</h2>
        <p className="tf-panel-description">{imp.original_name}{imp.project_code ? ` · ${imp.project_code} · ${imp.project_name}` : " · No project"} · Checked with the same rules as the review screen</p>
      </div>
      <button type="button" className="tf-link" onClick={onBack}>Change columns</button>
    </div>
    <div className="space-y-3 p-5">
      <div className="flex flex-wrap gap-2 text-xs" data-testid="import-counts" role="group" aria-label="Show rows">
        {([["all", `All ${counts.rows}`], ["import", `${counts.ready} ready`], ["duplicate", `${duplicates} duplicate${duplicates === 1 ? "" : "s"}`],
          ["problem", `${counts.problems} with problems`]] as const).map(([key, label]) =>
          <button key={key} type="button" aria-pressed={filter === key} onClick={() => setFilter(key)}
            className={`rounded border px-2.5 py-1 ${filter === key ? "border-[#1d2b38] bg-[#1d2b38] text-white" : "border-[#dce1e6] bg-white text-[#354454]"}`}>{label}</button>)}
      </div>
      {duplicates > 0 && <p className="tf-notice" role="note">Duplicates are skipped unless you choose otherwise: the same supplier and ticket number is already on record ({counts.duplicates_existing}) or earlier in this file ({counts.duplicates_in_file}).</p>}
      <div className="tf-table-wrap max-h-[60vh] overflow-auto rounded border border-[#e3e7eb]">
        <table className="tf-table" aria-label="Rows to import">
          <thead><tr><th>Row</th><th>Status</th><th>Ticket</th><th>Date</th><th>Material</th><th>Quantity</th><th>Supplier</th><th>Job</th><th>Truck</th><th>What to check</th></tr></thead>
          <tbody>{shown.map((r) => <tr key={r.row} data-testid={`row-${r.row}`}>
            <td>{r.row}</td>
            <td><span className={`tf-status ${ACTION_LABEL[r.action].style}`}>{ACTION_LABEL[r.action].text}</span></td>
            <td>{r.ticket_number ?? "—"}</td><td>{r.delivery_date ?? "—"}</td><td>{r.material_type ?? "—"}</td>
            <td className="whitespace-nowrap">{quantityOf(r)}</td><td>{r.supplier ?? "—"}</td><td>{r.job_location ?? "—"}</td><td>{r.truck_number ?? ""}</td>
            <td className="min-w-[240px] text-xs">
              {r.errors.map((e) => <p key={e} className="text-[#a73631]">{e}</p>)}
              {r.warnings.map((w) => <p key={w} className="text-[#995a18]">{w}</p>)}
              {r.duplicate && <div className="text-[#995a18]">
                <p>{r.duplicate.message}{r.duplicate.kind === "existing" && <> <a className="tf-link" href={`/upload?batch=${r.duplicate.batch_id}&ticket=${r.duplicate.ticket_id}`} target="_blank" rel="noreferrer">Open it</a></>}</p>
                <select className="tf-field mt-1 !min-h-0 !py-0.5 !text-xs" aria-label={`Row ${r.row} duplicate`} value={choices[r.row] ?? "skip"}
                  onChange={(e) => setChoices((c) => ({ ...c, [r.row]: e.target.value as "skip" | "import" }))}>
                  <option value="skip">Skip this row</option>
                  <option value="import">Import anyway, for review</option>
                </select>
              </div>}
            </td>
          </tr>)}</tbody>
        </table>
      </div>
      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-[#e9edf0] pt-3">
        <label className="flex items-start gap-2 text-xs text-[#334355]">
          <input type="checkbox" checked={withProblems} onChange={(e) => setWithProblems(e.target.checked)} disabled={!counts.problems} />
          <span><strong>Bring rows with problems in for review</strong><span className="block text-[#5f6c7b]">A value that couldn&apos;t be read is left blank, to be entered on the review screen. Otherwise these rows are left out: fix the file and import it again.</span></span>
        </label>
        <div className="flex items-center gap-3">
          <span className="text-xs text-[#657182]">Nothing is approved by importing.</span>
          <button type="button" className="tf-primary-button" disabled={busy || !canImport || coming === 0}
            onClick={() => onImport({ duplicates: choices, include_problem_rows: withProblems, revision: imp.revision })}>
            {busy ? "Importing…" : `Import ${coming} ticket${coming === 1 ? "" : "s"} for review`}
          </button>
        </div>
      </div>
      {!canImport && <p className="text-xs text-[#8a5a12]">Your role can check the preview but not import it.</p>}
    </div>
  </section>;
}

function Done({ imp, onAnother }: { imp: DeliveryImport; onAnother: () => void }) {
  const result = imp.result!;
  return <section className="tf-panel border-[#cce4d2] bg-[#f3f9f4]" data-testid="import-done">
    <div className="space-y-2 p-5">
      <h2 className="text-sm font-semibold text-[#216c43]">4. {result.imported} ticket{result.imported === 1 ? "" : "s"} imported from {imp.original_name}, waiting for review</h2>
      <p className="text-xs text-[#425060]">{result.skipped_duplicates} duplicate{result.skipped_duplicates === 1 ? "" : "s"} skipped · {result.error_rows} row{result.error_rows === 1 ? "" : "s"} left out for a problem. Each ticket is checked and approved like a scanned one; approval locks its price.</p>
      <div className="flex flex-wrap gap-2 pt-1">
        <a className="tf-primary-button" href={`/upload?batch=${result.batch_id}`}>Review the imported tickets</a>
        <a className="tf-secondary-button" href={deliveryImportFileUrl(imp.id)}>Download the original file</a>
        <button type="button" className="tf-secondary-button" onClick={onAnother}>Import another file</button>
      </div>
    </div>
  </section>;
}

function History({ rows, onOpen }: { rows: ImportHistoryRow[] | null; onOpen: (id: number) => void }) {
  if (!rows || rows.length === 0) return null;
  return <section className="tf-panel" aria-label="Import history">
    <div className="tf-panel-heading"><div><h2 className="tf-panel-title">Past imports</h2>
      <p className="tf-panel-description">Every spreadsheet uploaded, with what its import did and the original file</p></div></div>
    <div className="tf-table-wrap">
      <table className="tf-table">
        <thead><tr><th>File</th><th>Project</th><th>Uploaded</th><th>Rows</th><th>Imported</th><th>Duplicates skipped</th><th>Left out</th><th></th></tr></thead>
        <tbody>{rows.map((r) => <tr key={r.id}>
          <td><a className="tf-link" href={deliveryImportFileUrl(r.id)}>{r.original_name}</a></td>
          <td>{r.project_code ?? "—"}</td>
          <td className="whitespace-nowrap">{new Date(r.created_at).toLocaleDateString()}{r.created_by ? ` · ${r.created_by}` : ""}</td>
          <td>{r.rows}</td>
          <td>{r.status === "imported" ? r.imported : "—"}</td>
          <td>{r.status === "imported" ? r.skipped_duplicates : "—"}</td>
          <td>{r.status === "imported" ? r.error_rows : "—"}</td>
          <td className="whitespace-nowrap">{r.status === "imported" && r.batch_id
            ? <a className="tf-link" href={`/upload?batch=${r.batch_id}`}>Review tickets</a>
            : <button type="button" className="tf-link" onClick={() => onOpen(r.id)}>Continue</button>}</td>
        </tr>)}</tbody>
      </table>
    </div>
  </section>;
}
