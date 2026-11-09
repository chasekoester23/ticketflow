/**
 * A page with unsaved work registers a question here. Moving between pages inside the
 * app doesn't unload the document, so the browser's own leave-this-page warning can't help.
 */
let guard: (() => boolean) | null = null;

export function setNavigationGuard(next: (() => boolean) | null) {
  guard = next;
}

export function mayNavigate(): boolean {
  return guard ? guard() : true;
}
