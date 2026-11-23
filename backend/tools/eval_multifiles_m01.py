"""Private, live M01 seller spot check on selected existing originals.

Outputs contain customer text and must stay under backend/samples/real/.
This command calls DashScope and incurs cost; run only with data export approval.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.documents.files import decode_image  # noqa: E402
from src.integrations.crosscheck import combine  # noqa: E402
from src.integrations.dashscope import (  # noqa: E402
    CONTENT_PROMPT,
    CONTENT_PROMPT_SHA12,
    DashScopeClient,
    ModelCall,
    OcrServiceError,
    to_recognition_result,
)

BACKEND = Path(__file__).resolve().parents[1]
REAL = BACKEND / "samples" / "real"
SELLER_FIELDS = ("supplier", "supplier_english", "supplier_chinese", "supplier_evidence")


def private_path(value: str) -> Path:
    path = (BACKEND / value).resolve()
    if not path.is_relative_to(REAL.resolve()):
        raise ValueError("M01 originals and reports must stay under backend/samples/real/")
    return path


def field_payload(found) -> dict | None:
    if found is None:
        return None
    return {"status": str(found.status), "raw_text": found.raw_text, "page": found.page}


def combined_payload(outcome) -> dict:
    return {
        "fields": {name: field_payload(outcome.result.document_field(name)) for name in SELLER_FIELDS},
        "agreements": {name: {"agreed": outcome.agreements[name].agreed,
                              "values": list(outcome.agreements[name].values),
                              "note": outcome.agreements[name].note}
                       for name in SELLER_FIELDS},
        "line_count": len(outcome.result.lines),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", action="append", help="path under samples/real/")
    parser.add_argument("--rejudge", help="existing private live report; offline recheck, no OCR calls")
    parser.add_argument("--out", required=True, help="private JSON path under samples/real/")
    parser.add_argument("--models", default="qwen3.5-ocr,qwen3-vl-plus")
    args = parser.parse_args(argv)
    output = private_path(args.out)
    if args.rejudge:
        source = private_path(args.rejudge)
        report = json.loads(source.read_text(encoding="utf-8"))
        if report.get("mode") != "live_m01_supplier_spot":
            raise ValueError("--rejudge requires a live M01 spot report")
        for sample in report["samples"]:
            results = []
            used = []
            for model in report["models"]:
                stored = sample["models"].get(model, {})
                if "raw_response" not in stored:
                    continue
                call = ModelCall(
                    model=model, prompt_sha12=stored["prompt_sha12"],
                    content=stored["raw_response"], elapsed_seconds=0.0,
                )
                results.append(to_recognition_result(call))
                used.append(model)
            sample["combined"] = combined_payload(combine(results, models=used)) if results else None
        report["mode"] = "offline_rejudge_of_live_m01_supplier_spot"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"M01 offline rejudge: samples={len(report['samples'])} path={output}")
        return 0
    if not args.sample:
        parser.error("--sample is required unless --rejudge is given")
    samples = [private_path(value) for value in args.sample]
    if any(not path.is_file() for path in samples):
        raise FileNotFoundError("One or more selected M01 samples are missing")
    models = tuple(part.strip() for part in args.models.split(",") if part.strip())
    if not models:
        raise ValueError("At least one OCR model is required")

    load_dotenv(BACKEND / ".env")
    api_key = os.environ.get("OCR_API_KEY") or os.environ.get("DASHSCOPE_API_KEY")
    base_url = os.environ.get("OCR_BASE_URL") or os.environ.get("DASHSCOPE_BASE_URL")
    if not api_key or not base_url:
        raise RuntimeError("OCR API key or endpoint is not configured")
    client = DashScopeClient(base_url=base_url, api_key=api_key)
    report = {"mode": "live_m01_supplier_spot", "prompt_sha12": CONTENT_PROMPT_SHA12,
              "models": list(models), "samples": []}
    for path in samples:
        original = path.read_bytes()
        decoded = decode_image(original, original_name=path.name)
        sample = {"file": str(path.relative_to(REAL)),
                  "sha256": hashlib.sha256(original).hexdigest(), "models": {}, "combined": None}
        results = []
        used = []
        for model in models:
            try:
                call = client.recognize(
                    image=decoded.payload, media_type=decoded.media_type,
                    model=model, prompt=CONTENT_PROMPT,
                )
            except OcrServiceError as error:
                sample["models"][model] = {"error_code": error.code}
                continue
            result = to_recognition_result(call)
            sample["models"][model] = {
                "prompt_sha12": call.prompt_sha12,
                "fields": {name: field_payload(result.document_field(name)) for name in SELLER_FIELDS},
                "line_count": len(result.lines),
                "raw_response": call.content,
            }
            results.append(result)
            used.append(model)
        if results:
            outcome = combine(results, models=used)
            sample["combined"] = combined_payload(outcome)
        report["samples"].append(sample)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"M01 live spot report: samples={len(samples)} sha={CONTENT_PROMPT_SHA12} path={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
