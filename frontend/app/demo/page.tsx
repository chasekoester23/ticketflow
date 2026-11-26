"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { requestAccess, type AccessRequest } from "../../lib/api";
import { SiteFooter, SiteNav } from "../site-ui";

const EMPTY = { contact_name: "", company_name: "", email: "", phone: "", role: "", monthly_tickets: "", billing: [] as string[], message: "", website: "" };
type Problems = Partial<Record<"contact_name" | "company_name" | "email", string>>;

const ROLES = ["Owner or executive", "Controller or finance", "Project manager", "Project engineer", "Accounts payable", "Operations or field", "Other"];
const VOLUMES: [string, string][] = [["under_500", "Under 500"], ["500_2000", "500 to 2,000"], ["2000_10000", "2,000 to 10,000"], ["over_10000", "Over 10,000"]];
const BILLING = ["Material by the ton", "Material by the yard or load", "Hourly trucking", "Trucking per ton or per load"];

export default function RequestDemoPage() {
  const [form, setForm] = useState(EMPTY);
  const [problems, setProblems] = useState<Problems>({});
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const [sent, setSent] = useState(false);
  const doneRef = useRef<HTMLHeadingElement>(null);
  const refs = { contact_name: useRef<HTMLInputElement>(null), company_name: useRef<HTMLInputElement>(null), email: useRef<HTMLInputElement>(null) };
  const set = (key: keyof typeof EMPTY) => (value: string) => setForm((f) => ({ ...f, [key]: value }));
  const toggle = (value: string) => setForm((f) => ({ ...f, billing: f.billing.includes(value) ? f.billing.filter((b) => b !== value) : [...f.billing, value] }));

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
    const details = [
      "Demo request.",
      form.role && `Role: ${form.role}.`,
      form.billing.length ? `Buys or pays for: ${form.billing.join(", ")}.` : "",
      form.message.trim() && `Notes: ${form.message.trim()}`,
    ].filter(Boolean).join(" ").slice(0, 1000);
    setPending(true);
    try {
      await requestAccess({
        company_name: form.company_name.trim(), contact_name: form.contact_name.trim(), email,
        phone: form.phone.trim() || undefined, message: details,
        monthly_tickets: (form.monthly_tickets || undefined) as AccessRequest["monthly_tickets"], website: form.website,
      });
      setSent(true);
    } catch (err) {
      setError((err as Error).message || "Something went wrong. Please try again.");
    } finally {
      setPending(false);
    }
  }

  const field = (key: "contact_name" | "company_name" | "email", label: string, extra: Record<string, string | number> = {}) =>
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
          <h1 id="demo-title">See every delivery accounted for, on your own jobs.</h1>
          <p className="qs-demo-lead">A 30-minute walkthrough with the founder, built around how your company buys material and pays for trucking.</p>
          <ol className="qs-demo-steps">
            <li><strong>Tell us about your work.</strong> What you buy, how your haulers bill, and roughly how many tickets you handle.</li>
            <li><strong>We set up a walkthrough.</strong> We&apos;ll reach out by email to find a time that works.</li>
            <li><strong>Bring a real stack.</strong> If you&apos;d like, send a few tickets and an invoice and we&apos;ll run them through Quarvera with you.</li>
          </ol>
          <p className="qs-demo-note">No commitment and no account is created. We use your details only to follow up about Quarvera.</p>
        </section>

        <section className="qs-demo-card" aria-label="Demo request form">
          {sent ? <div className="qs-demo-done">
            <span className="qs-demo-done-mark" aria-hidden="true">
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"><path d="m5 13 4 4 10-11" /></svg>
            </span>
            <h2 ref={doneRef} tabIndex={-1}>Thanks, {form.contact_name.trim().split(" ")[0]}. We&apos;ll be in touch.</h2>
            <p>We&apos;ll email you at <strong>{form.email.trim()}</strong> to set up a time. In the meantime, have a look at how Quarvera handles trucking and invoices.</p>
            <Link href="/welcome#platform" className="qs-btn qs-btn-quiet">Explore the platform</Link>
          </div> : <form onSubmit={submit} noValidate aria-label="Request a demo">
            <h2>Request a demo</h2>
            {error && <p role="alert" className="qs-form-alert">{error}</p>}
            <div className="qs-field-row">
              {field("contact_name", "Your name", { autoComplete: "name", maxLength: 100 })}
              {field("company_name", "Company", { autoComplete: "organization", maxLength: 200 })}
            </div>
            <div className="qs-field-row">
              {field("email", "Work email", { type: "email", autoComplete: "email", maxLength: 254, autoCapitalize: "none", spellCheck: "false" })}
              <div className="qs-field">
                <label htmlFor="demo-phone">Phone <span className="qs-optional">(optional)</span></label>
                <input id="demo-phone" name="phone" type="tel" autoComplete="tel" maxLength={40} value={form.phone} onChange={(e) => set("phone")(e.target.value)} />
              </div>
            </div>
            <div className="qs-field-row">
              <div className="qs-field">
                <label htmlFor="demo-role">Your role <span className="qs-optional">(optional)</span></label>
                <select id="demo-role" value={form.role} onChange={(e) => set("role")(e.target.value)}>
                  <option value="">Choose…</option>
                  {ROLES.map((r) => <option key={r}>{r}</option>)}
                </select>
              </div>
              <div className="qs-field">
                <label htmlFor="demo-volume">Delivery tickets a month <span className="qs-optional">(optional)</span></label>
                <select id="demo-volume" value={form.monthly_tickets} onChange={(e) => set("monthly_tickets")(e.target.value)}>
                  <option value="">Choose…</option>
                  {VOLUMES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                </select>
              </div>
            </div>
            <fieldset className="qs-field qs-checks">
              <legend>What do you pay for? <span className="qs-optional">(optional)</span></legend>
              {BILLING.map((b) => <label key={b}><input type="checkbox" checked={form.billing.includes(b)} onChange={() => toggle(b)} />{b}</label>)}
            </fieldset>
            <div className="qs-field">
              <label htmlFor="demo-message">Anything you&apos;d like to see? <span className="qs-optional">(optional)</span></label>
              <textarea id="demo-message" rows={3} maxLength={700} value={form.message} onChange={(e) => set("message")(e.target.value)}
                placeholder="For example: we get about 200 tickets a week across four jobs and reconcile hauler invoices by hand." />
            </div>
            <div className="qs-trap" aria-hidden="true">
              <label htmlFor="demo-website">Website</label>
              <input id="demo-website" name="website" tabIndex={-1} autoComplete="off" value={form.website} onChange={(e) => set("website")(e.target.value)} />
            </div>
            <button type="submit" className="qs-btn qs-btn-primary qs-btn-block" disabled={pending} aria-busy={pending || undefined}>
              {pending ? "Sending…" : "Request a demo"}
            </button>
          </form>}
        </section>
      </div>
    </main>
    <SiteFooter />
  </div>;
}
