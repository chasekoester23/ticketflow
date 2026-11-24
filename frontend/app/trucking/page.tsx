"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ApiError, EQUIPMENT_LABEL, HaulerRow, HaulingRate, Project, ShiftFilters, ShiftInput, ShiftsPage, Ticket, TruckShift,
  approveShift, createHauler, createHaulingRate, createShift, createTruck, deleteHaulingRate, editShift, linkShiftDeliveries,
  listHaulers, listHaulingRates, listProjects, listShifts, reopenShift, searchRecords, shiftHistory,
} from "../../lib/api";
import { useAccount } from "../account-context";

const STATUS: Record<TruckShift["status"], [string, string]> = {
  needs_review: ["Awaiting approval", "tf-status-attention"], approved: ["Approved", "tf-status-open"], disputed: ["Disputed", "tf-status-error"],
};
// Display only: server decimals are shown, never added up here.
const money = (v: string | null) => v === null ? "—" : "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const hours = (v: string | null) => v === null ? "—" : `${Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 })} h`;
const time = (iso: string) => new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
const day = (iso: string) => new Date(`${iso.slice(0, 10)}T00:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
const EMPTY_SHIFT = { project_id: "", truck_id: "", start_at: "", end_at: "", unpaid_break_minutes: "0", standby_hours: "0",
  claimed_hours: "", loads: "", driver_name: "", field_signoff: "", cost_code: "", notes: "" };
type ShiftForm = typeof EMPTY_SHIFT;

function toInput(f: ShiftForm): ShiftInput {
  const text = (v: string) => v.trim() || null;
  return { project_id: Number(f.project_id), truck_id: Number(f.truck_id), start_at: f.start_at, end_at: f.end_at,
    unpaid_break_minutes: Number(f.unpaid_break_minutes || 0), standby_hours: f.standby_hours || "0",
    claimed_hours: text(f.claimed_hours), loads: f.loads ? Number(f.loads) : null, driver_name: text(f.driver_name),
    field_signoff: text(f.field_signoff), cost_code: text(f.cost_code), notes: text(f.notes) };
}
function fromShift(s: TruckShift): ShiftForm {
  return { project_id: String(s.project_id), truck_id: String(s.truck_id), start_at: s.start_at, end_at: s.end_at,
    unpaid_break_minutes: String(s.unpaid_break_minutes), standby_hours: s.standby_hours, claimed_hours: s.claimed_hours ?? "",
    loads: s.loads === null ? "" : String(s.loads), driver_name: s.driver_name ?? "", field_signoff: s.field_signoff ?? "",
    cost_code: s.cost_code ?? "", notes: s.notes ?? "" };
}

function Metric({ label, value, note, attention = false }: { label: string; value: string | number; note: string; attention?: boolean }) {
  return <div className={`min-w-0 border-b border-r border-[#e1e5e9] px-4 py-3 ${attention ? "bg-[#fffaf5]" : "bg-white"}`}>
    <p className="tf-stat-label">{label}</p>
    <p className={`mt-2 text-[24px] font-bold leading-none tabular-nums ${attention ? "text-[#a3511d]" : "text-[#192632]"}`}>{value}</p>
    <p className="mt-1 text-[11px] leading-4 text-[#5f6c7b]">{note}</p>
  </div>;
}

function ShiftFields({ form, set, projects, haulers }: { form: ShiftForm; set: (f: ShiftForm) => void; projects: Project[]; haulers: HaulerRow[] }) {
  const field = (key: keyof ShiftForm) => ({ value: form[key], onChange: (e: { target: { value: string } }) => set({ ...form, [key]: e.target.value }) });
  return <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
    <label className="tf-field-label">Project<select required className="tf-field w-full" {...field("project_id")}>
      <option value="">Select project</option>{projects.map((p) => <option key={p.id} value={p.id}>{p.code} · {p.name}</option>)}</select></label>
    <label className="tf-field-label">Truck<select required className="tf-field w-full" {...field("truck_id")}>
      <option value="">Select truck</option>
      {haulers.map((h) => <optgroup key={h.id} label={h.name}>{h.trucks.map((t) =>
        <option key={t.id} value={t.id}>{t.identifier} · {EQUIPMENT_LABEL[t.equipment_type] ?? t.equipment_type}</option>)}</optgroup>)}
    </select></label>
    <label className="tf-field-label">Start<input required type="datetime-local" className="tf-field w-full" {...field("start_at")} /></label>
    <label className="tf-field-label">End (next day if past midnight)<input required type="datetime-local" className="tf-field w-full" {...field("end_at")} /></label>
    <label className="tf-field-label">Unpaid break (minutes)<input inputMode="numeric" className="tf-field w-full" {...field("unpaid_break_minutes")} /></label>
    <label className="tf-field-label">Standby (hours)<input inputMode="decimal" className="tf-field w-full" {...field("standby_hours")} /></label>
    <label className="tf-field-label">Hours the hauler claims<input inputMode="decimal" className="tf-field w-full" placeholder="If the sheet says" {...field("claimed_hours")} /></label>
    <label className="tf-field-label">Loads<input inputMode="numeric" className="tf-field w-full" {...field("loads")} /></label>
    <label className="tf-field-label">Driver<input className="tf-field w-full" maxLength={100} {...field("driver_name")} /></label>
    <label className="tf-field-label">Signed off in the field by<input className="tf-field w-full" maxLength={100} placeholder="As written on the sheet" {...field("field_signoff")} /></label>
    <label className="tf-field-label">Cost code<input className="tf-field w-full" maxLength={50} {...field("cost_code")} /></label>
    <label className="tf-field-label">Notes<input className="tf-field w-full" maxLength={2000} {...field("notes")} /></label>
  </div>;
}

function Detail({ shift, projects, haulers, canEdit, canApprove, onChanged }: {
  shift: TruckShift; projects: Project[]; haulers: HaulerRow[]; canEdit: boolean; canApprove: boolean; onChanged: (s: TruckShift) => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState<ShiftForm>(fromShift(shift));
  const [work, setWork] = useState(shift.work_hours ?? "");
  const [standby, setStandby] = useState(shift.standby_hours);
  const [split, setSplit] = useState<{ project_id: string; cost_code: string; hours: string }[]>([]);
  const [reason, setReason] = useState("");
  const [candidates, setCandidates] = useState<Ticket[] | null>(null);
  const [chosen, setChosen] = useState<number[]>(shift.deliveries.ticket_ids);
  const [history, setHistory] = useState<{ field: string; old: string | null; new: string | null; at: string; actor: string | null }[]>([]);

  useEffect(() => {
    setForm(fromShift(shift)); setWork(shift.work_hours ?? ""); setStandby(shift.standby_hours); setSplit([]); setReason("");
    setChosen(shift.deliveries.ticket_ids); setCandidates(null); setError(null); setEditing(false);
    shiftHistory(shift.id).then(setHistory).catch(() => setHistory([]));
  }, [shift]);

  async function run(action: () => Promise<TruckShift>) {
    setBusy(true); setError(null);
    try { onChanged(await action()); }
    catch (e) { setError((e as Error).message); if (e instanceof ApiError && e.status === 409) setEditing(false); }
    finally { setBusy(false); }
  }
  async function findDeliveries() {
    const page = await searchRecords({ project_id: String(shift.project_id), date_from: shift.work_date, date_to: shift.end_at.slice(0, 10) },
      { page: 1, pageSize: 200, sort: "ticket_number", direction: "asc" });
    setCandidates(page.rows);
  }
  const approved = shift.status === "approved";
  return <section className="tf-panel" aria-label="Truck shift" data-testid="shift-detail">
    <div className="tf-panel-heading">
      <div className="min-w-0"><p className="tf-eyebrow">{shift.hauler}</p>
        <h2 className="tf-panel-title">Truck {shift.truck} · {day(shift.work_date)}</h2>
        <p className="tf-panel-description">{[shift.project, EQUIPMENT_LABEL[shift.equipment_type], shift.driver_name].filter(Boolean).join(" · ")}</p></div>
      <span className={`tf-status ${STATUS[shift.status][1]}`}>{STATUS[shift.status][0]}</span>
    </div>
    <div className="space-y-4 p-5">
      {shift.flags.length > 0 && !approved && <ul className="tf-alert tf-alert-warn list-disc space-y-1 pl-8" data-testid="shift-flags">
        {shift.flags.map((f) => <li key={f}>{f}</li>)}</ul>}
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-[12px] sm:grid-cols-4">
        <div><dt className="tf-stat-label">On site</dt><dd className="tabular-nums">{time(shift.start_at)} – {time(shift.end_at)}{shift.end_at.slice(0, 10) !== shift.start_at.slice(0, 10) && " (next day)"}</dd></div>
        <div><dt className="tf-stat-label">Unpaid break</dt><dd className="tabular-nums">{shift.unpaid_break_minutes} min</dd></div>
        <div><dt className="tf-stat-label">Hours on site</dt><dd className="font-semibold tabular-nums">{hours(shift.on_site_hours)}</dd></div>
        <div><dt className="tf-stat-label">Standby</dt><dd className="tabular-nums">{hours(shift.standby_hours)}</dd></div>
        <div><dt className="tf-stat-label">Hauler claims</dt><dd className="tabular-nums">{hours(shift.claimed_hours)}</dd></div>
        <div><dt className="tf-stat-label">Loads</dt><dd className="tabular-nums">{shift.loads ?? "—"}</dd></div>
        <div><dt className="tf-stat-label">Field sign-off</dt><dd>{shift.field_signoff ?? "—"}</dd></div>
        <div><dt className="tf-stat-label">Cost code</dt><dd>{shift.cost_code ?? "—"}</dd></div>
      </dl>

      {approved && shift.approved ? <div className="tf-alert tf-alert-good" data-testid="shift-approved">
        Approved by {shift.approved.by ?? "—"}: {hours(shift.approved.work_hours)} working, {hours(shift.approved.standby_hours)} standby,
        {" "}{hours(shift.approved.billable_hours)} billable at {money(shift.approved.hourly_rate)}/h = <b>{money(shift.cost)}</b>. Locked.
      </div> : <div>
        <h3 className="text-[12px] font-bold text-[#263646]">What the time sheet works out to</h3>
        {shift.cost_lines.length > 0 ? <table className="tf-table mt-1" aria-label="Cost">
          <tbody>{shift.cost_lines.map((line) => <tr key={line.label}><td>{line.label}</td>
            <td className="text-right tabular-nums">{hours(line.hours)} × {money(line.rate)}</td><td className="text-right font-semibold tabular-nums">{money(line.amount)}</td></tr>)}
            <tr><td colSpan={2} className="font-semibold">Total if approved as shown</td><td className="text-right font-bold tabular-nums">{money(shift.cost)}</td></tr></tbody>
        </table> : <p className="text-[12px] text-[#5f6c7b]">Can&apos;t be priced yet: see above.</p>}
      </div>}

      <div className="text-[12px]">
        <h3 className="font-bold text-[#263646]">Deliveries this truck carried</h3>
        <p className="text-[#5f6c7b]">{shift.deliveries.count} linked{shift.deliveries.tons ? ` · ${shift.deliveries.tons} t` : ""}
          {shift.deliveries.cost_per_ton && <> · {money(shift.deliveries.cost_per_ton)} hauling per ton</>}. Linking adds no charge: the shift&apos;s hours are the cost.</p>
        {canEdit && (candidates === null
          ? <button type="button" className="tf-link mt-1 text-[12px]" onClick={() => void findDeliveries().catch((e) => setError(e.message))}>Choose from this project&apos;s deliveries that day</button>
          : <div className="mt-2 max-h-48 overflow-auto rounded-[2px] border border-[#e1e5e9]">
            {candidates.length === 0 ? <p className="p-2 text-[#5f6c7b]">No delivery records for this project on those dates.</p>
              : candidates.map((t) => <label key={t.id} className="flex items-center gap-2 border-b border-[#eef0f2] px-2 py-1 last:border-0">
                <input type="checkbox" checked={chosen.includes(t.id)} onChange={(e) => setChosen(e.target.checked ? [...chosen, t.id] : chosen.filter((id) => id !== t.id))} />
                <span className="tabular-nums">{t.ticket_number ?? `#${t.id}`}</span><span className="text-[#5f6c7b]">{t.supplier} · {t.material_type} · {t.tonnage ?? "—"} t</span>
              </label>)}
            <div className="p-2"><button type="button" className="tf-secondary-button" disabled={busy}
              onClick={() => void run(() => linkShiftDeliveries(shift.id, chosen))}>Save linked deliveries</button></div>
          </div>)}
      </div>

      {error && <p role="alert" className="tf-alert tf-alert-bad">{error}</p>}

      {!approved && canApprove && <form className="space-y-3 border-t border-[#e9edf0] pt-4" aria-label="Approve shift" onSubmit={(e) => {
        e.preventDefault();
        void run(() => approveShift(shift.id, shift.revision, { work_hours: work, standby_hours: standby,
          ...(split.length ? { allocations: split.map((a) => ({ project_id: Number(a.project_id), cost_code: a.cost_code.trim() || null, hours: a.hours })) } : {}) }));
      }}>
        <p className="text-[12px] text-[#526273]">Approve the hours you&apos;ve checked. The time sheet&apos;s figures are filled in as a starting point; the price locks when you approve.</p>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="tf-field-label">Approved working hours<input required inputMode="decimal" className="tf-field w-full" value={work} onChange={(e) => setWork(e.target.value)} /></label>
          <label className="tf-field-label">Approved standby hours<input required inputMode="decimal" className="tf-field w-full" value={standby} onChange={(e) => setStandby(e.target.value)} /></label>
        </div>
        {split.length > 0 && <div className="space-y-2" aria-label="Split across projects">
          {split.map((a, n) => <div key={n} className="grid gap-2 sm:grid-cols-[1fr_120px_100px_auto]">
            <select className="tf-field" aria-label="Allocate to project" value={a.project_id} onChange={(e) => setSplit(split.map((x, i) => i === n ? { ...x, project_id: e.target.value } : x))}>
              {projects.map((p) => <option key={p.id} value={p.id}>{p.code}</option>)}</select>
            <input className="tf-field" aria-label="Cost code" placeholder="Cost code" value={a.cost_code} onChange={(e) => setSplit(split.map((x, i) => i === n ? { ...x, cost_code: e.target.value } : x))} />
            <input className="tf-field" aria-label="Hours" inputMode="decimal" placeholder="Hours" value={a.hours} onChange={(e) => setSplit(split.map((x, i) => i === n ? { ...x, hours: e.target.value } : x))} />
            <button type="button" className="tf-link text-[12px]" onClick={() => setSplit(split.filter((_, i) => i !== n))}>Remove</button>
          </div>)}
          <p className="text-[11px] text-[#5f6c7b]">The hours must add up to the shift&apos;s billable hours. Cost is split in proportion, to the cent.</p>
        </div>}
        <div className="flex flex-wrap gap-2">
          <button type="submit" className="tf-primary-button" disabled={busy || !work || !standby}>Approve {work ? `${work} h` : ""}</button>
          <button type="button" className="tf-secondary-button" onClick={() => setSplit([...split, { project_id: String(shift.project_id), cost_code: shift.cost_code ?? "", hours: "" }])}>
            Split across projects</button>
        </div>
      </form>}

      {approved && canApprove && <form className="flex flex-wrap items-end gap-2 border-t border-[#e9edf0] pt-4" onSubmit={(e) => { e.preventDefault(); void run(() => reopenShift(shift.id, shift.revision, reason)); }}>
        <label className="tf-field-label min-w-[240px] flex-1">Reason to reopen (unlocks the cost)<input className="tf-field w-full" minLength={3} value={reason} onChange={(e) => setReason(e.target.value)} /></label>
        <button type="submit" className="tf-secondary-button" disabled={busy || reason.trim().length < 3}>Reopen</button>
      </form>}

      {!approved && canEdit && (editing ? <form className="space-y-3 border-t border-[#e9edf0] pt-4" aria-label="Correct the time sheet" onSubmit={(e) => {
        e.preventDefault(); void run(() => editShift(shift.id, shift.revision, toInput(form)));
      }}>
        <ShiftFields form={form} set={setForm} projects={projects} haulers={haulers} />
        <div className="flex gap-2"><button type="submit" className="tf-primary-button" disabled={busy}>Save corrections</button>
          <button type="button" className="tf-secondary-button" onClick={() => setEditing(false)}>Cancel</button></div>
      </form> : <button type="button" className="tf-link text-[12px]" onClick={() => setEditing(true)}>Correct the time sheet entry</button>)}

      <div className="border-t border-[#e9edf0] pt-3 text-[12px]">
        <h3 className="font-bold text-[#263646]">History</h3>
        <ol className="mt-1 space-y-1" data-testid="shift-history">{history.map((h, n) => <li key={n}>
          <span className="text-[#5f6c7b]">{new Date(h.at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })} · {h.actor ?? "—"}:</span> {h.field.replaceAll("_", " ")}{h.new ? <> — {h.new}</> : null}
        </li>)}</ol>
      </div>
    </div>
  </section>;
}

function Setup({ haulers, rates, projects, canEdit, canPrice, onChanged }: {
  haulers: HaulerRow[]; rates: HaulingRate[]; projects: Project[]; canEdit: boolean; canPrice: boolean; onChanged: () => Promise<void>;
}) {
  const [error, setError] = useState<string | null>(null);
  const [hauler, setHauler] = useState("");
  const [truck, setTruck] = useState({ hauler_id: "", identifier: "", equipment_type: "quad_axle" });
  const [rate, setRate] = useState({ hauler_id: "", project_id: "", equipment_type: "", hourly_rate: "", minimum_hours: "", standby_rate: "", effective_from: "", effective_to: "" });
  const act = (fn: () => Promise<void>) => fn().then(onChanged).then(() => setError(null)).catch((e) => setError((e as Error).message));
  const haulerName = (id: number) => haulers.find((h) => h.id === id)?.name ?? `#${id}`;
  const code = (id: number | null) => id === null ? "All projects" : projects.find((p) => p.id === id)?.code ?? `#${id}`;
  return <section className="tf-panel" aria-label="Haulers, trucks and hourly agreements">
    <div className="tf-panel-heading"><div><h2 className="tf-panel-title">Haulers, trucks and hourly agreements</h2>
      <p className="tf-panel-description">A shift is priced by the most specific agreement that has started: project and truck type, then project, then truck type, then any. An expired agreement doesn&apos;t fall back to a broader one, and standby is paid only at an agreed standby rate.</p></div></div>
    <div className="space-y-4 p-5">
      {error && <p role="alert" className="tf-alert tf-alert-bad">{error}</p>}
      {canEdit && <div className="grid gap-4 lg:grid-cols-2">
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
      </div>}
      <ul className="divide-y divide-[#edf0f2] text-[12px]">{haulers.map((h) => <li key={h.id} className="py-1.5">
        <b>{h.name}</b> <span className="text-[#5f6c7b]">{h.trucks.length ? h.trucks.map((t) => `${t.identifier} (${EQUIPMENT_LABEL[t.equipment_type] ?? t.equipment_type})`).join(", ") : "No trucks yet"}</span></li>)}</ul>
      {canPrice && <form className="grid items-end gap-2 sm:grid-cols-4 xl:grid-cols-[1.2fr_1fr_1fr_100px_90px_100px_130px_130px_auto]" onSubmit={(e) => {
        e.preventDefault();
        void act(() => createHaulingRate({ hauler_id: Number(rate.hauler_id), project_id: rate.project_id ? Number(rate.project_id) : null,
          equipment_type: rate.equipment_type || null, hourly_rate: rate.hourly_rate, minimum_hours: rate.minimum_hours || null,
          standby_rate: rate.standby_rate || null, effective_from: rate.effective_from || null, effective_to: rate.effective_to || null }));
      }}>
        <label className="tf-field-label">Hauler<select className="tf-field w-full" value={rate.hauler_id} onChange={(e) => setRate({ ...rate, hauler_id: e.target.value })}>
          <option value="">Select</option>{haulers.map((h) => <option key={h.id} value={h.id}>{h.name}</option>)}</select></label>
        <label className="tf-field-label">Project<select className="tf-field w-full" value={rate.project_id} onChange={(e) => setRate({ ...rate, project_id: e.target.value })}>
          <option value="">All projects</option>{projects.map((p) => <option key={p.id} value={p.id}>{p.code}</option>)}</select></label>
        <label className="tf-field-label">Truck type<select className="tf-field w-full" value={rate.equipment_type} onChange={(e) => setRate({ ...rate, equipment_type: e.target.value })}>
          <option value="">Any</option>{Object.entries(EQUIPMENT_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></label>
        <label className="tf-field-label">$ / hour<input required inputMode="decimal" className="tf-field w-full" value={rate.hourly_rate} onChange={(e) => setRate({ ...rate, hourly_rate: e.target.value })} /></label>
        <label className="tf-field-label">Min. hours<input inputMode="decimal" className="tf-field w-full" value={rate.minimum_hours} onChange={(e) => setRate({ ...rate, minimum_hours: e.target.value })} /></label>
        <label className="tf-field-label">Standby $/h<input inputMode="decimal" className="tf-field w-full" value={rate.standby_rate} onChange={(e) => setRate({ ...rate, standby_rate: e.target.value })} /></label>
        <label className="tf-field-label">From<input type="date" className="tf-field w-full" value={rate.effective_from} onChange={(e) => setRate({ ...rate, effective_from: e.target.value })} /></label>
        <label className="tf-field-label">Until<input type="date" className="tf-field w-full" value={rate.effective_to} onChange={(e) => setRate({ ...rate, effective_to: e.target.value })} /></label>
        <button className="tf-primary-button" disabled={!rate.hauler_id || !rate.hourly_rate}>Save agreement</button>
      </form>}
      {rates.length > 0 ? <div className="tf-table-wrap"><table className="tf-table" aria-label="Hourly agreements">
        <thead><tr><th>Hauler</th><th>Applies to</th><th>Truck type</th><th className="text-right">$/hour</th><th className="text-right">Minimum</th><th className="text-right">Standby</th><th>From</th><th>Until</th><th /></tr></thead>
        <tbody>{rates.map((r) => <tr key={r.id}><td className="font-semibold">{haulerName(r.hauler_id)}</td><td>{code(r.project_id)}</td>
          <td>{r.equipment_type ? EQUIPMENT_LABEL[r.equipment_type] : "Any"}</td><td className="text-right tabular-nums">{money(r.hourly_rate)}</td>
          <td className="text-right tabular-nums">{r.minimum_hours ? hours(r.minimum_hours) : "—"}</td>
          <td className="text-right tabular-nums">{r.standby_rate ? money(r.standby_rate) : <span className="text-[#5f6c7b]">Not payable</span>}</td>
          <td>{r.effective_from ? day(r.effective_from) : "Any date"}</td><td>{r.effective_to ? day(r.effective_to) : "Open-ended"}</td>
          <td className="text-right">{canPrice && <button className="text-[11px] font-semibold text-[#8a5260] hover:underline" onClick={() => {
            if (window.confirm("Remove this agreement? Approved shifts keep the rate they were approved at.")) void act(() => deleteHaulingRate(r.id));
          }}>Remove</button>}</td></tr>)}</tbody>
      </table></div> : <p className="tf-empty">No hourly agreements yet. Shifts can be entered, but not approved, until one applies.</p>}
    </div>
  </section>;
}

export default function TruckingPage() {
  const { can } = useAccount();
  const canEdit = can("trucking.edit"), canApprove = can("trucking.approve"), canPrice = can("pricing.manage");
  const [filters, setFilters] = useState<ShiftFilters>({});
  const [page, setPage] = useState<ShiftsPage | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [haulers, setHaulers] = useState<HaulerRow[]>([]);
  const [rates, setRates] = useState<HaulingRate[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState<ShiftForm>(EMPTY_SHIFT);
  const [error, setError] = useState<string | null>(null);

  const loadSetup = useCallback(async () => {
    const [h, r] = await Promise.all([listHaulers(), listHaulingRates()]);
    setHaulers(h); setRates(r);
  }, []);
  const load = useCallback(async () => {
    try { setPage(await listShifts(filters)); setError(null); } catch (e) { setError((e as Error).message); }
  }, [filters]);
  const [ready, setReady] = useState(false);
  useEffect(() => {
    // Filters from the address, so the Overview's "to approve" link opens that view.
    const params = new URLSearchParams(window.location.search);
    const initial: ShiftFilters = {};
    for (const key of ["project_id", "hauler_id", "status", "date_from", "date_to"] as const) { const v = params.get(key); if (v) initial[key] = v; }
    setFilters(initial);
    setReady(true);
    listProjects().then(setProjects).catch(() => setProjects([]));
    loadSetup().catch((e) => setError(e.message));
  }, [loadSetup]);
  useEffect(() => { if (ready) void load(); }, [ready, load]);

  const shift = useMemo(() => page?.shifts.find((s) => s.id === selected) ?? null, [page, selected]);
  const replace = (updated: TruckShift) => setPage((p) => p && { ...p, shifts: p.shifts.map((s) => s.id === updated.id ? updated : s) });
  const s = page?.summary;
  const set = (key: keyof ShiftFilters, value: string) => setFilters((f) => ({ ...f, [key]: value || undefined }));

  return <div className="space-y-5">
    <div className="tf-page-heading">
      <div><h1 className="tf-page-title">Hourly trucking</h1>
        <p className="tf-page-description">Truck shifts from the time sheets, the hours a person approves, and what they cost under the hauler&apos;s agreement. Separate from material: linked deliveries never add a trucking charge.</p></div>
      {canEdit && <button type="button" className="tf-primary-button" onClick={() => setAdding((v) => !v)}>{adding ? "Close" : "+ Enter a shift"}</button>}
    </div>
    {error && <div role="alert" className="tf-alert tf-alert-bad">{error}</div>}

    {adding && <section className="tf-panel" aria-label="Enter a shift"><form className="space-y-3 p-5" onSubmit={(e) => {
      e.preventDefault();
      createShift(toInput(form)).then((created) => { setAdding(false); setForm(EMPTY_SHIFT); setSelected(created.id); return load(); })
        .catch((err) => setError((err as Error).message));
    }}>
      <ShiftFields form={form} set={setForm} projects={projects} haulers={haulers} />
      {haulers.every((h) => !h.trucks.length) && <p className="text-[12px] text-[#5f6c7b]">Add a hauler and its trucks below first.</p>}
      <button type="submit" className="tf-primary-button">Save shift</button>
    </form></section>}

    {s && <section className="tf-panel overflow-hidden" aria-label="Trucking totals"><div className="grid grid-cols-2 lg:grid-cols-5">
      <Metric label="Awaiting approval" value={s.awaiting_approval} note={`${s.with_flags} with something to check`} attention={s.awaiting_approval > 0} />
      <Metric label="Approved hours" value={hours(s.approved_hours)} note="Billable, including standby and minimums" />
      <Metric label="Approved hauling cost" value={money(s.approved_cost)} note="Locked at approval" />
      <Metric label="Loads" value={s.loads} note="As recorded on the time sheets" />
      <Metric label="Disputed" value={s.disputed} note="Shifts in dispute with the hauler" attention={s.disputed > 0} />
    </div></section>}

    <section className="tf-panel" aria-label="Filters"><div className="grid gap-3 p-4 sm:grid-cols-2 lg:grid-cols-5">
      <label className="tf-field-label">Project<select className="tf-field w-full" value={filters.project_id ?? ""} onChange={(e) => set("project_id", e.target.value)}>
        <option value="">All projects</option>{projects.map((p) => <option key={p.id} value={p.id}>{p.code}</option>)}</select></label>
      <label className="tf-field-label">Hauler<select className="tf-field w-full" value={filters.hauler_id ?? ""} onChange={(e) => set("hauler_id", e.target.value)}>
        <option value="">All haulers</option>{haulers.map((h) => <option key={h.id} value={h.id}>{h.name}</option>)}</select></label>
      <label className="tf-field-label">Status<select className="tf-field w-full" value={filters.status ?? ""} onChange={(e) => set("status", e.target.value)}>
        <option value="">Any</option><option value="needs_review">Awaiting approval</option><option value="approved">Approved</option><option value="disputed">Disputed</option></select></label>
      <label className="tf-field-label">From<input type="date" className="tf-field w-full" value={filters.date_from ?? ""} onChange={(e) => set("date_from", e.target.value)} /></label>
      <label className="tf-field-label">To<input type="date" className="tf-field w-full" value={filters.date_to ?? ""} onChange={(e) => set("date_to", e.target.value)} /></label>
    </div></section>

    <div className={`grid gap-5 ${shift ? "xl:grid-cols-[minmax(0,1fr)_480px]" : ""}`}>
      <section className="tf-panel min-w-0" aria-label="Truck shifts">
        {page && page.shifts.length === 0 ? <p className="tf-empty" data-testid="shifts-empty">No truck shifts match. Enter one from a time sheet with “Enter a shift”.</p>
          : <div className="tf-table-wrap"><table className="tf-table" aria-label="Shift list">
            <thead><tr><th>Date</th><th>Truck</th><th>Project</th><th className="text-right">On site</th><th className="text-right">Standby</th><th className="text-right">Loads</th><th className="text-right">Cost</th><th>Status</th></tr></thead>
            <tbody>{page?.shifts.map((row) => <tr key={row.id} aria-selected={row.id === selected} className={`cursor-pointer ${row.id === selected ? "bg-[#f3f7fb]" : ""}`} onClick={() => setSelected(row.id)}>
              <td className="whitespace-nowrap tabular-nums">{day(row.work_date)}</td>
              <td><button type="button" className="text-left font-semibold hover:underline" onClick={() => setSelected(row.id)}>{row.truck}</button>
                <span className="block text-[11px] text-[#5f6c7b]">{row.hauler}</span></td>
              <td>{row.project}</td>
              <td className="text-right tabular-nums">{hours(row.on_site_hours)}</td><td className="text-right tabular-nums">{hours(row.standby_hours)}</td>
              <td className="text-right tabular-nums">{row.loads ?? "—"}</td>
              <td className="text-right tabular-nums">{money(row.cost)}{!row.cost_is_locked && row.cost && <span className="block text-[10px] text-[#5f6c7b]">if approved</span>}</td>
              <td><span className={`tf-status ${STATUS[row.status][1]}`}>{STATUS[row.status][0]}</span>
                {row.status !== "approved" && row.flags.length > 0 && <span className="ml-1 tf-status tf-status-attention">{row.flags.length} to check</span>}</td>
            </tr>)}</tbody>
          </table></div>}
      </section>
      {shift && <Detail shift={shift} projects={projects} haulers={haulers} canEdit={canEdit} canApprove={canApprove}
        onChanged={(updated) => { replace(updated); void load(); }} />}
    </div>

    <Setup haulers={haulers} rates={rates} projects={projects} canEdit={canEdit} canPrice={canPrice} onChanged={loadSetup} />
    <p className="text-[11px] leading-5 text-[#5f6c7b]">Hours are on site less the unpaid break, to the hundredth. Each cost line rounds half up to the cent. Approved cost appears on the <Link prefetch={false} className="tf-link text-[11px]" href="/">Overview</Link> separately from material cost.</p>
  </div>;
}
