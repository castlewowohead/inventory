"""多模型交叉裁决：一致才预填，分歧就置 `UNREADABLE`（步骤 08.3c）。

### 为什么这是唯一保留下来的「可信度」来源

步骤 07 把 `imp_notes.md` 4.6 设想的几类置信度来源逐个实测了一遍，结果是：

| 手段 | 实测结果 |
| --- | --- |
| 模型自报置信度 / logprobs | 接口**接受 `logprobs` 参数但不返回** |
| 提示词要求「认不出就说认不出」 | 两个专用 OCR 模型 234 份里**一次都没照做** |
| 算术校验（数量 × 单价 = 金额） | **抓不到自洽的编造**——模型会由金额反推单价 |
| 重复采样 | 疑似服务端缓存，同一张图同一提示词返回同一答案 |
| **跨模型一致性** | ✅ **唯一有效的**，但不是充分条件 |

### 一致 ≠ 对

⚠️ TangFa p07 的单价纸面写作 `5 85`（即 5.85），`qwen-vl-ocr` 与 `qwen3.5-ocr`
**都报成 `185`**——两个模型一致，而且一致地错，差 20 倍量级。所以「多模型一致」
只是**预填的必要条件**，预填出来的值仍然带着「未经人工确认」的标记，而且手写件
一律不预填（`services/extraction.py` 的 08.3h）。

### 分歧率本身就是信息

步骤 07 实测手写件上中文商品名跨模型分歧率 **96.0%**、数字 **66.9%**。
分歧率是错误率的**下界**。所以在手写件上，「一致」这个条件本身就很少成立——
这也是「数字字段的交叉验证预填口子默认关闭」的实测依据。

### 行数不一致时宁可多留一行

两个模型报出的行数不同时，取**较多的那个**，少的那边缺的行全部置 `UNREADABLE`
并挂一条提示。理由：**漏行是这条链路上最难发现的错误**——各行金额之和对不上
单据总额只能抓到一部分，手写单常常连总额都没有，那时只剩人眼这一关。
多出来的空行是看得见的噪声，漏掉的一行是看不见的损失。

至于「模型凭空多报行」（`qwen-vl-ocr` 把印着 15 个行格的空表填满那次），
挡它的是 `documents.grid` 的**行容量上限**，在 `services/extraction.py` 里施加，
不在这里——那是一个可以逐像素复核的硬事实，比两个模型互相比对更可靠。
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

from src.contracts import (
    DOCUMENT_FIELDS,
    LINE_FIELDS,
    FieldEvidence,
    FieldStatus,
    FIELD_SUPPLIER,
    FIELD_SUPPLIER_ENGLISH,
    FIELD_SUPPLIER_CHINESE,
    RecognitionResult,
    RecognizedField,
    RecognizedLine,
    RecognizedPage,
)
from src.normalization import normalize_number_text

logger = logging.getLogger(__name__)

CROSSCHECK_PROVIDER = "crosscheck"

AGREEMENT_NOTE = "多模型一致"
"""一致字段的标记。会一路传到 `invoice_lines.raw_fields`，于是审核界面能把
「两个模型都这么读」与「人已经确认」在显示上分开。"""

# 哪些字段按数值比，哪些按文本比。
# 数值按**规范化之后**比：`1,040.00` 与 `1040.00` 是同一个数，当成分歧会白白
# 把一个本来可以预填的值推给人填。
_NUMERIC_FIELDS = frozenset({"quantity", "unit_price", "line_amount", "invoice_total"})


@dataclass(frozen=True)
class FieldVerdict:
    """一个字段的裁决结果。"""

    field: RecognizedField
    agreed: bool
    values: tuple[str | None, ...] = field(default_factory=tuple)
    """各模型给出的原文，按模型顺序。分歧时审核界面可以把两种读法都摆出来——
    让人**看图判断哪个对**，而不是让他在两个数字之间二选一。"""

    note: str | None = None


def _comparable(name: str, value: str | None) -> str | None:
    """把一个原文变成可比较的形式。比不出来就返回原文的归一化文本。"""
    if value is None:
        return None
    if name in {"supplier", "supplier_english", "supplier_chinese"}:
        # OCR often appends a full stop to a printed company heading. Ignore
        # boundary punctuation for agreement only; raw_text stays untouched.
        return _text_key(value.strip(" \t\r\n.,，。:：;；"))
    if name in _NUMERIC_FIELDS:
        normalized = normalize_number_text(value)
        if normalized.ok and normalized.text is not None:
            # 去掉末尾多余的零：`6.00` 与 `6` 是同一个数。
            try:
                from decimal import Decimal

                return format(Decimal(normalized.text).normalize(), "f")
            except Exception:  # noqa: BLE001  (比不出来就退回文本比较)
                return normalized.text
        return _text_key(value)
    return _text_key(value)


def _text_key(value: str) -> str:
    """文本比较键：NFKC 折宽、去掉所有空白、折大小写。

    空白差异不算分歧——`Beef  Short Ribs` 与 `Beef Short Ribs` 是同一个商品名。
    """
    folded = unicodedata.normalize("NFKC", value)
    return "".join(folded.split()).casefold()


def judge(name: str, fields: Sequence[RecognizedField | None]) -> FieldVerdict:
    """裁决一个字段。

    规则（保守方向一致：**拿不准就要人填**）：

    | 各模型的状态 | 结果 |
    | --- | --- |
    | 都识别出来且值相同 | `RECOGNIZED`，标注「多模型一致」 |
    | 都识别出来但值不同 | `UNREADABLE`，两种读法都记下来 |
    | 一个识别出来、另一个说认不出 / 没有 | `UNREADABLE`（这也是分歧） |
    | 都说单据上没有 | `MISSING` |
    | 都说认不出 | `UNREADABLE` |
    """
    present = [item for item in fields if item is not None]
    if not present:
        return FieldVerdict(RecognizedField.missing(name), agreed=False, note="没有任何模型报出这个字段")

    values = tuple(item.raw_text for item in present)
    page = next((item.page for item in present if item.page), None)
    evidence = FieldEvidence(page=page) if page else None

    statuses = {item.status for item in present}
    if statuses == {FieldStatus.MISSING}:
        return FieldVerdict(RecognizedField.missing(name), agreed=True, values=values)
    if statuses == {FieldStatus.UNREADABLE}:
        return FieldVerdict(
            RecognizedField.unreadable(name, evidence=evidence),
            agreed=True,
            values=values,
            note="所有模型都说这一格认不出",
        )
    if statuses != {FieldStatus.RECOGNIZED}:
        readable = [item.raw_text for item in present if item.is_recognized]
        return FieldVerdict(
            RecognizedField.unreadable(name, evidence=evidence),
            agreed=False,
            values=values,
            note=(
                "有模型读出了值、有模型说读不出——这也是分歧，按需要人填处理。"
                f"读出来的是：{readable}"
            ),
        )

    keys = {_comparable(name, item.raw_text) for item in present}
    if len(keys) == 1:
        return FieldVerdict(
            RecognizedField.recognized(
                name, present[0].raw_text or "", evidence=evidence
            ),
            agreed=True,
            values=values,
            note=AGREEMENT_NOTE,
        )
    return FieldVerdict(
        RecognizedField.unreadable(name, evidence=evidence),
        agreed=False,
        values=values,
        note=f"模型之间读法不一致：{list(values)}——必须人填",
    )


@dataclass(frozen=True)
class CrossCheckOutcome:
    """一次交叉裁决的完整产出。"""

    result: RecognitionResult
    agreements: dict[str, FieldVerdict] = field(default_factory=dict)
    """单据级字段的裁决，键是字段名。"""

    line_agreements: tuple[dict[str, FieldVerdict], ...] = field(default_factory=tuple)
    """逐行字段的裁决，顺序与 `result.lines` 一致。"""

    @property
    def disagreement_count(self) -> int:
        return sum(
            1
            for group in (self.agreements, *self.line_agreements)
            for verdict in group.values()
            if not verdict.agreed
        )


def combine(results: Sequence[RecognitionResult], *, models: Sequence[str]) -> CrossCheckOutcome:
    """把多个模型的结果合成一个，逐字段裁决。

    `results` 与 `models` 一一对应，顺序就是配置里的顺序（第一个是主模型）。
    """
    if not results:
        raise ValueError("没有任何模型结果可以合并")

    warnings: list[str] = []
    for model, result in zip(models, results):
        for item in result.warnings:
            warnings.append(f"[{model}] {item}")

    document: dict[str, FieldVerdict] = {}
    for name in DOCUMENT_FIELDS:
        document[name] = judge(name, [result.document_field(name) for result in results])
    # A shared display name is insufficient when the models disagree on whether
    # another paper spelling is present. Keep all readings, request human review.
    name_fields = (FIELD_SUPPLIER_ENGLISH, FIELD_SUPPLIER_CHINESE)
    has_name_readings = any(
        result.document_field(name) is not None
        for result in results for name in name_fields
    )
    if has_name_readings and not all(document[name].agreed for name in name_fields):
        chosen = document[FIELD_SUPPLIER]
        document[FIELD_SUPPLIER] = FieldVerdict(
            RecognizedField.unreadable(FIELD_SUPPLIER, evidence=chosen.field.evidence),
            agreed=False,
            values=chosen.values,
            note="模型对纸面英文或中文商家名有分歧，来源须人工确认",
        )

    counts = [len(result.lines) for result in results]
    line_total = max(counts)
    if len(set(counts)) > 1:
        warnings.append(
            "各模型报出的行数不一致："
            + "、".join(f"{model} {count} 行" for model, count in zip(models, counts))
            + f"。取较多的 {line_total} 行，缺的那些行一律置为需人填——"
            "**漏行比多一行空行危险得多**（手写单常常连总额都没有，对账抓不到漏行）。"
        )

    lines: list[RecognizedLine] = []
    line_verdicts: list[dict[str, FieldVerdict]] = []
    for index in range(1, line_total + 1):
        per_model = [result.line(index) for result in results]
        reported_by = [
            model for model, line in zip(models, per_model) if line is not None
        ]
        verdicts: dict[str, FieldVerdict] = {}
        for name in LINE_FIELDS:
            verdicts[name] = judge(
                name,
                [None if line is None else line.get(name) for line in per_model],
            )
        if len(reported_by) < len(results):
            warnings.append(
                f"第 {index} 行只有 {'、'.join(reported_by)} 报出来，"
                "其它模型没有这一行——该行全部字段需人填。"
            )
        page = next((line.page for line in per_model if line and line.page), None)
        raw_text = next((line.raw_text for line in per_model if line and line.raw_text), None)
        lines.append(
            RecognizedLine(
                index=index,
                page=page,
                raw_text=raw_text,
                fields=[verdict.field for verdict in verdicts.values()],
            )
        )
        line_verdicts.append(verdicts)

    pages = _merge_pages(results)
    combined = RecognitionResult(
        provider=CROSSCHECK_PROVIDER,
        is_fake=False,
        model="+".join(models),
        recognized_at=datetime.now(timezone.utc),
        pages=pages,
        document_fields={name: verdict.field for name, verdict in document.items()},
        lines=tuple(lines),
        # 各模型的原始输出全部留着：结果可疑时要能回看模型到底返回了什么。
        raw_response=_merge_raw(results, models),
        warnings=tuple(warnings),
        source_key=next((result.source_key for result in results if result.source_key), None),
    )
    logger.info(
        "crosscheck combined models=%s lines=%s disagreements=%s",
        list(models),
        len(lines),
        sum(1 for group in (document, *line_verdicts) for v in group.values() if not v.agreed),
    )
    return CrossCheckOutcome(
        result=combined,
        agreements=document,
        line_agreements=tuple(line_verdicts),
    )


def _merge_pages(results: Sequence[RecognitionResult]) -> tuple[RecognizedPage, ...]:
    merged: dict[int, RecognizedPage] = {}
    for result in results:
        for page in result.pages:
            existing = merged.get(page.number)
            if existing is None or (existing.width is None and page.width is not None):
                merged[page.number] = page
    return tuple(merged[number] for number in sorted(merged))


def _merge_raw(results: Sequence[RecognitionResult], models: Sequence[str]) -> str:
    import json

    return json.dumps(
        {
            model: result.raw_response
            for model, result in zip(models, results)
        },
        ensure_ascii=False,
    )


__all__ = [
    "AGREEMENT_NOTE",
    "CROSSCHECK_PROVIDER",
    "CrossCheckOutcome",
    "FieldVerdict",
    "combine",
    "judge",
]
