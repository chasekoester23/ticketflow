# Monthly cost estimates: Railway, Cloudflare R2 and Gemini

**Every price here is an estimate, from memory of public price lists, and must be checked on the provider's own pricing page before you decide anything.** Railway (railway.com/pricing), Cloudflare R2 (developers.cloudflare.com/r2/pricing) and Gemini (ai.google.dev/pricing) all change their prices. Nothing has been provisioned or measured on a hosted setup. Taxes are excluded, and amounts are in US dollars.

Fixed hosting and variable AI reading are kept apart on purpose. Hosting barely moves with volume. AI reading is roughly proportional to tickets.

## Assumptions (change these and redo the arithmetic)

| Assumption | Value used | Why, or how to measure it |
|---|---|---|
| Tickets per active company per month | **1,000** (likely range 300–3,000) | A small paving contractor's deliveries. Use the pilot's real count |
| Stored per ticket | **1.0 MB**: about 0.5 MB of original scan (four tickets per 200 dpi page, about 2 MB a page) plus a 0.3–0.5 MB PNG crop | Check with `du` on a pilot's `storage/` divided by its ticket count |
| Database per ticket | **about 5 KB** with audit history and indexes | Check with `pg_total_relation_size` after a pilot month |
| R2 writes (Class A) per ticket | **about 2**: the upload's share, the crop, and the odd rotation | From the code: one PUT per stored file, plus a HEAD after each write |
| R2 reads (Class B) per ticket | **about 15**: the worker's read, several views while reviewing, HEADs from health checks | System health checks up to 200 documents per visit |
| Gemini input tokens per ticket | **about 2,500**: one image of up to 1,800 px plus the prompt | Read `usage_metadata` from a benchmark run (`scripts/benchmark_ocr.py`) |
| Gemini output tokens per ticket | **about 1,500**, including thinking tokens, which are billed as output | The same. Lowering `GEMINI_THINKING_LEVEL` lowers it |
| Retry overhead | **+10%** on AI calls | `EXTRACTION_MAX_ATTEMPTS`, and re-reads after a restart |
| Documents kept | **everything, growing**, with no lifecycle expiry (railway.md §9) | Storage is shown at 12 months of growth |

## Price inputs (estimates: verify every one)

| Item | Estimate used | Notes |
|---|---|---|
| Railway plan | Hobby about $5/month (includes about $5 of usage). Pro about $20/month per seat (includes about $20 of usage) | Pro is the realistic one for customers: team access and higher limits |
| Railway compute | about $10 per GB of RAM per month and about $20 per vCPU per month, billed on actual use | Idle services cost little |
| Railway volume (Postgres) | about $0.15–0.25 per GB per month | |
| Railway egress | about $0.05–0.10 per GB | Mostly images shown while reviewing |
| R2 storage | about $0.015 per GB per month. First 10 GB free | |
| R2 Class A (writes) | about $4.50 per million. First 1 million free each month | |
| R2 Class B (reads) | about $0.36 per million. First 10 million free each month | |
| R2 egress | $0 | R2 doesn't charge for egress |
| Gemini | **P_in per million input tokens and P_out per million output tokens: look up the model in `GEMINI_MODEL`** | The illustration below uses P_in = $2 and P_out = $12. That's an assumption, not a quote |

## Fixed hosting

Expected average use, not peak. The worker is the heaviest service: it renders PDFs and crops with OpenCV.

| Service | Dev / staging | 5 companies | 25 companies | 100 companies |
|---|---|---|---|---|
| web (Next.js) | 0.05 vCPU, 0.2 GB → ~$3 | 0.1 vCPU, 0.3 GB → ~$5 | 0.2 vCPU, 0.4 GB → ~$8 | 0.4 vCPU, 0.6 GB → ~$14 |
| api (FastAPI, 1 replica) | 0.05 vCPU, 0.3 GB → ~$4 | 0.15 vCPU, 0.5 GB → ~$8 | 0.3 vCPU, 0.7 GB → ~$13 | 0.6 vCPU, 1.0 GB → ~$22 |
| worker (1 replica) | 0.05 vCPU, 0.4 GB → ~$5 | 0.2 vCPU, 0.8 GB → ~$12 | 0.4 vCPU, 1.0 GB → ~$18 | 1.0 vCPU, 1.5 GB → ~$35 |
| Postgres compute | 0.05 vCPU, 0.3 GB → ~$4 | 0.1 vCPU, 0.5 GB → ~$7 | 0.25 vCPU, 1.0 GB → ~$15 | 0.5 vCPU, 2.0 GB → ~$30 |
| Postgres volume, 12 months' growth | 1 GB → <$1 | 1 GB → <$1 | 2 GB → <$1 | 6 GB + indexes → ~$2 |
| Egress | ~$0 | ~$1 | ~$3 | ~$10 |
| **Railway usage** | **~$16** | **~$34** | **~$58** | **~$113** |
| Railway plan | Hobby $5, or a Pro seat $20 (usage credit included) | Pro, 1–2 seats: $20–40 | Pro, 2 seats: $40 | Pro, 2–3 seats: $40–60 |
| **Railway total (estimate)** | **~$15–25** | **~$40–60** | **~$60–90** | **~$115–170** |

At 100 companies, one worker probably can't keep up at peak hours. The fix is several workers, which needs leases first (audit #1). Count roughly double the worker line for that.

## Document storage on R2

| | Dev / staging | 5 companies | 25 companies | 100 companies |
|---|---|---|---|---|
| Tickets per month | ~200 (fictional) | 5,000 | 25,000 | 100,000 |
| New storage per month | ~0.2 GB | ~5 GB | ~25 GB | ~100 GB |
| Stored after 12 months | ~2 GB | ~60 GB | ~300 GB | ~1.2 TB |
| Storage cost at 12 months | $0 (free tier) | ~$0.75 | ~$4.35 | ~$18 |
| Class A writes per month | ~400 | ~10,000 | ~50,000 | ~200,000 |
| Class B reads per month | ~3,000 | ~75,000 | ~375,000 | ~1.5 million |
| Operations cost | $0 (free tier) | $0 | $0 | $0 (under the free allowance) |
| **R2 total (estimate)** | **$0** | **~$1** | **~$5** | **~$18–20** |

Backups of the bucket (audit #3) roughly double storage if they're a second R2 bucket.

## Variable: AI reading with Gemini

The cost per ticket is `(2,500 × P_in + 1,500 × P_out) / 1,000,000 × 1.10`.

As an illustration only, with P_in = $2 and P_out = $12 per million tokens: (0.005 + 0.018) × 1.10 ≈ **$0.025 per ticket**.

| | Dev / staging | 5 companies | 25 companies | 100 companies |
|---|---|---|---|---|
| Tickets read per month | ~200 | 5,000 | 25,000 | 100,000 |
| **Gemini (illustrative)** | **~$5** | **~$125** | **~$630** | **~$2,500** |

- AI is the largest cost from about five companies upward. Measure tokens per ticket on real pilot scans before pricing a plan.
- Manual intake costs no AI at all.
- Cheaper levers: a lower thinking level, a smaller model if a benchmark shows accuracy holds, JPEG instead of lossless WebP (`GEMINI_IMAGE_FORMAT`), and the per-company spend limits the proposal describes.

## Summary (monthly estimates, verify before relying on them)

| | Dev / staging | 5 companies | 25 companies | 100 companies |
|---|---|---|---|---|
| Hosting (Railway) | ~$15–25 | ~$40–60 | ~$60–90 | ~$115–170 |
| Documents (R2) | $0 | ~$1 | ~$5 | ~$20 |
| **Fixed subtotal** | **~$15–25** | **~$40–60** | **~$65–95** | **~$135–190** |
| AI reading (Gemini, illustrative) | ~$5 | ~$125 | ~$630 | ~$2,500 |

Not included: backups in a second store or account, log retention and monitoring services, email, a domain (about $10–20 a year), payment processing, and the one-off independent security review (the proposal estimates $5,000–25,000).
