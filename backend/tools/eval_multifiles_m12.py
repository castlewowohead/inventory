"""M12 real-original check: which code path every file in `samples/real/` takes.

Two things this answers, and one it refuses to:

1. **Route coverage.** For each real original the repository has, which ingest
   and recognition path it actually goes down — digital-PDF text layer, scanned
   page to the model, image page to the model — and what the local stroke-width
   profile says about printed vs non-uniform ink. This is M12 的第 2 项验收
   ("用仓库现有真实原件验证其实际涵盖的印刷、手写、小票与数字 PDF 路径").
2. **The zero-cost path, for real.** The one digital PDF is recognized here
   through the real text layer with **0 model calls**, so its dates and seller
   names are genuine output on a genuine original and can be compared with the
   paper.

It refuses to say anything about OCR accuracy on the scanned, handwritten and
receipt originals: that needs real model calls, which cost money and are not
made here. Those rows report the route and the local measurements only, and the
summary says so in as many words.

Run from the repository root:

    & .\\backend\\.venv\\Scripts\\python.exe backend/tools/eval_multifiles_m12.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from PIL import Image  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

import io  # noqa: E402

from src.db import Database  # noqa: E402
from src.documents.classify import profile_ink  # noqa: E402
from src.documents.files import media_type_for  # noqa: E402
from src.documents.pdf import extract_page_images, inspect_pdf  # noqa: E402
from src.integrations.file_storage import LocalFileStorage  # noqa: E402
from src.integrations.ocr import FakeRecognizer  # noqa: E402
from src.services.intake import IntakeService  # noqa: E402
from src.services.intake_ingest import IntakeIngestService  # noqa: E402
from src.services.intake_recognition import IntakeRecognitionService  # noqa: E402
from src.services.invoices import UploadedFile  # noqa: E402


# The paper of the one digital PDF, transcribed by hand (the same values the M08
# tool uses). Only this file has a text layer, so only this file can be compared
# with the paper without spending a model call.
DIGITAL_PAPER = {
    1: ("2026-06-11", "HARBOR FRESH PRODUCE LLC"),
    2: ("2026-06-11", "METRO PRIME MEAT WHOLESALE INC."),
    3: ("2026-06-11", "EAST RIVER SEAFOOD DISTRIBUTORS"),
    4: ("2026-06-10", "GOLDEN VALLEY DAIRY & FOODS CO."),
    5: ("2026-06-09", "NORTH STAR GROCERY WHOLESALE"),
}

# What each real original is, by the repository's own naming and the M00
# inventory. This is the layer the report groups by; it is not a claim about
# recognition quality.
LAYERS = {
    "01_ChangShun_Market_32p.pdf": "手写扫描",
    "02_FengYuan_Meat_18p.pdf": "手写扫描",
    "03_Kunkee_Trading_1p.pdf": "印刷扫描",
    "04_NewMoys_Meat_5p.pdf": "手写扫描",
    "05_TangFa_Wholesale_18p.pdf": "手写扫描",
    "sample_grocery_supplier_invoices.pdf": "数字 PDF",
    # ⚠️ 这四张**就是** `receipts/06..09` 那四张零售小票，字节完全相同（sha256
    # 一致），只是在仓库里存了两处名字。所以本工具只传一遍：再传一次会撞上
    # 「同一份文件不用传两次」的 409，而那恰好证明了判重是对的。
    "1.png": "零售小票",
    "2.jpg": "零售小票",
    "3.JPG": "零售小票",
    "4.jpg": "零售小票",
}
RECEIPT_ALIASES = {
    "1.png": "receipts/06_Receipt_IKEA_CN__p01.png",
    "2.jpg": "receipts/07_Receipt_Lowes_EN__p01.jpg",
    "3.JPG": "receipts/08_Receipt_Target_EN__p01.jpg",
    "4.jpg": "receipts/09_Receipt_VegMarket_CN__p01.jpg",
}
DIGITAL = "sample_grocery_supplier_invoices.pdf"


def _page_image(content: bytes, *, is_pdf: bool, page_no: int) -> bytes | None:
    if not is_pdf:
        return content
    images = extract_page_images(content, pages=(page_no,))
    return next((item.payload for item in images if item.payload), None)


def _alias_check(samples: Path) -> dict[str, bool]:
    """Are the four top-level images the same bytes as `receipts/`? (They are.)"""
    import hashlib

    out: dict[str, bool] = {}
    for top, alias in RECEIPT_ALIASES.items():
        a, b = samples / top, samples / alias
        out[f"{top} == {alias}"] = (
            a.is_file() and b.is_file()
            and hashlib.sha256(a.read_bytes()).digest()
            == hashlib.sha256(b.read_bytes()).digest())
    return out


def main() -> None:
    samples = BASE / "samples/real"
    present = [name for name in LAYERS if (samples / name).is_file()]
    missing = [name for name in LAYERS if name not in present]
    receipts = sorted(path for path in (samples / "receipts").glob("*")
                      if path.is_file()) if (samples / "receipts").is_dir() else []
    if not present:
        raise SystemExit(f"No real originals under {samples}")

    report: dict[str, object] = {
        "files": [], "missing": missing,
        "receipt_dir_files": [path.name for path in receipts],
        "receipt_aliases_same_bytes": _alias_check(samples),
    }

    with tempfile.TemporaryDirectory(prefix="inventory-m12-") as temp:
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
            ingest = IntakeIngestService(
                intake, file_storage=storage, upload_max_bytes=64 * 1024 * 1024)
            # The Fake recognizer is wired in only so a scanned page HAS a
            # recognizer to route to; its values are never read below.
            recognition = IntakeRecognitionService(
                intake, database=database, file_storage=storage, recognizer=FakeRecognizer())
            batch_id = intake.create_batch().id

            for name in present:
                content = (samples / name).read_bytes()
                media = media_type_for(content)
                uploaded = ingest.upload(
                    batch_id, UploadedFile(content, Path(name).name, media))
                layer = LAYERS.get(name, "零售小票")
                entry: dict[str, object] = {
                    "name": name, "layer": layer, "bytes": len(content),
                    "sniffed_media_type": media,
                    "status": uploaded.ref.status,
                    "pages": uploaded.ref.item_count,
                    "error": uploaded.ref.error_code,
                }
                if media == "application/pdf":
                    inspection = inspect_pdf(content)
                    entry["pdf_pages"] = inspection.page_count
                    entry["pages_with_text_layer"] = sum(
                        1 for facts in inspection.pages if facts.has_text)
                report["files"].append(entry)

            # 83 内容单元 > 一页 50 条：不翻页就会把后上传的文件整份漏掉，
            # 而漏掉的恰好是唯一一份数字 PDF。
            rows = []
            while True:
                page = intake.list_items(batch_id, limit=50, offset=len(rows))
                rows.extend(page.items)
                if len(rows) >= page.total or not page.items:
                    break
            report["item_count"] = len(rows)

            # --- the digital PDF, through the real text layer, 0 model calls ---
            digital_rows: list[dict[str, object]] = []
            calls = 0
            for row in sorted((item for item in rows if item.original_name == DIGITAL),
                              key=lambda item: item.page_no or 0):
                read = recognition.recognize_item(row.id)
                calls += read.calls
                view = intake.get_item(row.id)
                payload = view.recognition_payload or {}
                # ⚠️ **所有**候选都收下来，不按字段压成一个值：日/月歧义时同一个
                # 字段正好有两条候选，压成一个就会把「两种读法都留着」误报成
                # 「读错了」。
                dates = [candidate.value for candidate in view.candidates
                         if candidate.field == "purchase_date"]
                sources = [candidate.value for candidate in view.candidates
                           if candidate.field == "source_name"]
                paper_date, paper_seller = DIGITAL_PAPER.get(row.page_no or 0, (None, None))
                readings = payload.get("date_readings") or {}
                digital_rows.append({
                    "page": row.page_no,
                    "outcome": read.outcome,
                    "provider": read.provider,
                    "model_calls": read.calls,
                    "date_candidates": dates,
                    "date_ambiguous": bool(readings.get("ambiguous")),
                    "date_raw_text": readings.get("raw_text"),
                    "paper_date": paper_date,
                    "paper_date_among_candidates": paper_date in dates,
                    "source_candidates": sources,
                    "paper_seller": paper_seller,
                    "paper_seller_among_candidates": any(
                        (value or "").strip() == (paper_seller or "").strip()
                        for value in sources),
                    "line_count": len(payload.get("lines") or []),
                })
            report["digital_pdf"] = {"rows": digital_rows, "total_model_calls": calls}

            # --- printed / non-uniform ink, measured locally on real pages ----
            measured: list[dict[str, object]] = []
            for row in rows:
                if row.original_name == DIGITAL:
                    continue
                content = storage.read(row.storage_key)
                is_pdf = row.original_name.lower().endswith(".pdf")
                try:
                    payload = _page_image(content, is_pdf=is_pdf, page_no=row.page_no or 1)
                    if payload is None:
                        measured.append({"name": row.original_name, "page": row.page_no,
                                         "layer": LAYERS.get(row.original_name, "零售小票"),
                                         "style": "page_image_missing"})
                        continue
                    with Image.open(io.BytesIO(payload)) as image:
                        profile = profile_ink(image.convert("L"))
                    measured.append({
                        "name": row.original_name, "page": row.page_no,
                        "layer": LAYERS.get(row.original_name, "零售小票"),
                        "style": profile.style.value,
                        "stroke_samples": profile.stroke_samples,
                    })
                except Exception as error:  # noqa: BLE001 - the report records the reason
                    measured.append({"name": row.original_name, "page": row.page_no,
                                     "layer": LAYERS.get(row.original_name, "零售小票"),
                                     "style": f"error: {type(error).__name__}: {error}"})
            report["local_ink_profile"] = measured
        finally:
            database.dispose()

    print(json.dumps(report, ensure_ascii=False, indent=1))

    digital = report["digital_pdf"]
    matched_dates = sum(1 for row in digital["rows"] if row["paper_date_among_candidates"])
    matched_sources = sum(1 for row in digital["rows"] if row["paper_seller_among_candidates"])
    ambiguous = sum(1 for row in digital["rows"] if row["date_ambiguous"])
    no_source = sum(1 for row in digital["rows"] if not row["source_candidates"])
    by_layer: dict[str, int] = {}
    for entry in report["files"]:
        by_layer[entry["layer"]] = by_layer.get(entry["layer"], 0) + (entry["pages"] or 0)
    styles: dict[str, int] = {}
    for entry in report["local_ink_profile"]:
        key = f"{entry['layer']}/{entry['style']}"
        styles[key] = styles.get(key, 0) + 1
    print()
    print("M12 真实原件路径覆盖：",
          "，".join(f"{layer} {count} 页" for layer, count in sorted(by_layer.items())))
    print("本地笔迹风格（印刷 / 非均匀 / 判不出，按分层计数）：",
          "，".join(f"{key} {count}" for key, count in sorted(styles.items())))
    total = len(digital["rows"])
    print(f"数字 PDF 走真实文字层：{total} 页，模型调用 {digital['total_model_calls']} 次；"
          f"纸面日期出现在候选里 {matched_dates}/{total}，日/月歧义留两种读法 {ambiguous}/{total}；"
          f"纸面卖方出现在候选里 {matched_sources}/{total}，不给商家候选 {no_source}/{total}")
    print("⚠️ 后两项是**既定行为**而不是识别退化：没人确认商家时 date_order 是 strict，"
          "`6/11/2026` 两种读法都留着；这份 PDF 的卖方是页首一行没有标签的标题，"
          "唯一带标签的一方是 `Bill to:` 的买方，所以不给商家候选。")
    print("扫描件 / 手写件 / 小票：只报路径与本地测量——本轮没有发出任何真实模型调用，"
          "因此不给出它们的识别准确率。")


if __name__ == "__main__":
    main()
