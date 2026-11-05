"use client";

import { useCallback, useEffect, useState } from "react";
import {
  InvoiceDocument, InvoiceDraftLine, Project, addDraftLine, confirmDraftLine, getInvoiceDocument, invoiceOriginalUrl,
  invoicePageUrl, listInvoiceDocuments, listProjects, rejectDraftLine, saveDraftLine, uploadInvoiceDocument, voidInvoiceDocument,
} from "../../../lib/api";
import { useAccount } from "../../account-context";

const STATUS: Record<InvoiceDocument["status"], string> = {
  needs_review: "Draft lines to review", unsupported: "Not read automatically", failed: "Could not be read", voided: "Voided",
};
const STATE: Record<InvoiceDraftLine["state"], string> = {
  pending_review: "Draft", confirmed: "Confirmed", rejected: "Rejected",
};
const FIELDS: { key: keyof InvoiceDraftLine; label: string; type?: string }[] = [
  { key: "invoice_number", label: "Invoice number" },
  { key: "supplier_name", label: "Supplier" },
  { key: "material_name", label: "Material" },
  { key: "billed_quantity", label: "Billed quantity" },
  { key: "unit", label: "Unit, as printed" },
  { key: "billed_unit_price", label: "Rate" },
  { key: "billed_subtotal", label: "Material subtotal ($)" },
  { key: "date_from", label: "Delivery date from", type: "date" },
  { key: "date_to", label: "Delivery date through", type: "date" },
];

function LineForm({ line, document, projects, canManage, onDone, onError }: {
  line: InvoiceDraftLine; document: InvoiceDocument; projects: Project[]; canManage: boolean;
  onDone: () => Promise<void>; onError: (message: string | null) => void;
}) {
  const initial = Object.fromEntries(FIELDS.map((f) => [f.key, (line[f.key] as string | null) ?? ""]));
  const [values, setValues] = useState<Record<string, string>>(initial);
  const [project, setProject] = useState(line.project_id?.toString() ?? "");
  const [tickets, setTickets] = useState(line.ticket_list);
  const [busy, setBusy] = useState(false);
  const editable = canManage && line.state === "pending_review" && document.status !== "voided";

  const changes = () => {
    const body: Record<string, string | number | null> = {};
    FIELDS.forEach((f) => {
      if (values[f.key] !== initial[f.key]) body[f.key] = values[f.key].trim() || null;
    });
    if (project !== (line.project_id?.toString() ?? "")) body.project_id = project ? Number(project) : null;
    if (tickets !== line.ticket_list) body.ticket_list = tickets;
    return body;
  };
  const dirty = Object.keys(changes()).length > 0;

  async function run(action: () => Promise<void>) {
    setBusy(true);
    onError(null);
    try {
      await action();
      await onDone();
    } catch (exc) {
      onError((exc as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const save = async () => { if (dirty) await saveDraftLine(document.id, line.id, changes()); };

  return <article className="rounded border border-[#e2e7ec] p-4" aria-label={"Draft line " + line.id}>
    <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
      <p className="text-[12px] font-bold text-[#344352]">Page {line.page}</p>
      <span className={"tf-status " + (line.state === "confirmed" ? "tf-status-open" : line.state === "rejected" ? "" : "tf-status-attention")}>{STATE[line.state]}</span>
    </div>
    {line.source_text && <p className="mb-3 rounded bg-[#f6f8fa] px-3 py-2 font-mono text-[11px] text-[#526273]">Read from the PDF: {line.source_text}</p>}
    <div className="grid gap-3 sm:grid-cols-2">
      <label className="tf-field-label sm:col-span-2">Project
        <select className="tf-field w-full" value={project} disabled={!editable} onChange={(e) => setProject(e.target.value)}>
          <option value="">Select project</option>
          {projects.map((p) => <option key={p.id} value={p.id}>{p.code} · {p.name}</option>)}
        </select>
      </label>
      {FIELDS.map((f) => <label key={f.key} className="tf-field-label">{f.label}
        <input className="tf-field w-full" type={f.type ?? "text"} readOnly={!editable} value={values[f.key]}
          onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}/>
      </label>)}
      <label className="tf-field-label sm:col-span-2">Ticket numbers listed for this line (optional)
        <textarea className="tf-field h-24 w-full font-mono" spellCheck={false} readOnly={!editable} value={tickets}
          onChange={(e) => setTickets(e.target.value)}/>
      </label>
    </div>
    {line.warnings.length > 0 && <ul className="mt-3 rounded border border-[#f0d8ad] bg-[#fff9ed] px-3 py-2 text-[12px] text-[#754e20]">
      {line.warnings.map((w) => <li key={w}>{w}</li>)}
    </ul>}
    {line.blockers.length > 0 && <div className="mt-3 rounded border border-[#e2e7ec] bg-[#f6f8fa] px-3 py-2 text-[12px] text-[#526273]">
      <p className="font-bold">Needed before confirming</p>
      <ul className="mt-1 list-disc pl-5">{line.blockers.map((b) => <li key={b}>{b}</li>)}</ul>
    </div>}
    {line.comparison_id && <p className="mt-3 text-[12px]"><a className="tf-link" href="/invoices">Saved as comparison #{line.comparison_id} →</a></p>}
    {editable && <div className="mt-4 flex flex-wrap items-center gap-2">
      <button type="button" className="tf-secondary-button" disabled={busy || !dirty} onClick={() => void run(save)}>Save changes</button>
      <button type="button" className="tf-primary-button" disabled={busy || dirty || line.blockers.length > 0}
        title={dirty ? "Save your changes first" : undefined}
        onClick={() => void run(() => confirmDraftLine(document.id, line.id))}>Confirm line</button>
      <button type="button" className="tf-secondary-button" disabled={busy}
        onClick={() => void run(() => rejectDraftLine(document.id, line.id))}>Reject</button>
    </div>}
  </article>;
}

export default function InvoiceDocumentsPage() {
  const canManage = useAccount().can("invoices.manage");
  const [documents, setDocuments] = useState<InvoiceDocument[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [current, setCurrent] = useState<InvoiceDocument | null>(null);
  const [page, setPage] = useState(1);
  const [file, setFile] = useState<File | null>(null);
  const [project, setProject] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);

  const load = useCallback(async (id?: number) => {
    const list = await listInvoiceDocuments();
    setDocuments(list);
    if (id) {
      setCurrent(await getInvoiceDocument(id));
      setVersion((v) => v + 1);
    }
  }, []);

  useEffect(() => {
    const id = Number(new URLSearchParams(window.location.search).get("id")) || undefined;
    load(id).catch((e) => setError((e as Error).message));
    listProjects().then(setProjects).catch(() => undefined);
  }, [load]);

  async function upload(event: React.FormEvent) {
    event.preventDefault();
    if (!file || busy) return;
    setBusy(true);
    setError(null);
    try {
      const created = await uploadInvoiceDocument(file, project ? Number(project) : undefined);
      setFile(null);
      setPage(1);
      await load(created.id);
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function act(action: () => Promise<void>) {
    setError(null);
    try {
      await action();
      await load(current?.id);
    } catch (exc) {
      setError((exc as Error).message);
    }
  }

  return <div className="space-y-5">
    <div className="tf-page-heading">
      <div>
        <p className="tf-eyebrow">MATERIAL ACCOUNTING / INVOICE PDFS</p>
        <h1 className="tf-page-title">Invoice PDFs</h1>
        <p className="tf-page-description">Upload a supplier invoice, correct the draft lines beside the original, then confirm the ones to compare.</p>
      </div>
      <a href="/invoices" className="tf-secondary-button">Invoice comparison →</a>
    </div>
    <p className="rounded border border-[#e2e7ec] bg-white px-4 py-3 text-[12px] leading-5 text-[#47576a]">
      <strong>Drafts only — not approval for payment.</strong> Nothing counts until a manager confirms a line.
      Lines are read from the PDF's own text on this machine, with no AI and nothing sent elsewhere. Scanned invoices are entered by hand beside the original.
    </p>
    {error && <div role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">{error}</div>}
    {!canManage && <p className="tf-notice" role="note">Invoices are uploaded and confirmed by managers and owners. You can view them here.</p>}

    {canManage && <form onSubmit={(e) => void upload(e)} className="tf-panel grid items-end gap-4 p-5 sm:grid-cols-[1fr_1fr_auto]">
      <label className="tf-field-label">Invoice PDF
        <input className="tf-field w-full" type="file" accept=".pdf,application/pdf" onChange={(e) => setFile(e.target.files?.[0] ?? null)}/>
      </label>
      <label className="tf-field-label">Project
        <select className="tf-field w-full" value={project} onChange={(e) => setProject(e.target.value)}>
          <option value="">Select later</option>
          {projects.map((p) => <option key={p.id} value={p.id}>{p.code} · {p.name}</option>)}
        </select>
      </label>
      <button type="submit" className="tf-primary-button" disabled={!file || busy}>{busy ? "Uploading…" : "Upload invoice"}</button>
    </form>}

    <section className="tf-panel">
      <div className="tf-panel-heading"><h2 className="tf-panel-title">Uploaded invoices</h2></div>
      {documents.length ? <div className="tf-table-wrap"><table className="tf-table">
        <thead><tr><th>File</th><th>Pages</th><th>Status</th><th>Lines</th><th></th></tr></thead>
        <tbody>{documents.map((d) => <tr key={d.id}>
          <td className="font-semibold">{d.original_name}</td>
          <td className="tabular-nums">{d.page_count}</td>
          <td>{STATUS[d.status]}</td>
          <td className="tabular-nums">{d.lines.filter((l) => l.state === "confirmed").length} confirmed · {d.lines.filter((l) => l.state === "pending_review").length} draft</td>
          <td><button type="button" className="tf-link" onClick={() => { setPage(1); void act(() => load(d.id)); }}>Open</button></td>
        </tr>)}</tbody>
      </table></div> : <p className="tf-empty">No invoices uploaded yet.</p>}
    </section>

    {current && <section className="grid items-start gap-4 xl:grid-cols-2" aria-label="Invoice review">
      <div className="tf-panel">
        <div className="tf-panel-heading">
          <div><h2 className="tf-panel-title">{current.original_name}</h2>
            <p className="tf-panel-description">Page {page} of {current.page_count} · <a className="tf-link" href={invoiceOriginalUrl(current.id)} target="_blank" rel="noreferrer">Open original PDF</a></p></div>
          <div className="flex gap-2">
            <button type="button" className="tf-secondary-button" disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</button>
            <button type="button" className="tf-secondary-button" disabled={page >= current.page_count} onClick={() => setPage(page + 1)}>Next</button>
          </div>
        </div>
        <div className="bg-[#e6e9eb] p-4">
          <img src={invoicePageUrl(current.id, page)} alt={"Page " + page + " of " + current.original_name} className="w-full rounded border border-[#dce1e6] bg-white"/>
        </div>
      </div>
      <div className="space-y-4">
        {current.message && <div role="note" className="rounded border border-[#f0d8ad] bg-[#fff9ed] px-4 py-3 text-[12px] text-[#754e20]">{current.message}</div>}
        {current.unread_rows.length > 0 && <div className="rounded border border-[#e2e7ec] bg-white px-4 py-3 text-[12px] text-[#526273]">
          <p className="font-bold text-[#344352]">Not used as material lines. Check them against the original.</p>
          <ul className="mt-1 list-disc pl-5 font-mono text-[11px]">{current.unread_rows.map((row, i) => <li key={i}>{row}</li>)}</ul>
        </div>}
        {current.lines.map((line) => <LineForm key={line.id + "-" + version} line={line} document={current} projects={projects}
          canManage={canManage} onError={setError} onDone={() => load(current.id)}/>)}
        {canManage && current.status !== "voided" && <div className="flex flex-wrap gap-2">
          <button type="button" className="tf-secondary-button" onClick={() => void act(() => addDraftLine(current.id, page))}>Add a line by hand (page {page})</button>
          <button type="button" className="tf-secondary-button" onClick={() => void act(() => voidInvoiceDocument(current.id))}>Wrong file — void it</button>
        </div>}
      </div>
    </section>}
  </div>;
}
