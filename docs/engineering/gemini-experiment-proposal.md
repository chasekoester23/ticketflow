# Proposal: a small, authorized Gemini experiment

> **Not run. No paid or model call has been made for this document.** It needs the owner's explicit go-ahead and the `--i-authorize-paid-calls` flag. **Results on synthetic tickets are not real-world OCR accuracy**, whatever they turn out to be.

## How realistic is the synthetic handwriting?

I judged this from the committed preview sheets (`backend/eval_data/synthetic/preview/benchmark-mini-pages.jpg` and `-tickets.jpg`) and a full-resolution crop of a hard page, set against what real field tickets look like.

| Aspect | Synthetic set | Real field tickets | Gap |
|---|---|---|---|
| Stroke variety | Single-width pen strokes with per-glyph jitter, slant and baseline drift; neat, rushed and caps styles; overwrites, crossings-out, ambiguous pairs (1/7, 0/O, 5/S) | Ballpoint and felt-tip, pencil, marker; strokes that skip, blob and fade | **Large** |
| Glyph set | One stroke alphabet, shared by every "writer" | Every driver writes differently: cursive, mixed case, idiosyncratic digits | **Large**: a model could learn it or flatter it |
| Pen pressure | Uniform darkness along a stroke; faintness applied to the whole page | Pressure varies within a stroke; light strokes drop out on carbons | **Large** |
| Carbon copies | Tinted paper only (yellow, pink); no carbon transfer | Blurred, offset, bluish carbon or NCR impressions; the top sheet's ruling bleeds through | **Large** |
| Stains and damage | Blur, noise, grey cast, uneven light, JPEG | Mud, coffee, water, fold creases, torn corners, staples, stamps over fields | **Large** |
| Ruled forms | Ten clean, vector-sharp layouts in one sans typeface; field boxes are always where the form says | Hundreds of supplier forms, dot-matrix scale printouts, writing over lines and into neighbouring boxes | Moderate |
| Capture | Flatbed scans at 150 dpi, letter-size sheet, small skew and offset | Often **phone photos**: perspective, glare, shadows, motion blur, cluttered backgrounds, thumbs | **Large**: not modelled at all |

**Verdict:** the set is good for testing TicketFlow's pipeline, splitter geometry and money maths, which is what it was built for. It is a poor proxy for real handwriting. A model's score on it will probably be **optimistic**: one glyph set, clean forms, no carbon or photo effects. It could also be misleadingly **pessimistic** on deliberate effects (smudges, strike-throughs) that real tickets show differently.

## Is an experiment justified?

**Conditional go, small and capped.** It is worth a few dollars, but only to answer engineering questions the offline suite can't:

1. Does the real provider path work end to end on TicketFlow's actual crops: the request shape, JSON parsing, refusals, timeouts and retries? This includes the new adaptive 2×2 crops, which are larger and can be off-square.
2. Is Gemini's failure behaviour safe? When a field is cut off, smudged or crossed out, does it return null rather than invent a value? Does `compute_flags` then route the ticket to a person?
3. Does crop completeness matter to a real model? Compare the fixed cut with the adaptive cut on the same tickets.

It is **not** justified as a measure of accuracy, as a choice between providers, or as evidence for a pilot decision. Those need real tickets.

## Hypothesis

On synthetic tickets, with TicketFlow's adaptive crops:

- **H1:** at least 95% of calls return a valid, parseable extraction with no processing failure.
- **H2:** Gemini **never invents a value for a field that is absent, cut off or illegible on the paper** (`expected` is null). The primary safety metric is a fabrication count of 0, or at most 1 in the 48 tickets.
- **H3:** adaptive crops lower the share of tickets with a core field missing compared with the fixed cut, as the `oracle-crop-aware` simulation predicts (all-six 88.8% → 99.6% on the benchmark).

## Sample

**Synthetic: 48 tickets, stratified.** That is 12 per difficulty (easy, moderate, hard, extreme), generated from a fresh seed (31337) that was not used in development. The generator stratifies its ten layouts equally within each difficulty and records each ticket's writing style (neat, rushed, caps). That gives every layout at least once per difficulty, and a mix of 1-up and 2×2 pages and of styles. The ticket list is fixed in advance by the generated `labels.json`, with no cherry-picking.

**Real: separately, and only with the owner's written permission.** Use up to 20 real tickets that the owner chooses and labels by hand in `eval_data/real/` (`labelled_independently: true`). Include phone photos and carbons. Real and synthetic results are **never scored in one report**; the evaluator refuses mixed evidence. No real document is used until that permission is given.

## Model and version

- `gemini-3.1-pro-preview`, the model TicketFlow is configured with (`settings.gemini_model`, also the default in `benchmark_providers.DEFAULT_MODELS`). Pin it explicitly with `GEMINI_MODEL=gemini-3.1-pro-preview` in the run's environment.
- "Preview" models can change under the same name. Record the run date with the report, and the `model_version` the API returns if it reports one. Treat the result as valid for that model on that date.
- Also record the `google-genai` SDK version from `constraints.txt`.

## Estimated cost (estimate, not a quote)

| Item | Assumption | Per ticket |
|---|---|---|
| Image input | Crops shrunk to a maximum side of 1800 px. Somewhere from about 1,100 tokens (one high-resolution image) to about 1,600 (tiled at 258 tokens per 768 px tile) | ≤ 1,600 tokens |
| Prompt input | `PROMPT` is about 1,400 characters | about 400 tokens |
| Output | About 200 tokens of JSON plus up to about 2,500 thinking tokens, billed as output | ≤ 2,700 tokens |
| Price used | **About $2 per million input tokens and $12 per million output tokens**, my recollection of Gemini 3 Pro preview pricing for prompts under 200k. Check the published price before running. | |
| Cost | 2,000 × $2/M + 2,700 × $12/M | **about $0.036** |

- **48 synthetic tickets:** about **$1.75** per arm. With two arms (fixed and adaptive crop), three repeats to see run-to-run variance, and a retry allowance of 1.5×, that is **about $16** at most.
- **Real tickets:** 20 tickets × 3 repeats is about $2.
- **Hard cap: $25.** Set a billing alert at $20.

## Success metrics and stopping rule

| Metric | Pass |
|---|---|
| Valid responses (H1) | ≥ 95% of calls; 0 crashes or unhandled errors in TicketFlow |
| Fabricated values where `expected` is null (H2) | **0 in tonnage and ticket number**; ≤ 1 elsewhere |
| Critical errors that reach approval (tons, ticket number), after the simulated reviewer | 0; costs exact for every approved ticket (`financial_check`) |
| Adaptive versus fixed crop, share of tickets with a core field missing (H3) | Adaptive no worse on any difficulty |
| Field accuracy per difficulty | Reported only. There is no pass mark, because the number is synthetic. |

**Stopping rule:** stop at once and report if any of these happens:

- any fabricated tonnage;
- 5 consecutive API errors;
- spend reaches $20;
- the first 12 tickets (easy) score under 50% all-six, which would point to a pipeline or prompt fault, not handwriting.

Otherwise run each arm once. Repeat only if the first run passes H1.

## What the results could and couldn't show

**Could show:**
- whether the provider integration and TicketFlow's safety net (flags, review, money maths) behave correctly with a real model;
- whether Gemini abstains or invents when information is missing;
- whether crop completeness changes what a real model reads.

**Couldn't show:**
- real-world OCR accuracy;
- performance on real handwriting, carbons, stains or phone photos;
- which provider is better on real tickets;
- anything about pilot readiness.

A high synthetic score is expected and means little, because there is one glyph set and the forms are clean. A low score on easy tickets would point to a pipeline problem worth fixing before any real-ticket test.

## Prepared commands (DO NOT RUN without authorization)

From `backend/`, with a key configured only in the shell running the experiment. Never commit or print it.

```bash
# 1. Build the 48-ticket sample (offline, free)
for d in easy moderate hard extreme; do
  python -m synthetic generate --difficulty $d --count 12 --seed 31337 \
      --out eval_data/synthetic/generated/gemini-trial-$d --formats pdf
  python -m synthetic validate eval_data/synthetic/generated/gemini-trial-$d
done

# 2. Free dry run with the same sample: must pass before any paid call
for d in easy moderate hard extreme; do
  python -m synthetic evaluate eval_data/synthetic/generated/gemini-trial-$d --extractor oracle-crop-aware \
      --out reports/gemini-trial-$d-dryrun.json
done

# 3. PAID: only after the owner authorizes it, with a $25 cap and a billing alert
export GEMINI_MODEL=gemini-3.1-pro-preview
for d in easy moderate hard extreme; do
  python -m synthetic evaluate eval_data/synthetic/generated/gemini-trial-$d --extractor gemini \
      --i-authorize-paid-calls --seed 0 --out reports/gemini-trial-$d.json --markdown reports/gemini-trial-$d.md
done
```

- **Fixed-cut arm:** a fixed-cut run needs a `split_page(..., adaptive=False)` switch exposed to the evaluator. It isn't wired to the CLI yet, so add it before the paid run if H3 is wanted.
- **Real-ticket arm:** this runs only with the owner's permission, through `scripts/evaluate_workflow.py` on `eval_data/real/`, and never in the same report as synthetic results.
