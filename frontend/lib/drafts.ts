/**
 * Unsaved typing on a ticket, kept in this browser so a crash or a closed tab
 * doesn't lose it. A draft is never sent anywhere: it comes back into the form,
 * still unsaved, and only a person's save or approval reaches the server.
 *
 * Browser storage can be missing, full or blocked. Every touch of it is guarded,
 * because losing crash recovery must never stop someone working.
 *
 * On a shared computer these values would sit in the open, so drafts belong to
 * the account that typed them, and all of them go on sign-out.
 */
const PREFIX = "ticketflow.draft.";

export type TicketDraft = {
  ticketId: number;
  revision: string;
  accountId: number;
  savedAt: string;
  /** Only what the person changed: field name to the text they typed. */
  typed: Record<string, string>;
};

function storage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

const keyFor = (ticketId: number, revision: string) => `${PREFIX}${ticketId}.${revision}`;

function allKeys(store: Storage): string[] {
  const keys: string[] = [];
  for (let i = 0; i < store.length; i += 1) {
    const key = store.key(i);
    if (key?.startsWith(PREFIX)) keys.push(key);
  }
  return keys;
}

function parse(text: string | null): TicketDraft | null {
  if (!text) return null;
  try {
    const draft = JSON.parse(text) as TicketDraft;
    return typeof draft?.ticketId === "number" && draft.typed && typeof draft.typed === "object" ? draft : null;
  } catch {
    return null;
  }
}

export function saveDraft(ticketId: number, revision: string, accountId: number, typed: Record<string, string>): void {
  try {
    const draft: TicketDraft = { ticketId, revision, accountId, typed, savedAt: new Date().toISOString() };
    storage()?.setItem(keyFor(ticketId, revision), JSON.stringify(draft));
  } catch {
    // Full or blocked storage. The form still works; only crash recovery is lost.
  }
}

/** This person's drafts of one ticket, newest first, at any revision. */
export function draftsFor(ticketId: number, accountId: number): TicketDraft[] {
  try {
    const store = storage();
    if (!store) return [];
    const prefix = `${PREFIX}${ticketId}.`;
    return allKeys(store).filter((key) => key.startsWith(prefix))
      .map((key) => parse(store.getItem(key)))
      .filter((draft): draft is TicketDraft => draft !== null && draft.accountId === accountId)
      .sort((a, b) => b.savedAt.localeCompare(a.savedAt));
  } catch {
    return [];
  }
}

export function removeDraft(ticketId: number, revision: string): void {
  try {
    storage()?.removeItem(keyFor(ticketId, revision));
  } catch {
    // Nothing more can be done from here.
  }
}

/** Every draft of one ticket, once a person has saved, decided or discarded. */
export function removeTicketDrafts(ticketId: number): void {
  try {
    const store = storage();
    if (!store) return;
    const prefix = `${PREFIX}${ticketId}.`;
    allKeys(store).filter((key) => key.startsWith(prefix)).forEach((key) => store.removeItem(key));
  } catch {
    // Nothing more can be done from here.
  }
}

export function hasDrafts(): boolean {
  try {
    const store = storage();
    return store ? allKeys(store).length > 0 : false;
  } catch {
    return false;
  }
}

export function clearAllDrafts(): void {
  try {
    const store = storage();
    if (store) allKeys(store).forEach((key) => store.removeItem(key));
  } catch {
    // Nothing more can be done from here.
  }
}

/** Someone else's leftover typing, for example after a session ran out on a shared computer. */
export function clearOtherAccountsDrafts(accountId: number): void {
  try {
    const store = storage();
    if (!store) return;
    allKeys(store).forEach((key) => {
      const draft = parse(store.getItem(key));
      if (!draft || draft.accountId !== accountId) store.removeItem(key);
    });
  } catch {
    // Nothing more can be done from here.
  }
}

let signingOut = false;
/** Clears every draft, and tells the review page the unsaved-work warning no longer applies. */
export function beginSignOut(): void {
  signingOut = true;
  clearAllDrafts();
}
export const isSigningOut = () => signingOut;
