"use client";

import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";

const FOCUSABLE = "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])";

/**
 * A modal window. It takes the keyboard focus when it opens, keeps Tab inside
 * while open, closes on Escape or a click outside, and gives focus back to
 * whatever opened it, so a keyboard user never loses their place.
 */
export function Dialog({ label, onClose, onSubmit, children }: {
  label: string;
  onClose: () => void;
  onSubmit?: (event: FormEvent<HTMLFormElement>) => void;
  children: ReactNode;
}) {
  const box = useRef<HTMLDivElement & HTMLFormElement>(null);
  // Read while rendering, before anything inside can take focus.
  const [opener] = useState(() => (typeof document === "undefined" ? null : document.activeElement as HTMLElement | null));
  const close = useRef(onClose);
  close.current = onClose;

  useEffect(() => {
    const node = box.current;
    if (!node) return;
    const inside = () => [...node.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((element) => element.getClientRects().length > 0);
    if (!node.contains(document.activeElement)) (inside()[0] ?? node).focus();
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        close.current();
        return;
      }
      if (event.key !== "Tab") return;
      const list = inside();
      if (!list.length) return event.preventDefault();
      const first = list[0];
      const last = list[list.length - 1];
      const active = document.activeElement;
      if (!node!.contains(active)) {
        event.preventDefault();
        first.focus();
      } else if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      if (opener && document.contains(opener)) opener.focus();
    };
  }, [opener]);

  const props = {
    ref: box, role: "dialog", "aria-modal": true, "aria-label": label, tabIndex: -1, className: "tf-dialog",
    onClick: (event: { stopPropagation: () => void }) => event.stopPropagation(),
  } as const;
  return <div className="tf-dialog-scrim" onClick={() => close.current()}>
    {onSubmit ? <form {...props} onSubmit={onSubmit}>{children}</form> : <div {...props}>{children}</div>}
  </div>;
}
