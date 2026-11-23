"""Offline M08 check with the repository's existing five-page digital PDF.

The page dates and seller names below are manually transcribed from the paper.
This checks real bytes, page references and draft handoff, not OCR accuracy.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from pypdf import PdfReader  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from src.db import Database  # noqa: E402
from src.integrations.file_storage import LocalFileStorage  # noqa: E402
from src.models import Invoice, IntakeReviewLink, Purchase  # noqa: E402
from src.services.intake import IntakeService  # noqa: E402
from src.services.intake_ingest import IntakeIngestService  # noqa: E402
from src.services.intake_reviews import IntakeReviewService  # noqa: E402
from src.services.invoices import UploadedFile  # noqa: E402


PAPER = [
    ("2026-06-11", "HARBOR FRESH PRODUCE LLC", "6/11/2026"),
    ("2026-06-11", "METRO PRIME MEAT WHOLESALE INC.", "6/11/2026"),
    ("2026-06-11", "EAST RIVER SEAFOOD DISTRIBUTORS", "6/11/2026"),
    ("2026-06-10", "GOLDEN VALLEY DAIRY & FOODS CO.", "6/10/2026"),
    ("2026-06-09", "NORTH STAR GROCERY WHOLESALE", "6/9/2026"),
]


def main() -> None:
    sample = BASE / "samples/real/sample_grocery_supplier_invoices.pdf"
    if not sample.is_file():
        raise SystemExit(f"Sample unavailable: {sample}")
    content = sample.read_bytes()
    pdf = PdfReader(sample)
    assert len(pdf.pages) == len(PAPER)
    for page, (_, seller, printed_date) in zip(pdf.pages, PAPER):
        text = page.extract_text() or ""
        assert seller in text and printed_date in text

    with tempfile.TemporaryDirectory(prefix="inventory-m08-") as temp:
        root = Path(temp)
        db_path = root / "inventory.db"
        alembic = Config(str(BASE / "alembic.ini"))
        alembic.attributes["configure_logger"] = False
        alembic.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
        command.upgrade(alembic, "head")
        database = Database(f"sqlite:///{db_path.as_posix()}")
        storage = LocalFileStorage(root / "uploads")
        try:
            intake = IntakeService(database)
            ingest = IntakeIngestService(intake, file_storage=storage,
                                         upload_max_bytes=2 * 1024 * 1024)
            review = IntakeReviewService(database)
            batch_id = intake.create_batch().id
            result = ingest.upload(batch_id, UploadedFile(
                content, sample.name, "application/pdf"))
            assert result.ingested is not None and result.ingested.page_count == 5
            rows = intake.list_items(batch_id).items
            assert [row.page_no for row in rows] == [1, 2, 3, 4, 5]
            invoices = []
            for row, (date, seller, _) in zip(rows, PAPER):
                classified = intake.classify_item(row.id, purchase_date=date,
                                                  source_name=seller, version=row.version)
                handed = review.handoff(batch_id, items=[{
                    "id": classified.id, "version": classified.version}])
                invoices.append(handed["invoice_id"])
                assert storage.read(row.storage_key) == content
                assert review.handoff(batch_id, items=[{
                    "id": classified.id, "version": classified.version}])["invoice_id"] == handed["invoice_id"]
            with database.unit_of_work() as session:
                assert session.scalar(select(func.count()).select_from(Invoice)) == 5
                assert session.scalar(select(func.count()).select_from(IntakeReviewLink)) == 5
                assert session.scalar(select(func.count()).select_from(Purchase)) == 0
            assert len(set(invoices)) == 5
            print("M08 offline sample: 5 real PDF pages, 3 printed dates, 5 manually confirmed sellers, 5 review drafts, 0 purchases; original bytes match; retry idempotent.")
        finally:
            database.dispose()


if __name__ == "__main__":
    main()
