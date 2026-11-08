"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, getSetupStatus, logIn, setUpCompany } from "../../lib/api";

const MIN_PASSWORD = 12;
const MAX_PASSWORD = 1024;

/**
 * The first visit to a new server: no accounts exist, so this creates the company
 * and its first owner, and signs them in. The server allows it once, and by
 * default only from the computer it runs on.
 */
function SetupForm({ onAlreadySetUp }: { onAlreadySetUp: (message: string) => void }) {
  const [form, setForm] = useState({ company_name: "", display_name: "", username: "", password: "", confirm: "" });
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const sending = useRef(false);
  const set = (key: keyof typeof form) => (event: { target: { value: string } }) => setForm((old) => ({ ...old, [key]: event.target.value }));
  const passwordProblem = form.password.length > 0 && (form.password.length < MIN_PASSWORD || form.password.length > MAX_PASSWORD)
    ? `Use ${MIN_PASSWORD} to ${MAX_PASSWORD} characters. A short sentence is easy to remember.`
    : form.password.length > 0 && !form.password.trim() ? "A password can't be only spaces."
    : form.password && form.password.trim().toLowerCase() === form.username.trim().toLowerCase() ? "The password can't be your username." : "";
  const confirmProblem = form.confirm.length > 0 && form.confirm !== form.password ? "The two passwords don't match." : "";
  const ready = form.company_name.trim() && form.display_name.trim() && form.username.trim()
    && form.password && !passwordProblem && form.confirm === form.password;

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!ready || sending.current) return;
    sending.current = true;
    setPending(true);
    setError("");
    try {
      await setUpCompany({
        company_name: form.company_name.trim(), display_name: form.display_name.trim(),
        username: form.username.trim(), password: form.password,
      });
      window.location.replace("/");
    } catch (err) {
      sending.current = false;
      setPending(false);
      if (err instanceof ApiError && err.status === 409) {
        onAlreadySetUp(err.message);
      } else {
        setError((err as Error).message);
      }
    }
  }

  const field = "tf-field mt-1 w-full min-h-[44px]";
  return <>
    <h1 className="mt-2 text-[26px] font-bold tracking-[-.025em] text-[#1a2835]">Set up TicketFlow</h1>
    <p className="mt-3 max-w-sm text-[13px] leading-6 text-[#5f6c7b]">
      No one has an account on this server yet. Name your company and create the first account, which is its owner. You&apos;ll add the rest of your team afterwards, each with their own sign-in.
    </p>
    {error && <div role="alert" className="mt-5 rounded border border-red-200 bg-red-50 px-3 py-3 text-[12px] text-red-800">{error}</div>}
    <form className="mt-6 space-y-4" onSubmit={submit} aria-label="Set up TicketFlow">
      <label className="tf-field-label text-[12px]">Company name
        <input name="company_name" autoComplete="organization" required maxLength={200} value={form.company_name} onChange={set("company_name")} className={field} />
      </label>
      <div className="tf-field-label text-[12px]">
        <label htmlFor="setup-name">Your name</label>
        <input id="setup-name" name="display_name" autoComplete="name" required maxLength={100} value={form.display_name} onChange={set("display_name")}
          aria-describedby="setup-name-help" className={field} />
        <span id="setup-name-help" className="font-normal text-[#5f6c7b]">Shown on every ticket you approve.</span>
      </div>
      <label className="tf-field-label text-[12px]">Username
        <input name="username" autoComplete="username" required maxLength={100} autoCapitalize="none" spellCheck={false} value={form.username} onChange={set("username")} className={field} />
      </label>
      <div className="tf-field-label text-[12px]">
        <label htmlFor="setup-password">Password</label>
        <input id="setup-password" name="password" type="password" autoComplete="new-password" required value={form.password} onChange={set("password")}
          aria-invalid={passwordProblem ? true : undefined} aria-describedby="setup-password-help" className={field} />
        <span id="setup-password-help" aria-live="polite" className={"font-normal " + (passwordProblem ? "text-[#a73631]" : "text-[#5f6c7b]")}>{passwordProblem || `At least ${MIN_PASSWORD} characters.`}</span>
      </div>
      <div className="tf-field-label text-[12px]">
        <label htmlFor="setup-confirm">Password again</label>
        <input id="setup-confirm" name="confirm" type="password" autoComplete="new-password" required value={form.confirm} onChange={set("confirm")}
          aria-invalid={confirmProblem ? true : undefined} aria-describedby="setup-confirm-help" className={field} />
        <span id="setup-confirm-help" aria-live="polite" className="font-normal text-[#a73631]">{confirmProblem}</span>
      </div>
      <button type="submit" disabled={!ready || pending} className="tf-primary-button mt-1 min-h-[44px] w-full">
        {pending ? "Setting up…" : "Create company and sign in"}
      </button>
    </form>
    <p className="mt-6 border-t border-[#e4e8ed] pt-4 text-[11px] leading-5 text-[#5f6c7b]">
      This works once, for the first account. After that, people are added by an owner from the Team page.
    </p>
  </>;
}

export default function LoginPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const [mode, setMode] = useState<"checking" | "login" | "setup">("checking");
  const [notice, setNotice] = useState("");

  // A server without this check, or one that can't be reached, means the usual sign-in.
  useEffect(() => {
    getSetupStatus().then((status) => setMode(status.needed ? "setup" : "login")).catch(() => setMode("login"));
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) return;
    setError("");
    setPending(true);
    try {
      const account = await logIn(username, password);
      window.location.replace(account.must_change_password ? "/account" : "/");
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
      {mode === "checking" && <p role="status" className="text-[13px] text-[#5f6c7b]">Connecting to TicketFlow…</p>}
      {mode === "setup" && <SetupForm onAlreadySetUp={(message) => { setNotice(message); setMode("login"); }} />}
      {mode === "login" && <>
      {notice && <div role="status" className="mb-4 rounded border border-[#f2d6aa] bg-[#fff8ec] px-3 py-3 text-[12px] text-[#7a4a12]">{notice}</div>}
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
          {pending ? "Signing in…" : "Sign in"}
        </button>
      </form>
      <p className="mt-8 border-t border-[#e4e8ed] pt-5 text-[11px] leading-5 text-[#5f6c7b]">
        Accounts are created by your company’s TicketFlow owner. Each person signs in with their own account so every approval is attributed correctly. Forgot your password? Ask an owner to reset it.
      </p>
      </>}
    </section>
  </div>;
}
