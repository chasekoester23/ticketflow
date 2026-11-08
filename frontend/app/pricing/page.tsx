"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Material, NameAliasRow, PricingEventRow, Rate, RateSheetPlan, Supplier, addAlias, createMaterial, createSupplier, deleteItem,
  getPricingHistory, importRateSheet, listAliases, listMaterials, listRates, listSuppliers, removeAlias, saveRate,
} from "../../lib/api";
import { useAccount } from "../account-context";

const ACTION_LABEL: Record<PricingEventRow["action"], string> = {
  supplier_added: "Supplier added",
  supplier_removed: "Supplier removed",
  material_added: "Material added",
  material_removed: "Material removed",
  rate_set: "Rate set",
  rate_changed: "Rate changed",
  rate_removed: "Rate removed",
};
// Display only: keep the server's decimal string, trimmed to cents where exact.
const money = (value: string | null) => {
  if (value === null) return "—";
  const [whole, frac = ""] = value.split(".");
  const trimmed = frac.replace(/0+$/, "");
  return `$${whole}.${trimmed.padEnd(2, "0")}`;
};

export default function PricingPage() {
  const canManage = useAccount().can("pricing.manage");
  const [suppliers, setSuppliers] = useState<Supplier[]>([]);
  const [materials, setMaterials] = useState<Material[]>([]);
  const [rates, setRates] = useState<Rate[]>([]);
  const [history, setHistory] = useState<PricingEventRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [newSupplier, setNewSupplier] = useState("");
  const [newMaterial, setNewMaterial] = useState("");
  const [rateForm, setRateForm] = useState({ supplier: "", material: "", price: "" });
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

  async function remove(kind: "suppliers" | "materials" | "rates", id: number, label: string) {
    if (!window.confirm(`Remove ${label}? Existing verified ticket prices remain locked at their original values.`)) return;
    await act(() => deleteItem(kind, id));
  }

  return <div className="space-y-5">
    <div className="tf-page-heading">
      <div><h1 className="tf-page-title">Suppliers & rates</h1>
        <p className="tf-page-description">Maintain the supplier and material price catalog used during ticket verification.</p></div>
      <a className="tf-secondary-button" href="/records">View delivery records <span aria-hidden="true">→</span></a>
    </div>
    {error && <div role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">{error}</div>}
    {!canManage && <p className="tf-notice" role="note">Suppliers, materials and rates are maintained by managers and owners. You can view the current schedule.</p>}

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
            <div><span className="text-[12px] font-semibold text-[#354555]">{m.name}</span><span className="ml-2 text-[11px] text-[#5f6c7b]">/{m.unit}</span></div>
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
          <p className="tf-panel-description">Prices applied to new approvals; accepted records retain their historical rate</p></div>
        <span className="tf-status">{rates.length} rates</span>
      </div>
      {canManage && <form className="grid items-end gap-3 border-b border-[#e9edf0] p-5 sm:grid-cols-2 xl:grid-cols-[1fr_1fr_160px_auto]"
        onSubmit={(e) => { e.preventDefault(); void act(async () => {
          await saveRate(Number(rateForm.supplier), Number(rateForm.material), rateForm.price);
          setRateForm((v) => ({ ...v, price: "" }));
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
        <label className="tf-field-label">Unit price ($/ton)
          <input required className="tf-field w-full" inputMode="decimal" placeholder="30.00"
            value={rateForm.price} onChange={(e) => setRateForm({ ...rateForm, price: e.target.value })} />
        </label>
        <button type="submit" className="tf-primary-button" disabled={saving || !rateForm.supplier || !rateForm.material || !rateForm.price}>Save unit price</button>
      </form>}
      <div className="tf-table-wrap"><table className="tf-table">
        <thead><tr><th>Supplier</th><th>Material</th><th className="text-right">Rate / ton</th><th className="text-right">Action</th></tr></thead>
        <tbody>{rates.map((r) => <tr key={r.id}>
          <td className="font-semibold">{supplierName(r.supplier_id)}</td>
          <td>{materialName(r.material_id)}</td>
          <td className="text-right font-bold tabular-nums">{`$${Number(r.unit_price).toFixed(2)}`}</td>
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
          <p className="tf-panel-description">Copy three columns from Excel: supplier, material, price per ton. You&apos;ll see what changes before anything is saved.</p></div>
      </div>
      <div className="space-y-3 p-5">
        <label className="tf-field-label" htmlFor="rate-sheet">Rate sheet</label>
        <textarea id="rate-sheet" className="tf-field h-28 w-full font-mono" spellCheck={false} value={sheet}
          placeholder={"Example Aggregates\tNo. 57 Stone\t28.50"} onChange={(e) => { setSheet(e.target.value); setSheetPlan(null); setSheetDone(null); }} />
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" className="tf-secondary-button" disabled={!sheet.trim() || saving}
            onClick={() => void act(async () => { setSheetDone(null); setSheetPlan(await importRateSheet(sheet, false)); })}>Check this sheet</button>
          {sheetPlan && (sheetPlan.adds + sheetPlan.changes > 0) && <button type="button" className="tf-primary-button" disabled={saving}
            onClick={() => void act(async () => {
              const done = await importRateSheet(sheet, true);
              setSheetDone(`Saved ${done.adds} new and ${done.changes} changed rate(s). Tickets already approved keep their locked prices.`);
              setSheetPlan(null);
              setSheet("");
            })}>Save {sheetPlan.adds + sheetPlan.changes} rate{sheetPlan.adds + sheetPlan.changes === 1 ? "" : "s"}</button>}
          {sheetDone && <span role="status" className="text-[12px] font-semibold text-[#216c43]">{sheetDone}</span>}
        </div>
        {sheetPlan && <div>
          <p className="mb-2 text-[12px] text-[#526273]" data-testid="sheet-summary">
            {sheetPlan.adds} new · {sheetPlan.changes} changed · {sheetPlan.unchanged} unchanged
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
            <span className="ml-2 text-[#5d6b7b]">{[e.supplier, e.material].filter(Boolean).join(" · ")}</span></td>
          <td className="text-right tabular-nums">{money(e.old)}</td>
          <td className="text-right font-semibold tabular-nums">{money(e.new)}</td>
          <td>{e.actor ?? "—"}</td>
        </tr>)}</tbody>
      </table></div>}
    </section>
    <p className="text-[11px] leading-5 text-[#5f6c7b]">Supplier/material matching ignores case and punctuation. Calculated material costs are verified tons multiplied by the rate locked at acceptance; taxes and delivery charges are excluded.</p>
  </div>;
}
