"""Persistence queries for M08 intake-to-review links."""

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from src.models import IntakeItem, IntakeReviewLink


class IntakeReviewRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def links_for_items(self, item_ids: list[int]) -> list[IntakeReviewLink]:
        return list(self.session.scalars(
            select(IntakeReviewLink).where(IntakeReviewLink.item_id.in_(item_ids))
        ))

    def links_for_invoice(self, invoice_id: int) -> list[IntakeReviewLink]:
        return list(self.session.scalars(
            select(IntakeReviewLink)
            .where(IntakeReviewLink.invoice_id == invoice_id)
            .options(joinedload(IntakeReviewLink.item)
                     .joinedload(IntakeItem.batch_file))
            .order_by(IntakeReviewLink.item_id)
        ))

    def add(self, link: IntakeReviewLink) -> None:
        self.session.add(link)
