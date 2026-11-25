"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { BillingException, listExceptions } from "../../../lib/api";
import { useProject } from "../../project-context";
import { Unbilled } from "../../reports/project/Unbilled";

const tons = (v: string | null) => v === null ? "—" : Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 });

/**
 * Approved activity no invoice has billed yet: deliveries and truck shifts. The same
 * detection the Exceptions tab uses (category "not invoiced"), listed on its own so a
 * person can chase a missing bill. Nothing here is a cost; costs come from approvals.
 */
export default function UnbilledPage() {
  const { projectId, project } = useProject();
  const [items, setItems] = useState<BillingException[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    listExceptions({ category: "not_invoiced", status: "open", project_id: projectId || undefined })
      .then((page) => { if (live) { setItems(page.items); setError(null); } })
      .catch((e) => { if (live) setError(e.message); });
    return () => { live = false; };
  }, [projectId]);

  return <div className="space-y-4">
    <div className="tf-page-heading">
      <div><h1 className="tf-page-title">Unbilled activity</h1>
        <p className="tf-page-description">Approved deliveries and truck shifts{project ? ` on ${project.code}` : ""} that no invoice lists or covers yet. Expect a bill, or ask whether one was missed.</p></div>
    </div>
    {error && <div role="alert" className="tf-alert tf-alert-bad">{error}</div>}
    <section className="tf-panel" aria-labelledby="unbilled-title">
      <div className="tf-panel-heading"><h2 id="unbilled-title" className="tf-panel-title">Not billed yet</h2>
        <Link prefetch={false} className="tf-link" href="/invoices/exceptions?category=not_invoiced">Assign or note in Exceptions →</Link></div>
      {items === null ? <p className="tf-empty" role="status">Loading…</p>
        : items.length ? <div className="tf-table-wrap"><table className="tf-table" aria-label="Unbilled activity">
          <thead><tr><th>What</th><th className="tf-hide-narrow">Project</th><th>Company</th><th className="text-right">Approved</th><th><span className="sr-only">Open</span></th></tr></thead>
          <tbody>{items.map((i) => <tr key={i.key}>
            <td className="min-w-[260px]"><span className="block font-semibold text-[#041d2f]">{i.shift_id ? "Truck shift" : `${i.material ?? "Deliveries"}`}</span>
              <span className="block text-[11.5px] text-[#5f6c7b]">{i.explanation}</span></td>
            <td className="tf-hide-narrow">{i.project_id ? <Link prefetch={false} href={`/projects/${i.project_id}`}>{i.project}</Link> : "—"}</td>
            <td>{i.supplier}</td>
            <td className="text-right whitespace-nowrap">{i.shift_id ? "1 shift" : `${i.unbilled_tickets ?? 0} tickets · ${tons(i.approved_tons)} t`}</td>
            <td className="text-right"><Link prefetch={false} className="whitespace-nowrap" href={i.shift_id ? `/trucking?shift_id=${i.shift_id}` : `/invoices/exceptions?key=${encodeURIComponent(i.key)}`}>Open →</Link></td>
          </tr>)}</tbody>
        </table></div>
        : <p className="tf-empty" data-testid="unbilled-empty">Every approved delivery and shift{project ? ` on ${project.code}` : ""} is on an invoice.</p>}
    </section>
    {projectId ? <Unbilled projectId={projectId} />
      : <p className="text-[12px] text-[#5f6c7b]">Choose a project at the top to list its unbilled deliveries ticket by ticket.</p>}
  </div>;
}
