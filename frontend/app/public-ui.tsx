"use client";

import Link from "next/link";
import { useState, type ReactNode, type Ref } from "react";

export function Brand({ tone = "light" }: { tone?: "light" | "dark" }) {
  return <Link href="/welcome" className={`tf-pub-brand tf-pub-brand-${tone}`} aria-label="TicketFlow home">
    <span className="tf-pub-mark" aria-hidden="true">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="1.8" strokeLinejoin="round"><path d="m12 3 9 5-9 5-9-5 9-5ZM3 12l9 5 9-5M3 16l9 5 9-5" /></svg>
    </span>
    <span>TICKET<b>FLOW</b></span>
  </Link>;
}

/** The two-part page behind sign-in and sign-up. On a phone the side panel shrinks to the brand. */
export function PublicFrame({ aside, children }: { aside: ReactNode; children: ReactNode }) {
  return <div className="tf-pub-frame">
    <aside className="tf-pub-aside">
      <Brand />
      <div className="tf-pub-aside-body">{aside}</div>
    </aside>
    <main className="tf-pub-main">
      <div className="tf-pub-form-column">
        <Link href="/welcome" className="tf-pub-back">
          <svg aria-hidden="true" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M19 12H5m6 6-6-6 6-6" /></svg>
          Back to home
        </Link>
        {children}
      </div>
    </main>
  </div>;
}

type FieldProps = {
  id: string; label: string; name: string; value: string;
  onChange: (value: string) => void;
  type?: string; autoComplete?: string; maxLength?: number; optional?: boolean;
  help?: string; error?: string; inputRef?: Ref<HTMLInputElement>;
  autoCapitalize?: string; spellCheck?: boolean;
};

export function TextField({ id, label, name, value, onChange, type = "text", autoComplete, maxLength, optional, help, error, inputRef, autoCapitalize, spellCheck }: FieldProps) {
  const noteId = `${id}-note`;
  return <div className="tf-pub-field">
    <label htmlFor={id}>{label}{optional && <span className="tf-pub-optional"> (optional)</span>}</label>
    <input id={id} ref={inputRef} name={name} type={type} autoComplete={autoComplete} maxLength={maxLength} value={value}
      autoCapitalize={autoCapitalize} spellCheck={spellCheck}
      onChange={(event) => onChange(event.target.value)}
      aria-required={optional ? undefined : true} aria-invalid={error ? true : undefined} aria-describedby={error || help ? noteId : undefined} />
    {(error || help) && <span id={noteId} className={error ? "tf-pub-error" : "tf-pub-help"}>{error || help}</span>}
  </div>;
}

type PasswordProps = Omit<FieldProps, "type" | "optional"> & {
  /** Fields that share one Show button pass the same visible state, and only one sets onToggle. */
  visible: boolean; onToggle?: () => void; warnCapsLock?: boolean;
};

export function PasswordField({ id, label, name, value, onChange, autoComplete, help, error, inputRef, visible, onToggle, warnCapsLock }: PasswordProps) {
  const [capsLock, setCapsLock] = useState(false);
  const noteId = `${id}-note`;
  const note = error || help;
  return <div className="tf-pub-field">
    <label htmlFor={id}>{label}</label>
    <div className="tf-pub-password">
      <input id={id} ref={inputRef} name={name} type={visible ? "text" : "password"} autoComplete={autoComplete} value={value}
        autoCapitalize="none" spellCheck={false}
        onChange={(event) => onChange(event.target.value)}
        onKeyUp={(event) => { if (warnCapsLock) setCapsLock(event.getModifierState("CapsLock")); }}
        onBlur={() => setCapsLock(false)}
        aria-required aria-invalid={error ? true : undefined} aria-describedby={note ? noteId : undefined} />
      {onToggle && <button type="button" className="tf-pub-reveal" aria-pressed={visible} aria-controls={id}
        aria-label={visible ? "Hide password" : "Show password"} onClick={onToggle}>{visible ? "Hide" : "Show"}</button>}
    </div>
    <span id={noteId} aria-live="polite" className={error ? "tf-pub-error" : "tf-pub-help"}>{note}</span>
    {capsLock && <span className="tf-pub-help tf-pub-caps">Caps Lock is on.</span>}
  </div>;
}

export function SubmitButton({ pending, disabled, children, pendingText }: { pending: boolean; disabled?: boolean; children: ReactNode; pendingText: string }) {
  return <button type="submit" disabled={pending || disabled} aria-busy={pending || undefined} className="tf-pub-button tf-pub-button-primary tf-pub-submit">
    {pending && <span className="tf-pub-spinner" aria-hidden="true" />}
    {pending ? pendingText : children}
  </button>;
}
