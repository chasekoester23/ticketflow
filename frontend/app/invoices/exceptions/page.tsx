"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import {
  ApiError, BillingException, ExceptionEventRow, ExceptionFilters, ExceptionStatus, ExceptionsPage, downloadFile,
  exceptionHistory, exceptionsCsvUrl, listExceptions, reviewException,
} from "../../../lib/api";
import { useProject } from "../../project-context";

const STATUS_LABEL: Record<ExceptionStatus, string> = {
  open: "Open", investigating: "Investigating", disputed: "Disputed with supplier",
  recovered: "Credit received", resolved: "Resolved", dismissed: "Not an issue",
};
const SEVERITY_CLASS = { high: "tf-status-error", medium: "tf-status-attention", low: "" } as const;
const FIELD_LABEL: Record<string, string> = { status: "Status", assignee: "Owner", recovered_amount: "Credit recorded", note: "Note" };
const FILTER_KEYS = ["status", "category", "severity", "project_id", "assignee"] as const;

// Display only: server decimals are shown, never added up here.
function money(value: string | null, signedDiff = false) {
  if (value === null) return "—";
  const n = Number(value);
  const text = "$" + Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (!signedDiff) return n < 0 ? "−" + text : text;
  return n > 0 ? "+" + text : n < 0 ? "−" + text : text;
}

function Metric({ label, value, note, attention = false }: { label: string; value: string | number; note: string; attention?: boolean }) {
  return <div className={`min-w-0 border-b border-r border-[#e1e5e9] px-4 py-3 ${attention ? "bg-[#fffaf5]" : "bg-white"}`}>
    <p className="tf-stat-label">{label}</p>
    <p className={`mt-2 text-[24px] font-bold leading-none tabular-nums ${attention ? "text-[#a3511d]" : "text-[#192632]"}`}>{value}</p>
    <p className="mt-1 text-[11px] leading-4 text-[#5f6c7b]">{note}</p>
  </div>;
}

function Detail({ item, page, onSaved }: { item: BillingException; page: ExceptionsPage; onSaved: () => Promise<void> }) {
  const [history, setHistory] = useState<ExceptionEventRow[] | null>(null);
  const [form, setForm] = useState({ status: item.status, owner: item.assignee_user_id?.toString() ?? "", credit: item.recovered_amount ?? "", note: "" });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setForm({ status: item.status, owner: item.assignee_user_id?.toString() ?? "", credit: item.recovered_amount ?? "", note: "" });
    setError(null);
    setHistory(null);
    exceptionHistory(item.key).then(setHistory).catch(() => setHistory([]));
  }, [item.key, item.revision, item.status, item.assignee_user_id, item.recovered_amount]);

  async function save() {
    setSaving(true);
    setError(null);
    try {
      const changes: Parameters<typeof reviewException>[2] = {};
      if (form.status !== item.status) changes.status = form.status;
      const owner = form.owner ? Number(form.owner) : null;
      if (owner !== item.assignee_user_id) changes.assignee_user_id = owner;
      if (form.status === "recovered" && form.credit && form.credit !== item.recovered_amount) changes.recovered_amount = form.credit;
      if (form.note.trim()) changes.note = form.note.trim();
      await reviewException(item.key, item.revision, changes);
      await onSaved();
    } catch (e) {
      setError((e as Error).message);
      if (e instanceof ApiError && e.status === 409) await onSaved();
    } finally { setSaving(false); }
  }

  const closing = form.status === "recovered" || form.status === "resolved" || form.status === "dismissed";
  return <section className="tf-panel" aria-label="Exception detail" data-testid="exception-detail">
    <div className="tf-panel-heading">
      <div className="min-w-0">
        <p className="tf-eyebrow">{item.category_label}</p>
        <h2 className="tf-panel-title">{item.title}</h2>
        <p className="tf-panel-description">{[item.supplier, item.material, item.project].filter(Boolean).join(" · ")}</p>
      </div>
      <span className={`tf-status ${SEVERITY_CLASS[item.severity]}`}>{item.severity}</span>
    </div>
    <div className="space-y-4 p-5">
      {item.changed_since_closed && <p className="tf-alert tf-alert-warn" data-testid="changed-since-closed">
        Marked “{STATUS_LABEL[item.status]}”{item.updated_by ? ` by ${item.updated_by}` : ""}, but its figures have changed since. Check it again: saving records your decision on the new figures.
      </p>}
      <p className="text-[13px] leading-5 text-[#263646]">{item.explanation}</p>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-[12px] sm:grid-cols-3">
        {item.amount !== null && <div><dt className="tf-stat-label">This cause</dt><dd className="font-bold tabular-nums">{money(item.amount, true)}</dd></div>}
        {item.line_billed !== null && <div><dt className="tf-stat-label">Line billed</dt><dd className="tabular-nums">{money(item.line_billed)}</dd></div>}
        {item.line_ids.length === 1 && <div><dt className="tf-stat-label">Line approved</dt><dd className="tabular-nums">{item.line_approved === null ? "Not known yet" : money(item.line_approved)}</dd></div>}
        {item.billed_tons !== null && <div><dt className="tf-stat-label">Billed tons</dt><dd className="tabular-nums">{item.billed_tons}</dd></div>}
        {item.approved_tons !== null && <div><dt className="tf-stat-label">Approved tons</dt><dd className="tabular-nums">{item.approved_tons}</dd></div>}
        {item.unbilled_tickets !== null && <div><dt className="tf-stat-label">Deliveries</dt><dd className="tabular-nums">{item.unbilled_tickets}</dd></div>}
      </dl>
      <div className="flex flex-wrap gap-2 text-[12px]">
        {item.line_ids.map((id, n) => <Link key={id} prefetch={false} className="tf-secondary-button" href={`/invoices/lines?line=${id}`}>
          Invoice {item.invoice_numbers[n] ?? item.invoice_numbers[0]} <span aria-hidden="true">→</span></Link>)}
        {item.shift_id && <Link prefetch={false} className="tf-secondary-button" href={`/trucking?shift_id=${item.shift_id}`}>
          Truck shift <span aria-hidden="true">→</span></Link>}
        {item.ticket_number && <Link prefetch={false} className="tf-secondary-button" href={`/records?q=${encodeURIComponent(item.ticket_number)}`}>
          Ticket {item.ticket_number} in records <span aria-hidden="true">→</span></Link>}
        {item.category === "not_invoiced" && item.project_id !== null && <Link prefetch={false} className="tf-secondary-button" href={`/projects/${item.project_id}`}>
          Unbilled deliveries <span aria-hidden="true">→</span></Link>}
      </div>

      {page.can_manage ? <form className="space-y-3 border-t border-[#e9edf0] pt-4" onSubmit={(e) => { e.preventDefault(); void save(); }}>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="tf-field-label">Owner
            <select className="tf-field w-full" value={form.owner} onChange={(e) => setForm({ ...form, owner: e.target.value })}>
              <option value="">Nobody yet</option>
              {page.people.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </label>
          <label className="tf-field-label">Status
            <select className="tf-field w-full" value={form.status} onChange={(e) => setForm({ ...form, status: e.target.value as ExceptionStatus })}>
              {(Object.keys(STATUS_LABEL) as ExceptionStatus[]).map((s) => <option key={s} value={s}>{STATUS_LABEL[s]}</option>)}
            </select>
          </label>
        </div>
        {form.status === "recovered" && <label className="tf-field-label">Credit the supplier confirmed ($)
          <input className="tf-field w-full" inputMode="decimal" placeholder="0.00" value={form.credit}
            onChange={(e) => setForm({ ...form, credit: e.target.value })} />
        </label>}
        <label className="tf-field-label">{closing && form.status !== item.status ? "Why (required)" : "Note (optional)"}
          <textarea className="tf-field h-20 w-full" maxLength={2000} value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })}
            placeholder={form.status === "dismissed" ? "e.g. Fuel surcharge agreed by phone, see email of Nov 3" : "What you found or asked"} />
        </label>
        {error && <p role="alert" className="tf-alert tf-alert-bad">{error}</p>}
        <button type="submit" className="tf-primary-button" disabled={saving}>{saving ? "Saving…" : "Save"}</button>
        <p className="text-[11px] text-[#5f6c7b]">Saving records who did what. It never changes the invoice, a ticket or a locked price.</p>
      </form> : <p className="tf-notice" role="note">Managers and owners take and settle exceptions. You can see where each one stands.</p>}

      <div className="border-t border-[#e9edf0] pt-4">
        <h3 className="text-[12px] font-bold text-[#263646]">History</h3>
        {history === null ? <p className="text-[12px] text-[#5f6c7b]">Loading…</p> : history.length === 0
          ? <p className="text-[12px] text-[#5f6c7b]">Nobody has acted on this yet.</p>
          : <ol className="mt-2 space-y-1.5 text-[12px]" data-testid="exception-history">{history.map((e, n) => <li key={n}>
              <span className="text-[#5f6c7b]">{new Date(e.at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })} · {e.actor ?? "—"}:</span>{" "}
              {FIELD_LABEL[e.field] ?? e.field} {e.field === "note" ? <q>{e.new}</q> : <>{e.old ? <>from <b>{e.field === "status" ? STATUS_LABEL[e.old as ExceptionStatus] ?? e.old : e.old}</b> </> : null}to <b>{e.field === "status" ? STATUS_LABEL[e.new as ExceptionStatus] ?? e.new : e.new ?? "nobody"}</b></>}
            </li>)}</ol>}
      </div>
    </div>
  </section>;
}

export default function ExceptionsCenter() {
  const { projectId, setProjectId } = useProject();
  const [filters, setFilters] = useState<ExceptionFilters>({ status: "open" });
  const [page, setPage] = useState<ExceptionsPage | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [ready, setReady] = useState(false);

  // Filters live in the address, so a view can be shared or bookmarked.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const initial: ExceptionFilters = { status: "open" };
    for (const key of FILTER_KEYS) { const value = params.get(key); if (value) initial[key] = value; }
    // The project is the top bar's choice; a link naming one chooses it there.
    if (initial.project_id) setProjectId(initial.project_id);
    else if (projectId) initial.project_id = projectId;
    setFilters(initial);
    setSelected(params.get("key"));
    setReady(true);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try { setPage(await listExceptions(filters)); setError(null); }
    catch (e) { setError((e as Error).message); }
    finally { setLoading(false); }
  }, [filters]);
  useEffect(() => { if (ready) void load(); }, [ready, load]);
  useEffect(() => {
    if (ready && (filters.project_id ?? "") !== projectId) setFilters((f) => ({ ...f, project_id: projectId || undefined }));
  }, [projectId, ready]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!ready) return;
    const params = new URLSearchParams();
    for (const key of FILTER_KEYS) if (filters[key] && !(key === "status" && filters[key] === "open")) params.set(key, filters[key]!);
    if (selected) params.set("key", selected);
    const query = params.toString();
    window.history.replaceState(null, "", query ? `?${query}` : window.location.pathname);
  }, [filters, selected, ready]);

  const set = (key: keyof ExceptionFilters, value: string) => setFilters((f) => ({ ...f, [key]: value || undefined }));
  const item = page?.items.find((i) => i.key === selected) ?? null;
  const s = page?.summary;

  return <div className="space-y-5">
    <div className="tf-page-heading">
      <div><p className="tf-eyebrow">Invoices</p><h1 className="tf-page-title">Invoice exceptions</h1>
        <p className="tf-page-description">Every reason to question a supplier invoice, in one list: who owns it, where it stands, and what it&apos;s worth.</p></div>
      <div className="flex flex-wrap gap-2">
        <Link prefetch={false} className="tf-secondary-button" href="/invoices/lines">Invoice comparison</Link>
        <button type="button" className="tf-secondary-button" onClick={() => void downloadFile(exceptionsCsvUrl(filters), "invoice-exceptions.csv").catch((e) => setError((e as Error).message))}>Export CSV</button>
      </div>
    </div>
    {error && <div role="alert" className="tf-alert tf-alert-bad">{error}</div>}

    {s && <section className="tf-panel overflow-hidden" aria-label="Exception totals">
      <div className="grid grid-cols-2 lg:grid-cols-5">
        <Metric label="Open" value={s.open} attention={s.by_severity.high > 0 || s.changed_since_closed > 0}
          note={`${s.by_severity.high} high · ${s.unassigned} without an owner` + (s.changed_since_closed ? ` · ${s.changed_since_closed} changed after closing` : "")} />
        <Metric label="Potential overcharge" value={money(s.potential_overcharge)} attention={Number(s.potential_overcharge) > 0}
          note={s.money_unknown ? `Billed above approved, open. ${s.money_unknown} more can't be priced yet` : "Billed above approved, open, each cause once"} />
        <Metric label="Disputed with supplier" value={money(s.disputed_amount)} note="Part of the potential overcharge" />
        <Metric label="Credits received" value={money(s.recovered)} note="Recorded by a person, never computed" />
        <Metric label="Billed below approved" value={money(s.billed_below_approved)} note="Open; may be billed later" />
      </div>
    </section>}

    <section className="tf-panel" aria-label="Filters">
      <div className="grid gap-3 p-4 sm:grid-cols-2 lg:grid-cols-4">
        <label className="tf-field-label">Status
          <select className="tf-field w-full" value={filters.status ?? "open"} onChange={(e) => set("status", e.target.value)}>
            <option value="open">Open (any stage)</option><option value="all">All</option>
            {(Object.keys(STATUS_LABEL) as ExceptionStatus[]).map((st) => <option key={st} value={st}>{STATUS_LABEL[st]}</option>)}
          </select>
        </label>
        <label className="tf-field-label">Category
          <select className="tf-field w-full" value={filters.category ?? ""} onChange={(e) => set("category", e.target.value)}>
            <option value="">All categories</option>
            {page && Object.entries(page.categories).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
          </select>
        </label>
        <label className="tf-field-label">Severity
          <select className="tf-field w-full" value={filters.severity ?? ""} onChange={(e) => set("severity", e.target.value)}>
            <option value="">Any</option><option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option>
          </select>
        </label>
        <label className="tf-field-label">Owner
          <select className="tf-field w-full" value={filters.assignee ?? ""} onChange={(e) => set("assignee", e.target.value)}>
            <option value="">Anyone</option><option value="me">Mine</option><option value="none">Nobody yet</option>
            {page?.people.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </label>
      </div>
      {s && <div className="flex flex-wrap gap-1.5 border-t border-[#e9edf0] px-4 py-3" aria-label="Open by category">
        {page && Object.entries(page.categories).filter(([key]) => s.by_category[key as keyof typeof s.by_category] > 0).map(([key, label]) =>
          <button key={key} type="button" aria-pressed={filters.category === key}
            className={`tf-status ${filters.category === key ? "tf-status-open" : ""}`} onClick={() => set("category", filters.category === key ? "" : key)}>
            {label} <b className="tabular-nums">{s.by_category[key as keyof typeof s.by_category]}</b>
          </button>)}
      </div>}
    </section>

    <div className={`grid gap-5 ${item ? "xl:grid-cols-[minmax(0,1fr)_420px]" : ""}`}>
      <section className="tf-panel min-w-0" aria-label="Exceptions">
        {loading && !page ? <p className="tf-empty">Checking every invoice line…</p>
          : page && page.items.length === 0 ? <p className="tf-empty" data-testid="exceptions-empty">{filters.status === "open" && !filters.category && !filters.severity && !filters.project_id && !filters.assignee
            ? "Nothing to question. Every saved invoice line agrees with approved deliveries and rates, and no approved delivery is waiting for a bill."
            : "No exceptions match these filters."}</p>
          : <div className="tf-table-wrap"><table className="tf-table" aria-label="Exception list">
            <thead><tr><th>Severity</th><th>Exception</th><th className="text-right">Amount</th><th>Owner</th><th>Status</th></tr></thead>
            <tbody>{page?.items.map((i) => <tr key={i.key} aria-selected={i.key === selected}
              className={`cursor-pointer ${i.key === selected ? "bg-[#f3f7fb]" : ""}`} onClick={() => setSelected(i.key)}>
              <td><span className={`tf-status ${SEVERITY_CLASS[i.severity]}`}>{i.severity}</span></td>
              <td className="min-w-[260px]"><button type="button" className="text-left font-semibold text-[#192632] hover:underline" onClick={() => setSelected(i.key)}>{i.title}</button>
                <span className="block text-[11px] text-[#5f6c7b]">{[i.category_label, i.supplier, i.project].filter(Boolean).join(" · ")}</span></td>
              <td className="text-right tabular-nums">{i.amount === null ? <span className="text-[#5f6c7b]">—</span> : money(i.amount, true)}</td>
              <td>{i.assignee ?? <span className="text-[#5f6c7b]">—</span>}</td>
              <td><span className={`tf-status ${i.status === "disputed" ? "tf-status-attention" : i.status === "recovered" || i.status === "resolved" ? "tf-status-open" : ""}`}>{STATUS_LABEL[i.status]}</span>
                {i.changed_since_closed && <span className="tf-status tf-status-attention ml-1">Figures changed</span>}</td>
            </tr>)}</tbody>
          </table></div>}
      </section>
      {item && page && <Detail item={item} page={page} onSaved={load} />}
    </div>

    {page && page.cleared.length > 0 && <details className="tf-panel">
      <summary className="cursor-pointer px-5 py-3 text-[13px] font-semibold text-[#263646]">No longer found ({page.cleared.length}): the invoice or deliveries changed after someone acted</summary>
      <div className="tf-table-wrap"><table className="tf-table" aria-label="No longer found">
        <thead><tr><th>Exception, as it was</th><th>Last status</th><th className="text-right">Credit</th><th>Owner</th><th>Note</th></tr></thead>
        <tbody>{page.cleared.map((c) => <tr key={c.key}>
          <td className="font-semibold">{c.title}</td><td>{STATUS_LABEL[c.status]}</td>
          <td className="text-right tabular-nums">{money(c.recovered_amount)}</td><td>{c.assignee ?? "—"}</td><td className="max-w-[320px] truncate">{c.note ?? "—"}</td>
        </tr>)}</tbody>
      </table></div>
    </details>}
    {page && <p className="text-[11px] leading-5 text-[#5f6c7b]">{page.note}</p>}
  </div>;
}
