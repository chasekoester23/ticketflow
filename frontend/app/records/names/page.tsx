"use client";

import { useCallback, useEffect, useState } from "react";
import { NameVariant, addAlias, getNameVariants, listMaterials, listSuppliers, renameOnTickets } from "../../../lib/api";
import { useAccount } from "../../account-context";

const STATE_LABEL: Record<NameVariant["state"], { text: string; tone: string }> = {
  unknown: { text: "Not in your list", tone: "tf-status-attention" },
  alias: { text: "Saved other spelling", tone: "" },
  same_name: { text: "Same name, different punctuation", tone: "" },
  catalog: { text: "In your list", tone: "tf-status-open" },
};

function VariantRow({ row, catalog, canRemember, onApply }: {
  row: NameVariant;
  catalog: string[];
  canRemember: boolean;
  onApply: (row: NameVariant, target: string, remember: boolean) => Promise<void>;
}) {
  const [target, setTarget] = useState(row.target ?? "");
  const [remember, setRemember] = useState(false);
  const [busy, setBusy] = useState(false);
  const state = STATE_LABEL[row.state];
  return <tr>
    <td className="font-semibold">{row.value}</td>
    <td><span className={`tf-status ${state.tone}`}>{state.text}</span></td>
    <td className="text-right tabular-nums">{row.tickets}</td>
    <td className="text-right tabular-nums">{row.unverified}</td>
    <td className="text-right tabular-nums">{row.verified}</td>
    <td className="min-w-[320px]">
      {row.state === "catalog" ? <span className="text-[#7a8796]">Nothing to do</span>
        : <div className="flex flex-wrap items-center gap-2">
          <label className="sr-only" htmlFor={`target-${row.value}`}>Change {row.value} to</label>
          <select id={`target-${row.value}`} className="tf-field !min-h-0 !py-1 !text-xs" value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="">Change to…</option>
            {catalog.map((name) => <option key={name} value={name}>{name}{name === row.target && row.state === "unknown" ? " (closest)" : ""}</option>)}
          </select>
          {canRemember && row.state === "unknown" && <label className="flex items-center gap-1 text-[11px] text-[#526273]">
            <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} /> Remember spelling
          </label>}
          <button type="button" className="tf-secondary-button !min-h-0 !py-1 disabled:opacity-40" disabled={!target || busy || row.unverified === 0}
            onClick={async () => { setBusy(true); try { await onApply(row, target, remember); } finally { setBusy(false); } }}>
            Apply to {row.unverified} unapproved
          </button>
        </div>}
    </td>
  </tr>;
}

/** Tidy spellings across every ticket on record. Nothing changes until someone applies it. */
export default function NameCleanupPage() {
  const { can } = useAccount();
  const canEdit = can("tickets.edit");
  const canRemember = can("pricing.manage");
  const [field, setField] = useState<"supplier" | "material">("supplier");
  const [rows, setRows] = useState<NameVariant[] | null>(null);
  const [catalog, setCatalog] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const found = await getNameVariants(field);
      setRows(found.rows);
      setCatalog(found.catalog);
    } catch (e) {
      setError((e as Error).message);
      setRows([]);
    }
  }, [field]);
  useEffect(() => { setRows(null); void load(); }, [load]);

  async function apply(row: NameVariant, target: string, remember: boolean) {
    setError(null);
    try {
      const done = await renameOnTickets(field === "supplier" ? "supplier" : "material_type", row.value, target);
      const approved = done.left_alone.verified ?? 0;
      let text = `Changed "${row.value}" to "${target}" on ${done.changed} ticket(s).`;
      if (approved) text += ` ${approved} approved ticket(s) keep their spelling until someone opens them.`;
      if (remember) {
        const list = field === "supplier" ? await listSuppliers() : await listMaterials();
        const entry = list.find((item) => item.name === target);
        if (entry) {
          await addAlias({ kind: field, alias: row.value, target_id: entry.id });
          text += " Future tickets with this spelling will suggest the catalog name.";
        }
      }
      setNotice(text);
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  const open = rows?.filter((r) => r.state === "unknown").length ?? 0;
  return <div className="space-y-5">
    <div className="tf-page-heading">
      <div>
        <p className="tf-eyebrow">DELIVERY RECORDS / NAME CLEANUP</p>
        <h1 className="tf-page-title">Name cleanup</h1>
        <p className="tf-page-description">Every spelling on your tickets. A ticket only gets a rate when its names match your list, so tidy these before approving.</p>
      </div>
      <a className="tf-secondary-button" href="/records">Back to records</a>
    </div>
    {error && <div role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-800">{error}</div>}
    {notice && <div role="status" className="rounded border border-[#cce4d2] bg-[#f3f9f4] p-3 text-sm text-[#216c43]">{notice}</div>}
    <section className="tf-panel">
      <div className="tf-panel-heading flex-wrap">
        <div>
          <h2 className="tf-panel-title">{field === "supplier" ? "Supplier" : "Material"} spellings</h2>
          <p className="tf-panel-description">{rows === null ? "Loading…" : `${rows.length} spellings · ${open} not in your list`}</p>
        </div>
        <div className="tf-segmented" role="group" aria-label="Which names">
          <button type="button" aria-pressed={field === "supplier"} onClick={() => setField("supplier")}>Suppliers</button>
          <button type="button" aria-pressed={field === "material"} onClick={() => setField("material")}>Materials</button>
        </div>
      </div>
      {rows && rows.length > 0 ? <div className="tf-table-wrap"><table className="tf-table">
        <thead><tr><th>As written</th><th>Status</th><th className="text-right">Tickets</th><th className="text-right">Unapproved</th><th className="text-right">Approved</th><th>Change</th></tr></thead>
        <tbody>{rows.map((row) => <VariantRow key={row.value} row={row} catalog={catalog} canRemember={canRemember} onApply={apply} />)}</tbody>
      </table></div> : rows && <p className="tf-empty">No {field} names on record yet.</p>}
    </section>
    <p className="text-[11px] text-[#768391]">{canEdit ? "Approved tickets are never changed from here: open one to correct it, and it will need approving again." : "View-only access. Ask a clerk or manager to make changes."} The closest match is only a suggestion. Add missing names under <a className="tf-link" href="/pricing">Suppliers &amp; rates</a>.</p>
  </div>;
}
