/**
 * Advice on a value while it's being typed. Nothing here changes a value, and
 * none of it replaces the server's own checks: a warning asks for a second look,
 * and only a value that can't be stored at all stops a save.
 */
export type FieldMessage = {
  tone: "problem" | "warning";
  text: string;
  /** The value can't be stored as it stands, so saving is off until it's fixed. */
  blocksSaving?: boolean;
};

/** The same limit the server flags at (max_plausible_tons in backend/app/config.py). */
export const PLAUSIBLE_TONS = 60;
const OLDEST_PLAUSIBLE_YEARS = 2;

const TONS_FORMAT = /^(\d+\.?\d{0,3}|\.\d{1,3})$/;

/** Tons are compared as text, so a quantity never passes through a floating-point number. */
export function tonsMessage(raw: string): FieldMessage | null {
  const value = raw.trim();
  if (!value) return null;
  if (!TONS_FORMAT.test(value)) {
    return { tone: "problem", blocksSaving: true, text: "Enter tons as a number with up to 3 decimals, like 24.50. Don't convert pounds or yards here." };
  }
  const [whole, fraction = ""] = value.split(".");
  const digits = whole.replace(/^0+/, "");
  const hasFraction = /[1-9]/.test(fraction);
  if (!digits && !hasFraction) return { tone: "problem", text: "Tons must be more than zero. If this ticket has no quantity, flag it instead." };
  const limit = String(PLAUSIBLE_TONS);
  const over = digits.length > limit.length
    || (digits.length === limit.length && (digits > limit || (digits === limit && hasFraction)));
  if (over) {
    return {
      tone: "warning",
      text: `This is above ${PLAUSIBLE_TONS} tons, more than one truck usually carries. Check whether the ticket's figure is pounds rather than tons. TicketFlow doesn't convert units: enter the tons printed on the ticket.`,
    };
  }
  return null;
}

const dateText = (date: Date) => [
  String(date.getFullYear()).padStart(4, "0"),
  String(date.getMonth() + 1).padStart(2, "0"),
  String(date.getDate()).padStart(2, "0"),
].join("-");

/** A warning only. An unusual date can be right, so a person gets the final word. */
export function dateMessage(value: string, today = new Date()): FieldMessage | null {
  if (!value) return null;
  const now = dateText(today);
  if (value > now) {
    return { tone: "warning", text: `This date is in the future (today is ${now}). Check it against the scan, including the year.` };
  }
  const oldest = dateText(new Date(today.getFullYear() - OLDEST_PLAUSIBLE_YEARS, today.getMonth(), today.getDate()));
  if (value < oldest) {
    return { tone: "warning", text: `This date is more than ${OLDEST_PLAUSIBLE_YEARS} years ago. Check the year against the scan.` };
  }
  return null;
}

export function rateMessage(raw: string): FieldMessage | null {
  const value = raw.trim();
  if (!value || /^(\d+\.?\d{0,4}|\.\d{1,4})$/.test(value)) return null;
  return { tone: "problem", blocksSaving: true, text: "Enter the rate as dollars per ton, with up to 4 decimals, like 31.25." };
}

export const REQUIRED_TEXT: Record<string, string> = {
  ticket_number: "Ticket number is needed to accept. If it can't be read on the scan, flag this ticket.",
  delivery_date: "Delivery date is needed to accept. Use the date printed on the ticket.",
  material_type: "Material is needed to accept.",
  tonnage: "Tons are needed to accept. Enter the tons printed on the ticket.",
  supplier: "Supplier is needed to accept.",
  job_location: "Job location is needed to accept.",
};
export const NEEDED_LABEL: Record<string, string> = {
  ticket_number: "ticket number", delivery_date: "delivery date", material_type: "material",
  tonnage: "tons", supplier: "supplier", job_location: "job location",
};
