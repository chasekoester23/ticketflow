"use client";

import { useState, type FormEvent } from "react";
import { logIn } from "../../lib/api";

export default function LoginPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) return;
    setError("");
    setPending(true);
    try {
      await logIn(username, password);
      window.location.replace("/");
    } catch (err) {
      setError((err as Error).message);
      setPending(false);
    }
  }

  return <div className="grid w-full max-w-[960px] overflow-hidden rounded-md border border-[#d4d9df] bg-white shadow-[0_12px_40px_rgba(20,32,44,.08)] lg:min-h-[540px] lg:grid-cols-[.95fr_1fr]">
    <section className="relative hidden overflow-hidden bg-[#202c37] p-10 text-white lg:flex lg:flex-col lg:justify-between">
      <div className="relative z-10 flex items-center gap-3">
        <span className="flex h-9 w-9 items-center justify-center rounded bg-[#e66b2d]">
          <svg aria-hidden="true" width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="1.8"><path d="m12 3 9 5-9 5-9-5 9-5ZM3 12l9 5 9-5M3 16l9 5 9-5" /></svg>
        </span>
        <span className="text-[17px] font-extrabold tracking-[.07em]">TICKET<span className="text-[#f09a62]">FLOW</span></span>
      </div>
      <div className="relative z-10">
        <p className="mb-5 text-[11px] font-bold tracking-[.15em] text-[#d9946a]">CONSTRUCTION DOCUMENT CONTROL</p>
        <h2 className="max-w-[330px] text-[34px] font-bold leading-[1.18] tracking-[-.035em]">Every load.<br />Every ticket.<br />Accounted for.</h2>
        <p className="mt-5 max-w-[290px] text-[13px] leading-7 text-[#b5c1cc]">One workspace for delivery scans, ticket verification, material quantities, and cost records.</p>
      </div>
      <div className="relative z-10 border-t border-[#43505d] pt-5 text-[11px] font-semibold tracking-[.075em] text-[#91a3b3]">BUILT FOR FIELD & OFFICE TEAMS</div>
      <div aria-hidden="true" className="absolute -right-24 top-20 h-96 w-96 rotate-45 border border-[#3d4b58] opacity-70" />
      <div aria-hidden="true" className="absolute -right-40 top-20 h-96 w-96 rotate-45 border border-[#3d4b58] opacity-50" />
    </section>
    <section className="flex flex-col justify-center p-7 sm:p-12">
      <div className="mb-8 lg:hidden">
        <span className="inline-flex items-center gap-2 text-[18px] font-extrabold tracking-[.05em] text-[#263440]">
          <span className="inline-block h-3 w-3 bg-[#e66b2d]" /> TICKETFLOW
        </span>
      </div>
      <p className="tf-eyebrow">ACCOUNT ACCESS</p>
      <h1 className="mt-2 text-[26px] font-bold tracking-[-.025em] text-[#1a2835]">Sign in to TicketFlow</h1>
      <p className="mt-3 max-w-sm text-[13px] leading-6 text-[#6a7786]">
        Sign in to access your company’s delivery records and ticket review workspace.
      </p>
      {error && <div role="alert" className="mt-5 rounded border border-red-200 bg-red-50 px-3 py-3 text-[12px] text-red-800">{error}</div>}
      <form className="mt-8 space-y-5" onSubmit={submit}>
        <label className="tf-field-label text-[12px]" htmlFor="username">Username
          <input id="username" name="username" autoComplete="username" required value={username}
            onChange={(e) => setUsername(e.target.value)} className="tf-field mt-1 w-full min-h-[44px]" />
        </label>
        <label className="tf-field-label text-[12px]" htmlFor="password">Password
          <input id="password" name="password" type="password" autoComplete="current-password"
            required value={password} onChange={(e) => setPassword(e.target.value)}
            className="tf-field mt-1 w-full min-h-[44px]" />
        </label>
        <button type="submit" disabled={pending} className="tf-primary-button mt-1 min-h-[44px] w-full">
          {pending ? "Signing in…" : "Sign in to workspace"}
        </button>
      </form>
      <p className="mt-8 border-t border-[#e4e8ed] pt-5 text-[11px] leading-5 text-[#7c8996]">
        Accounts are provisioned by a TicketFlow administrator. There is no public registration.
      </p>
    </section>
  </div>;
}
