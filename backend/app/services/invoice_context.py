"""Read-once facts for one response that shows invoice lines.

Each line is still computed from current tickets. This only stops twenty lines
from issuing twenty copies of the same queries. Lives for one request, so a
value here can never go stale across requests.
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Batch, InvoiceComparison, InvoiceDraftLine, InvoiceTicketLink, InvoiceTicketRef, Ticket,
)
from . import scan_manifest
from .accounts import names_by_id
from .invoice_matching import TICKET_COLUMNS, NumberLookup, ticket_key
from .pricing import norm

_IN_CHUNK = 500


def _chunks(values: list, size: int = _IN_CHUNK):
    for start in range(0, len(values), size):
        yield values[start:start + size]


class LineContext:
    def __init__(self, db: Session, organization_id: int, items: list[InvoiceComparison]):
        self.db = db
        self.organization_id = organization_id
        self.numbers = NumberLookup(db, organization_id)
        self._projects: dict[int, tuple[dict, list]] = {}
        self._issues: dict[int, list[str]] = {}
        self._elsewhere: dict[int, tuple[int, str]] | None = None
        self._people: dict[int, str] | None = None
        ids = [item.id for item in items]

        self._refs: dict[int, list[InvoiceTicketRef]] = defaultdict(list)
        self._links: dict[int, list[InvoiceTicketLink]] = defaultdict(list)
        self._sources: dict[int, tuple[int, int]] = {}
        for chunk in _chunks(ids):
            for ref in db.scalars(select(InvoiceTicketRef).where(InvoiceTicketRef.comparison_id.in_(chunk))
                                  .order_by(InvoiceTicketRef.position)):
                self._refs[ref.comparison_id].append(ref)
            for link in db.scalars(select(InvoiceTicketLink).where(
                    InvoiceTicketLink.comparison_id.in_(chunk), InvoiceTicketLink.released_at.is_(None),
            ).order_by(InvoiceTicketLink.id)):
                self._links[link.comparison_id].append(link)
            for comparison_id, document_id, page in db.execute(
                select(InvoiceDraftLine.comparison_id, InvoiceDraftLine.document_id, InvoiceDraftLine.page).where(
                    InvoiceDraftLine.comparison_id.in_(chunk))):
                self._sources[comparison_id] = (document_id, page)

        self.linked_tickets: dict[int, Ticket] = {}
        for chunk in _chunks(sorted({link.ticket_id for rows in self._links.values() for link in rows})):
            self.linked_tickets.update({t.id: t for t in db.scalars(select(Ticket).where(Ticket.id.in_(chunk)))})
        self.numbers.prefetch({ticket_key(ref.ticket_number) for rows in self._refs.values() for ref in rows})

    def refs(self, comparison_id: int) -> list[InvoiceTicketRef]:
        return self._refs.get(comparison_id, [])

    def links(self, comparison_id: int) -> list[InvoiceTicketLink]:
        return self._links.get(comparison_id, [])

    def source(self, comparison_id: int) -> tuple[int, int] | None:
        return self._sources.get(comparison_id)

    @property
    def people(self) -> dict[int, str]:
        if self._people is None:
            self._people = names_by_id(self.db, self.organization_id)
        return self._people

    def _project(self, project_id: int) -> tuple[dict, list]:
        cached = self._projects.get(project_id)
        if cached is None:
            by_scope: dict[tuple[str, str], list] = defaultdict(list)
            unreviewed: list = []
            for row in self.db.execute(
                select(*TICKET_COLUMNS).join(Batch, Batch.id == Ticket.batch_id).where(
                    Batch.organization_id == self.organization_id, Batch.project_id == project_id,
                    Ticket.status != "skipped",
                ).order_by(Ticket.delivery_date, Ticket.id)
            ):
                by_scope[(norm(row.supplier), norm(row.material_type))].append(row)
                if row.status != "verified":
                    unreviewed.append(row)
            cached = self._projects[project_id] = (by_scope, unreviewed)
        return cached

    def in_scope(self, project_id: int, supplier_key: str, material_key: str) -> list:
        """This project's non-skipped deliveries with exactly this supplier and material."""
        return self._project(project_id)[0].get((supplier_key, material_key), [])

    def unreviewed(self, project_id: int) -> list:
        return self._project(project_id)[1]

    def intake_issues(self, project_id: int) -> list[str]:
        if project_id not in self._issues:
            self._issues[project_id] = scan_manifest.project_intake_issues(self.db, self.organization_id, project_id)
        return self._issues[project_id]

    def links_elsewhere(self, comparison_id: int, ticket_ids: set[int]) -> dict[int, str]:
        """{ticket id: invoice number} for active links held by other lines."""
        if not ticket_ids:
            return {}
        if self._elsewhere is None:
            self._elsewhere = {
                ticket_id: (holder, number) for ticket_id, holder, number in self.db.execute(
                    select(InvoiceTicketLink.ticket_id, InvoiceComparison.id, InvoiceComparison.invoice_number)
                    .join(InvoiceComparison, InvoiceComparison.id == InvoiceTicketLink.comparison_id)
                    .where(InvoiceComparison.organization_id == self.organization_id,
                           InvoiceTicketLink.released_at.is_(None)))
            }
        return {ticket_id: self._elsewhere[ticket_id][1] for ticket_id in ticket_ids
                if ticket_id in self._elsewhere and self._elsewhere[ticket_id][0] != comparison_id}
