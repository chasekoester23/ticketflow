"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Material,
  Rate,
  Supplier,
  createMaterial,
  createSupplier,
  deleteItem,
  listMaterials,
  listRates,
  listSuppliers,
  saveRate,
} from "../../lib/api";

export default function PricingPage() {
  const [suppliers, setSuppliers] = useState<Supplier[]>([]);
  const [materials, setMaterials] = useState<Material[]>([]);
  const [rates, setRates] = useState<Rate[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [newSupplier, setNewSupplier] = useState("");
  const [newMaterial, setNewMaterial] = useState("");
  const [rateForm, setRateForm] = useState({ supplier: "", material: "", price: "" });

  const load = useCallback(async () => {
    try {
      const [s, m, r] = await Promise.all([listSuppliers(), listMaterials(), listRates()]);
      setSuppliers(s);
      setMaterials(m);
      setRates(r);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  const act = (fn: () => Promise<void>) => async () => {
    setError(null);
    try {
      await fn();
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const sName = (id: number) => suppliers.find((s) => s.id === id)?.name ?? id;
  const mName = (id: number) => materials.find((m) => m.id === id)?.name ?? id;
  const box = "rounded-lg border border-slate-200 bg-white p-5";
  const input = "rounded border border-slate-300 px-3 py-2 text-sm";
  const btn = "rounded bg-slate-900 px-3 py-2 text-sm font-medium text-white disabled:opacity-40";

  return (
    <div className="space-y-6">
      {error && <div className="rounded border border-red-200 bg-red-50 px-4 py-2 text-sm text-red-800">{error}</div>}
      <p className="text-sm text-slate-600">
        Names must match what appears on tickets (case and punctuation are ignored). Material cost = confirmed tons × unit price.
        It does not include delivery charges or taxes.
      </p>
      <div className="grid gap-6 md:grid-cols-2">
        <section className={box}>
          <h2 className="font-semibold">Suppliers</h2>
          <div className="mt-3 flex gap-2">
            <input className={input} placeholder="Supplier name" value={newSupplier} onChange={(e) => setNewSupplier(e.target.value)} />
            <button className={btn} disabled={!newSupplier.trim()} onClick={act(async () => { await createSupplier(newSupplier.trim()); setNewSupplier(""); })}>Add</button>
          </div>
          <ul className="mt-3 space-y-1 text-sm">
            {suppliers.map((s) => (
              <li key={s.id} className="flex justify-between">
                {s.name}
                <button className="text-red-700" onClick={act(() => deleteItem("suppliers", s.id))}>Delete</button>
              </li>
            ))}
          </ul>
        </section>
        <section className={box}>
          <h2 className="font-semibold">Materials</h2>
          <div className="mt-3 flex gap-2">
            <input className={input} placeholder="e.g. No. 53 Stone" value={newMaterial} onChange={(e) => setNewMaterial(e.target.value)} />
            <button className={btn} disabled={!newMaterial.trim()} onClick={act(async () => { await createMaterial(newMaterial.trim()); setNewMaterial(""); })}>Add</button>
          </div>
          <ul className="mt-3 space-y-1 text-sm">
            {materials.map((m) => (
              <li key={m.id} className="flex justify-between">
                {m.name}
                <button className="text-red-700" onClick={act(() => deleteItem("materials", m.id))}>Delete</button>
              </li>
            ))}
          </ul>
        </section>
      </div>
      <section className={box}>
        <h2 className="font-semibold">Unit prices ($ per ton)</h2>
        <div className="mt-3 flex flex-wrap gap-2">
          <select className={input} value={rateForm.supplier} onChange={(e) => setRateForm({ ...rateForm, supplier: e.target.value })}>
            <option value="">Supplier…</option>
            {suppliers.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
          <select className={input} value={rateForm.material} onChange={(e) => setRateForm({ ...rateForm, material: e.target.value })}>
            <option value="">Material…</option>
            {materials.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
          </select>
          <input className={input} inputMode="decimal" placeholder="30.00" value={rateForm.price} onChange={(e) => setRateForm({ ...rateForm, price: e.target.value })} />
          <button
            className={btn}
            disabled={!rateForm.supplier || !rateForm.material || !rateForm.price}
            onClick={act(async () => { await saveRate(Number(rateForm.supplier), Number(rateForm.material), rateForm.price); setRateForm({ ...rateForm, price: "" }); })}
          >
            Save price
          </button>
        </div>
        <table className="mt-4 w-full text-left text-sm">
          <thead className="text-xs uppercase text-slate-500">
            <tr><th className="py-1">Supplier</th><th>Material</th><th>$ / ton</th><th /></tr>
          </thead>
          <tbody>
            {rates.map((r) => (
              <tr key={r.id} className="border-t border-slate-100">
                <td className="py-1">{sName(r.supplier_id)}</td>
                <td>{mName(r.material_id)}</td>
                <td>{r.unit_price}</td>
                <td className="text-right"><button className="text-red-700" onClick={act(() => deleteItem("rates", r.id))}>Delete</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}
