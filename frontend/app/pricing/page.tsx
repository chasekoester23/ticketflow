"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Material, Rate, Supplier, createMaterial, createSupplier, deleteItem,
  listMaterials, listRates, listSuppliers, saveRate,
} from "../../lib/api";

export default function PricingPage() {
  const [suppliers, setSuppliers] = useState<Supplier[]>([]);
  const [materials, setMaterials] = useState<Material[]>([]);
  const [rates, setRates] = useState<Rate[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [newSupplier, setNewSupplier] = useState("");
  const [newMaterial, setNewMaterial] = useState("");
  const [rateForm, setRateForm] = useState({ supplier: "", material: "", price: "" });
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const [s, m, r] = await Promise.all([listSuppliers(), listMaterials(), listRates()]);
      setSuppliers(s); setMaterials(m); setRates(r);
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
      <div><p className="tf-eyebrow">COST MANAGEMENT</p><h1 className="tf-page-title">Suppliers & rates</h1>
        <p className="tf-page-description">Maintain the supplier and material price catalog used during ticket verification.</p></div>
      <a className="tf-secondary-button" href="/records">View delivery records <span aria-hidden="true">→</span></a>
    </div>
    {error && <div role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">{error}</div>}

    <div className="grid gap-5 lg:grid-cols-2">
      <section className="tf-panel">
        <div className="tf-panel-heading">
          <div><h2 className="tf-panel-title">Suppliers</h2><p className="tf-panel-description">Companies delivering material to your jobs</p></div>
          <span className="tf-status">{suppliers.length} suppliers</span>
        </div>
        <form className="flex gap-2 border-b border-[#e9edf0] p-4"
          onSubmit={(e) => { e.preventDefault(); void act(async () => { await createSupplier(newSupplier.trim()); setNewSupplier(""); }); }}>
          <label className="sr-only" htmlFor="new-supplier">Supplier name</label>
          <input id="new-supplier" required className="tf-field min-w-0 flex-1" placeholder="Add supplier name"
            value={newSupplier} onChange={(e) => setNewSupplier(e.target.value)} />
          <button type="submit" className="tf-secondary-button" disabled={!newSupplier.trim() || saving}>+ Add</button>
        </form>
        {suppliers.length ? <ul className="divide-y divide-[#edf0f2]">
          {suppliers.map((s) => <li key={s.id} className="flex items-center justify-between gap-4 px-5 py-3">
            <span className="truncate text-[12px] font-semibold text-[#354555]">{s.name}</span>
            <button disabled={saving} onClick={() => void remove("suppliers", s.id, s.name)}
              className="text-[11px] font-semibold text-[#8a5260] hover:underline">Remove</button>
          </li>)}
        </ul> : <p className="tf-empty">No suppliers yet. Add a company above to begin pricing.</p>}
      </section>

      <section className="tf-panel">
        <div className="tf-panel-heading">
          <div><h2 className="tf-panel-title">Material types</h2><p className="tf-panel-description">Match the material descriptions on your tickets</p></div>
          <span className="tf-status">{materials.length} materials</span>
        </div>
        <form className="flex gap-2 border-b border-[#e9edf0] p-4"
          onSubmit={(e) => { e.preventDefault(); void act(async () => { await createMaterial(newMaterial.trim()); setNewMaterial(""); }); }}>
          <label className="sr-only" htmlFor="new-material">Material name</label>
          <input id="new-material" required className="tf-field min-w-0 flex-1" placeholder="e.g. No. 53 Crushed Stone"
            value={newMaterial} onChange={(e) => setNewMaterial(e.target.value)} />
          <button type="submit" className="tf-secondary-button" disabled={!newMaterial.trim() || saving}>+ Add</button>
        </form>
        {materials.length ? <ul className="divide-y divide-[#edf0f2]">
          {materials.map((m) => <li key={m.id} className="flex items-center justify-between gap-4 px-5 py-3">
            <div><span className="text-[12px] font-semibold text-[#354555]">{m.name}</span><span className="ml-2 text-[11px] text-[#8994a0]">/{m.unit}</span></div>
            <button disabled={saving} onClick={() => void remove("materials", m.id, m.name)}
              className="text-[11px] font-semibold text-[#8a5260] hover:underline">Remove</button>
          </li>)}
        </ul> : <p className="tf-empty">No materials yet. Add a material type above.</p>}
      </section>
    </div>

    <section className="tf-panel">
      <div className="tf-panel-heading">
        <div><h2 className="tf-panel-title">Unit price schedule</h2>
          <p className="tf-panel-description">Prices applied to new approvals; accepted records retain their historical rate</p></div>
        <span className="tf-status">{rates.length} rates</span>
      </div>
      <form className="grid items-end gap-3 border-b border-[#e9edf0] p-5 sm:grid-cols-2 xl:grid-cols-[1fr_1fr_160px_auto]"
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
      </form>
      <div className="tf-table-wrap"><table className="tf-table">
        <thead><tr><th>Supplier</th><th>Material</th><th className="text-right">Rate / ton</th><th className="text-right">Action</th></tr></thead>
        <tbody>{rates.map((r) => <tr key={r.id}>
          <td className="font-semibold">{supplierName(r.supplier_id)}</td>
          <td>{materialName(r.material_id)}</td>
          <td className="text-right font-bold tabular-nums">{`$${Number(r.unit_price).toFixed(2)}`}</td>
          <td className="text-right"><button disabled={saving} onClick={() => void remove("rates", r.id, "this rate")}
            className="text-[11px] font-semibold text-[#8a5260] hover:underline">Remove</button></td>
        </tr>)}</tbody>
      </table></div>
      {!rates.length && <p className="tf-empty">No unit prices configured. Add a supplier and material above, then save a rate.</p>}
    </section>
    <p className="text-[11px] leading-5 text-[#768391]">Supplier/material matching ignores case and punctuation. Calculated material costs are verified tons multiplied by the rate locked at acceptance; taxes and delivery charges are excluded.</p>
  </div>;
}
