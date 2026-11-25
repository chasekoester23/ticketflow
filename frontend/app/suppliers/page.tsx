"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { Dashboard, HaulerRow, HaulingRate, Rate, Supplier, getDashboard, listHaulers, listHaulingRates, listRates, listSuppliers } from "../../lib/api";
import { useAccount } from "../account-context";
import { useProject } from "../project-context";
import { HaulerSetup } from "../trucking/setup";

const money = (v: string | number) => `$${Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const tons = (v: string | number) => Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 });
const key = (name: string) => name.trim().toLowerCase().replace(/\s+/g, " ");

/**
 * The companies the contractor buys material and trucking from. Delivery figures are
 * approved tickets only, for the selected project; rates and agreements are company-wide
 * setup, edited in Rates & Pricing and Trucking.
 */
export default function SuppliersPage() {
  const { can } = useAccount();
  const { projectId, project } = useProject();
  const [suppliers, setSuppliers] = useState<Supplier[] | null>(null);
  const [rates, setRates] = useState<Rate[]>([]);
  const [haulers, setHaulers] = useState<HaulerRow[] | null>(null);
  const [agreements, setAgreements] = useState<HaulingRate[]>([]);
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([listSuppliers(), listRates(), listHaulers(), listHaulingRates()])
      .then(([s, r, h, a]) => { setSuppliers(s); setRates(r); setHaulers(h); setAgreements(a); })
      .catch((e) => setError(e.message));
  }, []);
  useEffect(() => {
    let live = true;
    getDashboard(projectId || undefined).then((d) => { if (live) setDashboard(d); }).catch(() => { if (live) setDashboard(null); });
    return () => { live = false; };
  }, [projectId]);

  const words = query.trim().toLowerCase();
  const delivered = useMemo(() => new Map((dashboard?.by_supplier ?? []).map((g) => [key(g.name), g])), [dashboard]);
  const known = new Set((suppliers ?? []).map((s) => key(s.name)));
  // Names read from tickets that aren't set up as a supplier: often a spelling to clean up.
  const unlisted = (dashboard?.by_supplier ?? []).filter((g) => g.name !== "(unknown)" && !known.has(key(g.name)));
  const supplierRows = (suppliers ?? []).filter((s) => !words || s.name.toLowerCase().includes(words));
  const haulerRows = (haulers ?? []).filter((h) => !words || h.name.toLowerCase().includes(words));
  const scope = project ? `${project.code}` : "all projects";

  return <div className="space-y-4">
    <div className="tf-page-heading">
      <div><h1 className="tf-page-title">Suppliers & Haulers</h1>
        <p className="tf-page-description">Who you buy material and trucking from, what they delivered, and the rates on file.</p></div>
      <input type="search" className="tf-field w-[260px]" placeholder="Search companies" aria-label="Search companies" value={query} onChange={(e) => setQuery(e.target.value)} />
    </div>
    {error && <div role="alert" className="tf-alert tf-alert-bad">{error}</div>}

    <section className="tf-panel" aria-labelledby="suppliers-title">
      <div className="tf-panel-heading">
        <div><h2 id="suppliers-title" className="tf-panel-title">Material suppliers</h2>
          <p className="tf-panel-description">Approved deliveries for {scope}</p></div>
        {can("pricing.manage") && <Link prefetch={false} href="/pricing" className="tf-link">Add a supplier or rate →</Link>}
      </div>
      {suppliers === null ? <p className="tf-empty" role="status">Loading…</p>
        : supplierRows.length ? <div className="tf-table-wrap"><table className="tf-table" aria-label="Material suppliers">
          <thead><tr><th>Supplier</th><th className="tf-hide-narrow">Contact</th><th className="text-right">Tickets</th><th className="text-right">Verified tons</th>
            <th className="text-right">Material cost</th><th className="text-right">Rates on file</th><th><span className="sr-only">Links</span></th></tr></thead>
          <tbody>{supplierRows.map((s) => {
            const g = delivered.get(key(s.name));
            const count = rates.filter((r) => r.supplier_id === s.id).length;
            return <tr key={s.id}>
              <td className="font-semibold text-[#041d2f]">{s.name}</td>
              <td className="tf-hide-narrow">{s.contact || <span className="text-[#6b7787]">—</span>}</td>
              <td className="text-right">{g ? g.tickets.toLocaleString() : 0}</td>
              <td className="text-right">{g ? tons(g.tons) : "0"}</td>
              <td className="text-right">{g ? money(g.cost) : money(0)}</td>
              <td className="text-right">{count ? count : <span className="font-semibold text-[#995a18]">None</span>}</td>
              <td><div className="flex justify-end gap-3 whitespace-nowrap text-[12px]">
                <Link prefetch={false} href={`/records?supplier=${encodeURIComponent(s.name)}`}>Deliveries</Link>
                <Link prefetch={false} href="/pricing">Rates</Link>
              </div></td>
            </tr>;
          })}</tbody>
        </table></div>
        : <p className="tf-empty">{words ? "No supplier matches." : "No suppliers set up yet. Add one with its first rate in Rates & Pricing."}</p>}
      {unlisted.length > 0 && !words && <div className="border-t border-[#dce1e6] px-4 py-3 text-[12.5px] text-[#3b4a59]">
        <p className="font-semibold">Read on tickets but not set up as a supplier</p>
        <p className="mt-1">{unlisted.map((g) => <span key={g.name} className="mr-2 inline-block"><span className="tf-status">{g.name} · {g.tickets}</span></span>)}</p>
        <p className="mt-1 text-[#5f6c7b]">Usually a spelling to merge in{" "}
          {can("tickets.edit") ? <Link prefetch={false} className="tf-link" href="/records/names">Name cleanup</Link> : "Name cleanup"}, or a new supplier to add.</p>
      </div>}
    </section>

    <section className="tf-panel" aria-labelledby="haulers-title">
      <div className="tf-panel-heading">
        <div><h2 id="haulers-title" className="tf-panel-title">Haulers</h2>
          <p className="tf-panel-description">Trucking companies, their trucks and hourly agreements</p></div>
        {can("pricing.manage") && <Link prefetch={false} href="/pricing?tab=trucking" className="tf-link">Hourly rates →</Link>}
      </div>
      {haulers === null ? <p className="tf-empty" role="status">Loading…</p>
        : haulerRows.length ? <div className="tf-table-wrap"><table className="tf-table" aria-label="Haulers">
          <thead><tr><th>Hauler</th><th className="tf-hide-narrow">Contact</th><th className="text-right">Active trucks</th><th className="text-right">Hourly rates</th><th><span className="sr-only">Links</span></th></tr></thead>
          <tbody>{haulerRows.map((h) => {
            const count = agreements.filter((a) => a.hauler_id === h.id).length;
            return <tr key={h.id}>
              <td className="font-semibold text-[#041d2f]">{h.name}</td>
              <td className="tf-hide-narrow">{h.contact || <span className="text-[#6b7787]">—</span>}</td>
              <td className="text-right">{h.trucks.filter((t) => t.active).length}</td>
              <td className="text-right">{count ? count : <span className="font-semibold text-[#995a18]">None</span>}</td>
              <td><div className="flex justify-end gap-3 whitespace-nowrap text-[12px]">
                <Link prefetch={false} href={`/trucking?hauler_id=${h.id}`}>Shifts</Link>
                <Link prefetch={false} href="/pricing?tab=trucking">Rates</Link>
              </div></td>
            </tr>;
          })}</tbody>
        </table></div>
        : <p className="tf-empty">{words ? "No hauler matches." : "No haulers yet. Add the first trucking company you hire by the hour."}</p>}
      <HaulerSetup haulers={haulers ?? []} canEdit={can("trucking.edit")} onChanged={async () => setHaulers(await listHaulers())} />
    </section>
  </div>;
}
