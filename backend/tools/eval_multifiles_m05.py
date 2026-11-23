"""Private M05 run over the existing originals in backend/samples/real/.

Two modes, and the difference is **cost**:

  local  — ingest every original into a throwaway batch, then run the M05
           recognition with a recognizer that **refuses to be called**. Every page
           that gets candidates this way went the digital-PDF text-layer route:
           zero network calls, zero cost. Pages that need a model show up as
           `blocked_by_guard`, which is exactly the "needs OCR" list.
  live   — the same walk with the real recognizer from .env. This **sends the
           customer originals to the configured OCR host and costs money**; run
           it only with explicit data-export approval.

Reports contain customer text and must stay under backend/samples/real/.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import Config  # noqa: E402
from src.contracts import RecognitionRequest, RecognitionResult  # noqa: E402
from src.db import Database  # noqa: E402
from src.integrations.file_storage import LocalFileStorage  # noqa: E402
from src.integrations.ocr import build_recognizer  # noqa: E402
from src.models import (  # noqa: E402
    INTAKE_CANDIDATE_FIELD_PURCHASE_DATE,
    INTAKE_CANDIDATE_FIELD_SOURCE_NAME,
)
from src.services.intake import IntakeService  # noqa: E402
from src.services.intake_ingest import IntakeIngestService  # noqa: E402
from src.services.intake_recognition import (  # noqa: E402
    OUTCOME_FAILED,
    IntakeRecognitionService,
)
from src.services.invoices import UploadedFile  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
REAL = BACKEND / "samples" / "real"
DOCUMENT_SUFFIXES = (".pdf", ".jpg", ".jpeg", ".png", ".heic")


def private_path(value: str) -> Path:
    path = (BACKEND / value).resolve()
    if not path.is_relative_to(REAL.resolve()):
        raise ValueError("M05 reports must stay under backend/samples/real/")
    return path


class RefusingRecognizer:
    """A recognizer that fails loudly instead of calling anything.

    The point of `local` mode is to prove which pages are read **without** a model
    call. A fake recognizer would quietly answer and hide that; refusing turns
    every attempted call into a visible `blocked_by_guard` row.
    """

    provider = "guard-no-calls"
    is_fake = False
    models = ()

    def __init__(self) -> None:
        self.attempts = 0

    @property
    def model(self) -> str:
        return self.provider

    def recognize(self, request: RecognitionRequest) -> RecognitionResult:
        self.attempts += 1
        from src.integrations.dashscope import OcrServiceError

        raise OcrServiceError(
            "local mode: this page needs a model call, which the guard refuses",
            code="blocked_by_guard",
            retryable=True,
        )


def originals() -> list[Path]:
    return sorted(
        path
        for path in REAL.iterdir()
        if path.is_file() and path.suffix.lower() in DOCUMENT_SUFFIXES
    )


def candidate_rows(item) -> list[dict]:
    return [
        {
            "field": candidate.field,
            "value": candidate.value,
            "basis": candidate.basis,
            "reading": (candidate.evidence or {}).get("reading"),
            "raw_text": (candidate.evidence or {}).get("raw_text"),
            "ambiguous": (candidate.evidence or {}).get("ambiguous"),
        }
        for candidate in item.candidates
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("local", "live"))
    parser.add_argument("--out", required=True, help="private JSON path under samples/real/")
    parser.add_argument("--limit", type=int, default=0, help="stop after N items (0 = all)")
    parser.add_argument(
        "--confirm-source",
        help=(
            "after the pass, confirm this supplier name on every item whose date "
            "stayed ambiguous, set date_order on that supplier's route, and reparse "
            "the saved raw text. Never calls a model. Pass the name on the command "
            "line so customer names stay out of the repository."
        ),
    )
    parser.add_argument("--confirm-date-order", default="mdy", choices=("mdy", "dmy"))
    parser.add_argument(
        "--keep-db",
        action="store_true",
        help=(
            "reuse the existing work database and its open batch instead of "
            "starting over. Pages already recognised stay `done`, so a second live "
            "run costs nothing for them -- which is also how the 'no double charge' "
            "claim gets checked on paid, real data."
        ),
    )
    parser.add_argument(
        "--include",
        action="append",
        help=(
            "restrict the pass to `name` or `name:page` (repeatable). Everything "
            "else is left pending and untouched -- this is how a paid live run is "
            "bounded to exactly the pages that were approved."
        ),
    )
    args = parser.parse_args(argv)
    output = private_path(args.out)

    workdir = REAL / "m05" / f"work-{args.mode}"
    workdir.mkdir(parents=True, exist_ok=True)
    db_path = workdir / "intake.db"
    if db_path.exists() and not args.keep_db:
        db_path.unlink()

    env = dict(os.environ)
    if args.mode == "live":
        load_dotenv(BACKEND / ".env")
        env = dict(os.environ)
        env["OCR_PROVIDER"] = env.get("OCR_PROVIDER") or "dashscope"
        if env["OCR_PROVIDER"] == "fake":
            env["OCR_PROVIDER"] = "dashscope"
    else:
        env["OCR_PROVIDER"] = "fake"
    env.update(
        {
            "APP_ENV": "development",
            "DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
            "FILE_STORAGE_DIR": str(workdir / "uploads"),
            "SECRET_KEY": "m05-eval-" + "x" * 24,
            "UPLOAD_MAX_BYTES": "40000000",
        }
    )
    config = Config.from_env(env)

    import subprocess

    upgrade = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-x",
            f"db_url=sqlite:///{db_path.as_posix()}",
            "upgrade",
            "head",
        ],
        cwd=BACKEND,
        capture_output=True,
        text=True,
    )
    if upgrade.returncode != 0:
        print(upgrade.stdout, upgrade.stderr)
        return 1

    database = Database.from_config(config)
    storage = LocalFileStorage(config.file_storage_dir)
    intake = IntakeService(database)
    ingest = IntakeIngestService(
        intake, file_storage=storage, upload_max_bytes=config.upload_max_bytes
    )
    recognizer = RefusingRecognizer() if args.mode == "local" else build_recognizer(config)
    recognition = IntakeRecognitionService(
        intake, database=database, file_storage=storage, recognizer=recognizer
    )

    existing = intake.latest_open_batch() if args.keep_db else None
    batch = existing or intake.create_batch()
    files: list[dict] = []
    for path in [] if existing else originals():
        outcome = ingest.upload(
            batch.id,
            UploadedFile(
                content=path.read_bytes(), original_name=path.name, content_type=None
            ),
        )
        ingested = outcome.ingested
        files.append(
            {
                "original_name": path.name,
                "status": outcome.ref.status,
                "item_count": outcome.ref.item_count,
                "sha256": outcome.ref.sha256,
                "error": outcome.ref.error_code,
            }
        )
        print(
            f"ingested {path.name}: {outcome.ref.status} "
            f"items={outcome.ref.item_count}"
        )

    def selected(view) -> bool:
        """Is this item in the --include list? No list means everything.

        Items that are not selected are LEFT UNTOUCHED (still pending), so a
        bounded sample never marks the rest of the batch as read -- and never
        spends money on pages nobody asked for.
        """
        if not args.include:
            return True
        for spec in args.include:
            name, _, page = spec.partition(":")
            if view.original_name != name:
                continue
            if not page or str(view.page_no) == page:
                return True
        return False

    def targets() -> list:
        """The items this pass should read, in a stable order.

        With --include we resolve the ids up front instead of draining the
        queue's 50-item window: the window is ordered by upload order, so a
        bounded sample that names a file near the end would never be reached.
        Already-`done` items are skipped by `recognize_item` itself (and cost
        nothing), so listing them here is safe.
        """
        if not args.include:
            return list(intake.recognition_waiting(batch_id=batch.id, limit=50))
        found = []
        offset = 0
        while True:
            page = intake.list_items(batch.id, limit=200, offset=offset)
            found.extend(view for view in page.items if selected(view))
            offset += len(page.items)
            if offset >= page.total or not page.items:
                break
        return found

    started = time.monotonic()
    rows: list[dict] = []
    processed = 0
    while True:
        waiting = [
            view
            for view in targets()
            if view.recognition_status != "done"
        ]
        if not waiting:
            break
        batch_results = []
        for view in waiting:
            batch_results.append(recognition.recognize_item(view.id))
            processed += 1
            if args.limit and processed >= args.limit:
                break
        for result in batch_results:
            item = result.item
            rows.append(
                {
                    "item_id": item.id,
                    "original_name": item.original_name,
                    "kind": item.kind,
                    "page_no": item.page_no,
                    "outcome": result.outcome,
                    "provider": result.provider,
                    "model_calls": result.calls,
                    "error_code": item.recognition_error_code,
                    "error_message": item.recognition_error_message,
                    "candidates": candidate_rows(item),
                    "date_readings": (item.recognition_payload or {}).get(
                        "date_readings"
                    ),
                    "has_text_layer": (
                        (item.recognition_payload or {}).get("source") or {}
                    ).get("has_text_layer"),
                }
            )
        if args.limit and processed >= args.limit:
            break
    elapsed = time.monotonic() - started

    calls = sum(row["model_calls"] for row in rows)
    text_layer = [row for row in rows if row["provider"] == "text-layer"]
    # The guard raises an OcrServiceError, so the item's own code is `ocr_failed`
    # (that is the honest code: the model call failed). The guard's own code is in
    # the message, which is what identifies "this page would have cost money".
    blocked = [row for row in rows if "blocked_by_guard" in (row["error_message"] or "")]
    failed = [row for row in rows if row["outcome"] == OUTCOME_FAILED]
    with_source = [
        row
        for row in rows
        if any(
            candidate["field"] == INTAKE_CANDIDATE_FIELD_SOURCE_NAME
            for candidate in row["candidates"]
        )
    ]
    with_date = [
        row
        for row in rows
        if any(
            candidate["field"] == INTAKE_CANDIDATE_FIELD_PURCHASE_DATE
            for candidate in row["candidates"]
        )
    ]
    ambiguous = [
        row for row in rows if (row["date_readings"] or {}).get("ambiguous") is True
    ]

    # Re-drive the queue: nothing may be re-read, and no call may happen.
    second = [recognition.recognize_item(view.id) for view in targets()]
    guard_attempts = getattr(recognizer, "attempts", None)

    # Optional: the M05 acceptance condition "confirm the supplier, set
    # date_order, get the date back" -- on real originals, with no model call.
    reparsed: list[dict] = []
    if args.confirm_source:
        from src.repositories.extraction import OcrRouteRepository
        from src.services.ingredients import normalize_name

        with database.unit_of_work() as session:
            OcrRouteRepository(session).upsert(
                source_name_normalized=normalize_name(args.confirm_source),
                source_name=args.confirm_source,
                doc_class="digital_pdf",
                decided_from="manual",
                date_order=args.confirm_date_order,
            )
        for row in ambiguous:
            intake.classify_item(row["item_id"], source_name=args.confirm_source)
            outcome = recognition.reparse_date(row["item_id"])
            reparsed.append(
                {
                    "item_id": row["item_id"],
                    "page_no": row["page_no"],
                    "raw_text": (row["date_readings"] or {}).get("raw_text"),
                    "model_calls": outcome.calls,
                    "date_candidates": list(outcome.date_candidates),
                    "date_order": (
                        (outcome.item.recognition_payload or {}).get("date_order") or {}
                    ).get("value"),
                    "still_ambiguous": (
                        (outcome.item.recognition_payload or {}).get("date_readings")
                        or {}
                    ).get("ambiguous"),
                    "decided_purchase_date": (
                        outcome.item.decided_purchase_date.isoformat()
                        if outcome.item.decided_purchase_date
                        else None
                    ),
                }
            )

    summary = {
        "mode": args.mode,
        "items": len(rows),
        "model_calls": calls,
        "seconds": round(elapsed, 2),
        "text_layer_items": len(text_layer),
        "blocked_by_guard": len(blocked),
        "failed": len(failed),
        "items_with_source_candidate": len(with_source),
        "items_with_date_candidate": len(with_date),
        "items_with_ambiguous_date": len(ambiguous),
        "second_pass_items": len(second),
        "second_pass_calls": sum(item.calls for item in second),
        "guard_attempts": guard_attempts,
        "reparsed_items": len(reparsed),
        "reparse_model_calls": sum(row["model_calls"] for row in reparsed),
        "reparse_single_candidate": sum(
            1 for row in reparsed if len(row["date_candidates"]) == 1
        ),
    }
    report = {
        "mode": args.mode,
        "provider": recognizer.provider,
        "models": list(getattr(recognizer, "models", ())),
        "summary": summary,
        "files": files,
        "items": rows,
        "reparsed": reparsed,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"report -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
