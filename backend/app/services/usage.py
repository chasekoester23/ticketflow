"""What reading tickets costs each company: reads, tokens and, where prices are set, money.

One extraction_usage row per reader call, written in the same commit as its ticket.
Rows hold counts and the model's name, never anything read from a ticket. Cost is
worked out when reported, from MODEL_PRICES in US dollars per million tokens. With no
price set for a model, its cost is reported as unknown: prices are never guessed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import ExtractionUsage
from .extraction import ExtractionError

LIMIT_KIND = "limit"
MILLION = Decimal(1_000_000)


def limit_reached() -> ExtractionError:
    cap = settings.monthly_ticket_reads_per_organization
    return ExtractionError(
        f"Not read: this company has used its {cap:,} ticket reads for this month. "
        "Enter this ticket by hand, or retry it once the limit is raised or next month starts.",
        kind=LIMIT_KIND, attempts=0)


def _count(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def row_for(result, *, organization_id: int | None, batch_id: int | None, ticket_id: int | None,
            model: str | None) -> ExtractionUsage | None:
    """The usage row for one read, or None when no call was made."""
    if organization_id is None:
        return None
    if isinstance(result, ExtractionError):
        if result.kind == LIMIT_KIND:
            return None
        return ExtractionUsage(organization_id=organization_id, batch_id=batch_id, ticket_id=ticket_id,
                               model=(model or None) and str(model)[:100], attempts=max(0, int(result.attempts or 0)),
                               outcome=str(result.kind)[:30])
    _extracted, raw = result
    meta = raw.get("_ticketflow") if isinstance(raw, dict) else None
    meta = meta if isinstance(meta, dict) else {}
    seconds = meta.get("seconds")
    return ExtractionUsage(
        organization_id=organization_id, batch_id=batch_id, ticket_id=ticket_id,
        model=str(meta.get("model") or model or "")[:100] or None,
        attempts=_count(meta.get("attempts")) or 1,
        prompt_tokens=_count(meta.get("prompt_tokens")), output_tokens=_count(meta.get("output_tokens")),
        thinking_tokens=_count(meta.get("thinking_tokens")),
        seconds=Decimal(str(round(seconds, 3))) if isinstance(seconds, (int, float)) and seconds >= 0 else None,
        outcome="ok")


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def reads_this_month(db: Session, organization_id: int, now: datetime | None = None) -> int:
    return db.scalar(select(func.count()).select_from(ExtractionUsage).where(
        ExtractionUsage.organization_id == organization_id, ExtractionUsage.created_at >= month_start(now))) or 0


def reads_left_this_month(db: Session, organization_id: int, now: datetime | None = None) -> int | None:
    """None when there is no monthly limit. Every call counts, failed or not, as each costs."""
    cap = settings.monthly_ticket_reads_per_organization
    if not cap:
        return None
    return max(0, cap - reads_this_month(db, organization_id, now))


def _price(model: str | None) -> tuple[Decimal, Decimal] | None:
    entry = settings.model_prices.get(model or "")
    if not entry or "input" not in entry or "output" not in entry:
        return None
    return Decimal(entry["input"]), Decimal(entry["output"])


def report(db: Session, organization_id: int, now: datetime | None = None) -> dict:
    """This month so far, for one company."""
    start = month_start(now)
    rows = db.execute(
        select(ExtractionUsage.model, ExtractionUsage.outcome, func.count(), func.sum(ExtractionUsage.attempts),
               func.sum(ExtractionUsage.prompt_tokens), func.sum(ExtractionUsage.output_tokens),
               func.sum(ExtractionUsage.thinking_tokens),
               func.count(ExtractionUsage.prompt_tokens))
        .where(ExtractionUsage.organization_id == organization_id, ExtractionUsage.created_at >= start)
        .group_by(ExtractionUsage.model, ExtractionUsage.outcome)).all()
    models: dict[str, dict] = {}
    for model, outcome, calls, attempts, prompt, output, thinking, counted in rows:
        entry = models.setdefault(model or "unknown", {
            "model": model or "unknown", "tickets_read": 0, "failed_reads": 0, "attempts": 0,
            "prompt_tokens": 0, "output_tokens": 0, "thinking_tokens": 0, "reads_without_token_counts": 0})
        entry["tickets_read" if outcome == "ok" else "failed_reads"] += calls
        entry["attempts"] += int(attempts or 0)
        entry["prompt_tokens"] += int(prompt or 0)
        entry["output_tokens"] += int(output or 0)
        entry["thinking_tokens"] += int(thinking or 0)
        if outcome == "ok":
            entry["reads_without_token_counts"] += calls - int(counted or 0)
    total_cost, cost_known = Decimal(0), bool(models)
    for entry in models.values():
        price = _price(entry["model"])
        if price is None:
            entry["estimated_cost_usd"], cost_known = None, False
            continue
        cost = (entry["prompt_tokens"] * price[0] + (entry["output_tokens"] + entry["thinking_tokens"]) * price[1]) / MILLION
        entry["estimated_cost_usd"] = str(cost.quantize(Decimal("0.0001")))
        total_cost += cost
    cap = settings.monthly_ticket_reads_per_organization or None
    calls = sum(e["tickets_read"] + e["failed_reads"] for e in models.values())
    return {
        "month_start": start.isoformat(),
        "tickets_read": sum(e["tickets_read"] for e in models.values()),
        "failed_reads": sum(e["failed_reads"] for e in models.values()),
        "prompt_tokens": sum(e["prompt_tokens"] for e in models.values()),
        "output_tokens": sum(e["output_tokens"] for e in models.values()),
        "thinking_tokens": sum(e["thinking_tokens"] for e in models.values()),
        # Unknown unless every model used has a price in MODEL_PRICES. Never guessed.
        "estimated_cost_usd": str(total_cost.quantize(Decimal("0.0001"))) if cost_known else None,
        "cost_note": None if cost_known or not models else
        "Unknown: set MODEL_PRICES for every model used, from your provider's price page.",
        "monthly_read_limit": cap,
        "reads_left": None if cap is None else max(0, cap - calls),
        "models": sorted(models.values(), key=lambda e: e["model"]),
    }
