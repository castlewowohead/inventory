"""Private M07 run over the existing originals in backend/samples/real/ (multifiles M07).

**This tool cannot cost money and cannot send anything anywhere.** The suggestion
service it drives has no recognizer dependency at all: every clue (same file,
history/aliases, layout fingerprint) is local computation plus database queries.

It reuses an M05 work database instead of re-ingesting 30 MB of originals:

  local — `samples/real/m05/work-local/intake.db`, where the 5 digital-PDF pages
          were read through the text layer and the other 78 pages are the
          "needs OCR" list. Nothing in it was paid for.
  live  — `samples/real/m05/work-live/intake.db`, which holds the **stored results**
          of the user-approved 10-page real OCR sample (2026-11-22). Reading them
          back is a REPLAY of saved results, not new recognition.

The source database is **copied** first and the original `uploads/` directory is
used read-only, so this run cannot disturb the M05 artefacts.

Reports contain customer text and must stay under backend/samples/real/.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import Config  # noqa: E402
from src.db import Database  # noqa: E402
from src.integrations.file_storage import LocalFileStorage  # noqa: E402
from src.models import (  # noqa: E402
    INTAKE_CANDIDATE_BASIS_HISTORY,
    INTAKE_CANDIDATE_BASIS_PAPER,
    INTAKE_CANDIDATE_BASIS_SAME_FILE,
    INTAKE_CANDIDATE_BASIS_SIMILAR_LAYOUT,
    INTAKE_CANDIDATE_FIELD_SOURCE_NAME,
)
from src.services.intake import IntakeService  # noqa: E402
from src.services.intake_suggestions import IntakeSuggestionService  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
REAL = BACKEND / "samples" / "real"


def private_path(value: str) -> Path:
    path = (BACKEND / value).resolve()
    if not path.is_relative_to(REAL.resolve()):
        raise ValueError("M07 reports must stay under backend/samples/real/")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", choices=("local", "live"))
    parser.add_argument("--out", required=True, help="private JSON path under samples/real/")
    parser.add_argument(
        "--confirm",
        action="append",
        default=[],
        help=(
            "simulate one human naming action: `file.pdf:page=Name` confirms that "
            "supplier on that page, so the same-file and layout-memory layers have "
            "something to work from. Pass names on the command line so customer "
            "names stay out of the repository. Repeatable."
        ),
    )
    parser.add_argument(
        "--confirm-from-paper",
        type=int,
        default=0,
        help=(
            "simulate the user accepting the paper reading on the first N items "
            "that have one (one per file, in upload order). Nothing is invented: "
            "the confirmed value is the writing M05 read off the paper, and the "
            "names never reach stdout -- only the private report. This is how the "
            "same-file and history layers get something real to work from."
        ),
    )
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args(argv)
    output = private_path(args.out)

    source_dir = REAL / "m05" / f"work-{args.source}"
    source_db = source_dir / "intake.db"
    if not source_db.exists():
        print(f"missing {source_db}; run tools/eval_multifiles_m05.py {args.source} first")
        return 1

    workdir = REAL / "m07" / f"work-{args.source}"
    workdir.mkdir(parents=True, exist_ok=True)
    db_path = workdir / "intake.db"
    shutil.copyfile(source_db, db_path)

    config = Config.from_env(
        {
            "APP_ENV": "development",
            "DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
            # The M05 upload directory, used READ-ONLY: layout profiling reads the
            # stored bytes and writes nothing back there.
            "FILE_STORAGE_DIR": str(source_dir / "uploads"),
            "SECRET_KEY": "m07-eval-" + "x" * 24,
            "OCR_PROVIDER": "fake",
        }
    )
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
    )
    if upgrade.returncode != 0:
        print(upgrade.stdout.decode("utf-8", "replace"))
        print(upgrade.stderr.decode("utf-8", "replace"))
        return 1

    database = Database.from_config(config)
    storage = LocalFileStorage(config.file_storage_dir)
    intake = IntakeService(database)
    suggestions = IntakeSuggestionService(
        intake, database=database, file_storage=storage
    )

    batch = intake.latest_open_batch()
    if batch is None:
        print("the copied database has no open batch")
        return 1

    # --- what the batch looks like before any suggestion ---------------------
    page = intake.list_items(batch.id, limit=args.limit, offset=0)
    before = {
        "items": page.total,
        "pages": sum(1 for item in page.items if item.kind == "page"),
        "with_paper_source": sum(
            1
            for item in page.items
            if any(
                candidate.basis == INTAKE_CANDIDATE_BASIS_PAPER
                and candidate.field == INTAKE_CANDIDATE_FIELD_SOURCE_NAME
                for candidate in item.candidates
            )
        ),
        "decided_source": sum(
            1 for item in page.items if item.decided_source_name_normalized
        ),
    }

    confirmations: list[dict] = []
    for spec in args.confirm:
        target, _, name = spec.partition("=")
        file_name, _, page_no = target.partition(":")
        match = next(
            (
                item
                for item in page.items
                if item.original_name == file_name
                and (not page_no or str(item.page_no) == page_no)
            ),
            None,
        )
        if match is None or not name:
            print(f"--confirm {spec!r} matched nothing")
            return 1
        intake.classify_item(match.id, source_name=name)
        confirmations.append(
            {"item_id": match.id, "original_name": file_name, "page_no": match.page_no}
        )

    if args.confirm_from_paper:
        seen_files: set[str] = set()
        for item in page.items:
            if len(confirmations) >= args.confirm_from_paper:
                break
            if item.decided_source_name_normalized or item.original_name in seen_files:
                continue
            paper = next(
                (
                    candidate
                    for candidate in item.candidates
                    if candidate.basis == INTAKE_CANDIDATE_BASIS_PAPER
                    and candidate.field == INTAKE_CANDIDATE_FIELD_SOURCE_NAME
                ),
                None,
            )
            if paper is None:
                continue
            seen_files.add(item.original_name)
            intake.classify_item(item.id, source_name=paper.value)
            confirmations.append(
                {
                    "item_id": item.id,
                    "original_name": item.original_name,
                    "page_no": item.page_no,
                    "from": "paper_reading",
                }
            )

    # --- drive the suggestion runs until the layout queue is empty -----------
    started = time.time()
    runs: list[dict] = []
    while True:
        run = suggestions.suggest_batch(batch.id, limit=args.limit, offset=0)
        runs.append(
            {
                "layout_profiled": run.layout_profiled,
                "layout_pending": run.layout_pending,
                "model_calls": run.model_calls,
                "piles": len(run.piles),
                "aliases_recorded": [
                    {"alias_key": item.alias_key, "source_key": item.source_key,
                     "basis": item.basis}
                    for item in run.aliases_recorded
                ],
            }
        )
        if run.layout_pending == 0 or run.layout_profiled == 0:
            break
    elapsed = round(time.time() - started, 1)

    board = suggestions.piles(batch.id)
    final = suggestions.suggest_batch(batch.id, limit=args.limit, offset=0)
    items = intake.list_items(batch.id, limit=args.limit, offset=0)
    by_id = {item.id: item for item in items.items}

    # Which originals does each pile mix? The sample file names carry the
    # customer's own supplier grouping, so "pile mixes two files" is a PROXY for
    # "pile mixes two suppliers" -- not verified per-item truth.
    pile_rows = []
    for pile in board.piles:
        files = Counter(
            by_id[item_id].original_name
            for item_id in pile.item_ids
            if item_id in by_id
        )
        pile_rows.append(
            {
                "fingerprint": pile.fingerprint,
                "basis_detail": pile.basis_detail,
                "template_evidence": pile.template_evidence,
                "features": pile.features,
                "item_count": pile.item_count,
                "excluded": list(pile.excluded_item_ids),
                "files_mixed": dict(files),
                "confirmed_sources": list(pile.confirmed_sources),
                "pages": [
                    {
                        "item_id": item_id,
                        "original_name": by_id[item_id].original_name,
                        "page_no": by_id[item_id].page_no,
                    }
                    for item_id in pile.item_ids
                    if item_id in by_id
                ],
            }
        )

    layers = Counter()
    for result in final.items:
        for suggestion in result.suggestions:
            layers[suggestion.basis] += 1
    coverage = {
        "items_with_any_suggestion": sum(
            1 for result in final.items if result.suggestions
        ),
        "items_with_conflict": sum(1 for result in final.items if result.conflicts),
        "same_file": layers[INTAKE_CANDIDATE_BASIS_SAME_FILE],
        "history": layers[INTAKE_CANDIDATE_BASIS_HISTORY],
        "similar_layout": layers[INTAKE_CANDIDATE_BASIS_SIMILAR_LAYOUT],
        "model_calls_total": sum(entry["model_calls"] for entry in runs)
        + final.model_calls,
    }

    fingerprints = defaultdict(list)
    layout_rows = []
    with database.unit_of_work() as session:
        from src.models import IntakeItem

        for item in session.query(IntakeItem).all():
            if item.kind != "page":
                continue
            layout_rows.append(
                {
                    "item_id": item.id,
                    "original_name": by_id[item.id].original_name
                    if item.id in by_id
                    else None,
                    "page_no": item.page_no,
                    "fingerprint": item.layout_fingerprint,
                    "features": json.loads(item.layout_features)
                    if item.layout_features
                    else None,
                }
            )
            if item.layout_fingerprint:
                fingerprints[item.layout_fingerprint].append(item.id)

    report = {
        "source": args.source,
        "note": (
            "No model call is possible here: the suggestion service has no "
            "recognizer dependency. The `live` source only REPLAYS results that "
            "were already stored by the approved M05 sample."
        ),
        "batch_id": batch.id,
        "before": before,
        "confirmations": confirmations,
        "runs": runs,
        "elapsed_seconds": elapsed,
        "coverage": coverage,
        "piles": pile_rows,
        "unprofiled_item_ids": list(board.unprofiled_item_ids),
        "fingerprint_sizes": {
            key: len(value) for key, value in sorted(fingerprints.items())
        },
        "layout": layout_rows,
        "source_memory": [
            {
                "source_key": entry.source_key,
                "source_name": entry.source_name,
                "confirmed_items": entry.confirmed_items,
                "purchase_records": entry.purchase_records,
                "aliases": [
                    {"alias_text": alias.alias_text, "basis": alias.basis}
                    for alias in entry.aliases
                ],
            }
            for entry in suggestions.source_memory()
        ],
        "items": [
            {
                "item_id": result.item_id,
                "original_name": result.original_name,
                "page_no": result.page_no,
                "paper_source": result.paper_source,
                "resolved": result.resolved,
                "suggestions": [
                    {
                        "basis": suggestion.basis,
                        "value": suggestion.value,
                        "score": str(suggestion.score),
                        "basis_detail": suggestion.evidence.get("basis_detail"),
                    }
                    for suggestion in result.suggestions
                ],
                "conflicts": [dict(item) for item in result.conflicts],
            }
            for result in final.items
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    database.dispose()

    print(f"source={args.source} batch={batch.id} items={before['items']}")
    print(f"  paper-read suppliers before this run: {before['with_paper_source']}")
    print(f"  layout profiled in {len(runs)} run(s), {elapsed}s, model_calls="
          f"{coverage['model_calls_total']}")
    print(f"  piles={len(pile_rows)} unprofiled={len(board.unprofiled_item_ids)}")
    for row in pile_rows:
        print(
            f"    {row['fingerprint']}  n={row['item_count']}  "
            f"{row['basis_detail']}  files={row['files_mixed']}"
        )
    print(
        "  suggestions: same_file={same_file} history={history} "
        "similar_layout={similar_layout} conflicts={items_with_conflict}".format(
            **coverage
        )
    )
    print(f"  report: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
