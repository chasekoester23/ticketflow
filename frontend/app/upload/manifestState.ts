import type { Manifest, Ticket } from "../../lib/api";

/**
 * The scan check with each slot's ticket status and number, and the status counts,
 * brought up to date from the batch's tickets, exactly as the server would compute
 * them (services/scan_manifest.build_manifest). Returns null when a change could
 * alter what only the server decides (its blockers and overall status): a ticket
 * that failed or was skipped, or a ticket the scan check doesn't know about. Then
 * the caller asks the server instead.
 */
export function withTicketStates(manifest: Manifest, tickets: Ticket[]): Manifest | null {
  const byId = new Map(tickets.map((t) => [t.id, t]));
  const status = (name: string) => tickets.filter((t) => t.status === name).length;
  const counts = {
    ...manifest.counts,
    records: tickets.filter((t) => t.status !== "skipped").length,
    needs_review: status("needs_review"), flagged: status("flagged"), verified: status("verified"),
    failed_tickets: status("failed"), skipped: status("skipped"),
  };
  if (counts.records !== manifest.counts.records || counts.failed_tickets !== manifest.counts.failed_tickets) return null;
  let known = 0;
  const files = manifest.files.map((file) => ({
    ...file,
    pages: file.pages.map((page) => ({
      ...page,
      slots: page.slots.map((slot) => {
        if (slot.ticket_id === null) return slot;
        const ticket = byId.get(slot.ticket_id);
        if (!ticket) return slot;
        known += 1;
        return ticket.status === slot.ticket_status && ticket.ticket_number === slot.ticket_number
          ? slot : { ...slot, ticket_status: ticket.status, ticket_number: ticket.ticket_number };
      }),
    })),
  }));
  if (known !== tickets.length) return null;
  return { ...manifest, counts, files };
}
