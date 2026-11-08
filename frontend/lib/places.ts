/**
 * Which ticket a person last had open in each batch, so opening the batch again
 * picks up there. Only ticket ids are kept, per account, in this browser. Like
 * drafts, every touch of storage is guarded: losing this costs a click, not work.
 */
const PREFIX = "ticketflow.place.";
const keyFor = (accountId: number, batchId: number) => `${PREFIX}${accountId}.${batchId}`;

export function rememberPlace(accountId: number, batchId: number, ticketId: number): void {
  try {
    window.localStorage.setItem(keyFor(accountId, batchId), String(ticketId));
  } catch {
    // Storage blocked or full: the batch opens at its first open ticket instead.
  }
}

export function lastPlace(accountId: number, batchId: number): number | null {
  try {
    const value = Number(window.localStorage.getItem(keyFor(accountId, batchId)));
    return Number.isInteger(value) && value > 0 ? value : null;
  } catch {
    return null;
  }
}
