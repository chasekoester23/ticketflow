"use client";

import { useEffect, useState } from "react";
import { UnbilledReport, downloadFile, getUnbilled, ticketImageUrl, unbilledCsvUrl } from "../../../lib/api";

const money = (v: string | null) => v === null ? "Unresolved" : `$${Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const tons = (v: string | null) => v === null ? "—" : Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 3 });

/** The register, filtered to exactly the approved tickets behind a figure. */
function recordsLink(projectId: string, filters: Record<string, string | null>) {
  const params = new URLSearchParams({ project_id: projectId, status: "verified" });
  // "(not entered)" is how the server names a blank. It isn't something to search for.
  Object.entries(filters).forEach(([key, value]) => { if (value && value !== "(not entered)") params.set(key, value); });
  return `/records?${params}`;
}

/**
 * Approved deliveries that no invoice line lists or covers. Every figure leads to
 * the tickets behind it. A starting point for questions, never a payment figure.
 */
export function Unbilled({ projectId }: { projectId: string }) {
  const [report, setReport] = useState<UnbilledReport | null | "unavailable">(null);
  const [failed, setFailed] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  async function download(code: string) {
    if (downloading) return;
    setDownloading(true);
    setDownloadError(null);
    try {
      await downloadFile(unbilledCsvUrl(projectId), `ticketflow-unbilled-${code}.csv`);
    } catch (e) {
      setDownloadError((e as Error).message);
    } finally {
      setDownloading(false);
    }
  }

  useEffect(() => {
    let active = true;
    setReport(null);
    setFailed(null);
    getUnbilled(projectId)
      .then((found) => { if (active) setReport(found ?? "unavailable"); })
      .catch((e) => { if (active) setFailed((e as Error).message); });
    return () => { active = false; };
  }, [projectId]);

  return <section className="tf-panel" aria-label="Unbilled deliveries">
    <div className="tf-panel-heading flex-wrap">
      <div>
        <h2 className="tf-panel-title">Unbilled deliveries</h2>
        <p className="tf-panel-description">Approved tickets that no invoice line lists, or covers by supplier, material and delivery dates.</p>
      </div>
      {report && report !== "unavailable" && report.total_tickets > 0 && <button type="button" className="tf-secondary-button" disabled={downloading}
        onClick={() => void download(report.project.code)}>{downloading ? "Preparing CSV…" : "Download every ticket (CSV)"}</button>}
    </div>
    {downloadError && <p role="alert" className="border-b border-red-200 bg-red-50 px-5 py-2.5 text-[12px] text-red-900">{downloadError}</p>}
    <p className="border-b border-[#e2e7ec] bg-[#f6f8fa] px-5 py-2.5 text-[12px] leading-5 text-[#344352]" role="note">
      <strong>For investigation, not approval for payment.</strong> A delivery listed here may be on an invoice that
      hasn&apos;t been entered yet, or billed under another name. Check with the supplier before acting on it.
    </p>
    {failed ? <p className="tf-empty" role="alert">Unbilled deliveries couldn&apos;t be loaded: {failed}</p>
      : report === null ? <p className="tf-empty" role="status">Checking approved deliveries against invoice lines…</p>
      : report === "unavailable" ? <p className="tf-empty">This server doesn&apos;t offer the unbilled check yet. It arrives with the next server update.</p>
      : report.total_tickets === 0 ? <p className="tf-empty">Every approved delivery on this project is listed or covered by an invoice line.</p>
      : <>
        <div className="grid divide-y divide-[#e7eaee] border-b border-[#e7eaee] sm:grid-cols-3 sm:divide-x sm:divide-y-0" data-testid="unbilled-totals">
          <div className="px-4 py-2.5"><p className="tf-stat-label">Tickets</p>
            <p className="mt-1 text-[20px] font-bold tabular-nums">{report.total_tickets.toLocaleString()}</p>
            <p className="text-[11px] text-[#5b6878]">Each one is listed below and in the CSV</p></div>
          <div className="px-4 py-2.5"><p className="tf-stat-label">Tons</p>
            <p className="mt-1 text-[20px] font-bold tabular-nums">{tons(report.total_tons)}</p>
            <p className="text-[11px] text-[#5b6878]">Approved tons, as entered</p></div>
          <div className="px-4 py-2.5"><p className="tf-stat-label">Approved cost</p>
            <p className="mt-1 text-[20px] font-bold tabular-nums">{money(report.total_cost)}</p>
            <p className="text-[11px] text-[#5b6878]">At the rates locked when each was approved</p></div>
        </div>

        {report.gaps.length > 0 && <div className="border-b border-[#e7eaee] px-5 py-4">
          <h3 className="text-[12px] font-bold text-[#344352]">Delivery dates no invoice line covers</h3>
          <p className="mb-2 text-[11px] text-[#5b6878]">Runs of dates with approved deliveries and no invoice line for that supplier and material. Often an invoice not entered yet.</p>
          <div className="tf-table-wrap"><table className="tf-table" aria-label="Delivery dates no invoice line covers">
            <thead><tr><th>Supplier</th><th>Material</th><th>Delivery dates</th><th className="text-right">Tickets</th><th className="text-right">Tons</th><th><span className="sr-only">Tickets behind this</span></th></tr></thead>
            <tbody>{report.gaps.map((gap) => <tr key={[gap.supplier, gap.material, gap.date_from].join("|")}>
              <td className="font-semibold">{gap.supplier}</td><td>{gap.material}</td>
              <td className="whitespace-nowrap tabular-nums">{gap.date_from === gap.date_to ? gap.date_from : `${gap.date_from} to ${gap.date_to}`}</td>
              <td className="text-right tabular-nums">{gap.tickets}</td><td className="text-right tabular-nums">{tons(gap.tons)}</td>
              <td className="whitespace-nowrap"><a href={recordsLink(projectId, { supplier: gap.supplier, material: gap.material, date_from: gap.date_from, date_to: gap.date_to })}>In records →<span className="sr-only"> approved {gap.supplier} {gap.material} tickets, {gap.date_from} to {gap.date_to}</span></a></td>
            </tr>)}</tbody>
          </table></div>
        </div>}

        <div className="tf-table-wrap"><table className="tf-table" aria-label="Unbilled approved tickets">
          <thead><tr><th>Ticket</th><th>Delivered</th><th>Supplier</th><th>Material</th><th className="text-right">Tons</th><th className="text-right">Approved cost</th><th><span className="sr-only">Evidence</span></th></tr></thead>
          <tbody>{report.rows.map((row) => <tr key={row.ticket_id}>
            <td className="font-semibold tabular-nums">{row.ticket_number ?? "Unnumbered"}</td>
            <td className="whitespace-nowrap tabular-nums">{row.delivery_date ?? "—"}</td>
            <td>{row.supplier ?? "—"}</td><td>{row.material ?? "—"}</td>
            <td className="text-right tabular-nums">{tons(row.tons)}</td><td className="text-right tabular-nums">{money(row.cost)}</td>
            <td className="whitespace-nowrap">
              <a href={ticketImageUrl(row.ticket_id)} target="_blank" rel="noreferrer">Scan<span className="sr-only"> of ticket {row.ticket_number ?? row.ticket_id}</span></a>
              {" · "}
              <a href={`/upload?batch=${row.batch_id}&ticket=${row.ticket_id}`}>Ticket<span className="sr-only"> {row.ticket_number ?? row.ticket_id} and its history</span></a>
            </td>
          </tr>)}</tbody>
        </table></div>
        {report.truncated && <p className="border-t border-[#e9edf0] px-5 py-3 text-[12px] text-[#344352]" data-testid="unbilled-truncated">
          Showing the first {report.rows.length.toLocaleString()} of {report.total_tickets.toLocaleString()} tickets. The totals above, and the CSV, cover all of them.
        </p>}
      </>}
  </section>;
}
