"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { requestAccess } from "../../lib/api";
import { MARKETING, SiteFooter, SiteNav } from "../site-ui";

const EMPTY = { contact_name: "", company_name: "", email: "", message: "", website: "" };
type Required = "contact_name" | "company_name" | "email";
type Problems = Partial<Record<Required, string>>;

export default function RequestDemoPage() {
  const [form, setForm] = useState(EMPTY);
  const [problems, setProblems] = useState<Problems>({});
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const [sent, setSent] = useState(false);
  const doneRef = useRef<HTMLHeadingElement>(null);
  const refs = { contact_name: useRef<HTMLInputElement>(null), company_name: useRef<HTMLInputElement>(null), email: useRef<HTMLInputElement>(null) };
  const set = (key: keyof typeof EMPTY) => (value: string) => setForm((f) => ({ ...f, [key]: value }));

  useEffect(() => { document.title = "Request a demo · Quarvera"; }, []);
  useEffect(() => { if (sent) doneRef.current?.focus(); }, [sent]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) return;
    const email = form.email.trim();
    const found: Problems = {};
    if (!form.contact_name.trim()) found.contact_name = "Enter your name.";
    if (!form.company_name.trim()) found.company_name = "Enter your company's name.";
    if (!/^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$/.test(email)) found.email = "Enter an email address like name@company.com.";
    setProblems(found);
    setError("");
    const first = (["contact_name", "company_name", "email"] as const).find((k) => found[k]);
    if (first) { refs[first].current?.focus(); return; }
    setPending(true);
    try {
      const body = {
        company_name: form.company_name.trim(), contact_name: form.contact_name.trim(), email,
        message: ["Demo request.", form.message.trim()].filter(Boolean).join(" ").slice(0, 1000), website: form.website,
      };
      if (MARKETING) {
        // The website alone: its own handler emails the request (app/api/demo-request).
        const response = await fetch("/api/demo-request", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        if (!response.ok) throw new Error(((await response.json().catch(() => ({}))) as { detail?: string }).detail || "Something went wrong. Please try again.");
      } else {
        await requestAccess(body);
      }
      setSent(true);
    } catch (err) {
      setError((err as Error).message || "Something went wrong. Please try again.");
    } finally {
      setPending(false);
    }
  }

  const field = (key: Required, label: string, extra: Record<string, string | number> = {}) =>
    <div className="qs-field">
      <label htmlFor={`demo-${key}`}>{label}</label>
      <input id={`demo-${key}`} ref={refs[key]} name={key} value={form[key]} onChange={(e) => set(key)(e.target.value)}
        aria-invalid={problems[key] ? true : undefined} aria-describedby={problems[key] ? `demo-${key}-error` : undefined} aria-required {...extra} />
      {problems[key] && <span id={`demo-${key}-error`} className="qs-field-error">{problems[key]}</span>}
    </div>;

  return <div className="qs">
    <SiteNav />
    <main className="qs-demo">
      <div className="qs-wrap qs-demo-row">
        <section className="qs-demo-copy" aria-labelledby="demo-title">
          <p className="qs-eyebrow">Request a demo</p>
          <h1 id="demo-title">See Quarvera on your own jobs.</h1>
          <p className="qs-demo-lead">A 30-minute walkthrough of tickets, trucking and invoice reconciliation, built around how your company works.</p>
          <ul className="qs-demo-points">
            <li>Bring a stack of tickets and an invoice, or use our demo data.</li>
            <li>See what Quarvera would flag before you pay.</li>
            <li>No commitment, and no account is created.</li>
          </ul>
        </section>

        <section className="qs-demo-card" aria-label="Demo request form">
          {sent ? <div className="qs-demo-done">
            <span className="qs-demo-done-mark" aria-hidden="true">
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"><path d="m5 13 4 4 10-11" /></svg>
            </span>
            <h2 ref={doneRef} tabIndex={-1}>Thanks, {form.contact_name.trim().split(" ")[0]}. We&apos;ll be in touch.</h2>
            <p>We&apos;ll email you at <strong>{form.email.trim()}</strong> to find a time.</p>
            <Link href="/welcome" className="qs-btn qs-btn-quiet">Back to the homepage</Link>
          </div> : <form onSubmit={submit} noValidate aria-label="Request a demo">
            <h2>Request a demo</h2>
            {error && <p role="alert" className="qs-form-alert">{error}</p>}
            {field("contact_name", "Your name", { autoComplete: "name", maxLength: 100 })}
            {field("company_name", "Company", { autoComplete: "organization", maxLength: 200 })}
            {field("email", "Work email", { type: "email", autoComplete: "email", maxLength: 254, autoCapitalize: "none", spellCheck: "false" })}
            <div className="qs-field">
              <label htmlFor="demo-message">Anything we should know? <span className="qs-optional">(optional)</span></label>
              <textarea id="demo-message" rows={3} maxLength={800} value={form.message} onChange={(e) => set("message")(e.target.value)}
                placeholder="What you buy, how your haulers bill, roughly how many tickets a month." />
            </div>
            <div className="qs-trap" aria-hidden="true">
              <label htmlFor="demo-website">Website</label>
              <input id="demo-website" name="website" tabIndex={-1} autoComplete="off" value={form.website} onChange={(e) => set("website")(e.target.value)} />
            </div>
            <button type="submit" className="qs-btn qs-btn-primary qs-btn-block" disabled={pending} aria-busy={pending || undefined}>
              {pending ? "Sending…" : "Request a demo"}
            </button>
            <p className="qs-demo-privacy">We use your details only to follow up about Quarvera.</p>
          </form>}
        </section>
      </div>
    </main>
    <SiteFooter />
  </div>;
}
