"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import {
  Material, NameAliasRow, PricingEventRow, Project, Rate, RatePreview, RateScope, RateSheetPlan, RateTimeline, Supplier, addAlias,
  createMaterial, createSupplier, deleteItem, getPricingHistory, getRateTimeline, importRateSheet, listAliases, listMaterials, listProjects, listRates, listSuppliers,
  previewRate, removeAlias, saveRate, HaulerRow, HaulingRate, listHaulers, listHaulingRates, UNIT_LABEL,
} from "../../lib/api";
import { useAccount } from "../account-context";
import { HaulingRates } from "../trucking/setup";

const ACTION_LABEL: Record<PricingEventRow["action"], string> = {
  supplier_added: "Supplier added",
  supplier_removed: "Supplier removed",
  material_added: "Material added",
  material_removed: "Material removed",
  rate_set: "Rate set",
  rate_changed: "Rate changed",
  rate_validity_changed: "Validity changed",
  freight_changed: "Freight changed",
  unit_changed: "Unit changed",
  rate_removed: "Rate removed",
};
// Display only: keep the server's decimal string, trimmed to cents where exact.
const money = (value: string | null) => {
  if (value === null) return "—";
  const negative = value.startsWith("-");
  const [whole, frac = ""] = (negative ? value.slice(1) : value).split(".");
  const trimmed = frac.replace(/0+$/, "");
  return `${negative ? "−" : ""}$${whole}.${trimmed.padEnd(2, "0")}`;
};

// "" in a form means company-wide / no start date.
const scopeOf = (project: string, from: string, until = ""): RateScope =>
  ({ project_id: project ? Number(project) : null, effective_from: from || null, effective_to: until || null });
const longDate = (iso: string) => new Date(`${iso}T00:00:00`).toLocaleDateString(undefined, { dateStyle: "medium" });

function ScopeFields({ idPrefix, projects, project, from, until, onChange }: {
  idPrefix: string; projects: Project[]; project: string; from: string; until: string;
  onChange: (project: string, from: string, until: string) => void;
}) {
  return <>
    <label className="tf-field-label" htmlFor={`${idPrefix}-project`}>Applies to
      <select id={`${idPrefix}-project`} className="tf-field w-full" value={project} onChange={(e) => onChange(e.target.value, from, until)}>
        <option value="">All projects (company rate)</option>
        {projects.map((p) => <option key={p.id} value={p.id}>{p.code} · {p.name}</option>)}
      </select>
    </label>
    <label className="tf-field-label" htmlFor={`${idPrefix}-from`}>From (optional)
      <input id={`${idPrefix}-from`} type="date" className="tf-field w-full" value={from} onChange={(e) => onChange(project, e.target.value, until)} />
    </label>
    <label className="tf-field-label" htmlFor={`${idPrefix}-until`}>Until (optional)
      <input id={`${idPrefix}-until`} type="date" className="tf-field w-full" value={until} min={from || undefined}
        onChange={(e) => onChange(project, from, e.target.value)} />
    </label>
  </>;
}

export default function PricingPage() {
  const canManage = useAccount().can("pricing.manage");
  const [tab, setTab] = useState<"material" | "trucking">("material");
  useEffect(() => { if (new URLSearchParams(window.location.search).get("tab") === "trucking") setTab("trucking"); }, []);
  const [suppliers, setSuppliers] = useState<Supplier[]>([]);
  const [materials, setMaterials] = useState<Material[]>([]);
  const [rates, setRates] = useState<Rate[]>([]);
  const [history, setHistory] = useState<PricingEventRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [newSupplier, setNewSupplier] = useState("");
  const [newMaterial, setNewMaterial] = useState("");
  const [rateForm, setRateForm] = useState({ supplier: "", material: "", price: "", project: "", from: "", until: "", freight: "", unit: "" });
  const [timeline, setTimeline] = useState<RateTimeline | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [sheetScope, setSheetScope] = useState({ project: "", from: "", until: "" });
  const [ratePreview, setRatePreview] = useState<RatePreview | null>(null);
  const [saving, setSaving] = useState(false);
  const [aliases, setAliases] = useState<NameAliasRow[]>([]);
  const [aliasForm, setAliasForm] = useState({ kind: "supplier" as "supplier" | "material", alias: "", target: "" });
  const [sheet, setSheet] = useState("");
  const [sheetPlan, setSheetPlan] = useState<RateSheetPlan | null>(null);
  const [sheetDone, setSheetDone] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [s, m, r, h] = await Promise.all([listSuppliers(), listMaterials(), listRates(), getPricingHistory()]);
      setSuppliers(s); setMaterials(m); setRates(r); setHistory(h);
      listAliases().then(setAliases).catch(() => setAliases([]));
      listProjects().then(setProjects).catch(() => setProjects([]));
      getRateTimeline().then(setTimeline).catch(() => setTimeline(null));
      setError(null);
    } catch (e) { setError((e as Error).message); }
  }, []);
  useEffect(() => { void load(); }, [load]);

  async function act(fn: () => Promise<void>) {
    if (saving) return;
    setSaving(true);
    setError(null);
    try { await fn(); await load(); }
    catch (e) { setError((e as Error).message); }
    finally { setSaving(false); }
  }
  const supplierName = (id: number) => suppliers.find((s) => s.id === id)?.name ?? `#${id}`;
  const materialName = (id: number) => materials.find((m) => m.id === id)?.name ?? `#${id}`;
  const projectLabel = (id: number | null | undefined) => {
    if (id == null) return "All projects";
    const p = projects.find((x) => x.id === id);
    return p ? `${p.code} · ${p.name}` : `Project #${id}`;
  };
  // Grouped by supplier and material, company rate first, then each project, each in date order:
  // read top to bottom, that is the order a ticket's price is looked for in.
  const schedule = [...rates].sort((a, b) =>
    supplierName(a.supplier_id).localeCompare(supplierName(b.supplier_id))
    || materialName(a.material_id).localeCompare(materialName(b.material_id))
    || (a.project_id == null ? -1 : 0) - (b.project_id == null ? -1 : 0)
    || projectLabel(a.project_id).localeCompare(projectLabel(b.project_id))
    || (a.effective_from ?? "").localeCompare(b.effective_from ?? ""));
  const scoped = rates.some((r) => r.project_id != null || r.effective_from != null);

  async function remove(kind: "suppliers" | "materials" | "rates", id: number, label: string) {
    if (!window.confirm(`Remove ${label}? Existing verified ticket prices remain locked at their original values.`)) return;
    await act(() => deleteItem(kind, id));
  }

  return <div className="space-y-5">
    <div className="tf-page-heading">
      <div><h1 className="tf-page-title">Rates & Pricing</h1>
        <p className="tf-page-description">Set each rate once; it applies to every eligible delivery and shift. Approved records keep the rate they were approved at.</p></div>
    </div>
    <div role="tablist" aria-label="Rates" className="tf-section-tabs">
      {([["material", "Material pricing"], ["trucking", "Trucking rates"]] as const).map(([key, label]) =>
        <button key={key} type="button" role="tab" aria-selected={tab === key} className={tab === key ? "tf-section-tab-active" : ""}
          onClick={() => { setTab(key); window.history.replaceState(null, "", key === "material" ? "/pricing" : `/pricing?tab=${key}`); }}>{label}</button>)}
    </div>
    {error && <div role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">{error}</div>}
    {!canManage && <p className="tf-notice" role="note">Rates are maintained by managers and owners. You can view the current schedule.</p>}
    {tab === "trucking" && <TruckingRates canPrice={canManage} />}
    {tab === "material" && <>

    <div className="grid gap-5 lg:grid-cols-2">
      <section className="tf-panel">
        <div className="tf-panel-heading">
          <div><h2 className="tf-panel-title">Suppliers</h2><p className="tf-panel-description">Companies delivering material to your jobs</p></div>
          <span className="tf-status">{suppliers.length} suppliers</span>
        </div>
        {canManage && <form className="flex gap-2 border-b border-[#e9edf0] p-4"
          onSubmit={(e) => { e.preventDefault(); void act(async () => { await createSupplier(newSupplier.trim()); setNewSupplier(""); }); }}>
          <label className="sr-only" htmlFor="new-supplier">Supplier name</label>
          <input id="new-supplier" required className="tf-field min-w-0 flex-1" placeholder="Add supplier name"
            value={newSupplier} onChange={(e) => setNewSupplier(e.target.value)} />
          <button type="submit" className="tf-secondary-button" disabled={!newSupplier.trim() || saving}>+ Add</button>
        </form>}
        {suppliers.length ? <ul className="divide-y divide-[#edf0f2]">
          {suppliers.map((s) => <li key={s.id} className="flex items-center justify-between gap-4 px-5 py-3">
            <span className="truncate text-[12px] font-semibold text-[#354555]">{s.name}</span>
            {canManage && <button disabled={saving} onClick={() => void remove("suppliers", s.id, s.name)}
              className="text-[11px] font-semibold text-[#8a5260] hover:underline">Remove</button>}
          </li>)}
        </ul> : <p className="tf-empty" data-testid="suppliers-empty">{canManage
          ? "No suppliers yet. Add each company that delivers to your jobs, spelled the way your tickets spell it. Rates come next."
          : "No suppliers yet. A manager or owner sets up suppliers, materials and rates."}</p>}
      </section>

      <section className="tf-panel">
        <div className="tf-panel-heading">
          <div><h2 className="tf-panel-title">Material types</h2><p className="tf-panel-description">Match the material descriptions on your tickets</p></div>
          <span className="tf-status">{materials.length} materials</span>
        </div>
        {canManage && <form className="flex gap-2 border-b border-[#e9edf0] p-4"
          onSubmit={(e) => { e.preventDefault(); void act(async () => { await createMaterial(newMaterial.trim()); setNewMaterial(""); }); }}>
          <label className="sr-only" htmlFor="new-material">Material name</label>
          <input id="new-material" required className="tf-field min-w-0 flex-1" placeholder="e.g. No. 53 Crushed Stone"
            value={newMaterial} onChange={(e) => setNewMaterial(e.target.value)} />
          <button type="submit" className="tf-secondary-button" disabled={!newMaterial.trim() || saving}>+ Add</button>
        </form>}
        {materials.length ? <ul className="divide-y divide-[#edf0f2]">
          {materials.map((m) => <li key={m.id} className="flex items-center justify-between gap-4 px-5 py-3">
            <div><span className="text-[12px] font-semibold text-[#354555]">{m.name}</span></div>
            {canManage && <button disabled={saving} onClick={() => void remove("materials", m.id, m.name)}
              className="text-[11px] font-semibold text-[#8a5260] hover:underline">Remove</button>}
          </li>)}
        </ul> : <p className="tf-empty">{canManage
          ? "No materials yet. Add each material as your tickets name it, such as No. 57 Stone."
          : "No materials yet. A manager or owner adds them."}</p>}
      </section>
    </div>

    <section className="tf-panel">
      <div className="tf-panel-heading">
        <div><h2 className="tf-panel-title">Unit price schedule</h2>
          <p className="tf-panel-description">A ticket takes its project&apos;s agreement if there is one, otherwise the company rate, and within either the latest rate starting on or before its delivery date. Approved tickets keep the rate they were approved at.</p></div>
        <span className="tf-status">{rates.length} rates</span>
      </div>
      {canManage && <form className="grid items-end gap-3 border-b border-[#e9edf0] p-5 sm:grid-cols-2 xl:grid-cols-[1fr_1fr_1fr_140px_140px_130px_120px_auto]"
        onSubmit={(e) => { e.preventDefault(); void act(async () => {
          await saveRate(Number(rateForm.supplier), Number(rateForm.material), rateForm.price, scopeOf(rateForm.project, rateForm.from, rateForm.until),
            // Only when chosen: re-saving a price never clears a freight setting by accident.
            rateForm.freight ? rateForm.freight as "included" | "separate" : undefined,
            // Likewise the unit: blank keeps the rate's unit (tons for a new one).
            rateForm.unit || undefined);
          setRateForm((v) => ({ ...v, price: "" }));
          setRatePreview(null);
        }); }}>
        <label className="tf-field-label">Supplier
          <select required className="tf-field w-full" value={rateForm.supplier} onChange={(e) => setRateForm({ ...rateForm, supplier: e.target.value })}>
            <option value="">Select supplier</option>
            {suppliers.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        </label>
        <label className="tf-field-label">Material
          <select required className="tf-field w-full" value={rateForm.material} onChange={(e) => setRateForm({ ...rateForm, material: e.target.value })}>
            <option value="">Select material</option>
            {materials.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
          </select>
        </label>
        <ScopeFields idPrefix="rate" projects={projects} project={rateForm.project} from={rateForm.from} until={rateForm.until}
          onChange={(project, from, until) => { setRateForm({ ...rateForm, project, from, until }); setRatePreview(null); }} />
        <label className="tf-field-label">Priced per
          <select className="tf-field w-full" value={rateForm.unit} onChange={(e) => { setRateForm({ ...rateForm, unit: e.target.value }); setRatePreview(null); }}>
            <option value="">Leave as is (ton when new)</option>
            {Object.entries(UNIT_LABEL).map(([key, label]) => <option key={key} value={key}>{label.per[0].toUpperCase() + label.per.slice(1)}</option>)}
          </select>
        </label>
        <label className="tf-field-label">{`Unit price ($/${UNIT_LABEL[rateForm.unit || "ton"].per})`}
          <input required className="tf-field w-full" inputMode="decimal" placeholder="30.00"
            value={rateForm.price} onChange={(e) => { setRateForm({ ...rateForm, price: e.target.value }); setRatePreview(null); }} />
        </label>
        <label className="tf-field-label">Freight
          <select className="tf-field w-full" value={rateForm.freight} onChange={(e) => setRateForm({ ...rateForm, freight: e.target.value })}>
            <option value="">Leave as is / not stated</option><option value="included">Included (delivered price)</option><option value="separate">Billed separately</option>
          </select>
        </label>
        <div className="flex gap-2">
          <button type="button" className="tf-secondary-button" disabled={saving || !rateForm.supplier || !rateForm.material || !rateForm.price}
            onClick={() => void act(async () => setRatePreview(await previewRate(Number(rateForm.supplier), Number(rateForm.material), rateForm.price,
              scopeOf(rateForm.project, rateForm.from, rateForm.until))))}>Preview effect</button>
          <button type="submit" className="tf-primary-button" disabled={saving || !rateForm.supplier || !rateForm.material || !rateForm.price}>Save unit price</button>
        </div>
      </form>}
      {canManage && ratePreview && <div className="border-b border-[#e9edf0] px-5 py-4" data-testid="rate-preview" role="status">
        <p className="text-[12.5px] text-[#263646]">{ratePreview.tickets === 0
          ? <>No ticket waiting for approval would change price. {ratePreview.note}</>
          : <><b>{ratePreview.tickets}</b> ticket{ratePreview.tickets === 1 ? "" : "s"} waiting for approval would change price
            {ratePreview.newly_priced ? <> ({ratePreview.newly_priced} priced for the first time)</> : null}: {money(ratePreview.cost_change)} in total. {ratePreview.note}</>}</p>
        {ratePreview.rows.length > 0 && <div className="tf-table-wrap mt-2 max-h-64 overflow-auto"><table className="tf-table" aria-label="Tickets this rate would reprice">
          <thead><tr><th>Ticket</th><th>Delivered</th><th>Project</th><th className="text-right">Tons</th><th className="text-right">Now</th><th className="text-right">After</th><th className="text-right">Cost change</th></tr></thead>
          <tbody>{ratePreview.rows.map((r) => <tr key={r.ticket_id}>
            <td><Link prefetch={false} className="tf-link" href={`/upload?batch=${r.batch_id}&ticket=${r.ticket_id}`}>{r.ticket_number ?? `#${r.ticket_id}`}</Link></td>
            <td className="tabular-nums">{r.delivery_date ?? "—"}</td><td>{r.project ?? "—"}</td><td className="text-right tabular-nums">{r.tons ?? "—"}</td>
            <td className="text-right tabular-nums">{money(r.price_now)}</td><td className="text-right tabular-nums">{money(r.price_after)}</td>
            <td className="text-right tabular-nums">{r.cost_change ? money(r.cost_change) : "—"}</td>
          </tr>)}</tbody>
        </table></div>}
        {ratePreview.truncated && <p className="mt-1 text-[11px] text-[#5f6c7b]">Showing the first {ratePreview.rows.length}.</p>}
      </div>}
      <div className="tf-table-wrap"><table className="tf-table">
        <thead><tr><th>Supplier</th><th>Material</th><th>Applies to</th><th>From</th><th>Until</th><th className="text-right">Rate</th><th className="text-right">Action</th></tr></thead>
        <tbody>{schedule.map((r) => <tr key={r.id}>
          <td className="font-semibold">{supplierName(r.supplier_id)}</td>
          <td>{materialName(r.material_id)}</td>
          <td>{r.project_id == null ? <span className="text-[#5d6b7b]">All projects</span>
            : <span className="tf-status tf-status-open">{projectLabel(r.project_id)}</span>}</td>
          <td className="whitespace-nowrap tabular-nums">{r.effective_from ? longDate(r.effective_from) : <span className="text-[#5d6b7b]">Any date</span>}</td>
          <td className="whitespace-nowrap tabular-nums">{r.effective_to ? longDate(r.effective_to) : <span className="text-[#5d6b7b]">Open-ended</span>}</td>
          <td className="text-right font-bold tabular-nums">{`$${Number(r.unit_price).toFixed(2)}`}<span className="font-normal text-[#5f6c7b]">/{UNIT_LABEL[r.unit ?? "ton"]?.per ?? r.unit}</span>{r.freight === "included" && <span className="block text-[10.5px] font-semibold text-[#216c43]">freight included</span>}</td>
          <td className="text-right">{canManage && <button disabled={saving} onClick={() => void remove("rates", r.id, "this rate")}
            className="text-[11px] font-semibold text-[#8a5260] hover:underline">Remove</button>}</td>
        </tr>)}</tbody>
      </table></div>
      {!rates.length && <p className="tf-empty" data-testid="rates-empty">{!canManage
        ? "No rates yet. Approved tickets show no cost until a manager or owner sets the rate for each supplier and material."
        : !suppliers.length || !materials.length ? "No rates yet. Add a supplier and a material above first, then set what that supplier charges per ton for it."
        : "No rates yet. Choose a supplier and material above and save the price per ton. Each approved ticket locks the rate in force when it's approved."}</p>}
    </section>
    {canManage && <section className="tf-panel" aria-label="Paste a rate sheet">
      <div className="tf-panel-heading">
        <div><h2 className="tf-panel-title">Paste a rate sheet</h2>
          <p className="tf-panel-description">Copy three columns from Excel: supplier, material, price per ton. For a supplier&apos;s quote on one job, choose the project and the date it starts. You&apos;ll see what changes before anything is saved.</p></div>
      </div>
      <div className="space-y-3 p-5">
        <div className="grid gap-3 sm:max-w-2xl sm:grid-cols-[1fr_150px_150px]">
          <ScopeFields idPrefix="sheet" projects={projects} project={sheetScope.project} from={sheetScope.from} until={sheetScope.until}
            onChange={(project, from, until) => { setSheetScope({ project, from, until }); setSheetPlan(null); setSheetDone(null); }} />
        </div>
        <label className="tf-field-label" htmlFor="rate-sheet">Rate sheet</label>
        <textarea id="rate-sheet" className="tf-field h-28 w-full font-mono" spellCheck={false} value={sheet}
          placeholder={"Example Aggregates\tNo. 57 Stone\t28.50"} onChange={(e) => { setSheet(e.target.value); setSheetPlan(null); setSheetDone(null); }} />
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" className="tf-secondary-button" disabled={!sheet.trim() || saving}
            onClick={() => void act(async () => { setSheetDone(null); setSheetPlan(await importRateSheet(sheet, false, scopeOf(sheetScope.project, sheetScope.from, sheetScope.until))); })}>Check this sheet</button>
          {sheetPlan && (sheetPlan.adds + sheetPlan.changes > 0) && <button type="button" className="tf-primary-button" disabled={saving}
            onClick={() => void act(async () => {
              const done = await importRateSheet(sheet, true, scopeOf(sheetScope.project, sheetScope.from, sheetScope.until));
              setSheetDone(`Saved ${done.adds} new and ${done.changes} changed rate(s). Tickets already approved keep their locked prices.`);
              setSheetPlan(null);
              setSheet("");
            })}>Save {sheetPlan.adds + sheetPlan.changes} rate{sheetPlan.adds + sheetPlan.changes === 1 ? "" : "s"}</button>}
          {sheetDone && <span role="status" className="text-[12px] font-semibold text-[#216c43]">{sheetDone}</span>}
        </div>
        {sheetPlan && <div>
          <p className="mb-2 text-[12px] text-[#526273]" data-testid="sheet-summary">
            {sheetPlan.adds} new · {sheetPlan.changes} changed · {sheetPlan.unchanged} unchanged
            {sheetPlan.scope && sheetPlan.scope !== "Company-wide" && <> · for {sheetPlan.scope}</>}
            {sheetPlan.new_suppliers.length > 0 && <> · adds supplier{sheetPlan.new_suppliers.length === 1 ? "" : "s"}: {sheetPlan.new_suppliers.join(", ")}</>}
            {sheetPlan.new_materials.length > 0 && <> · adds material{sheetPlan.new_materials.length === 1 ? "" : "s"}: {sheetPlan.new_materials.join(", ")}</>}
          </p>
          <div className="tf-table-wrap max-h-72 overflow-auto"><table className="tf-table" aria-label="What this sheet would change">
            <thead><tr><th>Supplier</th><th>Material</th><th className="text-right">Now</th><th className="text-right">New</th><th>Change</th></tr></thead>
            <tbody>{sheetPlan.plan.map((row) => <tr key={row.line}>
              <td className="font-semibold">{row.supplier}</td><td>{row.material}</td>
              <td className="text-right tabular-nums">{money(row.old_price)}</td><td className="text-right tabular-nums">{money(row.price)}</td>
              <td><span className={`tf-status ${row.action === "change" ? "tf-status-attention" : row.action === "add" ? "tf-status-open" : ""}`}>{row.action === "add" ? "New" : row.action === "change" ? "Changed" : "Unchanged"}</span></td>
            </tr>)}</tbody>
          </table></div>
        </div>}
      </div>
    </section>}

    {timeline && timeline.pairs.length > 0 && <section className="tf-panel" aria-label="Pricing timeline">
      <div className="tf-panel-heading">
        <div><h2 className="tf-panel-title">Pricing timeline</h2>
          <p className="tf-panel-description">Which price applies when, for each supplier and material. A later rate takes over from its start; after a rate&apos;s last day there is no price until the next one. An expired project agreement never falls back to the company rate.</p></div>
        <span className={`tf-status ${timeline.pairs.some((p) => p.warnings) ? "tf-status-attention" : ""}`}>
          {timeline.pairs.reduce((n, p) => n + p.warnings, 0)} to check</span>
      </div>
      <ul className="divide-y divide-[#edf0f2]">{[...timeline.pairs].sort((a, b) => b.warnings - a.warnings || b.waiting_unpriced - a.waiting_unpriced).map((pair) =>
        <li key={pair.supplier + pair.material} className="space-y-2 px-5 py-3" data-testid="timeline-pair">
          <div className="flex flex-wrap items-baseline gap-x-3">
            <span className="text-[13px] font-semibold text-[#192632]">{pair.supplier} · {pair.material}</span>
            {pair.waiting_unpriced > 0 && <span className="tf-status tf-status-error">{pair.waiting_unpriced} waiting ticket{pair.waiting_unpriced === 1 ? "" : "s"} can&apos;t be priced</span>}
          </div>
          {pair.scopes.map((scope) => <div key={scope.label} className="grid gap-1 sm:grid-cols-[150px_minmax(0,1fr)]">
            <span className="text-[12px] text-[#5d6b7b]">{scope.label}</span>
            <div className="min-w-0 space-y-1">
              <ol className="flex flex-wrap gap-1.5">{scope.segments.map((seg) =>
                <li key={seg.rate_id} className={`rounded-[2px] border px-2 py-1 text-[11.5px] tabular-nums ${seg.state === "current" ? "border-[#cce4d2] bg-[#edf5ef] text-[#1d5e3b]" : seg.state === "future" ? "border-[#cdd4db] bg-white text-[#455467]" : "border-[#e1e5e9] bg-[#f5f6f7] text-[#7a8794]"}`}>
                  <b>{money(seg.price)}</b> {seg.from ? longDate(seg.from) : "start"} – {seg.to ? longDate(seg.to) : "open-ended"}
                  {seg.state === "current" && <span className="sr-only"> (in force today)</span>}
                </li>)}</ol>
              {scope.warnings.map((w, n) => <p key={n} className={`text-[11.5px] ${w.kind === "expired" || w.kind === "gap" ? "text-[#a73631]" : "text-[#995a18]"}`}>{w.text}</p>)}
            </div>
          </div>)}
        </li>)}</ul>
    </section>}

    <section className="tf-panel">
      <div className="tf-panel-heading">
        <div><h2 className="tf-panel-title">Other spellings</h2>
          <p className="tf-panel-description">When a ticket uses one of these spellings, the review screen offers your catalog name. A person always applies it; saved tickets and locked prices never change on their own.</p></div>
      </div>
      {canManage && <form className="grid items-end gap-3 border-b border-[#e9edf0] p-4 sm:grid-cols-[140px_1fr_1fr_auto]"
        onSubmit={(e) => { e.preventDefault(); void act(async () => {
          await addAlias({ kind: aliasForm.kind, alias: aliasForm.alias, target_id: Number(aliasForm.target) });
          setAliasForm({ ...aliasForm, alias: "", target: "" });
        }); }}>
        <label className="tf-field-label">Type
          <select className="tf-field w-full" value={aliasForm.kind}
            onChange={(e) => setAliasForm({ kind: e.target.value as "supplier" | "material", alias: aliasForm.alias, target: "" })}>
            <option value="supplier">Supplier</option>
            <option value="material">Material</option>
          </select>
        </label>
        <label className="tf-field-label">Spelling on tickets
          <input required maxLength={200} className="tf-field w-full" placeholder="Ex. Agg. North Yard" value={aliasForm.alias}
            onChange={(e) => setAliasForm({ ...aliasForm, alias: e.target.value })} />
        </label>
        <label className="tf-field-label">Means
          <select required className="tf-field w-full" value={aliasForm.target} onChange={(e) => setAliasForm({ ...aliasForm, target: e.target.value })}>
            <option value="">Select from your list</option>
            {(aliasForm.kind === "supplier" ? suppliers : materials).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select>
        </label>
        <button type="submit" className="tf-secondary-button" disabled={saving || !aliasForm.alias.trim() || !aliasForm.target}>Save spelling</button>
      </form>}
      {aliases.some((a) => !a.removed_at) ? <div className="tf-table-wrap"><table className="tf-table" aria-label="Other spellings">
        <thead><tr><th>Type</th><th>Spelling on tickets</th><th>Means</th><th>Added by</th><th className="text-right">Action</th></tr></thead>
        <tbody>{aliases.filter((a) => !a.removed_at).map((a) => <tr key={a.id}>
          <td className="capitalize">{a.kind}</td><td className="font-semibold">{a.alias}</td><td>{a.target}</td><td>{a.added_by ?? "—"}</td>
          <td className="text-right">{canManage && <button disabled={saving} onClick={() => void act(() => removeAlias(a.id))}
            className="text-[11px] font-semibold text-[#8a5260] hover:underline">Remove</button>}</td>
        </tr>)}</tbody>
      </table></div> : <p className="tf-empty">No other spellings saved.</p>}
    </section>
    <section className="tf-panel">
      <div className="tf-panel-heading">
        <div><h2 className="tf-panel-title">Price change history</h2>
          <p className="tf-panel-description">Who changed the catalog, and when. Changes made before this history existed were not recorded.</p></div>
      </div>
      {history.length === 0 ? <p className="tf-empty">No price changes recorded yet.</p> : <div className="tf-table-wrap"><table className="tf-table">
        <thead><tr><th>When</th><th>Change</th><th className="text-right">Old rate</th><th className="text-right">New rate</th><th>Changed by</th></tr></thead>
        <tbody>{history.slice(0, 50).map((e) => <tr key={e.id}>
          <td className="whitespace-nowrap text-[#5d6b7b]">{new Date(e.at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}</td>
          <td><span className="font-semibold text-[#263646]">{ACTION_LABEL[e.action] ?? e.action}</span>
            <span className="ml-2 text-[#5d6b7b]">{[e.supplier, e.material].filter(Boolean).join(" · ")}</span>
            {e.scope && e.scope !== "Company-wide" && <span className="ml-2 text-[11px] text-[#5d6b7b]">({e.scope})</span>}</td>
          <td className="text-right tabular-nums">{money(e.old)}</td>
          <td className="text-right font-semibold tabular-nums">{money(e.new)}</td>
          <td>{e.actor ?? "—"}</td>
        </tr>)}</tbody>
      </table></div>}
    </section>
    </>}
    {tab === "material" && scoped && <p className="text-[11px] leading-5 text-[#5f6c7b]">A project agreement that starts after a delivery gives way to the company rate for that delivery. A ticket with no delivery date can&apos;t be priced from a rate that changes over time; the review screen says so.</p>}
    {tab === "material" && <p className="text-[11px] leading-5 text-[#5f6c7b]">Supplier/material matching ignores case and punctuation. Calculated material costs are verified tons multiplied by the rate locked at acceptance; taxes and delivery charges are excluded.</p>}
  </div>;
}

/** Hourly trucking agreements, beside material pricing so every rate lives in one place. */
function TruckingRates({ canPrice }: { canPrice: boolean }) {
  const [haulers, setHaulers] = useState<HaulerRow[]>([]);
  const [rates, setRates] = useState<HaulingRate[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      const [h, r, p] = await Promise.all([listHaulers(), listHaulingRates(), listProjects()]);
      setHaulers(h); setRates(r); setProjects(p); setError(null);
    } catch (e) { setError((e as Error).message); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  return <>
    {error && <div role="alert" className="tf-alert tf-alert-bad">{error}</div>}
    <HaulingRates haulers={haulers} rates={rates} projects={projects} canPrice={canPrice} onChanged={load} />
    <p className="text-[11px] leading-5 text-[#5f6c7b]">A material rate marked &ldquo;freight included&rdquo; (Material pricing tab) is never charged hauling again. See how each billing method is counted in the trucking rates description above.</p>
  </>;
}
