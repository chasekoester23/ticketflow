/**
 * Same-origin API proxy. The browser calls /api/... on the frontend's own origin, and
 * this forwards each request to the backend at API_INTERNAL_URL (a private address,
 * such as Railway's http://api.railway.internal:8080), then streams the answer back.
 *
 * Because the browser never talks to another origin, production needs no CORS at all,
 * and the session cookie is a plain first-party cookie on the frontend's domain.
 * See docs/deploy/railway.md.
 *
 * Read at request time, so one image works in every environment. When the browser is
 * pointed straight at an API instead (NEXT_PUBLIC_API_URL, the localhost dev mode),
 * nothing calls this route.
 */
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

// Hop-by-hop headers belong to one connection and must never be forwarded.
const HOP_BY_HOP = new Set([
  "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection",
  "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding",
  // Set below from what the trusted edge said, never from what the browser claims.
  "forwarded", "x-real-ip", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-forwarded-port",
]);
// The response body is passed through as received, so these must describe it exactly.
const RESPONSE_DROP = new Set(["connection", "keep-alive", "transfer-encoding", "content-encoding", "content-length", "set-cookie"]);

/**
 * The visitor's address, as the platform's edge proxy recorded it: the entry that many
 * hops from the right of X-Forwarded-For (TRUSTED_PROXY_HOPS, default 1: Railway's edge).
 * Entries further left were written by the browser and could be anything, so login
 * throttling and the access-request limit never trust them.
 */
function clientAddress(request: Request): string | null {
  const hops = Math.max(1, Number.parseInt(process.env.TRUSTED_PROXY_HOPS ?? "1", 10) || 1);
  const chain = (request.headers.get("x-forwarded-for") ?? "").split(",").map((part) => part.trim()).filter(Boolean);
  const address = chain.length >= hops ? chain[chain.length - hops] : null;
  return address && /^[0-9A-Fa-f:.]+$/.test(address) ? address : null;
}

function backend(): string | null {
  const configured = process.env.API_INTERNAL_URL?.trim();
  if (!configured || !/^https?:\/\/[^/?#@\s]+\/?$/.test(configured)) return null;
  return configured.replace(/\/$/, "");
}

async function proxy(request: Request): Promise<Response> {
  const base = backend();
  if (base === null) {
    return Response.json({ detail: "The TicketFlow API isn't configured for this site (API_INTERNAL_URL)." },
      { status: 503, headers: { "Cache-Control": "no-store", "Retry-After": "30" } });
  }
  const incoming = new URL(request.url);
  // Only /api/... reaches this route. Checked anyway, so no path can name anything else on the backend.
  if (!incoming.pathname.startsWith("/api/") || /%2e|%2f|%5c/i.test(incoming.pathname)) {
    return Response.json({ detail: "Not Found" }, { status: 404, headers: { "Cache-Control": "no-store" } });
  }
  const target = `${base}${incoming.pathname}${incoming.search}`;

  const headers = new Headers();
  request.headers.forEach((value, name) => {
    if (!HOP_BY_HOP.has(name.toLowerCase())) headers.set(name, value);
  });
  // Uncompressed from the backend, so the body passes through byte for byte. Next
  // compresses for the browser where it should.
  headers.set("accept-encoding", "identity");
  // The address the browser used, for logs and redirects. The backend trusts these only
  // from the proxies it's told to (TRUSTED_PROXY_IPS, read by app/proxy.py).
  headers.set("x-forwarded-host", request.headers.get("host") ?? incoming.host);
  const proto = (request.headers.get("x-forwarded-proto") ?? "").split(",").pop()?.trim();
  headers.set("x-forwarded-proto", proto === "https" || proto === "http" ? proto : incoming.protocol.replace(":", ""));
  const address = clientAddress(request);
  if (address) headers.set("x-forwarded-for", address);

  const length = request.headers.get("content-length");
  const hasBody = !["GET", "HEAD"].includes(request.method);
  if (hasBody && length) headers.set("content-length", length);

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: request.method,
      headers,
      body: hasBody ? request.body : undefined,
      // Uploads stream through without being held in memory.
      ...(hasBody ? { duplex: "half" } : {}),
      redirect: "manual",
      cache: "no-store",
      signal: request.signal,
    } as RequestInit);
  } catch {
    return Response.json({ detail: "Couldn't reach the TicketFlow API. Try again in a moment." },
      { status: 502, headers: { "Cache-Control": "no-store", "Retry-After": "5" } });
  }

  const out = new Headers();
  upstream.headers.forEach((value, name) => {
    if (!RESPONSE_DROP.has(name.toLowerCase())) out.set(name, value);
  });
  // Several cookies can be set at once, and must stay separate headers.
  for (const cookie of upstream.headers.getSetCookie()) out.append("set-cookie", cookie);
  const encoded = upstream.headers.get("content-encoding");
  if (encoded && encoded !== "identity") out.set("content-encoding", encoded);
  else if (upstream.headers.get("content-length")) out.set("content-length", upstream.headers.get("content-length")!);

  const empty = request.method === "HEAD" || [101, 204, 205, 304].includes(upstream.status);
  return new Response(empty ? null : upstream.body, { status: upstream.status, statusText: upstream.statusText, headers: out });
}

export const GET = proxy;
export const HEAD = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
export const OPTIONS = proxy;
