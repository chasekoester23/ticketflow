"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, getSetupStatus, requestAccess, setUpCompany, type AccessRequest } from "../../lib/api";
import { PasswordField, PublicFrame, SubmitButton, TextField } from "../public-ui";

const MIN_PASSWORD = 12;
const MAX_PASSWORD = 1024;

/**
 * The first visit to a new server: no accounts exist, so this creates the company
 * and its first owner. The server allows it once, and by default only from the
 * computer it runs on.
 */
function SetupForm({ onAlreadySetUp }: { onAlreadySetUp: (message: string) => void }) {
  const [form, setForm] = useState({ company_name: "", display_name: "", username: "", password: "", confirm: "" });
  const [show, setShow] = useState(false);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const sending = useRef(false);
  const set = (key: keyof typeof form) => (value: string) => setForm((old) => ({ ...old, [key]: value }));
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
      if (err instanceof ApiError && err.status === 409) onAlreadySetUp(err.message);
      else setError((err as Error).message);
    }
  }

  return <>
    <h1 className="tf-pub-title">Set up TicketFlow</h1>
    <p className="tf-pub-lead">No one has an account on this server yet. Name your company and create the first account, which is its owner.</p>
    {error && <div role="alert" className="tf-pub-banner tf-pub-banner-error">{error}</div>}
    <form className="tf-pub-form" onSubmit={submit} aria-label="Set up TicketFlow">
      <TextField id="setup-company" name="company_name" label="Company name" autoComplete="organization" maxLength={200} value={form.company_name} onChange={set("company_name")} />
      <TextField id="setup-name" name="display_name" label="Your name" autoComplete="name" maxLength={100} value={form.display_name} onChange={set("display_name")} help="Shown on every ticket you approve." />
      <TextField id="setup-username" name="username" label="Username" autoComplete="username" maxLength={100} autoCapitalize="none" spellCheck={false} value={form.username} onChange={set("username")} />
      <PasswordField id="setup-password" name="password" label="Password" autoComplete="new-password" value={form.password} onChange={set("password")}
        error={passwordProblem} help={`At least ${MIN_PASSWORD} characters.`} visible={show} onToggle={() => setShow((old) => !old)} />
      <PasswordField id="setup-confirm" name="confirm" label="Password again" autoComplete="new-password" value={form.confirm} onChange={set("confirm")}
        error={confirmProblem} visible={show} />
      <SubmitButton pending={pending} disabled={!ready} pendingText="Setting up…">Create company and sign in</SubmitButton>
    </form>
    <p className="tf-pub-footnote">This works once, for the first account. After that, an owner adds people from the Team page.</p>
  </>;
}

type RequestForm = { company_name: string; contact_name: string; email: string; phone: string; monthly_tickets: string; message: string; website: string };
const EMPTY: RequestForm = { company_name: "", contact_name: "", email: "", phone: "", monthly_tickets: "", message: "", website: "" };
type Problems = Partial<Record<"company_name" | "contact_name" | "email", string>>;

function RequestAccessForm() {
  const [form, setForm] = useState(EMPTY);
  const [problems, setProblems] = useState<Problems>({});
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const [sent, setSent] = useState(false);
  const companyRef = useRef<HTMLInputElement>(null);
  const contactRef = useRef<HTMLInputElement>(null);
  const emailRef = useRef<HTMLInputElement>(null);
  const doneRef = useRef<HTMLHeadingElement>(null);
  const set = (key: keyof RequestForm) => (value: string) => setForm((old) => ({ ...old, [key]: value }));

  useEffect(() => { if (sent) doneRef.current?.focus(); }, [sent]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) return;
    const email = form.email.trim();
    const found: Problems = {};
    if (!form.company_name.trim()) found.company_name = "Enter your company's name.";
    if (!form.contact_name.trim()) found.contact_name = "Enter your name.";
    if (!/^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$/.test(email)) found.email = "Enter an email address like name@company.com.";
    setProblems(found);
    setError("");
    if (Object.keys(found).length) {
      (found.company_name ? companyRef : found.contact_name ? contactRef : emailRef).current?.focus();
      return;
    }
    setPending(true);
    try {
      await requestAccess({
        company_name: form.company_name.trim(), contact_name: form.contact_name.trim(), email,
        phone: form.phone.trim() || undefined, message: form.message.trim() || undefined,
        monthly_tickets: (form.monthly_tickets || undefined) as AccessRequest["monthly_tickets"], website: form.website,
      });
      setSent(true);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setPending(false);
    }
  }

  if (sent) {
    return <div className="tf-pub-done">
      <span className="tf-pub-done-mark" aria-hidden="true">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"><path d="m5 13 4 4 10-11" /></svg>
      </span>
      <h1 ref={doneRef} tabIndex={-1} className="tf-pub-title">Request received</h1>
      <p className="tf-pub-lead">Thanks. No account has been created yet. We&apos;ll contact you at <strong>{form.email.trim()}</strong> to talk through your tickets and, if it&apos;s a fit, set up your company&apos;s workspace.</p>
      <div className="tf-pub-done-actions">
        <Link href="/welcome" className="tf-pub-button tf-pub-button-outline">Back to home</Link>
      </div>
    </div>;
  }

  return <>
    <h1 className="tf-pub-title">Request your company&apos;s workspace</h1>
    <p className="tf-pub-lead">TicketFlow is in a private pilot, so each workspace is set up with the company using it. Send your details to request one. This doesn&apos;t create an account yet.</p>
    {error && <div role="alert" className="tf-pub-banner tf-pub-banner-error">{error}</div>}
    <form className="tf-pub-form" onSubmit={submit} aria-label="Request access" noValidate>
      <TextField id="request-company" name="company_name" label="Company name" autoComplete="organization" maxLength={200} value={form.company_name} onChange={set("company_name")} error={problems.company_name} inputRef={companyRef} />
      <div className="tf-pub-field-row">
        <TextField id="request-name" name="contact_name" label="Your name" autoComplete="name" maxLength={100} value={form.contact_name} onChange={set("contact_name")} error={problems.contact_name} inputRef={contactRef} />
        <TextField id="request-phone" name="phone" label="Phone" type="tel" autoComplete="tel" maxLength={40} optional value={form.phone} onChange={set("phone")} />
      </div>
      <TextField id="request-email" name="email" label="Work email" type="email" autoComplete="email" maxLength={254} autoCapitalize="none" spellCheck={false} value={form.email} onChange={set("email")} error={problems.email} inputRef={emailRef} />
      <div className="tf-pub-field">
        <label htmlFor="request-volume">Delivery tickets per month<span className="tf-pub-optional"> (optional)</span></label>
        <select id="request-volume" name="monthly_tickets" value={form.monthly_tickets} onChange={(event) => set("monthly_tickets")(event.target.value)}>
          <option value="">Select a range</option>
          <option value="under_500">Under 500</option>
          <option value="500_2000">500 to 2,000</option>
          <option value="2000_10000">2,000 to 10,000</option>
          <option value="over_10000">Over 10,000</option>
        </select>
      </div>
      <div className="tf-pub-field">
        <label htmlFor="request-message">What are you trying to fix?<span className="tf-pub-optional"> (optional)</span></label>
        <textarea id="request-message" name="message" rows={3} maxLength={1000} value={form.message} onChange={(event) => set("message")(event.target.value)} />
      </div>
      <div className="tf-pub-trap" aria-hidden="true">
        <label>Website<input name="website" tabIndex={-1} autoComplete="off" value={form.website} onChange={(event) => set("website")(event.target.value)} /></label>
      </div>
      <SubmitButton pending={pending} pendingText="Sending…">Request access</SubmitButton>
    </form>
    <p className="tf-pub-footnote">We use these details only to follow up about TicketFlow. Please don&apos;t include ticket images or financial records.</p>
    <p className="tf-pub-switch">Already have an account? <Link href="/login">Sign in</Link></p>
  </>;
}

export default function SignupPage() {
  const router = useRouter();
  const [mode, setMode] = useState<"checking" | "setup" | "request">("checking");

  useEffect(() => {
    getSetupStatus().then((status) => setMode(status.needed ? "setup" : "request")).catch(() => setMode("request"));
  }, []);

  function alreadySetUp(message: string) {
    try { sessionStorage.setItem("tf-login-notice", message); } catch { /* Sign-in still works without the note. */ }
    router.replace("/login");
  }

  return <PublicFrame aside={mode === "setup" ? <>
    <p className="tf-pub-kicker">First-time setup</p>
    <p className="tf-pub-statement tf-pub-statement-small">Start with your company and its owner.</p>
    <p className="tf-pub-aside-note">Next you&apos;ll add your team from the Team page, each with their own sign-in, then create a project and upload your first tickets.</p>
  </> : <>
    <p className="tf-pub-kicker">How a pilot starts</p>
    <ol className="tf-pub-steps">
      <li><strong>Tell us about your company</strong><span>Who to contact and roughly how many tickets you handle.</span></li>
      <li><strong>We set up your workspace</strong><span>It&apos;s kept separate from every other company on TicketFlow.</span></li>
      <li><strong>You sign in as its owner</strong><span>You&apos;ll replace your temporary password the first time.</span></li>
      <li><strong>Add your team and first project</strong><span>Then upload your first delivery tickets.</span></li>
    </ol>
  </>}>
    {mode === "checking" && <p role="status" className="tf-pub-lead">Connecting to TicketFlow…</p>}
    {mode === "setup" && <SetupForm onAlreadySetUp={alreadySetUp} />}
    {mode === "request" && <RequestAccessForm />}
  </PublicFrame>;
}
