"""Duplicate detection. Only ever flags with an explanation; never deletes anything.

Different suppliers reuse ticket numbers, so a number match only counts within the same supplier.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Ticket
from .pricing import norm


def find_duplicates(db: Session, t: Ticket) -> list[str]:
    others = db.scalars(select(Ticket).where(Ticket.id != t.id, Ticket.status != "skipped")).all()
    reasons: list[str] = []
    for o in others:
        where = f"batch {o.batch_id}, page {o.source_page}, ticket {o.position + 1}"
        if t.crop_sha256 and o.crop_sha256 == t.crop_sha256:
            reasons.append(f"Identical scanned image to an existing ticket ({where})")
            continue
        same_supplier = bool(t.supplier) and norm(o.supplier) == norm(t.supplier)
        if same_supplier and t.ticket_number and o.ticket_number == t.ticket_number:
            reasons.append(f"Same supplier and ticket number {t.ticket_number} as an existing ticket ({where})")
            continue
        if (same_supplier and t.delivery_date and o.delivery_date == t.delivery_date
                and t.tonnage is not None and o.tonnage == t.tonnage
                and norm(o.material_type) == norm(t.material_type) and t.material_type):
            reasons.append(f"Same supplier, date, material and tonnage as an existing ticket ({where}) with a different ticket number")
    return reasons
