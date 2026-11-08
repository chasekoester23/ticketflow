"use client";

import { useState, type FormEvent } from "react";
import { changePassword } from "../../lib/api";
import { useAccount } from "../account-context";

const MIN_LENGTH = 12;

export default function AccountPage() {
  const { account } = useAccount();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [pending, setPending] = useState(false);
  if (!account) return null;
  const required = account.must_change_password;

  const mismatch = confirm.length > 0 && next !== confirm;
  const tooShort = next.length > 0 && next.length < MIN_LENGTH;
  const ready = current.length > 0 && next.length >= MIN_LENGTH && next === confirm && !pending;

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!ready) return;
    setError(null);
    setPending(true);
    try {
      await changePassword(current, next);
      setCurrent(""); setNext(""); setConfirm("");
      if (required) {
        window.location.replace("/");
        return;
      }
      setSaved(true);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setPending(false);
    }
  }

  return <div className="max-w-[760px] space-y-5">
    <div className="tf-page-heading">
      <div><h1 className="tf-page-title">Account settings</h1>
        <p className="tf-page-description">Your sign-in identity. Ticket approvals, edits and invoice corrections are recorded under this name.</p></div>
    </div>

    {required && <div role="alert" className="rounded border border-[#f2d6aa] bg-[#fff8ec] px-4 py-3 text-[13px] leading-6 text-[#7a4a12]">
      <strong>Set your own password to continue.</strong> You signed in with a temporary password from your company’s owner.
      Choose a new one only you know; TicketFlow is unavailable until you do.
    </div>}

    <section className="tf-panel">
      <div className="tf-panel-heading"><div><h2 className="tf-panel-title">Profile</h2>
        <p className="tf-panel-description">Ask an owner to change your name or role.</p></div></div>
      <dl className="grid gap-4 p-5 text-[13px] sm:grid-cols-2">
        <div><dt className="tf-eyebrow">Name</dt><dd className="mt-1 font-semibold text-[#1d2b38]">{account.display_name || "—"}</dd></div>
        <div><dt className="tf-eyebrow">Username</dt><dd className="mt-1 font-mono text-[#1d2b38]">{account.username}</dd></div>
        <div><dt className="tf-eyebrow">Role</dt><dd className="mt-1"><span className={`tf-role-pill ${account.role === "owner" ? "tf-role-pill-owner" : ""}`}>{account.role_label}</span></dd></div>
        <div><dt className="tf-eyebrow">Company</dt><dd className="mt-1 text-[#1d2b38]">{account.organization_name ?? "—"}</dd></div>
      </dl>
    </section>

    <section className="tf-panel">
      <div className="tf-panel-heading"><div><h2 className="tf-panel-title">{required ? "Choose your password" : "Change password"}</h2>
        <p className="tf-panel-description">At least {MIN_LENGTH} characters. Your other signed-in browsers will be signed out.</p></div></div>
      <form className="grid max-w-md gap-4 p-5" onSubmit={submit}>
        {error && <div role="alert" className="rounded border border-red-200 bg-red-50 px-3 py-2 text-[12px] text-red-800">{error}</div>}
        {saved && <div role="status" className="rounded border border-[#cce4d2] bg-[#edf5ef] px-3 py-2 text-[12px] text-[#216c43]">Password changed. Other sessions were signed out.</div>}
        <label className="tf-field-label">{required ? "Temporary password" : "Current password"}
          <input type="password" autoComplete="current-password" className="tf-field" required
            value={current} onChange={(e) => { setCurrent(e.target.value); setSaved(false); }} />
        </label>
        <div className="grid gap-1.5">
          <label className="tf-field-label">New password
            <input type="password" autoComplete="new-password" className="tf-field" required minLength={MIN_LENGTH}
              aria-invalid={tooShort} aria-describedby="new-password-hint"
              value={next} onChange={(e) => { setNext(e.target.value); setSaved(false); }} />
          </label>
          <span id="new-password-hint" aria-live="polite" className={`text-[11px] ${tooShort ? "text-[#a73631]" : "text-[#5f6c7b]"}`}>
            {tooShort ? `${MIN_LENGTH - next.length} more character${MIN_LENGTH - next.length === 1 ? "" : "s"} needed` : "A short phrase of several words works well."}
          </span>
        </div>
        <div className="grid gap-1.5">
          <label className="tf-field-label">Confirm new password
            <input type="password" autoComplete="new-password" className="tf-field" required
              aria-invalid={mismatch} aria-describedby={mismatch ? "confirm-password-error" : undefined}
              value={confirm} onChange={(e) => setConfirm(e.target.value)} />
          </label>
          {mismatch && <span id="confirm-password-error" className="text-[11px] text-[#a73631]">Passwords do not match</span>}
        </div>
        <div><button type="submit" className="tf-primary-button" disabled={!ready}>
          {pending ? "Saving…" : required ? "Set password and continue" : "Change password"}
        </button></div>
      </form>
    </section>
  </div>;
}
