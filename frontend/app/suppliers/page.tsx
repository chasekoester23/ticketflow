"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  CostReport, HaulerRow, HaulingRate, Rate, Supplier, createHauler, createSupplier, getCostReport, listHaulers, listHaulingRates, listRates, listSuppliers,
} from "../../lib/api";
import { useAccount } from "../account-context";
import { Dialog } from "../Dialog";
import { useProject } from "../project-context";
import { HaulerSetup } from "../trucking/setup";

const money = (v: string | number) => `$${Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const tons = (v: string | number) => Number(v).toLocaleString(undefined, { maximumFractionDigits: 1 });
const key = (name: string) => name.trim().toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

type Vendor = { key: string; name: string; supplier?: Supplier; hauler?: HaulerRow; contact: string | null };

/** Add a company that supplies material, hauls, or both. The same names everyone else sees. */
function AddVendor({ canSupply, canHaul, onClose, onSaved }: { canSupply: boolean; canHaul: boolean; onClose: () => void; onSaved: () => void }) {
  const [form, setForm] = useState({ name: "", contact: "", supplies: canSupply, hauls: !canSupply && canHaul });
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  return <Dialog label="Add a company" onClose={onClose} onSubmit={async (event) => {
    event.preventDefault();
    if (saving || !form.name.trim() || !(form.supplies || form.hauls)) return;
    setSaving(true);
    setError(null);
    try {
      if (form.supplies) await createSupplier(form.name.trim(), form.contact.trim() || undefined);
      if (form.hauls) await createHauler(form.name.trim(), form.contact.trim() || undefined);
      onSaved();
    } catch (e) { setError((e as Error).message); setSaving(false); }
  }}>
    <div className="space-y-3 p-5">
      <h2 className="text-[16px] font-semibold text-[#041d2f]">Add a company</h2>
      {error && <div role="alert" className="tf-alert tf-alert-bad">{error}</div>}
      <label className="tf-field-label">Company name<input className="tf-field w-full" required maxLength={200} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
      <label className="tf-field-label">Contact <span className="font-normal text-[#5f6c7b]">(name, phone or email)</span>
        <input className="tf-field w-full" maxLength={200} value={form.contact} onChange={(e) => setForm({ ...form, contact: e.target.value })} /></label>
      <fieldset className="space-y-1 text-[13px]"><legend className="tf-field-label">Provides</legend>
        <label className="flex items-center gap-2"><input type="checkbox" disabled={!canSupply} checked={form.supplies} onChange={(e) => setForm({ ...form, supplies: e.target.checked })} /> Material (a supplier)</label>
        <label className="flex items-center gap-2"><input type="checkbox" disabled={!canHaul} checked={form.hauls} onChange={(e) => setForm({ ...form, hauls: e.target.checked })} /> Trucking (a hauler)</label>
      </fieldset>
      <p className="text-[12px] text-[#5f6c7b]">Rates are set in Rates &amp; Pricing; a hauler&apos;s trucks are added below the directory.</p>
    </div>
    <div className="flex justify-end gap-2 border-t border-[#dce1e6] bg-[#f7f8f9] px-5 py-3">
      <button type="button" className="tf-secondary-button" onClick={onClose}>Cancel</button>
      <button type="submit" className="tf-primary-button" disabled={saving || !form.name.trim() || !(form.supplies || form.hauls)}>{saving ? "Saving…" : "Add company"}</button>
    </div>
  </Dialog>;
}

/**
 * Every company the contractor buys material or trucking from, once each, with what
 * they provide and where their records are. Figures are the shared cost report's,
 * for the chosen project; rates and records open in their own screens.
 */
export default function VendorsPage() {
  const { can } = useAccount();
  const { projectId, project } = useProject();
  const [suppliers, setSuppliers] = useState<Supplier[] | null>(null);
  const [rates, setRates] = useState<Rate[]>([]);
  const [haulers, setHaulers] = useState<HaulerRow[]>([]);
  const [haulRates, setHaulRates] = useState<HaulingRate[]>([]);
  const [report, setReport] = useState<CostReport | null>(null);
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState<"" | "supplier" | "hauler">("");
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => Promise.all([listSuppliers(), listRates(), listHaulers(), listHaulingRates()])
    .then(([s, r, h, a]) => { setSuppliers(s); setRates(r); setHaulers(h); setHaulRates(a); })
    .catch((e) => setError(e.message)), []);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    let live = true;
    getCostReport({ project_id: projectId || undefined, period: "month" }).then((r) => { if (live) setReport(r); }).catch(() => { if (live) setReport(null); });
    return () => { live = false; };
  }, [projectId]);

  const vendors = useMemo(() => {
    const byKey = new Map<string, Vendor>();
    (suppliers ?? []).forEach((s) => byKey.set(key(s.name), { key: key(s.name), name: s.name, supplier: s, contact: s.contact }));
    haulers.forEach((h) => {
      const found = byKey.get(key(h.name));
      if (found) { found.hauler = h; found.contact = found.contact || h.contact; } else byKey.set(key(h.name), { key: key(h.name), name: h.name, hauler: h, contact: h.contact });
    });
    return [...byKey.values()].sort((a, b) => a.name.localeCompare(b.name));
  }, [suppliers, haulers]);
  const delivered = new Map((report?.material.by_supplier ?? []).map((g) => [key(g.name), g]));
  const hauled = new Map((report?.trucking.by_hauler ?? []).map((g) => [key(g.name), g]));
  const words = query.trim().toLowerCase();
  const shown = vendors.filter((v) => (!words || v.name.toLowerCase().includes(words)) && (!kind || (kind === "supplier" ? v.supplier : v.hauler)));
  const known = new Set(vendors.filter((v) => v.supplier).map((v) => v.key));
  const unlisted = (report?.material.by_supplier ?? []).filter((g) => g.name !== "(unknown)" && !known.has(key(g.name)));
  const canAdd = can("pricing.manage") || can("trucking.edit");

  return <div className="space-y-4">
    <div className="tf-page-heading">
      <div><h1 className="tf-page-title">Suppliers & Haulers</h1>
        <p className="tf-page-description">The companies you buy material and trucking from, what they provide, and where their records are.</p></div>
      {canAdd && <button type="button" className="tf-primary-button" onClick={() => setAdding(true)}>+ Add company</button>}
    </div>
    {error && <div role="alert" className="tf-alert tf-alert-bad">{error}</div>}

    <section className="tf-panel" aria-labelledby="vendors-title">
      <div className="tf-panel-heading flex-wrap">
        <div><h2 id="vendors-title" className="tf-panel-title">Vendor directory</h2>
          <p className="tf-panel-description">Approved activity for {project ? project.code : "all projects"}, all dates</p></div>
        <div className="flex flex-wrap items-center gap-2">
          <input type="search" className="tf-field w-[220px]" placeholder="Search companies" aria-label="Search companies" value={query} onChange={(e) => setQuery(e.target.value)} />
          <div className="tf-segmented" role="group" aria-label="Show">
            {([["", "All"], ["supplier", "Suppliers"], ["hauler", "Haulers"]] as const).map(([k, label]) =>
              <button key={k || "all"} type="button" aria-pressed={kind === k} onClick={() => setKind(k)}>{label}</button>)}
          </div>
        </div>
      </div>
      {suppliers === null ? <p className="tf-empty" role="status">Loading…</p>
        : shown.length ? <div className="tf-table-wrap"><table className="tf-table" aria-label="Vendor directory">
          <thead><tr><th>Company</th><th>Provides</th><th className="tf-hide-narrow">Contact</th><th className="text-right">Material delivered</th>
            <th className="text-right">Hauling</th><th className="text-right">Rates on file</th><th><span className="sr-only">Records</span></th></tr></thead>
          <tbody>{shown.map((v) => {
            const d = delivered.get(v.key), h = hauled.get(v.key);
            const materialRates = v.supplier ? rates.filter((r) => r.supplier_id === v.supplier!.id).length : 0;
            const truckingRates = v.hauler ? haulRates.filter((r) => r.hauler_id === v.hauler!.id).length : 0;
            return <tr key={v.key}>
              <td className="min-w-[180px] font-semibold text-[#041d2f]">{v.name}
                {v.hauler && <span className="block text-[11.5px] font-normal text-[#5f6c7b]">{v.hauler.trucks.filter((t) => t.active).length} active trucks</span>}</td>
              <td><span className="flex flex-wrap gap-1">{v.supplier && <span className="tf-status">Material</span>}{v.hauler && <span className="tf-status">Trucking</span>}</span></td>
              <td className="tf-hide-narrow">{v.contact || <span className="text-[#6b7787]">—</span>}</td>
              <td className="text-right whitespace-nowrap">{d ? <>{money(d.cost)}<span className="block text-[11px] text-[#5f6c7b]">{tons(d.tons)} t · {d.tickets} deliveries</span>
                {d.unpriced > 0 && <span className="block text-[11px] font-semibold text-[#995a18]">+ {d.unpriced} without a rate</span>}</> : <span className="text-[#6b7787]">—</span>}</td>
              <td className="text-right whitespace-nowrap">{h ? <>{money(h.cost)}<span className="block text-[11px] text-[#5f6c7b]">{Number(h.hours) ? `${Number(h.hours).toLocaleString()} h` : `${h.deliveries ?? 0} loads`}</span></> : <span className="text-[#6b7787]">—</span>}</td>
              <td className="text-right whitespace-nowrap">{v.supplier && <span className={materialRates ? "" : "font-semibold text-[#995a18]"}>{materialRates || "No"} material</span>}
                {v.supplier && v.hauler && <br />}{v.hauler && <span className={truckingRates ? "" : "font-semibold text-[#995a18]"}>{truckingRates || "No"} trucking</span>}</td>
              <td><div className="flex flex-wrap justify-end gap-x-3 gap-y-2 text-[12px]">
                {v.supplier && <Link prefetch={false} href={`/records?supplier=${encodeURIComponent(v.name)}`}>Deliveries</Link>}
                {v.hauler && <Link prefetch={false} href={`/trucking?hauler_id=${v.hauler.id}`}>Truck shifts</Link>}
                <Link prefetch={false} href="/invoices">Invoices</Link>
                <Link prefetch={false} href={v.hauler && !v.supplier ? "/pricing?tab=trucking" : "/pricing"}>Rates</Link>
              </div></td>
            </tr>;
          })}</tbody>
        </table></div>
        : <p className="tf-empty">{words || kind ? "No company matches." : "No companies yet. Add the suppliers and haulers you work with."}</p>}
      {unlisted.length > 0 && !words && <div className="border-t border-[#dce1e6] px-4 py-3 text-[12.5px] text-[#3b4a59]">
        <p className="font-semibold">Read on tickets but not in the directory</p>
        <p className="mt-1">{unlisted.map((g) => <span key={g.name} className="mr-2 inline-block"><span className="tf-status">{g.name} · {g.tickets}</span></span>)}</p>
        <p className="mt-1 text-[#5f6c7b]">Usually a spelling to merge in{" "}
          {can("tickets.edit") ? <Link prefetch={false} className="tf-link" href="/records/names">Name cleanup</Link> : "Name cleanup"}, or a company to add.</p>
      </div>}
      <HaulerSetup haulers={haulers} canEdit={can("trucking.edit")} onChanged={async () => setHaulers(await listHaulers())} />
    </section>
    {adding && <AddVendor canSupply={can("pricing.manage")} canHaul={can("trucking.edit")} onClose={() => setAdding(false)}
      onSaved={() => { setAdding(false); void load(); }} />}
  </div>;
}
