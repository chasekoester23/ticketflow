"use client";

import { useEffect, useState } from "react";
import { Batch, Ticket, deliveryImportFileUrl, listDeliveryImports, ticketImageUrl } from "../../../lib/api";

/** A batch made from a supplier's spreadsheet rather than scans (routers/delivery_imports.py). */
export const isImportBatch = (batch: Batch | null | undefined) => (batch?.intake_mode as string | undefined) === "import";

/**
 * Where the scan is for a scanned ticket: the spreadsheet row it came from. The picture
 * is the row as the file has it, kept with the ticket; the original file is a click away.
 */
export function ImportedRow({ batch, ticket }: { batch: Batch; ticket: Ticket }) {
  const [importId, setImportId] = useState<number | null>(null);
  const [failed, setFailed] = useState(false);
  const fileName = batch.files[0]?.original_name ?? "a spreadsheet";
  useEffect(() => {
    listDeliveryImports().then((all) => setImportId(all.find((i) => i.batch_id === batch.id)?.id ?? null)).catch(() => setImportId(null));
  }, [batch.id]);
  useEffect(() => { setFailed(false); }, [ticket.id]);
  return <div className="min-h-[260px] flex-1 overflow-auto bg-[#e6e9eb] p-3 max-xl:h-[48vh] xl:min-h-[320px]" data-testid="imported-row">
    <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2 rounded border border-[#dce1e6] bg-white px-3 py-2">
      <p className="text-sm font-semibold text-[#1d2b38]">Imported from {fileName}, row {ticket.source_page}</p>
      {importId !== null && <a className="tf-link text-xs" href={deliveryImportFileUrl(importId)}>Download the original file</a>}
    </div>
    {failed
      ? <p role="alert" className="rounded border border-red-200 bg-red-50 p-3 text-sm text-red-900">
        The row&apos;s record could not be loaded. Check the values against the original file before approving.
      </p>
      // eslint-disable-next-line @next/next/no-img-element -- authenticated API image
      : <img src={ticketImageUrl(ticket.id)} alt={`Row ${ticket.source_page} of ${fileName}, as the file has it`}
        className="w-full max-w-[820px] border border-slate-300 bg-white" onError={() => setFailed(true)} />}
  </div>;
}
