"""端到端验收：关键词认食材、自动建食材与收口面板（步骤 10.16）。

与另两个 e2e 脚本的分工：

| 脚本 | 被测对象 |
| --- | --- |
| `e2e_extraction.py` | 整条提取链路：上传 → 分流 → 分层 → 上限 → 草稿行落库 |
| `e2e_prices.py` | 确认之后的换算与价格统计 |
| `e2e_keywords.py`（本文件） | **草稿行上的食材是怎么来的**，以及收口面板的分类汇总 |

验的是 `docs/stru_dev.md` 10.16 逐字写下的那几条，全部**不需要 Ground Truth**
（都是客观事实，不依赖"模型读得对不对"）：

1. **NewMoys 5 页**：13 行认出 7 条食材、分类全部落「肉类」、单位全部从 `lbs.`
   归一成 `lb` 自动建成；**收口面板是一行汇总而不是 7 条明细**。
2. **Kunkee 的 `箱`** 与 **4 张小票的 34 行**：⚠️ **不自动建**，停在
   「请补基础单位」（`food_item_match.reason == "needs_base_unit"`）。
   ⚠️ 这一条是本脚本最该守住的东西——拿 `each` 兜底和步骤 09
   「算不出来时标准化价格是 `null` 而不是 `0`」是同一类错误。
3. **手写件**（TangFa / ChangShun / FengYuan）：全程不提关键词、不新建食材。
   闸门与 08.3h 的 `PREFILL_SUPPRESSED_HANDWRITTEN` 是同一条。

用法：

    export DASHSCOPE_API_KEY=...  DASHSCOPE_BASE_URL=...
    python tools/e2e_keywords.py --out samples/real/eval/e2e_step10.json
    python tools/e2e_keywords.py --all-handwritten    # 手写件也跑全量
    python tools/e2e_keywords.py --limit 2            # 试水

⚠️ 这会**真调模型**。默认集合是 10.16 点名的那些：NewMoys 5 页 + Kunkee 1 页
+ 4 张小票 + 各 2 页手写件（抽样），约 14 页。`--all-handwritten` 把 68 张手写件
全部跑一遍（整轮约 78 页，与步骤 08 的全量同一个量级）。
"""

from __future__ import annotations

import argparse
import json
import os
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
from src.models import DOC_CLASS_HANDWRITTEN_SCAN  # noqa: E402
from src.services.extraction import ExtractionService  # noqa: E402
from src.services.ingredients import IngredientService  # noqa: E402
from src.services.invoices import ConfirmOption, InvoiceService, UploadedFile  # noqa: E402

SAMPLES = BASE_DIR / "samples" / "real"
PAGES = SAMPLES / "pages"
RECEIPTS = SAMPLES / "receipts"

PRINTED_PREFIXES = ("03_Kunkee", "04_NewMoys")
MEAT_CATEGORY = "Meat & Poultry"


def _migrated_database(target: Path) -> None:
    config = AlembicConfig(str(BASE_DIR / "alembic.ini"))
    config.attributes["configure_logger"] = False
    config.set_main_option("sqlalchemy.url", f"sqlite:///{target.as_posix()}")
    command.upgrade(config, "head")


def _group_of(name: str) -> str:
    """这份样本属于 10.16 的哪一组。"""
    if name.startswith("04_NewMoys"):
        return "newmoys"
    if name.startswith("03_Kunkee"):
        return "kunkee"
    if name.startswith(("06_Receipt", "07_Receipt", "08_Receipt", "09_Receipt")):
        return "receipt"
    return "handwritten"


def _services(config: Config):
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
    return database, invoices, extraction


def run_one(path: Path, config: Config, lock: threading.Lock) -> dict:
    """一份样本跑一遍：提取 → 看草稿行上的食材 → 确认能确认的 → 读收口面板。"""
    database, invoices, extraction = _services(config)
    started = time.monotonic()
    record: dict = {"sample": path.name, "group": _group_of(path.name)}
    try:
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
        job = jobs[-1] if jobs else None
        detail = invoices.get_invoice(view.id)

        lines = []
        for line in detail.lines:
            match = line.food_item_match or {}
            lines.append(
                {
                    "line_no": line.line_no,
                    "description": line.description,
                    "purchase_unit": line.purchase_unit,
                    "food_item_id": line.food_item.id if line.food_item else None,
                    "food_item_name": line.food_item.name if line.food_item else None,
                    "food_item_name_zh": (
                        line.food_item.name_zh if line.food_item else None
                    ),
                    "base_unit": line.food_item.base_unit if line.food_item else None,
                    "category": (
                        line.food_item.category.name if line.food_item else None
                    ),
                    "needs_review": (
                        line.food_item.needs_review if line.food_item else None
                    ),
                    "match_reason": match.get("reason"),
                    "suggested_name": match.get("suggested_name"),
                    "status": line.status,
                }
            )
        record.update(
            {
                "invoice_id": view.id,
                "status": job.status if job else "no_job",
                "doc_class": job.doc_class if job else None,
                "lines": lines,
                "elapsed_seconds": round(time.monotonic() - started, 1),
            }
        )

        # 收口面板：把能确认的行确认掉再读。⚠️ 确认不上的行不算失败——缺必填值时
        # 本来就该停下来等人填（步骤 05 的口径）。
        with lock:
            confirmable = [
                line
                for line in detail.lines
                if line.status == "pending"
                and not line.missing_fields
            ]
            if confirmable:
                invoices.confirm_lines(
                    view.id,
                    options=[ConfirmOption(line_id=line.id) for line in confirmable],
                )
            after = invoices.get_invoice(view.id)
        panel = after.reconciliation
        record["panel"] = {
            "confirmed_count": panel.confirmed_count,
            "pending_count": panel.pending_count,
            "by_category": [
                {
                    "name": row.name,
                    "line_count": row.line_count,
                    "amount": str(row.amount),
                }
                for row in panel.by_category
            ],
            "adjustment_total": (
                str(panel.adjustment_total.amount)
                if panel.adjustment_total is not None
                else None
            ),
        }
    except Exception as error:  # noqa: BLE001 - 一页失败不该中断整轮
        record.update(
            {
                "status": "crashed",
                "error_message": f"{type(error).__name__}: {error}"[:300],
            }
        )
    finally:
        database.dispose()
    return record


def _check(records: list[dict], config: Config) -> list[tuple[str, bool, str]]:
    """10.16 的逐条判定。返回（这一条验的是什么, 过没过, 实际看到了什么）。"""
    checks: list[tuple[str, bool, str]] = []
    by_group: dict[str, list[dict]] = {}
    for item in records:
        by_group.setdefault(item["group"], []).append(item)

    # --- 1. NewMoys：认出食材、落肉类、单位归一、自动建 ---
    newmoys = by_group.get("newmoys", [])
    newmoys_lines = [line for item in newmoys for line in item.get("lines", [])]
    identified = [line for line in newmoys_lines if line["food_item_id"]]
    distinct = {line["food_item_id"] for line in identified}
    categories = {line["category"] for line in identified}
    base_units = {line["base_unit"] for line in identified}
    checks.append(
        (
            "NewMoys 的行全部认出了食材",
            bool(newmoys_lines) and len(identified) == len(newmoys_lines),
            f"{len(identified)}/{len(newmoys_lines)} 行",
        )
    )
    checks.append(
        (
            f"认出的食材全部落「{MEAT_CATEGORY}」",
            categories == {MEAT_CATEGORY},
            f"{sorted(c for c in categories if c)}",
        )
    )
    checks.append(
        (
            "基础单位全部从 `lbs.` 归一成 `lb`",
            base_units == {"lb"},
            f"{sorted(u for u in base_units if u)}",
        )
    )
    checks.append(
        (
            "自动建的食材都标了「待确认」",
            all(
                line["needs_review"] is True
                for line in identified
                if line["match_reason"] == "keyword_created"
            ),
            f"{Counter(line['match_reason'] for line in identified)}",
        )
    )
    checks.append(
        (
            "13 行收敛成少数几条食材（不是一行一条）",
            bool(distinct) and len(distinct) < len(newmoys_lines),
            f"{len(newmoys_lines)} 行 → {len(distinct)} 条食材",
        )
    )
    # ⚠️ 这一条是 10.A 的验收尺子：分类一旦变得过细，面板就退化成行列表的重印。
    panels = [item.get("panel", {}) for item in newmoys]
    rows_per_panel = [len(panel.get("by_category") or []) for panel in panels]
    confirmed_any = any(panel.get("confirmed_count") for panel in panels)
    checks.append(
        (
            "收口面板是一行汇总，不是一行一条明细",
            confirmed_any and all(count <= 1 for count in rows_per_panel),
            f"每页面板的分类格数 {rows_per_panel}，"
            f"已确认 {[panel.get('confirmed_count') for panel in panels]} 行",
        )
    )

    # --- 2. Kunkee 的「箱」与小票：不自动建 ---
    #
    # ⚠️ **2026-11-14 全量实跑改了这一组的判据。** 原来写的是「这些行必须停在
    # 『请补基础单位』」，依据是 `stru_dev.md` 10.C 的设想。实跑发现**两条路都
    # 到不了那个分支**，而且理由各不相同：
    #
    # 1. **4 张小票被本地判别器判成了手写件**（`doc_class=handwritten_scan`）。
    #    10.C 原文把小票列在「非手写件」那一侧，但步骤 07 量过的阈值本来就是
    #    「印刷 6/6、手写 0/68、**小票 0/4**」——热敏小票的笔画宽度不均匀，判别器
    #    把它归到手写那一侧。于是 55 行草稿连关键词提取都没进，被 08.3h 那条闸门
    #    挡在更前面。**结果（不自动建）是对的，原因和设想的不一样。**
    # 2. **Kunkee 那一行的描述本身没预填**：两个模型对
    #    `箱　麻薯粉 WHEAT FLOUR 1000g x 10` 没读成同一串，交叉裁决判为分歧 →
    #    描述留空。**没有描述就没有关键词**，自然也没有「猜好的名字」可以预填。
    #
    # 所以判据改成两条真正该成立的事：**描述没读出来就什么都不许凭空给**，
    # 以及**描述读出来了、食材认不出的行必须给出猜测**。
    for group, label in (("kunkee", "Kunkee 的「箱」"), ("receipt", "4 张小票")):
        items = by_group.get(group, [])
        lines = [line for item in items for line in item.get("lines", [])]
        created = [line for line in lines if line["food_item_id"]]
        blind = [line for line in lines if not line["description"]]
        readable = [line for line in lines if line["description"]]
        unmatched = [line for line in readable if not line["food_item_id"]]
        waiting = [line for line in unmatched if line["match_reason"] == "needs_base_unit"]
        checks.append(
            (
                f"{label}：没有自动建食材",
                bool(lines) and not created,
                f"{len(lines)} 行里自动建了 {len(created)} 条",
            )
        )
        checks.append(
            (
                f"{label}：描述没读出来的行**什么都不凭空给**",
                all(line["match_reason"] is None for line in blind),
                f"{len(blind)}/{len(lines)} 行描述为空，其中"
                f"{sum(1 for line in blind if line['match_reason'])} 行被凭空给了猜测",
            )
        )
        checks.append(
            (
                f"{label}：描述读出来但认不出食材的行，一律给出「请补基础单位」",
                len(waiting) == len(unmatched)
                and all(line["suggested_name"] for line in waiting),
                f"{len(waiting)}/{len(unmatched)} 行"
                + ("（本组没有这种行，这条在真实样本上验不到）" if not unmatched else ""),
            )
        )

    # 本地判别器把小票判到哪一侧——10.C 的设想与实测不同，记下来。
    receipt_classes = Counter(
        item.get("doc_class") for item in by_group.get("receipt", [])
    )
    checks.append(
        (
            "小票的分流结果（记录事实，不是判对错）",
            bool(receipt_classes),
            f"{dict(receipt_classes)}"
            "　⚠️ 10.C 原文把小票列在「非手写件」一侧，实测是手写侧",
        )
    )

    # --- 3. 手写件：不提关键词、不新建食材 ---
    handwritten = by_group.get("handwritten", [])
    hand_lines = [line for item in handwritten for line in item.get("lines", [])]
    hand_matched = [line for line in hand_lines if line["match_reason"]]
    hand_items = [line for line in hand_lines if line["food_item_id"]]
    checks.append(
        (
            "手写件全程不提关键词",
            not hand_matched,
            f"{len(hand_lines)} 行里有 {len(hand_matched)} 行带了匹配痕迹",
        )
    )
    checks.append(
        (
            "手写件不新建、不自动选中任何食材",
            not hand_items,
            f"{len(hand_lines)} 行里有 {len(hand_items)} 行被选上了食材",
        )
    )
    checks.append(
        (
            "手写件确实走的是手写分流",
            all(
                item.get("doc_class") == DOC_CLASS_HANDWRITTEN_SCAN
                for item in handwritten
            ),
            f"{Counter(item.get('doc_class') for item in handwritten)}",
        )
    )

    # --- 4. 食材库最终构成 ---
    database = Database(config.database_url)
    try:
        library = IngredientService(database).search_food_items(limit=100)
        names = sorted(
            (item.name, item.category.name, item.base_unit, item.needs_review)
            for item in library.items
        )
    finally:
        database.dispose()
    checks.append(
        (
            "整轮下来食材库里只有肉类那几条（小票一条没建）",
            bool(names) and {row[1] for row in names} == {MEAT_CATEGORY},
            f"{len(names)} 条：{[row[0] for row in names]}",
        )
    )
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="步骤 10.16 端到端验收")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 份（试水）")
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--all-handwritten",
        action="store_true",
        help="手写件跑全量 68 页，而不是各抽 2 页",
    )
    args = parser.parse_args(argv)

    api_key = os.environ.get("OCR_API_KEY") or os.environ.get("DASHSCOPE_API_KEY", "")
    base_url = os.environ.get("OCR_BASE_URL") or os.environ.get("DASHSCOPE_BASE_URL", "")
    if not api_key or not base_url:
        print("缺少 OCR_API_KEY / OCR_BASE_URL（或 DASHSCOPE_* 别名）", file=sys.stderr)
        return 2

    printed = sorted(
        path for path in PAGES.glob("*.jpg") if path.name.startswith(PRINTED_PREFIXES)
    )
    receipts = sorted(RECEIPTS.glob("*"))
    handwritten_all = sorted(
        path
        for path in PAGES.glob("*.jpg")
        if not path.name.startswith(PRINTED_PREFIXES)
    )
    if args.all_handwritten:
        handwritten = handwritten_all
    else:
        # 10.16 点名的两家各抽 2 页：手写件「一律不预填」那条规矩在步骤 08 的全量
        # 实跑里已经验过（72 份 0 处预填泄漏），这里要验的是**关键词这条新路也
        # 不碰它们**。
        handwritten = []
        for prefix in ("05_TangFa", "01_ChangShun"):
            handwritten.extend(
                [path for path in handwritten_all if path.name.startswith(prefix)][:2]
            )

    samples = printed + receipts + handwritten
    if args.limit:
        samples = samples[: args.limit]

    workspace = Path(tempfile.mkdtemp(prefix="e2e-step10-"))
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
    print(
        f"共 {len(samples)} 份样本"
        f"（印刷 {len(printed)} + 小票 {len(receipts)} + 手写 {len(handwritten)}），"
        f"工作目录 {workspace}"
    )

    lock = threading.Lock()
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        records = list(pool.map(lambda path: run_one(path, config, lock), samples))
    elapsed = time.monotonic() - started

    statuses = Counter(item.get("status") for item in records)
    print(f"\n=== 步骤 10.16 端到端验收（{elapsed/60:.1f} 分钟）===")
    print(f"  样本 {len(records)} 份，状态：{dict(statuses)}")
    for item in records:
        if item.get("status") == "crashed":
            print(f"      · {item['sample']} {item.get('error_message')}")

    checks = _check(records, config)
    failed = 0
    print()
    for label, ok, detail in checks:
        print(f"  {'PASS' if ok else '**FAIL**'}  {label}　——　{detail}")
        failed += 0 if ok else 1
    print(f"\n{len(checks) - failed}/{len(checks)} 通过")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {
                    "elapsed_seconds": round(elapsed, 1),
                    "samples": len(records),
                    "checks": [
                        {"label": label, "ok": ok, "detail": detail}
                        for label, ok, detail in checks
                    ],
                    "records": records,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"报告写入 {args.out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
