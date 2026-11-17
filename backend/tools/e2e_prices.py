"""真实单据上的价格验收：把真实数据算成 $/基础单位（步骤 09）。

与 pytest 的分工（**两者分开记录**，同步骤 08 的约定）：

| 跑什么 | 被测对象 | 数据 |
| --- | --- | --- |
| `pytest tests/test_prices_*.py` | 换算与统计的每条规则、09.10 的三个算例 | 受控数据 |
| `tools/e2e_prices.py`（本文件） | **真实单据上到底算不算得出来** | 真实样本 |

本脚本回答一个 pytest 回答不了的问题：**真实供应商单据上的单位、箱规与日期，
有多少比例能真的变成可比价格。** 它用的是真实数据里本来就有的东西：

- `samples/real/sample_grocery_supplier_invoices.pdf`（5 页数字 PDF，29 行）走
  **真实提取链路**：上传 → 文字层解析 → 草稿 → 逐行确认 → 采购记录 → 价格。
  ⚠️ **这条路一次模型调用也不发**（数字 PDF 直接抽文字层），所以它零成本。
- `samples/real/ground_truth/*.json` 里 6 张印刷体供应商发票的 **人工 Ground
  Truth**（14 行，步骤 07 标注）按原值录入。⚠️ 报告里标明这些是**人核过的值**，
  不是本次模型读出来的——除非带 `--with-ocr`，那时印刷件会**真调模型**再确认，
  用来回答"模型读出来的价格与人核过的价格一不一样"。
- 4 张零售小票的 GT 也读进来，但它们**录不进去**：纸面上没有计量单位。
  这不是缺陷，是事实，报告里按行数记下来。

用法：

    # 零成本（不调模型；仍需凭据，因为真实适配器要能构造出来）
    python tools/e2e_prices.py --out samples/real/eval/e2e_step09.json

    # 真调模型：6 张印刷件 × 2 个模型 ≈ 12 次调用
    python tools/e2e_prices.py --with-ocr

输出是一份 JSON 报告 + 一段可读摘要。**任何算不出来的行都要落到报告里并说明
原因**，不允许"少算了也看不出来"。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alembic import command  # noqa: E402
from alembic.config import Config as AlembicConfig  # noqa: E402

from src.config import BASE_DIR, Config  # noqa: E402
from src.db import Database  # noqa: E402
from src.integrations.file_storage import LocalFileStorage  # noqa: E402
from src.integrations.ocr import build_recognizer  # noqa: E402
from src.normalization import (  # noqa: E402
    DATE_ORDER_MDY,
    NUMBER_FORMAT_DOT,
    normalize_date_text,
    normalize_number_text,
)
from src.services.extraction import ExtractionService  # noqa: E402
from src.services.ingredients import IngredientService, normalize_name  # noqa: E402
from src.services.invoices import (  # noqa: E402
    ConfirmOption,
    InvoiceService,
    UploadedFile,
)
from src.services.prices import (  # noqa: E402
    CONVERSION_AVAILABLE,
    PriceService,
    format_percent,
    format_price,
)
from src.services.purchases import PurchaseService, format_money  # noqa: E402
from src.services.units import (  # noqa: E402
    canonical_unit,
    find_pack_spec_in_description,
)

SAMPLES = BASE_DIR / "samples" / "real"
GROUND_TRUTH = SAMPLES / "ground_truth"
DIGITAL_PDF = SAMPLES / "sample_grocery_supplier_invoices.pdf"
PAGES = SAMPLES / "pages"

# 这两个前缀是印刷体供应商发票的 GT（其余 GT 是零售小票）。
PRINTED_GT_PREFIXES = ("03_Kunkee", "04_NewMoys")

# ⚠️ 这是一次**显式的人工决定**，不是程序猜的：这两家的发票日期写作 `6/3/2026`
# 与 `06/26/2026`，日/月顺序文本本身定不死（`normalize_date_text` 默认拒绝，那是
# 正确行为）。它们是美国供应商、而且 `06/26` 的 26 不可能是月份，所以按 mdy 读。
# 步骤 08 的 `ocr_routes.date_order` 就是让这件事"每家供应商说一次"的机制。
GT_DATE_ORDER = DATE_ORDER_MDY

# 数字 PDF 的表头日期同样是 `6/11/2026`。⚠️ 这份 PDF **纸面上没有供应商名**
# （GT 与文字层都是 missing），所以 `ocr_routes` 这条按供应商名建的路由**无键可用**，
# 日期只能一张一张地人工确认。本次实跑把这个决定记在这里并写进报告。
PDF_DATE_ORDER = DATE_ORDER_MDY

# 容器单位：没有箱规候选时，按「一个容器算一件」确认。
# ⚠️ 这是一个**盘货口径的决定**，不是解析结果——一罐玉米是 15 oz 还是一件，
# 取决于老板怎么盘货。真实使用时这一下由他点；实跑脚本把它写在这里并记进报告。
CONTAINER_UNITS: tuple[str, ...] = ("cup", "can", "jar", "bottle")


# --- 装配 -------------------------------------------------------------------


def _migrated_database(target: Path) -> None:
    config = AlembicConfig(str(BASE_DIR / "alembic.ini"))
    config.attributes["configure_logger"] = False
    config.set_main_option("sqlalchemy.url", f"sqlite:///{target.as_posix()}")
    command.upgrade(config, "head")


class Harness:
    """一套真实服务 + 一个干净的临时库。用的全是生产代码路径。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.database = Database(config.database_url)
        self.storage = LocalFileStorage(config.file_storage_dir)
        self.recognizer = build_recognizer(config)
        self.ingredients = IngredientService(self.database)
        self.purchases = PurchaseService(self.database)
        self.prices = PriceService(self.database)
        self.invoices = InvoiceService(
            self.database,
            file_storage=self.storage,
            recognizer=self.recognizer,
            upload_max_bytes=config.upload_max_bytes,
        )
        self.extraction = ExtractionService(
            self.database,
            file_storage=self.storage,
            recognizer=self.recognizer,
            invoice_service=self.invoices,
            config=config,
        )
        self.invoices.attach_extraction(self.extraction)
        self._items: dict[str, int] = {}

    def close(self) -> None:
        self.database.dispose()

    def food_item(self, description: str, *, unit_text: str | None) -> int:
        """按商品描述取（或建）一条食材，并给它定一个基础单位。

        定基础单位的顺序（⚠️ **这是实跑脚本的取舍，不是产品行为**。真实使用时
        基础单位由老板选，他知道这货是按磅还是按个盘的；这里要一个不偏不倚的
        默认值，好让"有多少行能算出价格"这个数字反映换算层本身的能力）：

        1. 单据上的单位是计量单位 → 就用它（`120 lbs.` → lb）；
        2. 是包装单位，但描述里写着带单位的箱规 → 用箱规里的那个单位
           （Kun Kee 的 `WHEAT FLOUR 1000g x 10` → g）。不这么做的话这条食材的
           基础单位会落成 `each`，而 `10000 g` 的箱规与 `each` 不是同一类计量，
           那条箱规根本存不进去——**真实数据的第一轮实跑就是这么卡住的**；
        3. 其余 → `each`。
        """
        key = normalize_name(description)[:120]
        if key in self._items:
            return self._items[key]
        base_unit = canonical_unit(unit_text)
        if base_unit is None:
            reading = find_pack_spec_in_description(description)
            if reading is not None and reading.pack_unit:
                base_unit = reading.pack_unit
        view = self.ingredients.create_food_item(
            name=description[:120],
            category_id=1,
            base_unit=base_unit or "each",
        )
        self._items[key] = view.id
        return view.id


# --- 阶段 A：数字 PDF 走真实提取链路 ----------------------------------------


def stage_digital_pdf(harness: Harness) -> dict[str, Any]:
    """上传 → 文字层解析 → 草稿 → 逐行确认 → 采购记录。**0 次模型调用。**"""
    report: dict[str, Any] = {"sample": DIGITAL_PDF.name, "model_calls": 0}
    if not DIGITAL_PDF.exists():
        report["skipped"] = "样本不存在"
        return report

    view = harness.invoices.create_invoice(
        [
            UploadedFile(
                content=DIGITAL_PDF.read_bytes(),
                original_name=DIGITAL_PDF.name,
                content_type=None,
            )
        ],
        recognize=False,
    )
    jobs = harness.extraction.schedule(view.id)
    job = jobs[-1]
    report.update(
        {
            "invoice_id": view.id,
            "job_status": job.status,
            "doc_class": job.doc_class,
            "route_source": job.route_source,
            "models": list(job.models),
            "lines_created": job.lines_created,
        }
    )

    detail = harness.invoices.get_invoice(view.id)
    report["lines"] = len(detail.lines)
    report["header"] = {
        "source_name": detail.source_name,
        "invoice_number": detail.invoice_number,
        "invoice_date": detail.invoice_date.isoformat() if detail.invoice_date else None,
        "invoice_total": (
            format_money(detail.invoice_total) if detail.invoice_total else None
        ),
        "currency": detail.currency,
    }

    # ⚠️ 真实发现：表头日期是 `6/11/2026`，日/月顺序定不死，所以提取之后
    # invoice_date 是空的（正确行为：不编造日期）。而这份 PDF 纸上**没有供应商名**，
    # 于是 ocr_routes 的 date_order 没有键可以记。本次实跑显式按 mdy 填一次。
    if detail.invoice_date is None:
        decided = normalize_date_text("6/11/2026", date_order=PDF_DATE_ORDER)
        if decided.ok:
            harness.invoices.update_invoice(view.id, invoice_date=decided.text)
            report["date_decision"] = {
                "raw": "6/11/2026",
                "date_order": PDF_DATE_ORDER,
                "filled": decided.text,
                "why": (
                    "提取后 invoice_date 为空（日/月顺序定不死，系统拒绝解析——"
                    "正确行为）。这份 PDF 纸面没有供应商名，ocr_routes.date_order "
                    "无键可记，只能人工决定一次。"
                ),
            }

    options: list[ConfirmOption] = []
    unconfirmable: list[dict[str, Any]] = []
    for line in detail.lines:
        missing = [
            name
            for name, value in (
                ("quantity", line.quantity),
                ("purchase_unit", line.purchase_unit),
                ("unit_price", line.unit_price),
                ("line_amount", line.line_amount),
            )
            if value is None
        ]
        if missing:
            unconfirmable.append(
                {
                    "line_no": line.line_no,
                    "description": line.description,
                    "missing": missing,
                }
            )
            continue
        options.append(
            ConfirmOption(
                line_id=line.id,
                food_item_id=harness.food_item(
                    line.description or f"line {line.line_no}",
                    unit_text=line.purchase_unit,
                ),
            )
        )

    outcome = harness.invoices.confirm_lines(view.id, options=options)
    report.update(
        {
            "confirmed": outcome.confirmed_count,
            "failed": outcome.failed_count,
            "failures": [
                {
                    "line_id": result.line_id,
                    "code": result.error_code,
                    "message": (result.error_message or "")[:200],
                }
                for result in outcome.results
                if not result.ok
            ],
            "unconfirmable": unconfirmable,
        }
    )
    return report


# --- 阶段 B：印刷体发票的人工 Ground Truth 按原值录入 -----------------------


def _gt_number(raw: str | None) -> str | None:
    if raw is None:
        return None
    result = normalize_number_text(raw, number_format=NUMBER_FORMAT_DOT)
    return result.text


def _gt_field(payload: dict | None) -> str | None:
    if not payload or payload.get("status") != "recognized":
        return None
    return payload.get("raw_text")


def stage_ground_truth(harness: Harness) -> dict[str, Any]:
    """6 张印刷体发票的 GT 行 → 采购记录。

    ⚠️ 这些数值是**人核过的**（步骤 07 标注），不是本次模型读的。报告里的
    `source` 字段写明这一点，不能把它当成"OCR 的准确率"。
    """
    report: dict[str, Any] = {"source": "human_ground_truth", "invoices": []}
    recorded = 0
    skipped: list[dict[str, Any]] = []

    for path in sorted(GROUND_TRUTH.glob("*.json")):
        if not path.name.startswith(PRINTED_GT_PREFIXES):
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        document = payload.get("document", {})
        supplier = _gt_field(document.get("supplier"))
        raw_date = _gt_field(document.get("invoice_date"))
        parsed_date = (
            normalize_date_text(raw_date, date_order=GT_DATE_ORDER)
            if raw_date
            else None
        )
        purchase_date = parsed_date.text if parsed_date and parsed_date.ok else None

        entry: dict[str, Any] = {
            "sample": path.name,
            "supplier": supplier,
            "invoice_date_raw": raw_date,
            "purchase_date": purchase_date,
            "lines": 0,
        }
        for line in payload.get("lines", []):
            description = _gt_field(line.get("item_description"))
            quantity = _gt_number(_gt_field(line.get("quantity")))
            unit = _gt_field(line.get("unit"))
            unit_price = _gt_number(_gt_field(line.get("unit_price")))
            line_amount = _gt_number(_gt_field(line.get("line_amount")))
            pack_spec = _gt_field(line.get("pack_spec"))
            if not (description and quantity and unit and unit_price and line_amount):
                skipped.append(
                    {
                        "sample": path.name,
                        "index": line.get("index"),
                        "description": description,
                        "why": "GT 里缺必填值（描述/数量/单位/单价/行金额）",
                    }
                )
                continue
            # 箱规原文接到描述后面：真实单据上 Kun Kee 的 `1000g x 10` 本来就写在
            # 描述里，New Moy's 的在独立列。接上去之后「从描述里读箱规候选」这条
            # 对两家一视同仁——⚠️ 而裸数字（`6X30`）仍然读不出结论，不会给候选。
            full_description = description
            if pack_spec and pack_spec not in description:
                full_description = f"{description} {pack_spec}"
            harness.purchases.create_purchase(
                food_item_id=harness.food_item(description, unit_text=unit),
                quantity=quantity,
                purchase_unit=unit,
                unit_price=unit_price,
                line_amount=line_amount,
                purchase_date=purchase_date,
                source_name=supplier,
                original_description=full_description[:300],
                accept_amount_mismatch=True,
            )
            recorded += 1
            entry["lines"] += 1
        report["invoices"].append(entry)

    report["recorded"] = recorded
    report["skipped"] = skipped
    return report


def stage_receipts(harness: Harness) -> dict[str, Any]:
    """4 张零售小票：**录不进去**，把为什么数出来。

    不是缺陷，是纸面事实——零售小票不印计量单位（步骤 07 的 GT 里 `unit` 全是
    `missing`）。采购单位是必填的（步骤 04），所以这些行必须由人补一个单位；
    系统不替他猜"一件"还是"一斤"。
    """
    report: dict[str, Any] = {"receipts": [], "lines_total": 0, "lines_blocked": 0}
    for path in sorted(GROUND_TRUTH.glob("*.json")):
        if path.name.startswith(PRINTED_GT_PREFIXES):
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        reasons: Counter[str] = Counter()
        for line in payload.get("lines", []):
            report["lines_total"] += 1
            missing = [
                name
                for name, key in (
                    ("unit", "unit"),
                    ("quantity", "quantity"),
                    ("description", "item_description"),
                )
                if _gt_field(line.get(key)) is None
            ]
            if missing:
                report["lines_blocked"] += 1
                reasons[",".join(missing)] += 1
        report["receipts"].append(
            {
                "sample": path.name,
                "currency": _gt_field(payload.get("document", {}).get("currency")),
                "lines": len(payload.get("lines", [])),
                "blocked_by": dict(reasons),
            }
        )
    return report


# --- 阶段 C：箱规 -----------------------------------------------------------


def stage_pack_specs(harness: Harness) -> dict[str, Any]:
    """先看「待补箱规」清单，照单据上的写法确认，再看一次清单。

    确认的依据有两种，报告里分开标：

    - `from_suggestion`：清单给出的候选（从商品描述里读出来的，例如 Kun Kee 的
      `1000g x 10` → 每箱 10000 g）。⚠️ 候选不会自动生效，这里的"确认"就是
      老板点那一下；
    - `counted_as_pieces`：容器单位（cup / can / jar / bottle）没有候选时，按
      「一杯装算一件、一罐算一件」确认。这是一个**盘货口径的决定**，不是解析结果。

    没有候选、又不是容器单位的组一律**留在清单里**并写进报告——宁可留着让人填，
    也不猜一个会被当成事实的数。
    """
    before, before_total = harness.prices.pending_pack_specs(limit=50)
    report: dict[str, Any] = {
        "pending_before_total": before_total,
        "pending_before": [
            {
                "food_item": row.food_item.name,
                "base_unit": row.food_item.base_unit,
                "source_name": row.source_name,
                "purchase_unit": row.purchase_unit,
                "purchase_count": row.purchase_count,
                "total_amount": format_money(row.total_amount),
                "suggestion": (
                    {
                        "pack_size": str(row.suggestion.pack_size),
                        "pack_unit": row.suggestion.pack_unit,
                        "spec_text": row.suggestion.spec_text,
                    }
                    if row.suggestion
                    else None
                ),
                "descriptions": list(row.descriptions),
            }
            for row in before
        ],
        "decisions": [],
    }

    for row in before:
        decision: dict[str, Any] = {
            "food_item": row.food_item.name,
            "purchase_unit": row.purchase_unit,
        }
        if row.suggestion is not None:
            pack_size = str(row.suggestion.pack_size)
            pack_unit = row.suggestion.pack_unit
            decision["basis"] = "from_suggestion"
            decision["spec_text"] = row.suggestion.spec_text
        elif row.purchase_unit in CONTAINER_UNITS:
            pack_size = "1"
            pack_unit = "each"
            decision["basis"] = "counted_as_pieces"
        else:
            decision.update(
                {
                    "basis": "left_pending",
                    "why": "清单没有给出候选，而它不是容器单位——不猜，留着让人填",
                }
            )
            report["decisions"].append(decision)
            continue
        try:
            view, created, affected = harness.prices.save_pack_spec(
                food_item_id=row.food_item.id,
                source_name=row.source_name,
                purchase_unit=row.purchase_unit,
                pack_size=pack_size,
                pack_unit=pack_unit,
                spec_text=decision.get("spec_text"),
                note=f"步骤 09 实跑：{decision['basis']}",
            )
            decision.update(
                {
                    "applied": True,
                    "rule": (
                        f"1 {view.purchase_unit} = {view.pack_size} {view.pack_unit}"
                    ),
                    "created": created,
                    "affected_purchase_count": affected,
                }
            )
        except Exception as error:  # noqa: BLE001 - 拒绝也是结果，记下来
            decision.update(
                {
                    "applied": False,
                    "refused": f"{type(error).__name__}: {error}"[:300],
                }
            )
        report["decisions"].append(decision)

    after, after_total = harness.prices.pending_pack_specs(limit=50)
    report["pending_after_total"] = after_total
    report["pending_after"] = [
        {
            "food_item": row.food_item.name,
            "purchase_unit": row.purchase_unit,
            "purchase_count": row.purchase_count,
            "total_amount": format_money(row.total_amount),
            "suggestion": bool(row.suggestion),
        }
        for row in after
    ]
    return report


# --- 阶段 D：价格查询 -------------------------------------------------------


def stage_prices(harness: Harness) -> dict[str, Any]:
    """对每条食材查一次价格历史与月度统计，把换算结果汇总出来。"""
    items = harness.ingredients.search_food_items(limit=100)
    report: dict[str, Any] = {
        "food_items": len(items.items),
        "conversion": Counter(),
        "priced": [],
        "unpriced": [],
        "monthly": [],
        "sources": [],
    }

    for item in items.items:
        page = harness.prices.price_history(item.id, limit=100)
        for row in page.items:
            report["conversion"][row.conversion.status] += 1
        available = [
            row for row in page.items if row.conversion.status == CONVERSION_AVAILABLE
        ]
        if available:
            report["priced"].append(
                {
                    "food_item": item.name,
                    "base_unit": item.base_unit,
                    "lines": len(available),
                    "unit_prices": [
                        format_price(row.conversion.normalized_unit_price)
                        for row in available
                        if row.conversion.normalized_unit_price is not None
                    ],
                    "pack_spec_used": next(
                        (
                            f"1 {row.conversion.pack_spec.purchase_unit} = "
                            f"{row.conversion.pack_spec.pack_size} "
                            f"{row.conversion.pack_spec.pack_unit}"
                            for row in available
                            if row.conversion.pack_spec is not None
                        ),
                        None,
                    ),
                }
            )
        else:
            report["unpriced"].append(
                {
                    "food_item": item.name,
                    "base_unit": item.base_unit,
                    "reasons": sorted(
                        {
                            row.conversion.status
                            for row in page.items
                        }
                    ),
                    "example_reason": next(
                        (
                            row.conversion.reason
                            for row in page.items
                            if row.conversion.reason
                        ),
                        None,
                    ),
                }
            )

        try:
            monthly = harness.prices.monthly_prices(item.id, months=12)
        except Exception as error:  # noqa: BLE001 - 多币种等情况要记下来而不是中断
            report["monthly"].append(
                {"food_item": item.name, "error": f"{type(error).__name__}: {error}"[:200]}
            )
            continue
        months_with_data = [
            {
                "month": stats.period.key,
                "weighted_average_unit_price": format_price(
                    stats.weighted_average_unit_price
                ),
                "total_base_quantity": str(stats.total_base_quantity),
                "purchase_count": stats.purchase_count,
            }
            for stats in monthly.months
            if stats.has_data
        ]
        if months_with_data:
            report["monthly"].append(
                {
                    "food_item": item.name,
                    "base_unit": monthly.base_unit,
                    "currency": monthly.currency,
                    "months": months_with_data,
                    "comparison_status": monthly.comparison.status,
                    "change_percent": (
                        format_percent(monthly.comparison.change_percent)
                        if monthly.comparison.change_percent is not None
                        else None
                    ),
                }
            )

        comparison = harness.prices.source_comparison(
            item.id, date_from="2026-01-01", date_to="2026-12-31"
        )
        if len(comparison.sources) >= 2:
            report["sources"].append(
                {
                    "food_item": item.name,
                    "sources": [
                        {
                            "source_name": stats.source_name,
                            "unit_price": format_price(
                                stats.weighted_average_unit_price
                            ),
                        }
                        for stats in comparison.sources
                    ],
                    "spread_percent": (
                        format_percent(comparison.spread_percent)
                        if comparison.spread_percent is not None
                        else None
                    ),
                }
            )

    report["conversion"] = dict(report["conversion"])
    alerts = harness.prices.alerts()
    report["alerts"] = {
        "threshold_percent": format_percent(alerts.threshold_percent),
        "compared_count": alerts.compared_count,
        "fired": len(alerts.alerts),
        "skipped": alerts.skipped,
        "current_period": alerts.current.key,
        "base_period": alerts.base.key,
    }
    return report


# --- 阶段 E（可选）：印刷件真调模型，再比价格 -------------------------------


def _per_unit_price(quantity: str | None, amount: str | None) -> str | None:
    """「单据上这一行的每单位价格」= 行金额 ÷ 数量，按**单据上写的那个单位**。

    为什么比的是这个而不是标准化价格：标准化价格还要经过箱规与食材基础单位，
    那两样在"模型读的"与"人核的"两条路上可能落到不同的食材记录上，比起来会把
    两种差异混在一起。每单位价格只取决于这一行的两个数字——**模型把哪个数读错了，
    这个数就会动**，而这正是要比的东西。`lbs.` 的行上它就是 $/lb。
    """
    if quantity is None or amount is None:
        return None
    try:
        q = Decimal(quantity)
        a = Decimal(amount)
    except Exception:  # noqa: BLE001
        return None
    if q == 0:
        return None
    return format_price(a / q)


def _same_number(left: str | None, right: str | None) -> bool | None:
    """两个数值字符串是不是同一个数。任一边缺就返回 None（比不了）。"""
    if left is None or right is None:
        return None
    try:
        return Decimal(left) == Decimal(right)
    except Exception:  # noqa: BLE001
        return left.strip() == right.strip()


def _same_unit(left: str | None, right: str | None) -> bool | None:
    """两个单位原文是不是同一个单位（按项目的归一口径，`lbs.` 与 `lbs` 相同）。"""
    if left is None or right is None:
        return None
    left_token = canonical_unit(left) or normalize_name(left)
    right_token = canonical_unit(right) or normalize_name(right)
    return left_token == right_token


def stage_with_ocr(harness: Harness) -> dict[str, Any]:
    """6 张印刷件走真实 OCR → 确认 → 价格，与人核过的 GT 逐行逐字段比。

    ⚠️ **这一阶段真花钱**：6 页 × 2 个模型 = 12 次调用。它回答一个 pytest 回答
    不了的问题——**模型读出来的那套数字，算出来的每单位价格与人核过的一样吗。**

    比的东西分三层，报告里分开记：

    1. **逐字段**：数量 / 单位 / 单价 / 行金额，与 GT 比对（`field_matches`）；
    2. **每单位价格**：行金额 ÷ 数量，两边各算一次再比（`per_unit_price_*`）；
    3. **整条链路**：确认入账之后，服务算出来的标准化价格（`normalized_*`）——
       ⚠️ 取的是**这次确认返回的那条采购记录**，不是"这个描述最新的一条"。
       第一轮实跑就是在这里读错了：同一个商品描述在 GT 阶段已经录过，于是
       `history[0]` 返回的是另一笔采购，报出来的价格看起来像模型读错了数。
       **那是报告代码的错，不是链路的错**，但它会被当成结论——所以记在这里。

    模型把某个字段判成"需人填"（两个模型分歧 → `UNREADABLE`）时记
    `needs_human`，**不算读错**：那是交叉裁决在起作用。
    """
    report: dict[str, Any] = {
        "pages": [],
        "model_calls": 0,
        "totals": {
            "lines_compared": 0,
            "per_unit_price_same": 0,
            "per_unit_price_differs": 0,
            "needs_human": 0,
            "no_gt_line": 0,
        },
    }
    pages = [
        path
        for path in sorted(PAGES.glob("*.jpg"))
        if path.name.startswith(PRINTED_GT_PREFIXES)
    ]
    for path in pages:
        started = time.monotonic()
        entry: dict[str, Any] = {"sample": path.name}
        gt_path = GROUND_TRUTH / f"{path.stem}.json"
        gt_lines: list[dict[str, Any]] = []
        if gt_path.exists():
            gt_lines = json.loads(gt_path.read_text(encoding="utf-8")).get("lines", [])
        try:
            view = harness.invoices.create_invoice(
                [
                    UploadedFile(
                        content=path.read_bytes(),
                        original_name=path.name,
                        content_type=None,
                    )
                ],
                recognize=False,
            )
            jobs = harness.extraction.schedule(view.id)
            job = jobs[-1]
            report["model_calls"] += len(job.models)
            detail = harness.invoices.get_invoice(view.id)
            entry.update(
                {
                    "invoice_id": view.id,
                    "doc_class": job.doc_class,
                    "models": list(job.models),
                    "lines": len(detail.lines),
                    "gt_lines": len(gt_lines),
                    "elapsed_seconds": round(time.monotonic() - started, 1),
                    "comparison": [],
                }
            )
            for line in sorted(detail.lines, key=lambda item: item.line_no):
                model_values = {
                    "quantity": None if line.quantity is None else str(line.quantity),
                    "unit": line.purchase_unit,
                    "unit_price": (
                        None if line.unit_price is None else str(line.unit_price)
                    ),
                    "line_amount": (
                        None if line.line_amount is None else str(line.line_amount)
                    ),
                    "description": line.description,
                }
                row: dict[str, Any] = {"line_no": line.line_no, "model": model_values}
                gt_line = (
                    gt_lines[line.line_no - 1]
                    if 0 < line.line_no <= len(gt_lines)
                    else None
                )
                if gt_line is None:
                    row["verdict"] = "no_gt_line"
                    report["totals"]["no_gt_line"] += 1
                    entry["comparison"].append(row)
                    continue
                gt_values = {
                    "quantity": _gt_number(_gt_field(gt_line.get("quantity"))),
                    "unit": _gt_field(gt_line.get("unit")),
                    "unit_price": _gt_number(_gt_field(gt_line.get("unit_price"))),
                    "line_amount": _gt_number(_gt_field(gt_line.get("line_amount"))),
                    "description": _gt_field(gt_line.get("item_description")),
                }
                row["ground_truth"] = gt_values
                row["field_matches"] = {
                    "quantity": _same_number(
                        model_values["quantity"], gt_values["quantity"]
                    ),
                    "unit": _same_unit(model_values["unit"], gt_values["unit"]),
                    "unit_price": _same_number(
                        model_values["unit_price"], gt_values["unit_price"]
                    ),
                    "line_amount": _same_number(
                        model_values["line_amount"], gt_values["line_amount"]
                    ),
                }
                model_price = _per_unit_price(
                    model_values["quantity"], model_values["line_amount"]
                )
                gt_price = _per_unit_price(
                    gt_values["quantity"], gt_values["line_amount"]
                )
                row["per_unit_price_model"] = model_price
                row["per_unit_price_ground_truth"] = gt_price
                report["totals"]["lines_compared"] += 1
                if model_price is None:
                    row["verdict"] = "needs_human"
                    report["totals"]["needs_human"] += 1
                elif model_price == gt_price:
                    row["verdict"] = "same_price"
                    report["totals"]["per_unit_price_same"] += 1
                else:
                    row["verdict"] = "different_price"
                    report["totals"]["per_unit_price_differs"] += 1

                # 整条链路：确认入账，再看服务算出来的标准化价格。
                if model_price is not None:
                    item_id = harness.food_item(
                        line.description or f"{path.stem} line {line.line_no}",
                        unit_text=line.purchase_unit,
                    )
                    outcome = harness.invoices.confirm_lines(
                        view.id,
                        options=[
                            ConfirmOption(
                                line_id=line.id,
                                food_item_id=item_id,
                                accept_amount_mismatch=True,
                            )
                        ],
                    )
                    result = outcome.results[0] if outcome.results else None
                    if result is not None and result.ok and result.purchase_id:
                        row["purchase_id"] = result.purchase_id
                        history = harness.prices.price_history(item_id, limit=100)
                        match = next(
                            (
                                item
                                for item in history.items
                                if item.purchase.id == result.purchase_id
                            ),
                            None,
                        )
                        if match is not None:
                            row["normalized_status"] = match.conversion.status
                            row["normalized_unit_price"] = (
                                format_price(match.conversion.normalized_unit_price)
                                if match.conversion.normalized_unit_price is not None
                                else None
                            )
                            row["normalized_base_unit"] = match.conversion.base_unit
                    elif result is not None:
                        row["confirm_error"] = (result.error_message or "")[:200]
                entry["comparison"].append(row)
        except Exception as error:  # noqa: BLE001 - 一页失败不中断整轮
            entry["error"] = f"{type(error).__name__}: {error}"[:300]
        report["pages"].append(entry)
    return report


# --- 摘要 -------------------------------------------------------------------


def print_summary(report: dict[str, Any]) -> None:
    """打印可读摘要。

    ⚠️ 摘要里**不用 emoji**：Windows 控制台默认是 GBK，`⚠` 不在该编码里，
    打印时会直接抛 UnicodeEncodeError 把整轮实跑的结果丢掉（本次实跑撞到过）。
    JSON 报告是 UTF-8，想看带符号的说明看报告。
    """
    pdf = report["digital_pdf"]
    gt = report["ground_truth"]
    receipts = report["receipts"]
    packs = report["pack_specs"]
    prices = report["prices"]

    print("\n================ 步骤 09 真实数据实跑 ================")
    print(
        f"数字 PDF：{pdf.get('lines')} 行草稿 / 确认 {pdf.get('confirmed')} 行"
        f"（模型调用 {pdf.get('model_calls')} 次，走文字层）"
    )
    if pdf.get("date_decision"):
        print(f"  [注意] 日期：{pdf['date_decision']['why']}")
    print(f"印刷件 GT：录入 {gt.get('recorded')} 行（人核过的值，不是本次模型读的）")
    print(
        f"零售小票：{receipts.get('lines_total')} 行中 "
        f"{receipts.get('lines_blocked')} 行录不进去（纸面没有计量单位）"
    )
    print(
        f"待补箱规：确认前 {packs.get('pending_before_total')} 组 → "
        f"确认 {sum(1 for d in packs['decisions'] if d.get('applied'))} 条后 "
        f"{packs.get('pending_after_total')} 组"
    )
    print(f"换算结果：{prices.get('conversion')}")
    print(f"算出价格的食材：{len(prices.get('priced', []))} 条")
    print(f"算不出价格的食材：{len(prices.get('unpriced', []))} 条")
    for item in prices.get("unpriced", [])[:5]:
        print(f"  - {item['food_item']}（{item['base_unit']}）：{item['reasons']}")
    print(f"来源比价（同一食材 ≥2 个来源）：{len(prices.get('sources', []))} 条")
    print(f"涨价提醒：{prices.get('alerts')}")
    if report.get("with_ocr"):
        ocr = report["with_ocr"]
        totals = ocr["totals"]
        print(
            f"真实 OCR 对照：{len(ocr['pages'])} 页、{ocr['model_calls']} 次调用；"
            f"逐行比 {totals['lines_compared']} 行 -> "
            f"每单位价格相同 {totals['per_unit_price_same']} 行、"
            f"不同 {totals['per_unit_price_differs']} 行、"
            f"模型判为需人填 {totals['needs_human']} 行"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=str(SAMPLES / "eval" / "e2e_step09.json"),
        help="报告落盘路径",
    )
    parser.add_argument(
        "--with-ocr",
        action="store_true",
        help="额外把 6 张印刷件真调模型跑一遍（会产生费用）",
    )
    args = parser.parse_args(argv)

    api_key = os.environ.get("OCR_API_KEY") or os.environ.get("DASHSCOPE_API_KEY", "")
    base_url = os.environ.get("OCR_BASE_URL") or os.environ.get(
        "DASHSCOPE_BASE_URL", ""
    )
    if not api_key or not base_url:
        print(
            "缺少 OCR_API_KEY / OCR_BASE_URL（或 DASHSCOPE_* 别名）。\n"
            "⚠️ 不带 --with-ocr 时本脚本一次模型调用也不发，但真实适配器仍要能"
            "构造出来（缺凭据时它按设计直接失败，不会静默退回假数据）。",
            file=sys.stderr,
        )
        return 2

    workspace = Path(tempfile.mkdtemp(prefix="e2e-step09-"))
    database_path = workspace / "inventory.db"
    _migrated_database(database_path)
    config = Config.from_env(
        {
            "APP_ENV": "development",
            "SECRET_KEY": "e2e-step09-acceptance-run",
            "DATABASE_URL": f"sqlite:///{database_path.as_posix()}",
            "FILE_STORAGE_DIR": str(workspace / "uploads"),
            "OCR_PROVIDER": "dashscope",
            "OCR_WORKER_MODE": "inline",
            "OCR_API_KEY": api_key,
            "OCR_BASE_URL": base_url,
            "UPLOAD_MAX_BYTES": str(40 * 1024 * 1024),
        }
    )

    started = time.monotonic()
    harness = Harness(config)
    report: dict[str, Any] = {
        "workspace": str(workspace),
        "with_ocr_requested": args.with_ocr,
    }
    try:
        report["digital_pdf"] = stage_digital_pdf(harness)
        report["ground_truth"] = stage_ground_truth(harness)
        report["receipts"] = stage_receipts(harness)
        report["pack_specs"] = stage_pack_specs(harness)
        report["prices"] = stage_prices(harness)
        if args.with_ocr:
            report["with_ocr"] = stage_with_ocr(harness)
            report["prices_after_ocr"] = stage_prices(harness)
    finally:
        harness.close()

    report["elapsed_seconds"] = round(time.monotonic() - started, 1)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print_summary(report)
    print(f"\n报告：{out}")
    print(f"耗时：{report['elapsed_seconds']} 秒")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
