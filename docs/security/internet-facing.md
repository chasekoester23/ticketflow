# Running TicketFlow on the internet

How the API protects itself behind a platform's proxy (Railway, or anything like it),
what to configure, and what the application can't do alone. Every behaviour here is
tested in `backend/tests/test_c_internet_facing.py` unless it says otherwise.

## Settings for a hosted deployment

| Setting | Value on Railway (example) | What it does |
|---|---|---|
| `TICKETFLOW_ENV` | `production` | Refuses to start with anything unsafe; requires HTTPS |
| `SESSION_SECURE_COOKIE` | `true` | Secure cookie, `__Host-` name, HSTS |
| `SESSION_COOKIE_SAMESITE` | `strict` when the app and API share one origin, else `lax` | |
| `APP_ORIGIN` | `https://app.example.com` | Same-origin deployment: changes may come from here |
| `CORS_ORIGINS` | `[]` same-origin, or `["https://app.example.com"]` for a separate frontend | |
| `TRUSTED_HOSTS` | `["api.internal-name", "app.example.com"]` | Exact hosts the API answers to |
| `HEALTHCHECK_HOSTS` | `["healthcheck.railway.app"]` | Accepted for `/health*` only |
| `TRUSTED_PROXY_IPS` | `["100.64.0.0/10", "fd00::/8", "10.0.0.0/8"]` | Who may say who the client is |
| `REQUIRE_HTTPS` | (implied by production) | Plain HTTP refused |

Run uvicorn with `--no-proxy-headers` (and `--no-access-log` if you like; the access log
no longer contains query strings either way). The application decides proxy trust in
one place, `app/proxy.py`, so uvicorn mustn't decide it a second time.

## Real client addresses

`X-Forwarded-For` and `X-Forwarded-Proto` are believed **only** when the connection
comes from an address in `TRUSTED_PROXY_IPS`. Then `X-Forwarded-For` is read from the
right: every trusted proxy appends what it saw, so the first address from the right
that isn't one of ours is the client. Anything a client writes at the left is never
reached. Garbage stops the walk. `0.0.0.0/0`, `::/0`, `*` and hostnames refuse to start.

The result is `request.client.host` everywhere: sign-in throttling, the access-request
limit, first-run setup's "this machine only" rule (which additionally refuses any
forwarded address that isn't loopback), and logs. Tested: eight failures from one
real address behind the proxy lock that address, not everyone behind the proxy, and a
client sending its own `X-Forwarded-For` can't escape a lockout.

Railway's proxy addresses aren't published as a fixed list. The private ranges above
cover its edge (`100.64.0.0/10`) and private networking (`fd00::/8`); check the
`X-Forwarded-For` your service actually receives before going live, and narrow the list
to what you see. With the same-origin `/api` proxy through Next, the Next service is a
second hop and must be in the list too (its private address).

## HTTPS only

With `TICKETFLOW_ENV=production` (or `REQUIRE_HTTPS=true`), a request whose scheme, as
reported by a trusted proxy, isn't HTTPS gets a `308` to the same URL on `https://` for
`GET`/`HEAD`, and `403` for anything else, so nothing is changed over plain HTTP. The
redirect only ever points at an approved host (the host check runs first). The three
probes are exempt so the platform's checker can use plain HTTP inside its network.
HSTS (`max-age=31536000; includeSubDomains`) is sent whenever cookies are secure. Add
the domain to the HSTS preload list only once every subdomain is HTTPS for good.

## The session cookie

`HttpOnly`, `Secure` (production), `SameSite=Lax` or `Strict`, `Path=/`, no `Domain`,
12 hours, idle timeout. With secure cookies its name is `__Host-ticketflow_session`:
browsers then refuse it unless it was set over HTTPS by this exact host, so a sibling
subdomain or a plain-HTTP page can't plant or overwrite it, and the plain name is not
read at all. Local development keeps `ticketflow_session`. Changing the setting signs
everyone out once.

## CORS and CSRF

Same-origin (Next proxies `/api`) needs no CORS: leave `CORS_ORIGINS` empty and set
`APP_ORIGIN`. Production refuses to start with neither. Every mutating `/api` request
must carry `X-TicketFlow-Request: 1` (an HTML form can't add it) and, if the browser
sent an `Origin`, it must be `APP_ORIGIN` or in `CORS_ORIGINS`. The origin is compared
with configuration, never with `Host` or `X-Forwarded-Host`, so a proxy rewriting
those can't widen it. Tested through a trusted proxy: no header, a foreign origin, and a
foreign origin with a forged `X-Forwarded-Host` are all refused.

## Rate limits and login throttling: the decision

The application's limits (sign-in lockouts, uploads, exports, reports, access requests)
are **per process**, in memory. We considered a database-backed sign-in throttle and
chose not to build it now:

- Railway runs one API replica unless scaled. With one process the in-memory limits are
  exact; with N replicas an attacker gets at most N× the attempts (8 per address and
  20 per account per 15 minutes, each), still far too slow to guess a 12-character
  password that isn't on the common list.
- The real risk at internet scale is distributed credential stuffing and request
  floods, which no application-level counter stops cheaply; that belongs at the edge.
- A database counter adds a write to every failed sign-in on the hot path, and a table
  that is itself a flooding target, for little gain at one or two replicas.

**Required before more than one API replica, or any public launch:** an edge rate limit.
Cloudflare's free plan in front of the custom domain allows a rate-limiting rule; use it
for `/api/auth/login`, `/api/access-requests` and `/api/setup` (for example 20 requests
per 10 seconds per IP, then block for 10 minutes), plus its standard DDoS protection.
If scaling past two replicas without an edge, build the shared throttle: a
`login_failures(key text primary key, count int, window_started timestamptz)` table
upserted on failure and read on sign-in (B's workstream owns the migration).

## Errors

Unexpected errors answer `500` with a reference only; the log line has the error's type
and code location, never its message (which can quote a record or a path). 422s list
fields, never values. Database errors never show the database's text. Tested with an
error whose message holds a path and fictional values. The uvicorn access log used to
write whole URLs, so a search for a supplier or a person was logged; the query is now
removed from it (tested against a real uvicorn).

## What's still open

- Edge rate limits (above) and request-size limits at the edge.
- An independent penetration test of the deployed service.
- A content security policy with nonces on the frontend (frontend workstream).
- Serving stored documents from a separate origin or with a sandboxing policy.
