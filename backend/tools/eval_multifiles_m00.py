"""M00 private sample inventory, annotation draft, and historical supplier replay.

Customer files, per-item labels, and replay details stay under samples/real/.
This tool never calls an OCR provider. It does not turn old GT into reviewed M00 truth.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from eval_ocr import model_value, parse_model_output
from pypdf import PdfReader


BACKEND = Path(__file__).resolve().parent.parent
REAL = BACKEND / "samples" / "real"
GT = REAL / "ground_truth"
OCR = REAL / "ocr"
DEFAULT_MANIFEST = REAL / "m00" / "manifest.json"
MODELS = ("qwen3.5-ocr", "qwen3-vl-plus")


def inventory() -> dict[str, int]:
    pages = list((REAL / "pages").glob("*.jpg"))
    truth = list(GT.glob("*.json"))
    return {
        "scanned_pages": len(pages),
        "legacy_gt_pages": len(truth),
        "scanned_pages_without_legacy_gt": len(pages) - sum(
            (REAL / "pages" / f"{path.stem}.jpg").exists() for path in truth
        ),
        "receipt_images": len(list((REAL / "receipts").glob("*.jpg")))
        + len(list((REAL / "receipts").glob("*.png"))),
        "digital_pdf_pages": len(PdfReader(REAL / "sample_grocery_supplier_invoices.pdf").pages)
        if (REAL / "sample_grocery_supplier_invoices.pdf").exists()
        else 0,
        "csv_files": len(list(REAL.rglob("*.csv"))),
    }


def draft_entries() -> list[dict]:
    entries = []
    old_truth = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in GT.glob("*.json")
    }
    images = sorted((REAL / "pages").glob("*.jpg")) + sorted(
        path for path in (REAL / "receipts").iterdir() if path.suffix.lower() in {".jpg", ".png"}
    )
    for image in images:
        old = old_truth.get(image.stem, {})
        folder = image.parent.name
        supplier = old.get("document", {}).get("supplier", {})
        date = old.get("document", {}).get("invoice_date", {})
        source_document = image.stem.split("__p", 1)[0] + ".pdf" if folder == "pages" else None
        entries.append(
            {
                "id": image.stem,
                "unit_kind": "page" if folder == "pages" else "image",
                "file": f"{folder}/{image.name}",
                "source_document": source_document,
                "page_number": int(image.stem.rsplit("__p", 1)[1]) if folder == "pages" else None,
                "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                "doc_class": old.get("doc_class", "handwritten"),
                "review_state": "pending_original_review",
                "label_provenance": "legacy_gt_only" if old else "none",
                "legacy_supplier_status": supplier.get("status"),
                "legacy_supplier_raw": supplier.get("raw_text"),
                "paper_date_raw": date.get("raw_text"),
                "actual_purchase_date": None,
                "seller_en_raw": None,
                "seller_zh_raw": None,
                "buyer_raw": None,
                "source_printed": "unreviewed",
                "correct_group": None,
                "notes": "",
            }
        )
    digital = REAL / "sample_grocery_supplier_invoices.pdf"
    if digital.exists():
        sha = hashlib.sha256(digital.read_bytes()).hexdigest()
        for page in range(1, len(PdfReader(digital).pages) + 1):
            entries.append(
                {
                    "id": f"sample_grocery_supplier_invoices__p{page:02d}",
                    "unit_kind": "page",
                    "file": digital.name,
                    "source_document": digital.name,
                    "page_number": page,
                    "sha256": sha,
                    "doc_class": "digital_pdf",
                    "review_state": "pending_original_review",
                    "label_provenance": "none",
                    "legacy_supplier_status": None,
                    "legacy_supplier_raw": None,
                    "paper_date_raw": None,
                    "actual_purchase_date": None,
                    "seller_en_raw": None,
                    "seller_zh_raw": None,
                    "buyer_raw": None,
                    "source_printed": "unreviewed",
                    "correct_group": None,
                    "notes": "",
                }
            )
    return entries


def init_manifest(path: Path) -> None:
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite existing private labels: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"schema": "multifiles-m00/1", "entries": draft_entries()}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def supplier_verdict(prompt: str, content: str, label: dict) -> str:
    """Distinguish an unasked field from an attempted read before comparing GT."""
    asks_new = '"supplier_english"' in prompt and '"supplier_chinese"' in prompt
    if not asks_new and '"supplier"' not in prompt:
        return "not_asked"
    data = parse_model_output(content)
    if data is None:
        return "unparseable"
    if asks_new and ("supplier_english" not in data or "supplier_chinese" not in data):
        return "missing_response_field"
    if label.get("review_state") != "reviewed":
        return "awaiting_reviewed_truth"
    presence = label.get("source_printed")
    if presence not in {"present", "absent", "unreadable"}:
        return "awaiting_reviewed_truth"
    got = (model_value(data, "supplier_english") or model_value(data, "supplier_chinese")) if asks_new else model_value(data, "supplier")
    if presence == "absent":
        return "correct_absent" if got is None else "invented_source"
    if presence == "unreadable":
        return "correct_unreadable" if got is None else "unverifiable_claim"
    expected = label.get("seller_en_raw") or label.get("seller_zh_raw")
    if not expected:
        return "awaiting_reviewed_truth"
    if got is None:
        return "paper_present_unread"
    compact = lambda value: " ".join(value.casefold().split())
    return "correct_candidate" if compact(got) == compact(expected) else "wrong_candidate"


def replay(manifest: Path) -> dict:
    labels = json.loads(manifest.read_text(encoding="utf-8"))
    if labels.get("schema") != "multifiles-m00/1":
        raise ValueError("Unknown M00 manifest schema")
    entries = labels["entries"]
    if len({entry["id"] for entry in entries}) != len(entries):
        raise ValueError("Duplicate annotation IDs")
    by_class: dict[str, Counter] = defaultdict(Counter)
    by_model: dict[str, Counter] = defaultdict(Counter)
    prompt_hashes: dict[str, Counter] = defaultdict(Counter)
    provenance = Counter(entry.get("review_state", "unknown") for entry in entries)
    legacy_supplier_status = Counter(
        entry["legacy_supplier_status"]
        for entry in entries
        if entry.get("legacy_supplier_status") is not None
    )
    for entry in entries:
        sample = REAL / entry["file"]
        if not sample.is_file():
            raise FileNotFoundError(sample)
        if hashlib.sha256(sample.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError(f"Sample changed since annotation draft: {entry['id']}")
        for model in MODELS:
            path = OCR / model / f"{entry['id']}.json"
            if not path.exists():
                verdict = "no_recorded_response"
            else:
                recorded = json.loads(path.read_text(encoding="utf-8"))
                if "error" in recorded:
                    verdict = "recorded_error"
                else:
                    prompt = recorded.get("prompt", "")
                    sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
                    prompt_hashes[model][sha] += 1
                    verdict = supplier_verdict(prompt, recorded.get("content", ""), entry)
            by_class[entry["doc_class"]][verdict] += 1
            by_model[model][verdict] += 1
    precision = {}
    coverage = {}
    for model, counts in by_model.items():
        proposed = sum(counts[key] for key in ("correct_candidate", "wrong_candidate", "invented_source"))
        readable = sum(counts[key] for key in ("correct_candidate", "wrong_candidate", "paper_present_unread"))
        precision[model] = counts["correct_candidate"] / proposed if proposed else None
        coverage[model] = (
            (counts["correct_candidate"] + counts["wrong_candidate"]) / readable
            if readable else None
        )
    return {
        "mode": "historical_response_replay_no_live_ocr",
        "inventory": inventory(),
        "manifest_entries": len(entries),
        "annotation_review_states": dict(provenance),
        "legacy_supplier_status_unreviewed": dict(legacy_supplier_status),
        "prompt_sha12_by_model": {key: dict(value) for key, value in prompt_hashes.items()},
        "supplier_verdict_by_class": {key: dict(value) for key, value in by_class.items()},
        "supplier_verdict_by_model": {key: dict(value) for key, value in by_model.items()},
        "candidate_precision_by_model": precision,
        "candidate_coverage_by_model": coverage,
        "wrong_group_rate": None,
        "ungrouped_rate": None,
        "manual_actions_per_batch": None,
        "completion_seconds_per_batch": None,
        "limitations": [
            "Legacy prompt did not ask supplier; not_asked is excluded from OCR miss rate.",
            "Unreviewed legacy GT is not complete M00 truth.",
            "No classification UI or batch workflow exists for group and timing metrics.",
        ],
    }


def private_path(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(REAL.resolve()):
        raise ValueError("M00 labels and reports must stay under backend/samples/real/")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("inventory", "init", "replay"))
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.mode == "inventory":
        print(json.dumps(inventory(), ensure_ascii=False, indent=2))
    elif args.mode == "init":
        path = private_path(args.manifest)
        init_manifest(path)
        print(f"Private annotation draft: {path} ({len(draft_entries())} entries)")
    else:
        result = replay(private_path(args.manifest))
        if args.out:
            path = private_path(args.out)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
