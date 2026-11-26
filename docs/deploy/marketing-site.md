# quarvera.com: the marketing website on Railway

The website alone, with no database, backend, worker or AI. It's the `frontend/` image built with `NEXT_PUBLIC_SITE_MODE=marketing`:
- It serves the homepage at `/`, plus `/welcome`, `/demo` and static files.
- Every application page redirects home.
- Every other `/api/` path returns 404.
- Demo requests go to `app/api/demo-request/route.ts`. Each one is emailed through Resend and written to the service log as a backup.

## Railway service

| Setting | Value |
|---|---|
| Source | GitHub `chasekoester23/ticketflow`, branch `feature/website` (switch to `main` once merged) |
| Root directory | `frontend` |
| Builder | Dockerfile (from `frontend/railway.json`) |
| Health check | `/welcome` |

## Variables

| Variable | Value |
|---|---|
| `NEXT_PUBLIC_SITE_MODE` | `marketing` (read at build time; redeploy after changing) |
| `RESEND_API_KEY` | from Resend → API Keys (sending access only) |
| `DEMO_REQUEST_TO` | your company inbox; comma-separate several |
| `DEMO_REQUEST_FROM` | `Quarvera Website <website@quarvera.com>` (a domain verified in Resend) |

Leave `NEXT_PUBLIC_API_URL` and `API_INTERNAL_URL` unset.

## DNS

Add only these records, and leave every existing MX, SPF, DKIM and DMARC record alone:
- the records Railway shows for `quarvera.com` and `www.quarvera.com`;
- the records Resend shows for `quarvera.com`, which are a DKIM record and records on `send.quarvera.com`.
