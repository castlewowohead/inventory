"""Score a real-OCR pass over `samples/real/` against the repository's own truth.

`tools/eval_multifiles_m05.py live` spends the money and writes the report; this
reads that report (and the work database it left behind, for the line items) and
turns it into the numbers M00 defined — **per layer, with both numerator and
denominator, and with the unverifiable units left out of the fractions instead
of quietly counted as right or wrong**.

Three kinds of truth are in play here, and they are never mixed:

1. **逐字段人工真值** — `samples/real/ground_truth/*.json`, written by hand from
   the paper. Ten units have it (印刷 6、小票 4). Only these produce an accuracy.
2. **人工转录的纸面日期/卖方** — the five digital-PDF pages, transcribed into
   `eval_multifiles_m12.py`. They go the text-layer route, so they cost nothing
   and are reported separately from the paid layers.
3. **文件名代理** — for the 68 handwritten pages there is no per-page truth in
   this repository. The file name says which supplier the stack came from, so a
   candidate can be checked for *consistency with its own file*. That is a
   coverage/consistency figure and is printed as one; it is **not** an accuracy.

Run (after a live pass has written the report):

    & .\\backend\\.venv\\Scripts\\python.exe backend/tools/eval_multifiles_m12_live.py \\
        --report backend/samples/real/m05/m12_live_full.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

REAL = BACKEND / "samples" / "real"

from src.db import Database  # noqa: E402
from src.services.intake import IntakeService  # noqa: E402

# Which ground-truth file belongs to which (original file, page). The four
# top-level images ARE `receipts/06..09` byte for byte, so their truth lives
# under the receipt names.
GROUND_TRUTH = {
    ("03_Kunkee_Trading_1p.pdf", 1): "03_Kunkee_Trading_1p__p01.json",
    ("04_NewMoys_Meat_5p.pdf", 1): "04_NewMoys_Meat_5p__p01.json",
    ("04_NewMoys_Meat_5p.pdf", 2): "04_NewMoys_Meat_5p__p02.json",
    ("04_NewMoys_Meat_5p.pdf", 3): "04_NewMoys_Meat_5p__p03.json",
    ("04_NewMoys_Meat_5p.pdf", 4): "04_NewMoys_Meat_5p__p04.json",
    ("04_NewMoys_Meat_5p.pdf", 5): "04_NewMoys_Meat_5p__p05.json",
    ("1.png", 1): "06_Receipt_IKEA_CN__p01.json",
    ("2.jpg", 1): "07_Receipt_Lowes_EN__p01.json",
    ("3.JPG", 1): "08_Receipt_Target_EN__p01.json",
    ("4.jpg", 1): "09_Receipt_VegMarket_CN__p01.json",
}

# The layer each original belongs to, by the M00 inventory. `04_NewMoys` is a
# scan of **printed** invoices inside a stack the file name calls handwritten —
# its ground truth says `doc_class: printed`, and the local ink profile agrees,
# so it is scored as printed.
LAYERS = {
    "01_ChangShun_Market_32p.pdf": "手写",
    "02_FengYuan_Meat_18p.pdf": "手写",
    "03_Kunkee_Trading_1p.pdf": "印刷",
    "04_NewMoys_Meat_5p.pdf": "印刷",
    "05_TangFa_Wholesale_18p.pdf": "手写",
    "sample_grocery_supplier_invoices.pdf": "数字 PDF",
    "1.png": "小票",
    "2.jpg": "小票",
    "3.JPG": "小票",
    "4.jpg": "小票",
}

# The file-name proxy for the handwritten stacks: the key token a supplier name
# read off that stack should contain. Nothing else about the page is claimed.
NAME_PROXY = {
    "01_ChangShun_Market_32p.pdf": "changshun",
    "02_FengYuan_Meat_18p.pdf": "fengyuan",
    "05_TangFa_Wholesale_18p.pdf": "tangfa",
}


def squash(value: str | None) -> str:
    """Casefolded letters and digits only — 'KUN KEE TRADING INC.' → 'kunkeetradinginc'."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^0-9a-z一-鿿]+", "", text)


def date_digits(value: str | None) -> str:
    """'06/26/2026' → '06262026'; '21.12.26 20:37:43' → '211226203743'."""
    return re.sub(r"\D", "", value or "")


def load_truth(name: str) -> dict:
    return json.loads((REAL / "ground_truth" / name).read_text(encoding="utf-8"))


def field(truth: dict, name: str) -> tuple[str | None, str | None]:
    entry = (truth.get("document") or {}).get(name) or {}
    return entry.get("status"), entry.get("raw_text")


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # 控制台默认 GBK，报告里有中文和 ⚠️
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        required=True,
        action="append",
        help=(
            "the JSON a live pass wrote; repeatable, because a bounded first pass "
            "and the rest of the batch are two reports over the same work database "
            "(the second pass skips what the first already read, and pays nothing "
            "for it, so its rows are only in the first report)"
        ),
    )
    parser.add_argument(
        "--db",
        default="samples/real/m05/work-live/intake.db",
        help="the work database that pass left behind (for the recognised line items)",
    )
    args = parser.parse_args(argv)

    reports = [json.loads(Path(path).read_text(encoding="utf-8")) for path in args.report]
    report = reports[-1]
    rows = []
    seen: set[int] = set()
    for one in reports:
        for row in one["items"]:
            if row["item_id"] in seen:
                continue
            seen.add(row["item_id"])
            rows.append(row)

    database = Database(f"sqlite:///{(BACKEND / args.db).as_posix()}")
    intake = IntakeService(database)
    payloads: dict[int, dict] = {}
    try:
        for row in rows:
            view = intake.get_item(row["item_id"])
            payloads[row["item_id"]] = view.recognition_payload or {}
    finally:
        database.dispose()

    scored: list[dict] = []
    for row in rows:
        name, page = row["original_name"], row["page_no"] or 1
        layer = LAYERS.get(name, "其它")
        payload = payloads.get(row["item_id"], {})
        sources = [c["value"] for c in row["candidates"] if c["field"] == "source_name"]
        dates = [c["value"] for c in row["candidates"] if c["field"] == "purchase_date"]
        raw_date = (row.get("date_readings") or {}).get("raw_text")
        entry: dict[str, object] = {
            "name": name,
            "page": page,
            "layer": layer,
            "outcome": row["outcome"],
            "provider": row["provider"],
            "model_calls": row["model_calls"],
            "error": row["error_code"],
            "source_candidates": sources,
            "date_candidates": dates,
            "date_raw_text": raw_date,
            "line_count": len(payload.get("lines") or []),
            "truth": None,
        }
        key = GROUND_TRUTH.get((name, page))
        if key:
            truth = load_truth(key)
            supplier_status, supplier_text = field(truth, "supplier")
            date_status, date_text = field(truth, "invoice_date")
            gt_lines = len(truth.get("lines") or [])
            entry["truth"] = {
                "file": key,
                "doc_class": truth.get("doc_class"),
                "supplier_status": supplier_status,
                "supplier_text": supplier_text,
                "supplier_verdict": (
                    # Paper has no readable supplier: the honest answer is to say
                    # nothing. A name here would be invented.
                    ("正确：纸面无来源，也没有编造" if not sources else "编造：纸面无来源却给了候选")
                    if supplier_status != "recognized"
                    else (
                        "正确"
                        if any(squash(value) == squash(supplier_text) for value in sources)
                        else ("漏读：没有给候选" if not sources else "错误")
                    )
                ),
                "date_status": date_status,
                "date_text": date_text,
                "date_raw_verdict": (
                    "未判（纸面无日期）"
                    if date_status != "recognized"
                    else (
                        "原文一致"
                        if date_digits(raw_date) == date_digits(date_text)
                        else ("漏读：没有读到日期原文" if not raw_date else "原文不一致")
                    )
                ),
                "gt_lines": gt_lines,
                "line_verdict": (
                    "行数一致"
                    if entry["line_count"] == gt_lines
                    else ("少报" if entry["line_count"] < gt_lines else "多报")
                ),
            }
        elif name in NAME_PROXY:
            token = NAME_PROXY[name]
            entry["truth"] = {
                "proxy": token,
                "proxy_verdict": (
                    "无候选"
                    if not sources
                    else (
                        "与文件名一致"
                        if any(token in squash(value) for value in sources)
                        else "文件名代理判不出（候选可能是中文名或另一种写法）"
                    )
                ),
            }
        scored.append(entry)

    # ---------------------------------------------------------------- 汇总
    def tally(entries: list[dict], path: tuple[str, str]) -> dict[str, int]:
        out: dict[str, int] = {}
        for entry in entries:
            truth = entry.get("truth") or {}
            value = truth.get(path[1])
            if value is None:
                continue
            out[value] = out.get(value, 0) + 1
        return out

    layers = sorted({entry["layer"] for entry in scored})
    summary: dict[str, object] = {"by_layer": {}}
    for layer in layers:
        entries = [entry for entry in scored if entry["layer"] == layer]
        with_truth = [entry for entry in entries if (entry.get("truth") or {}).get("file")]
        summary["by_layer"][layer] = {
            "units": len(entries),
            "model_calls": sum(entry["model_calls"] for entry in entries),
            "failed": sum(1 for entry in entries if entry["outcome"] == "failed"),
            "units_with_source_candidate": sum(1 for entry in entries if entry["source_candidates"]),
            "units_with_date_raw_text": sum(1 for entry in entries if entry["date_raw_text"]),
            "units_with_lines": sum(1 for entry in entries if entry["line_count"]),
            "ground_truth_units": len(with_truth),
            "supplier": tally(with_truth, ("truth", "supplier_verdict")),
            "date_raw": tally(with_truth, ("truth", "date_raw_verdict")),
            "lines": tally(with_truth, ("truth", "line_verdict")),
            "name_proxy": tally(
                [entry for entry in entries if (entry.get("truth") or {}).get("proxy")],
                ("truth", "proxy_verdict"),
            ),
        }
    summary["totals"] = {
        "units": len(scored),
        "model_calls": sum(entry["model_calls"] for entry in scored),
        "failed": sum(1 for entry in scored if entry["outcome"] == "failed"),
        "ground_truth_units": sum(
            1 for entry in scored if (entry.get("truth") or {}).get("file")
        ),
        "provider": report.get("provider"),
        "models": report.get("models"),
        "seconds": round(
            sum((one.get("summary") or {}).get("seconds") or 0 for one in reports), 2
        ),
    }

    last = Path(args.report[-1])
    out_path = last.with_name(last.stem + "_scored.json")
    out_path.write_text(
        json.dumps({"summary": summary, "units": scored}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"scored -> {out_path}")
    print()
    print(
        "⚠️ 准确率只来自带逐字段人工真值的 "
        f"{summary['totals']['ground_truth_units']} 个单元；"
        "其余单元只报覆盖与一致性，不进准确率分母。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
