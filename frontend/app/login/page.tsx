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

  return <section className="mx-auto mt-12 max-w-md rounded-2xl border border-slate-200 bg-white p-8 shadow-sm">
    <p className="text-xs font-semibold uppercase tracking-widest text-slate-500">Secure workspace</p>
    <h1 className="mt-2 text-2xl font-semibold tracking-tight text-slate-900">Sign in to TicketFlow</h1>
    <p className="mt-2 text-sm leading-6 text-slate-600">
      Your delivery scans, project records, and cost exports are available only after signing in.
    </p>
    {error && <div role="alert" className="mt-4 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-800">{error}</div>}
    <form className="mt-6 space-y-4" onSubmit={submit}>
      <label className="block text-sm font-medium text-slate-800" htmlFor="username">Username
        <input id="username" name="username" autoComplete="username" required value={username}
          onChange={(e) => setUsername(e.target.value)} className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2.5 outline-none focus:border-slate-800" />
      </label>
      <label className="block text-sm font-medium text-slate-800" htmlFor="password">Password
        <input id="password" name="password" type="password" autoComplete="current-password"
          required value={password} onChange={(e) => setPassword(e.target.value)}
          className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2.5 outline-none focus:border-slate-800" />
      </label>
      <button type="submit" disabled={pending}
        className="w-full rounded-lg bg-slate-900 px-4 py-3 text-sm font-semibold text-white hover:bg-slate-700 disabled:opacity-50">
        {pending ? "Signing in…" : "Sign in"}
      </button>
    </form>
    <p className="mt-5 text-xs leading-5 text-slate-500">
      Access is provisioned by the workspace owner. There is no public registration.
    </p>
  </section>;
}
