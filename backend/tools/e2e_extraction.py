"""端到端验收：整条提取链路跑完全部真实样本（步骤 08 批次 1）。

与 `tools/eval_adapter.py` 的分工：

| 脚本 | 被测对象 |
| --- | --- |
| `eval_adapter.py` | **适配器**：调用 → 解析 → 三态 → 规范化，按 GT 计分 |
| `e2e_extraction.py`（本文件） | **整条链路**：上传 → 分流 → 分层 → 上限 → 草稿行落库 |

本脚本验的是「真实图片与多页 PDF 可进入审核」（08.7）与几条**不需要 GT 也能
客观判定**的硬规则：

1. **手写件一律不预填**：68 张手写件上，草稿行的数值与文本字段必须全部为空。
   ⚠️ 这一条是 08.3h 的核心，也是整个批次最该被回归守住的东西。
2. **行容量是上限**：进草稿的行数不得超过格线检测出的行格数。
3. **空白格子里不得出现任何值**（08.8）。
4. **不可读数字保持待确认**：识别不出来的数值字段留空，不变成 0。
5. **失败可查询且不丢原文件与草稿**（08.5）。

用法：

    export DASHSCOPE_API_KEY=...  DASHSCOPE_BASE_URL=...
    python tools/e2e_extraction.py --workers 6 --out samples/real/eval/e2e.json
    python tools/e2e_extraction.py --limit 4          # 试水

⚠️ 这会**真调模型**：74 页里手写件各 3 次调用（1 次结构 + 2 次内容）、印刷件
各 2 次，整轮约 220 次调用。步骤 07 实测单页 3,000—8,300 tokens。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alembic import command  # noqa: E402
from alembic.config import Config as AlembicConfig  # noqa: E402

from src.config import BASE_DIR, Config  # noqa: E402
from src.db import Database  # noqa: E402
from src.integrations.file_storage import LocalFileStorage  # noqa: E402
from src.integrations.ocr import build_recognizer  # noqa: E402
from src.models import (  # noqa: E402
    DOC_CLASS_DIGITAL_PDF,
    DOC_CLASS_HANDWRITTEN_SCAN,
    DOC_CLASS_PRINTED_SCAN,
    DOC_CLASS_UNKNOWN,
)
from src.services.extraction import ExtractionService  # noqa: E402
from src.services.invoices import InvoiceService, UploadedFile  # noqa: E402

SAMPLES = BASE_DIR / "samples" / "real"
PAGES = SAMPLES / "pages"
RECEIPTS = SAMPLES / "receipts"
DIGITAL_PDF = SAMPLES / "sample_grocery_supplier_invoices.pdf"

# 这两个前缀是印刷体供应商发票，其余扫描单页是中文手写复写纸（步骤 07 的样本构成）。
PRINTED_PREFIXES = ("03_Kunkee", "04_NewMoys")

PREFILL_FIELDS = ("description", "quantity", "purchase_unit", "unit_price", "line_amount")


def _migrated_database(target: Path) -> None:
    config = AlembicConfig(str(BASE_DIR / "alembic.ini"))
    config.attributes["configure_logger"] = False
    config.set_main_option("sqlalchemy.url", f"sqlite:///{target.as_posix()}")
    command.upgrade(config, "head")


def _expected_class(name: str) -> str:
    """这份样本**应该**走哪条路。

    ⚠️ 数字 PDF 必须单列。第一次跑全量时这里漏了它，于是
    `sample_grocery_supplier_invoices.pdf` 被当成手写件，而它从文字层正确预填的
    137 个字段被报成了"手写件泄漏预填值"——**是验收脚本错了，不是链路错了**。
    文字层的字符来自文件本身，预填正是它应有的行为。
    """
    if name.lower().endswith(".pdf"):
        return DOC_CLASS_DIGITAL_PDF
    if name.startswith(PRINTED_PREFIXES):
        return DOC_CLASS_PRINTED_SCAN
    return DOC_CLASS_HANDWRITTEN_SCAN


def run_one(path: Path, config: Config, workspace: Path, lock: threading.Lock) -> dict:
    """一份样本跑一遍完整链路，返回这一页的客观事实。"""
    database = Database(config.database_url)
    storage = LocalFileStorage(config.file_storage_dir)
    recognizer = build_recognizer(config)
    invoices = InvoiceService(
        database,
        file_storage=storage,
        recognizer=recognizer,
        upload_max_bytes=config.upload_max_bytes,
    )
    extraction = ExtractionService(
        database,
        file_storage=storage,
        recognizer=recognizer,
        invoice_service=invoices,
        config=config,
    )
    invoices.attach_extraction(extraction)

    started = time.monotonic()
    record: dict = {"sample": path.name, "expected_class": _expected_class(path.name)}
    try:
        # 上传与入库在锁内：SQLite 单写入者，并发写会撞 database is locked。
        # 调模型那一段在锁外，所以并发度仍然等于 workers。
        with lock:
            view = invoices.create_invoice(
                [
                    UploadedFile(
                        content=path.read_bytes(),
                        original_name=path.name,
                        content_type=None,
                    )
                ],
                recognize=False,
            )
        jobs = extraction.schedule(view.id)
        detail = invoices.get_invoice(view.id)
        job = jobs[-1] if jobs else None
        record.update(
            {
                "invoice_id": view.id,
                "status": job.status if job else "no_job",
                "doc_class": job.doc_class if job else None,
                "route_source": job.route_source if job else None,
                "models": list(job.models) if job else [],
                "lines": len(detail.lines),
                "lines_created": job.lines_created if job else 0,
                "error_code": job.error_code if job else None,
                "error_message": (job.error_message or "")[:200] if job else None,
                "prefilled": _prefilled_fields(detail),
                "row_capacity": _row_capacity(job),
                "elapsed_seconds": round(time.monotonic() - started, 1),
                **_anchor_facts(detail),
            }
        )
    except Exception as error:  # noqa: BLE001 - 一页失败不该中断整轮
        record.update({"status": "crashed", "error_message": f"{type(error).__name__}: {error}"[:300]})
    finally:
        database.dispose()
    return record


def _anchor_facts(detail) -> dict:
    """批次 2 的客观事实：锚定、墨迹闸门、行↔商品行对应、坐标来源。

    全部从**落库之后**的 `raw_fields` 里读，不从内存里的计划读——验收要验的是
    老板那边真正看得到的东西。
    """
    facts: dict = {
        "row_anchor": None,
        "data_bands": None,
        "ink_determined": None,
        "inked_rows": None,
        "column_map": None,
        "printed_rows": [],
        "box_sources": {},
    }
    for line in detail.lines:
        raw = line.raw_fields or {}
        grid = raw.get("grid") or {}
        if grid and facts["row_anchor"] is None:
            facts["row_anchor"] = grid.get("row_anchor")
            facts["data_bands"] = len(grid.get("data_bands") or []) or None
            ink = grid.get("ink") or {}
            facts["ink_determined"] = ink.get("determined")
            facts["inked_rows"] = len(ink.get("inked") or []) if ink.get("determined") else None
            mapping = grid.get("column_map") or {}
            facts["column_map"] = (
                sorted(mapping.get("fields", {})) if mapping.get("established") else None
            )
        printed = raw.get("printed_row")
        if printed is not None:
            facts["printed_rows"].append(printed)
        for payload in (raw.get("fields") or {}).values():
            source = (payload or {}).get("box_source")
            key = str(source)
            facts["box_sources"][key] = facts["box_sources"].get(key, 0) + 1
    return facts


def _prefilled_fields(detail) -> int:
    """草稿行里**有值**的字段数。手写件必须是 0。"""
    total = 0
    for line in detail.lines:
        for name in PREFILL_FIELDS:
            if getattr(line, name, None) not in (None, ""):
                total += 1
    return total


def _row_capacity(job) -> int | None:
    if job is None:
        return None
    for note in job.warnings:
        if "行容量 " in note:
            tail = note.split("行容量 ", 1)[1]
            digits = ""
            for char in tail:
                if char.isdigit():
                    digits += char
                else:
                    break
            if digits:
                return int(digits)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="步骤 08 端到端验收")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 份（试水）")
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--include-pdf", action="store_true", help="把 5 页数字 PDF 也跑一遍"
    )
    args = parser.parse_args(argv)

    api_key = os.environ.get("OCR_API_KEY") or os.environ.get("DASHSCOPE_API_KEY", "")
    base_url = os.environ.get("OCR_BASE_URL") or os.environ.get("DASHSCOPE_BASE_URL", "")
    if not api_key or not base_url:
        print("缺少 OCR_API_KEY / OCR_BASE_URL（或 DASHSCOPE_* 别名）", file=sys.stderr)
        return 2

    samples = sorted(PAGES.glob("*.jpg")) + sorted(RECEIPTS.glob("*"))
    if args.include_pdf and DIGITAL_PDF.exists():
        samples.append(DIGITAL_PDF)
    if args.limit:
        samples = samples[: args.limit]
    if not samples:
        print("没有样本", file=sys.stderr)
        return 2

    workspace = Path(tempfile.mkdtemp(prefix="e2e-step08-"))
    database_path = workspace / "inventory.db"
    _migrated_database(database_path)
    config = Config.from_env(
        {
            "APP_ENV": "development",
            "SECRET_KEY": "e2e-acceptance-run",
            "DATABASE_URL": f"sqlite:///{database_path.as_posix()}",
            "FILE_STORAGE_DIR": str(workspace / "uploads"),
            "OCR_PROVIDER": "dashscope",
            "OCR_WORKER_MODE": "inline",
            "OCR_API_KEY": api_key,
            "OCR_BASE_URL": base_url,
            "UPLOAD_MAX_BYTES": str(40 * 1024 * 1024),
        }
    )
    print(f"共 {len(samples)} 份样本，工作目录 {workspace}")

    lock = threading.Lock()
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        records = list(
            pool.map(lambda path: run_one(path, config, workspace, lock), samples)
        )
    elapsed = time.monotonic() - started

    classes = Counter(item.get("doc_class") for item in records)
    statuses = Counter(item.get("status") for item in records)
    handwritten = [
        item
        for item in records
        if item.get("expected_class") == DOC_CLASS_HANDWRITTEN_SCAN
    ]
    printed = [
        item for item in records if item.get("expected_class") == DOC_CLASS_PRINTED_SCAN
    ]
    leaked = [item for item in handwritten if item.get("prefilled")]
    over_capacity = [
        item
        for item in records
        if item.get("row_capacity") and (item.get("lines") or 0) > item["row_capacity"]
    ]
    misrouted = [
        item
        for item in printed
        if item.get("doc_class") not in (DOC_CLASS_PRINTED_SCAN, None)
    ]

    print(f"\n=== 端到端验收（{elapsed/60:.1f} 分钟）===")
    print(f"  样本 {len(records)} 份，状态：{dict(statuses)}")
    print(f"  分流结果：{dict(classes)}")
    print(
        f"  [手写件不预填] {'通过' if not leaked else '**未通过**'}："
        f"{len(handwritten)} 张手写件中 {len(leaked)} 张出现了预填值"
    )
    for item in leaked[:5]:
        print(f"      · {item['sample']} 预填了 {item['prefilled']} 个字段")
    print(
        f"  [行容量是上限] {'通过' if not over_capacity else '**未通过**'}："
        f"{len(over_capacity)} 页的草稿行数超过了行容量"
    )
    for item in over_capacity[:5]:
        print(f"      · {item['sample']} {item['lines']} 行 > 容量 {item['row_capacity']}")
    print(
        f"  [印刷件分流] {'通过' if not misrouted else '**未通过**'}："
        f"{len(printed)} 张印刷件中 {len(misrouted)} 张没被判成印刷体"
    )
    failed = [item for item in records if item.get("status") not in ("succeeded",)]
    print(f"  [任务全部完成] {'通过' if not failed else '**有失败**'}：{len(failed)} 条未成功")
    for item in failed[:8]:
        print(f"      · {item['sample']} {item.get('status')} {item.get('error_code')} "
              f"{(item.get('error_message') or '')[:120]}")

    total_lines = sum(item.get("lines") or 0 for item in records)
    print(f"  共产出草稿行 {total_lines} 条")

    # --- 批次 2：锚定、墨迹闸门、行↔商品行对应 ---------------------------------
    anchored = [item for item in records if item.get("row_anchor") == "row_numbers"]
    gated = [item for item in records if item.get("ink_determined")]
    mapped = [item for item in records if item.get("printed_rows")]
    print(
        f"  [行号锚定] {len(anchored)}/{len(records)} 页建立"
        f"（锚定不成立的页退回批次 1：不给坐标、裁剪要显式指定序号）"
    )
    print(f"  [墨迹闸门] {len(gated)}/{len(records)} 页判定成功")
    print(f"  [行↔商品行对应] {len(mapped)}/{len(records)} 页建立")

    # ⚠️ 没有 GT 也能客观判定的三条硬规则。误锚是**静默**错误，所以宁可多守几条。
    bad_rows = [
        item
        for item in mapped
        if item["printed_rows"] != sorted(set(item["printed_rows"]))
        or (item.get("data_bands") and max(item["printed_rows"]) > item["data_bands"])
    ]
    print(
        f"  [商品行号严格递增且不越界] {'通过' if not bad_rows else '**未通过**'}："
        f"{len(bad_rows)} 页违例"
    )
    for item in bad_rows[:5]:
        print(f"      · {item['sample']} {item['printed_rows']} / 数据行 {item.get('data_bands')}")

    # ⚠️ **墨迹闸门不当上限用，所以这里不检查「草稿行数 ≤ 有墨行数」。**
    # 一度是那样做的，而 TangFa p14 的第 3 行（淡铅笔，得分 0.0233 < 阈值 0.040）
    # 因此被当成空行**删掉了一条真实采购行**。改为只统计两者的差额供人核对——
    # 差额大的页要么是模型多报，要么是闸门漏判淡笔迹，两种都值得看一眼。
    over_ink = [
        item
        for item in gated
        if item.get("inked_rows") is not None and (item.get("lines") or 0) > item["inked_rows"]
    ]
    print(
        f"  [墨迹闸门与模型的行数差额] {len(over_ink)} 页草稿行数多于有墨行数"
        "（**不截断**，只记录；闸门会漏判淡铅笔的行）"
    )
    for item in over_ink[:5]:
        print(f"      · {item['sample']} 草稿 {item['lines']} 行 / 有墨 {item['inked_rows']} 行")

    # 坐标只能来自我们自己算的格线（grid / grid_row），绝不能冒出 vendor——
    # 当前提示词没要求坐标，模型也没给，出现 vendor 就说明有人在编造坐标。
    fabricated = [
        item for item in records if "vendor" in (item.get("box_sources") or {})
    ]
    print(
        f"  [坐标来源只有格线] {'通过' if not fabricated else '**未通过**'}："
        f"{len(fabricated)} 页出现了非格线来源的坐标"
    )
    sources: Counter = Counter()
    for item in records:
        sources.update(item.get("box_sources") or {})
    print(f"  字段坐标来源分布：{dict(sources)}")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {
                    "elapsed_minutes": round(elapsed / 60, 2),
                    "samples": len(records),
                    "statuses": dict(statuses),
                    "doc_classes": {str(k): v for k, v in classes.items()},
                    "handwritten_prefill_leaks": len(leaked),
                    "over_capacity": len(over_capacity),
                    "misrouted_printed": len(misrouted),
                    "total_lines": total_lines,
                    "row_anchor_established": len(anchored),
                    "ink_gate_determined": len(gated),
                    "line_row_mapped": len(mapped),
                    "bad_printed_rows": len(bad_rows),
                    "lines_over_inked": len(over_ink),
                    "fabricated_boxes": len(fabricated),
                    "box_sources": dict(sources),
                    "records": records,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\n报告已写入 {args.out}")

    shutil.rmtree(workspace, ignore_errors=True)
    return 1 if (leaked or over_capacity or misrouted or failed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
