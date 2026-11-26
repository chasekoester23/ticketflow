"use client";

import { useState } from "react";
import {
  EQUIPMENT_LABEL, HaulerRow, HaulingRate, Project, createHauler, createHaulingRate, createTruck, deleteHaulingRate,
} from "../../lib/api";

// Display only: server decimals are shown, never added up here.
const money = (v: string | null) => v === null ? "—" : "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const hours = (v: string | null) => v === null ? "—" : `${Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 })} h`;
const day = (iso: string) => new Date(`${iso.slice(0, 10)}T00:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });

/** Trucking companies and their trucks. Managed in Suppliers & Haulers. */
export function HaulerSetup({ haulers, canEdit, onChanged }: { haulers: HaulerRow[]; canEdit: boolean; onChanged: () => Promise<void> }) {
  const [error, setError] = useState<string | null>(null);
  const [hauler, setHauler] = useState("");
  const [truck, setTruck] = useState({ hauler_id: "", identifier: "", equipment_type: "quad_axle" });
  const act = (fn: () => Promise<void>) => fn().then(onChanged).then(() => setError(null)).catch((e) => setError((e as Error).message));
  if (!canEdit) return null;
  return <div className="space-y-2 border-t border-[#dce1e6] px-4 py-3" aria-label="Add haulers and trucks" role="group">
    {error && <p role="alert" className="tf-alert tf-alert-bad">{error}</p>}
    <div className="grid gap-4 lg:grid-cols-2">
      <form className="flex items-end gap-2" onSubmit={(e) => { e.preventDefault(); void act(async () => { await createHauler(hauler); setHauler(""); }); }}>
        <label className="tf-field-label flex-1">New hauler<input className="tf-field w-full" value={hauler} onChange={(e) => setHauler(e.target.value)} placeholder="Trucking company name" /></label>
        <button className="tf-secondary-button" disabled={!hauler.trim()}>+ Add hauler</button></form>
      <form className="grid grid-cols-[1fr_90px_1fr_auto] items-end gap-2" onSubmit={(e) => { e.preventDefault(); void act(async () => { await createTruck(Number(truck.hauler_id), truck.identifier, truck.equipment_type); setTruck({ ...truck, identifier: "" }); }); }}>
        <label className="tf-field-label">Hauler<select className="tf-field w-full" value={truck.hauler_id} onChange={(e) => setTruck({ ...truck, hauler_id: e.target.value })}>
          <option value="">Select</option>{haulers.map((h) => <option key={h.id} value={h.id}>{h.name}</option>)}</select></label>
        <label className="tf-field-label">Truck #<input className="tf-field w-full" value={truck.identifier} onChange={(e) => setTruck({ ...truck, identifier: e.target.value })} /></label>
        <label className="tf-field-label">Type<select className="tf-field w-full" value={truck.equipment_type} onChange={(e) => setTruck({ ...truck, equipment_type: e.target.value })}>
          {Object.entries(EQUIPMENT_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
        <button className="tf-secondary-button" disabled={!truck.hauler_id || !truck.identifier.trim()}>+ Add truck</button></form>
    </div>
  </div>;
}

/** Hourly trucking agreements: the trucking side of Rates & Pricing. */
export function HaulingRates({ haulers, rates, projects, canPrice, onChanged }: {
  haulers: HaulerRow[]; rates: HaulingRate[]; projects: Project[]; canPrice: boolean; onChanged: () => Promise<void>;
}) {
  const [error, setError] = useState<string | null>(null);
  const [rate, setRate] = useState({ billing_method: "hourly" as "hourly" | "per_ton" | "per_load", hauler_id: "", project_id: "", equipment_type: "", hourly_rate: "",
    unit_rate: "", minimum_hours: "", standby_rate: "", effective_from: "", effective_to: "", billing_increment: "", increment_rounding: "up" });
  const hourly = rate.billing_method === "hourly";
  const act = (fn: () => Promise<void>) => fn().then(onChanged).then(() => setError(null)).catch((e) => setError((e as Error).message));
  const haulerName = (id: number) => haulers.find((h) => h.id === id)?.name ?? `#${id}`;
  const code = (id: number | null) => id === null ? "All projects" : projects.find((p) => p.id === id)?.code ?? `#${id}`;
  return <section className="tf-panel" aria-label="Trucking rates">
    <div className="tf-panel-heading"><div><h2 className="tf-panel-title">Trucking rates</h2>
      <p className="tf-panel-description"><b>By the hour</b>: prices truck shifts from time sheets, by the most specific agreement that has started (project and truck type, then project, then truck type, then any).
        {" "}<b>Per ton</b> or <b>per load</b>: prices each approved delivery that names the hauler. A delivery is never charged twice: not when the material price includes freight, and not when it rode on an hourly shift.</p></div></div>
    <div className="space-y-4 p-4">
      {error && <p role="alert" className="tf-alert tf-alert-bad">{error}</p>}
      {canPrice && (haulers.length ? <form className="grid items-end gap-2 sm:grid-cols-4 xl:grid-cols-[1.2fr_1fr_1fr_100px_90px_100px_130px_130px_auto]" onSubmit={(e) => {
        e.preventDefault();
        void act(() => createHaulingRate(hourly ? { billing_method: "hourly", hauler_id: Number(rate.hauler_id), project_id: rate.project_id ? Number(rate.project_id) : null,
          equipment_type: rate.equipment_type || null, hourly_rate: rate.hourly_rate, minimum_hours: rate.minimum_hours || null,
          standby_rate: rate.standby_rate || null, effective_from: rate.effective_from || null, effective_to: rate.effective_to || null,
          billing_increment: rate.billing_increment || null, increment_rounding: rate.billing_increment ? rate.increment_rounding as "up" | "nearest" : null }
          : { billing_method: rate.billing_method, hauler_id: Number(rate.hauler_id), project_id: rate.project_id ? Number(rate.project_id) : null,
            equipment_type: null, hourly_rate: null, unit_rate: rate.unit_rate, minimum_hours: null, standby_rate: null,
            effective_from: rate.effective_from || null, effective_to: rate.effective_to || null, billing_increment: null, increment_rounding: null }));
      }}>
        <label className="tf-field-label">Billed<select className="tf-field w-full" value={rate.billing_method} onChange={(e) => setRate({ ...rate, billing_method: e.target.value as typeof rate.billing_method })}>
          <option value="hourly">By the hour</option><option value="per_ton">Per ton</option><option value="per_load">Per load</option></select></label>
        <label className="tf-field-label">Hauler<select className="tf-field w-full" value={rate.hauler_id} onChange={(e) => setRate({ ...rate, hauler_id: e.target.value })}>
          <option value="">Select</option>{haulers.map((h) => <option key={h.id} value={h.id}>{h.name}</option>)}</select></label>
        <label className="tf-field-label">Project<select className="tf-field w-full" value={rate.project_id} onChange={(e) => setRate({ ...rate, project_id: e.target.value })}>
          <option value="">All projects</option>{projects.map((p) => <option key={p.id} value={p.id}>{p.code}</option>)}</select></label>
        {hourly ? <>
          <label className="tf-field-label">Truck type<select className="tf-field w-full" value={rate.equipment_type} onChange={(e) => setRate({ ...rate, equipment_type: e.target.value })}>
            <option value="">Any</option>{Object.entries(EQUIPMENT_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
          <label className="tf-field-label">$ / hour<input required inputMode="decimal" className="tf-field w-full" value={rate.hourly_rate} onChange={(e) => setRate({ ...rate, hourly_rate: e.target.value })} /></label>
          <label className="tf-field-label">Min. hours<input inputMode="decimal" className="tf-field w-full" value={rate.minimum_hours} onChange={(e) => setRate({ ...rate, minimum_hours: e.target.value })} /></label>
          <label className="tf-field-label">Standby $/h<input inputMode="decimal" className="tf-field w-full" value={rate.standby_rate} onChange={(e) => setRate({ ...rate, standby_rate: e.target.value })} /></label>
        </> : <label className="tf-field-label">{rate.billing_method === "per_ton" ? "$ / ton" : "$ / load"}<input required inputMode="decimal" className="tf-field w-full" value={rate.unit_rate} onChange={(e) => setRate({ ...rate, unit_rate: e.target.value })} /></label>}
        <label className="tf-field-label">From<input type="date" className="tf-field w-full" value={rate.effective_from} onChange={(e) => setRate({ ...rate, effective_from: e.target.value })} /></label>
        <label className="tf-field-label">Until<input type="date" className="tf-field w-full" value={rate.effective_to} onChange={(e) => setRate({ ...rate, effective_to: e.target.value })} /></label>
        {hourly && <label className="tf-field-label">Billed in<select className="tf-field w-full" value={rate.billing_increment} onChange={(e) => setRate({ ...rate, billing_increment: e.target.value })}>
          <option value="">Hundredths</option><option value="0.25">Quarter hours</option><option value="0.5">Half hours</option><option value="1">Whole hours</option></select></label>}
        {hourly && rate.billing_increment && <label className="tf-field-label">Rounded<select className="tf-field w-full" value={rate.increment_rounding} onChange={(e) => setRate({ ...rate, increment_rounding: e.target.value })}>
          <option value="up">Up</option><option value="nearest">To nearest</option></select></label>}
        <button className="tf-primary-button" disabled={!rate.hauler_id || !(hourly ? rate.hourly_rate : rate.unit_rate)}>Save rate</button>
      </form> : <p className="tf-notice">Add the trucking company in Suppliers &amp; Haulers first, then its hourly rate here.</p>)}
      {rates.length > 0 ? <div className="tf-table-wrap"><table className="tf-table" aria-label="Trucking rates">
        <thead><tr><th>Hauler</th><th>Billed</th><th>Applies to</th><th>Truck type</th><th className="text-right">Rate</th><th className="text-right">Minimum</th><th className="text-right">Standby</th><th>From</th><th>Until</th><th>Increment</th><th /></tr></thead>
        <tbody>{rates.map((r) => <tr key={r.id}><td className="font-semibold">{haulerName(r.hauler_id)}</td>
          <td>{r.billing_method === "per_ton" ? "Per ton" : r.billing_method === "per_load" ? "Per load" : "By the hour"}</td><td>{code(r.project_id)}</td>
          <td>{r.equipment_type ? EQUIPMENT_LABEL[r.equipment_type] : "Any"}</td>
          <td className="text-right tabular-nums">{r.billing_method && r.billing_method !== "hourly" ? `${money(r.unit_rate ?? null)}/${r.billing_method === "per_ton" ? "t" : "load"}` : `${money(r.hourly_rate)}/h`}</td>
          <td className="text-right tabular-nums">{r.minimum_hours ? hours(r.minimum_hours) : "—"}</td>
          <td className="text-right tabular-nums">{r.standby_rate ? money(r.standby_rate) : <span className="text-[#5f6c7b]">Not payable</span>}</td>
          <td>{r.effective_from ? day(r.effective_from) : "Any date"}</td><td>{r.effective_to ? day(r.effective_to) : "Open-ended"}</td>
          <td>{r.billing_increment ? `${r.billing_increment} h, ${r.increment_rounding}` : "Hundredths"}</td>
          <td className="text-right">{canPrice && <button className="text-[11px] font-semibold text-[#8a5260] hover:underline" onClick={() => {
            if (window.confirm("Remove this agreement? Approved shifts keep the rate they were approved at.")) void act(() => deleteHaulingRate(r.id));
          }}>Remove</button>}</td></tr>)}</tbody>
      </table></div> : <p className="tf-empty">No trucking rates yet. Shifts can be entered, but not approved, until an hourly rate applies.</p>}
    </div>
  </section>;
}
