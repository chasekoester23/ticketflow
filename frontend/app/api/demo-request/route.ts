/**
 * Demo requests from the marketing website, handled by the website itself so it can run
 * without the application backend (docs/deploy/marketing-site.md).
 *
 * Each request is emailed through Resend's HTTP API (no extra dependency) and also written
 * to the server log as a backup, so nothing is lost if email isn't configured yet.
 * Environment: RESEND_API_KEY, DEMO_REQUEST_TO (your inbox), DEMO_REQUEST_FROM (a sender
 * on a domain verified in Resend, e.g. "Quarvera Website <website@quarvera.com>").
 */
import { NextResponse } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const WINDOW_MS = 10 * 60 * 1000;
const PER_WINDOW = 5;
const recent = new Map<string, number[]>();
const EMAIL = /^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$/;

function clean(value: unknown, max: number): string {
  return typeof value === "string" ? value.replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, "").trim().slice(0, max) : "";
}

function limited(ip: string): boolean {
  const now = Date.now();
  const times = (recent.get(ip) ?? []).filter((t) => now - t < WINDOW_MS);
  times.push(now);
  recent.set(ip, times);
  if (recent.size > 5000) recent.clear();  // bounded memory; a restart clears it anyway
  return times.length > PER_WINDOW;
}

export async function POST(request: Request) {
  const ip = (request.headers.get("x-forwarded-for") ?? "").split(",")[0].trim() || "unknown";
  const origin = request.headers.get("origin");
  const host = request.headers.get("host");
  if (origin && host && new URL(origin).host !== host) {
    return NextResponse.json({ detail: "Not allowed." }, { status: 403 });
  }
  const raw = await request.text();
  if (raw.length > 8000) return NextResponse.json({ detail: "That request is too long." }, { status: 413 });
  let body: Record<string, unknown>;
  try { body = JSON.parse(raw); } catch { return NextResponse.json({ detail: "Something went wrong. Please try again." }, { status: 400 }); }

  // A filled-in hidden field means a bot: say thanks and do nothing.
  if (clean(body.website, 200)) return NextResponse.json({ received: true }, { status: 202 });
  if (limited(ip)) return NextResponse.json({ detail: "Too many requests. Please try again in a few minutes." }, { status: 429 });

  const name = clean(body.contact_name, 100);
  const company = clean(body.company_name, 200);
  const email = clean(body.email, 254).toLowerCase();
  const message = clean(body.message, 1000);
  if (!name || !company) return NextResponse.json({ detail: "Enter your name and company." }, { status: 422 });
  if (!EMAIL.test(email)) return NextResponse.json({ detail: "Enter a valid email address." }, { status: 422 });

  const at = new Date().toISOString();
  // The log line is the backup record (Railway keeps the service's logs).
  console.log(JSON.stringify({ event: "demo_request", at, name, company, email, message }));

  const key = process.env.RESEND_API_KEY;
  const to = process.env.DEMO_REQUEST_TO;
  const from = process.env.DEMO_REQUEST_FROM;
  if (key && to && from) {
    const text = [`New demo request from quarvera.com`, ``, `Name: ${name}`, `Company: ${company}`, `Email: ${email}`,
      `Note: ${message.replace(/^Demo request\.\s*/, "") || "(none)"}`, ``, `Received ${at}. Reply to this email to answer ${name} directly.`].join("\n");
    try {
      const sent = await fetch("https://api.resend.com/emails", {
        method: "POST",
        headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json" },
        body: JSON.stringify({ from, to: to.split(",").map((s) => s.trim()).filter(Boolean), reply_to: email,
          subject: `Demo request: ${company}`, text }),
        signal: AbortSignal.timeout(10_000),
      });
      if (!sent.ok) console.error(JSON.stringify({ event: "demo_request_email_failed", status: sent.status, body: (await sent.text()).slice(0, 300) }));
    } catch (error) {
      console.error(JSON.stringify({ event: "demo_request_email_failed", error: String(error) }));
    }
  } else {
    console.warn(JSON.stringify({ event: "demo_request_email_not_configured" }));
  }
  return NextResponse.json({ received: true }, { status: 202 });
}
