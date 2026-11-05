"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  BulkResult, Filters, Project, RecordsPage as RecordsResult, RecordsSort, SavedView, Ticket, bulkEditTickets,
  createSavedView, deleteSavedView, listMaterials, listProjects, listSavedViews, listSuppliers, recordsExportUrl,
  searchRecords,
} from "../../lib/api";
import { useAccount } from "../account-context";

const STATUS_LABEL: Record<string, string> = {
  needs_review: "Needs review", verified: "Verified", flagged: "Flagged",
  skipped: "Skipped", failed: "Failed",
};
const statusClass = (status: string) => status === "verified" ? "tf-status-open" :
  status === "failed" ? "tf-status-error" :
    status === "needs_review" || status === "flagged" ? "tf-status-attention" : "";
const FILTER_KEYS = ["project_id", "supplier", "material", "status", "date_from", "date_to", "q"] as const;
const PAGE_SIZES = [25, 50, 100, 200];
const money = (v: string | null) => v == null ? "—" : `$${Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

const EXPORT_COLUMNS: [string, string][] = [
  ["ticket_number", "Ticket number"], ["delivery_date", "Delivery date"], ["material", "Material"], ["tons", "Tons"],
  ["supplier", "Supplier"], ["job_location", "Job location"], ["unit_price", "Unit price"], ["cost", "Cost"],
  ["status", "Status"], ["source", "Source file"], ["approved_by", "Approved by"], ["approved_at", "Approved at"],
  ["project", "Project"], ["batch", "Batch"],
];
const DEFAULT_EXPORT = EXPORT_COLUMNS.slice(0, 12).map(([key]) => key);

const HEADERS: { label: string; sort?: RecordsSort; align?: string }[] = [
  { label: "Ticket #", sort: "ticket_number" }, { label: "Date", sort: "date" }, { label: "Material", sort: "material" },
  { label: "Tons", sort: "tons", align: "text-right" }, { label: "Supplier", sort: "supplier" }, { label: "Job location", sort: "job" },
  { label: "Unit price", align: "text-right" }, { label: "Cost", align: "text-right" }, { label: "Status", sort: "status" },
];
const BULK_FIELDS: [string, string][] = [["supplier", "Supplier"], ["material_type", "Material"], ["job_location", "Job location"], ["delivery_date", "Delivery date"]];

function resultText(result: BulkResult): string {
  const left = Object.entries(result.left_alone).map(([status, count]) => `${count} ${(STATUS_LABEL[status] ?? status).toLowerCase()}`);
  return `Changed ${result.changed}.` + (result.unchanged ? ` ${result.unchanged} already had that value.` : "")
    + (left.length ? ` Left alone: ${left.join(", ")}. Open an approved ticket to change it.` : "");
}

export default function RecordsPage() {
  const { can } = useAccount();
  const canEdit = can("tickets.edit");
  const [filters, setFilters] = useState<Filters>({});
  const [applied, setApplied] = useState<Filters>({});
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [sort, setSort] = useState<RecordsSort>("date");
  const [direction, setDirection] = useState<"asc" | "desc">("desc");
  const [result, setResult] = useState<RecordsResult | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [views, setViews] = useState<SavedView[]>([]);
  const [viewId, setViewId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [initialized, setInitialized] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [bulkField, setBulkField] = useState("supplier");
  const [bulkValue, setBulkValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [columns, setColumns] = useState<string[]>(DEFAULT_EXPORT);
  const [dialog, setDialog] = useState<"export" | "save" | null>(null);
  const [viewName, setViewName] = useState("");
  const [catalog, setCatalog] = useState<{ suppliers: string[]; materials: string[] }>({ suppliers: [], materials: [] });
  const reloadKey = useRef(0);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    listProjects(true).then(setProjects).catch(() => {});
    listSavedViews().then(setViews).catch(() => {});
    Promise.all([listSuppliers(), listMaterials()])
      .then(([s, m]) => setCatalog({ suppliers: s.map((x) => x.name), materials: m.map((x) => x.name) })).catch(() => {});
    const params = new URLSearchParams(window.location.search);
    const next: Filters = {};
    for (const key of FILTER_KEYS) {
      const value = params.get(key);
      if (value) next[key] = value;
    }
    setFilters(next);
    setApplied(next);
    setInitialized(true);
  }, []);

  // Typing waits a moment, so a search isn't sent for every keystroke.
  useEffect(() => {
    if (!initialized || JSON.stringify(filters) === JSON.stringify(applied)) return;
    const timer = setTimeout(() => {
      setApplied(filters);
      setPage(1);
    }, 250);
    return () => clearTimeout(timer);
  }, [filters, applied, initialized]);

  useEffect(() => {
    if (!initialized) return;
    let active = true;
    setLoading(true);
    searchRecords(applied, { page, pageSize, sort, direction })
      .then((found) => { if (active) { setResult(found); setError(null); } })
      .catch((e) => { if (active) setError(e.message); })
      .finally(() => { if (active) setLoading(false); });
    const params = new URLSearchParams();
    Object.entries(applied).forEach(([k, v]) => v && params.set(k, v));
    const text = params.toString();
    window.history.replaceState(null, "", `/records${text ? `?${text}` : ""}`);
    return () => { active = false; };
  }, [applied, page, pageSize, sort, direction, initialized, reload]);

  useEffect(() => { setSelected(new Set()); }, [applied]);

  const set = (key: keyof Filters) => (e: { target: { value: string } }) => {
    setViewId("");
    setFilters((old) => ({ ...old, [key]: e.target.value || undefined }));
  };
  const hasFilter = Object.values(filters).some(Boolean);
  const rows: Ticket[] = result?.rows ?? [];
  const total = result?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const firstRow = total ? (page - 1) * pageSize + 1 : 0;
  const lastRow = Math.min(page * pageSize, total);
  const summary = result?.summary;
  const allOnPage = rows.length > 0 && rows.every((r) => selected.has(r.id));
  const suggestions = useMemo(() => bulkField === "supplier" ? catalog.suppliers : bulkField === "material_type" ? catalog.materials : [], [bulkField, catalog]);

  function toggleSort(column: RecordsSort) {
    if (sort === column) setDirection((d) => d === "asc" ? "desc" : "asc");
    else {
      setSort(column);
      setDirection("asc");
    }
    setPage(1);
  }
  function toggle(id: number) {
    setSelected((old) => {
      const next = new Set(old);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }
  function togglePage() {
    setSelected((old) => {
      const next = new Set(old);
      rows.forEach((r) => { if (allOnPage) next.delete(r.id); else next.add(r.id); });
      return next;
    });
  }
  async function applyBulk() {
    if (!bulkValue.trim() || busy) return;
    setBusy(true);
    setNotice(null);
    try {
      const done = await bulkEditTickets([...selected], bulkField, bulkValue);
      setNotice(resultText(done));
      setSelected(new Set());
      setBulkValue("");
      reloadKey.current += 1;
      setReload(reloadKey.current);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function openView(id: string) {
    setViewId(id);
    const view = views.find((v) => String(v.id) === id);
    if (!view) return;
    setFilters(view.filters);
    setApplied(view.filters);
    setPage(1);
    setColumns(view.columns.length ? view.columns : DEFAULT_EXPORT);
  }
  async function saveView() {
    try {
      const view = await createSavedView(viewName, applied, columns);
      setViews((old) => [...old, view].sort((a, b) => a.name.localeCompare(b.name)));
      setViewId(String(view.id));
      setViewName("");
      setDialog(null);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
      setDialog(null);
    }
  }
  async function removeView() {
    const view = views.find((v) => String(v.id) === viewId);
    if (!view || !window.confirm(`Remove the saved view "${view.name}"? Records aren't affected.`)) return;
    try {
      await deleteSavedView(view.id);
      setViews((old) => old.filter((v) => v.id !== view.id));
      setViewId("");
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return <div className="space-y-5">
    <div className="tf-page-heading">
      <div><p className="tf-eyebrow">DELIVERY DOCUMENTATION</p><h1 className="tf-page-title">Delivery records</h1>
        <p className="tf-page-description">Search, audit, and export material delivery tickets by project and status.</p></div>
      <div className="flex flex-wrap gap-2">
        {canEdit && <a href="/records/names" className="tf-secondary-button">Name cleanup</a>}
        <a href="/upload" className="tf-primary-button"><span aria-hidden="true">+</span> New ticket upload</a>
      </div>
    </div>
    {error && <div role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">{error}</div>}
    {notice && <div role="status" className="rounded border border-[#cce4d2] bg-[#f3f9f4] p-3 text-sm text-[#216c43]">{notice}</div>}

    <section className="tf-panel">
      <div className="tf-panel-heading flex-wrap">
        <div><h2 className="tf-panel-title">Filter delivery records</h2><p className="tf-panel-description">Totals and exports follow your filters</p></div>
        <div className="flex flex-wrap items-center gap-2">
          <label className="sr-only" htmlFor="saved-view">Saved view</label>
          <select id="saved-view" className="tf-field !min-h-0 !py-1.5 !text-xs" value={viewId} onChange={(e) => openView(e.target.value)}>
            <option value="">Saved views…</option>
            {views.map((v) => <option key={v.id} value={v.id}>{v.name}</option>)}
          </select>
          {viewId && <button type="button" className="tf-link" onClick={removeView}>Remove view</button>}
          <button type="button" className="tf-link" onClick={() => setDialog("save")}>Save this view</button>
          {hasFilter && <button type="button" onClick={() => { setViewId(""); setFilters({}); }} className="tf-link">Clear filters</button>}
        </div>
      </div>
      <div className="grid gap-3 p-5 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-7">
        <label className="tf-field-label">Search
          <input type="search" className="tf-field w-full" placeholder="Number, supplier, job…" value={filters.q ?? ""} onChange={set("q")} />
        </label>
        <label className="tf-field-label">Project
          <select className="tf-field w-full" value={filters.project_id ?? ""} onChange={set("project_id")}>
            <option value="">All projects</option>
            {projects.map((p) => <option key={p.id} value={p.id}>{p.code} · {p.name}</option>)}
          </select>
        </label>
        <label className="tf-field-label">Supplier
          <input className="tf-field w-full" placeholder="Supplier name" value={filters.supplier ?? ""} onChange={set("supplier")} />
        </label>
        <label className="tf-field-label">Material
          <input className="tf-field w-full" placeholder="Material type" value={filters.material ?? ""} onChange={set("material")} />
        </label>
        <label className="tf-field-label">Review status
          <select className="tf-field w-full" value={filters.status ?? ""} onChange={set("status")}>
            <option value="">All statuses</option>
            {Object.entries(STATUS_LABEL).map(([k, label]) => <option key={k} value={k}>{label}</option>)}
          </select>
        </label>
        <label className="tf-field-label">Delivery from
          <input type="date" className="tf-field w-full" value={filters.date_from ?? ""} onChange={set("date_from")} />
        </label>
        <label className="tf-field-label">Delivery to
          <input type="date" className="tf-field w-full" value={filters.date_to ?? ""} onChange={set("date_to")} />
        </label>
      </div>
    </section>

    <section className="tf-panel">
      <div className="tf-panel-heading flex-wrap">
        <div>
          <h2 className="tf-panel-title">Ticket register</h2>
          <p className="tf-panel-description" data-testid="records-summary">
            {!summary ? "Loading records…" : <>
              {total.toLocaleString()} records · {(summary.by_status.verified ?? 0).toLocaleString()} verified · {Number(summary.verified_tons).toLocaleString(undefined, { maximumFractionDigits: 3 })} verified tons · {money(summary.verified_cost)} material cost
              {summary.verified_unpriced > 0 && <span className="ml-1 font-semibold text-[#a14c1c]">· {summary.verified_unpriced} verified with no locked price</span>}
            </>}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button type="button" className="tf-secondary-button" onClick={() => setDialog("export")}>Choose columns</button>
          <a href={recordsExportUrl(applied, "csv", columns)} className="tf-secondary-button">Export CSV</a>
          <a href={recordsExportUrl(applied, "xlsx", columns)} className="tf-secondary-button" aria-label="Download Excel (filtered)">
            <svg aria-hidden="true" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M12 3v12m-5-5 5 5 5-5M4 17v4h16v-4" /></svg>
            Export Excel
          </a>
        </div>
      </div>
      {canEdit && selected.size > 0 && <div className="flex flex-wrap items-end gap-2 border-b border-[#f0d8ad] bg-[#fff9ed] px-5 py-3" role="group" aria-label="Change selected tickets">
        <span className="pb-2 text-xs font-bold text-[#754e20]">{selected.size} selected</span>
        <label className="tf-field-label">Set
          <select className="tf-field" value={bulkField} onChange={(e) => { setBulkField(e.target.value); setBulkValue(""); }}>
            {BULK_FIELDS.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
          </select>
        </label>
        <label className="tf-field-label">To
          <input className="tf-field w-56" type={bulkField === "delivery_date" ? "date" : "text"} list="bulk-values" value={bulkValue} onChange={(e) => setBulkValue(e.target.value)} />
          <datalist id="bulk-values">{suggestions.map((name) => <option key={name} value={name} />)}</datalist>
        </label>
        <button type="button" className="tf-primary-button" disabled={!bulkValue.trim() || busy} onClick={applyBulk}>Apply to {selected.size} ticket{selected.size === 1 ? "" : "s"}</button>
        <button type="button" className="tf-link pb-2" onClick={() => setSelected(new Set())}>Clear selection</button>
        <p className="w-full text-[11px] text-[#754e20]">Approved tickets are left as they are. Every change is recorded in each ticket&apos;s history, and nothing here approves a ticket. Tons and ticket numbers can only be changed one ticket at a time.</p>
      </div>}
      <div className="tf-table-wrap"><table className="tf-table" aria-busy={loading}>
        <thead><tr>
          {canEdit && <th className="w-8"><input type="checkbox" aria-label="Select all on this page" checked={allOnPage} onChange={togglePage} /></th>}
          {HEADERS.map((h) => <th key={h.label} className={h.align} aria-sort={h.sort === sort ? (direction === "asc" ? "ascending" : "descending") : undefined}>
            {h.sort ? <button type="button" className="font-[inherit] uppercase tracking-[inherit] hover:text-[#19232e]" onClick={() => toggleSort(h.sort as RecordsSort)}>
              {h.label}{h.sort === sort && <span aria-hidden="true"> {direction === "asc" ? "▲" : "▼"}</span>}
            </button> : h.label}
          </th>)}
          <th><span className="sr-only">Open</span></th>
        </tr></thead>
        <tbody>{rows.map((t) => <tr key={t.id}>
          {canEdit && <td><input type="checkbox" aria-label={`Select ticket ${t.ticket_number ?? t.id}`} checked={selected.has(t.id)} onChange={() => toggle(t.id)} /></td>}
          <td className="font-bold tabular-nums">{t.ticket_number || "—"}</td>
          <td className="whitespace-nowrap">{t.delivery_date || "—"}</td>
          <td className="min-w-[125px]">{t.material_type || "—"}</td>
          <td className="whitespace-nowrap text-right tabular-nums">{t.tonnage ?? "—"}</td>
          <td className="min-w-[130px]">{t.supplier || "—"}</td>
          <td>{t.job_location || "—"}</td>
          <td className="whitespace-nowrap text-right tabular-nums">{money(t.unit_price)}</td>
          <td className="whitespace-nowrap text-right tabular-nums">{money(t.cost)}</td>
          <td><span className={`tf-status ${statusClass(t.status)}`}>
            {STATUS_LABEL[t.status] ?? t.status}
          </span>{t.duplicates.length > 0 &&
            <span className="ml-1.5 text-[10px] font-bold text-red-700" title={t.duplicates.join("\n")}>Possible duplicate</span>}</td>
          <td className="whitespace-nowrap"><a href={`/upload?batch=${t.batch_id}&ticket=${t.id}`}>Review →</a></td>
        </tr>)}</tbody>
      </table></div>
      {!loading && !rows.length && <div className="tf-empty">
        <p>{hasFilter ? "No delivery records match the current filters." : "No delivery tickets recorded yet."}</p>
        {hasFilter ? <button className="tf-link mt-2" onClick={() => { setViewId(""); setFilters({}); }}>Clear filters</button> :
          <a className="tf-link mt-2 inline-block" href="/upload">Start ticket intake →</a>}
      </div>}
      {total > 0 && <nav className="flex flex-wrap items-center justify-between gap-3 border-t border-[#e9edf0] px-5 py-3 text-xs text-[#526273]" aria-label="Pages of records">
        <span className="tabular-nums">Showing {firstRow.toLocaleString()}–{lastRow.toLocaleString()} of {total.toLocaleString()}</span>
        <div className="flex items-center gap-2">
          <label className="flex items-center gap-1.5">Rows per page
            <select className="tf-field !min-h-0 !py-1 !text-xs" value={pageSize} onChange={(e) => { setPageSize(Number(e.target.value)); setPage(1); }}>
              {PAGE_SIZES.map((size) => <option key={size} value={size}>{size}</option>)}
            </select>
          </label>
          <button type="button" className="tf-secondary-button !min-h-0 !py-1 disabled:opacity-40" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>Previous</button>
          <span className="tabular-nums">Page {page} of {pages.toLocaleString()}</span>
          <button type="button" className="tf-secondary-button !min-h-0 !py-1 disabled:opacity-40" disabled={page >= pages} onClick={() => setPage((p) => p + 1)}>Next</button>
        </div>
      </nav>}
    </section>
    <p className="text-[11px] text-[#768391]">Verified tonnage and costs include approved tickets only. Unverified values may be visible in the register but are not included in financial totals.</p>

    {dialog === "export" && <div className="tf-dialog-scrim" onClick={() => setDialog(null)}>
      <div role="dialog" aria-modal="true" aria-label="Export columns" className="tf-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="tf-panel-heading"><h2 className="tf-panel-title">Export columns</h2>
          <button type="button" className="tf-link" onClick={() => setColumns(DEFAULT_EXPORT)}>Reset</button></div>
        <div className="grid grid-cols-2 gap-2 p-5 text-xs text-[#354454]">
          {EXPORT_COLUMNS.map(([key, label]) => <label key={key} className="flex items-center gap-2">
            <input type="checkbox" checked={columns.includes(key)}
              onChange={(e) => setColumns((old) => e.target.checked
                ? EXPORT_COLUMNS.map(([k]) => k).filter((k) => old.includes(k) || k === key)
                : old.filter((k) => k !== key))} />
            {label}
          </label>)}
        </div>
        <div className="flex items-center justify-between gap-3 border-t border-[#e9edf0] px-5 py-3">
          <p className="text-[11px] text-[#657182]">Save this view to keep these columns with your filters.</p>
          <button type="button" className="tf-primary-button" disabled={!columns.length} onClick={() => setDialog(null)} autoFocus>Done</button>
        </div>
      </div>
    </div>}
    {dialog === "save" && <div className="tf-dialog-scrim" onClick={() => setDialog(null)}>
      <form role="dialog" aria-modal="true" aria-label="Save this view" className="tf-dialog" onClick={(e) => e.stopPropagation()}
        onSubmit={(e) => { e.preventDefault(); void saveView(); }}>
        <div className="tf-panel-heading"><h2 className="tf-panel-title">Save this view</h2></div>
        <div className="p-5">
          <label className="tf-field-label">Name
            <input className="tf-field w-full" maxLength={100} value={viewName} onChange={(e) => setViewName(e.target.value)} autoFocus />
          </label>
          <p className="mt-2 text-[11px] text-[#657182]">Keeps these filters and export columns. Everyone in your company can open it.</p>
        </div>
        <div className="flex justify-end gap-2 border-t border-[#e9edf0] px-5 py-3">
          <button type="button" className="tf-secondary-button" onClick={() => setDialog(null)}>Cancel</button>
          <button type="submit" className="tf-primary-button" disabled={!viewName.trim()}>Save view</button>
        </div>
      </form>
    </div>}
  </div>;
}
