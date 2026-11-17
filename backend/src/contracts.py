"""与厂商无关的内部类型：识别请求与识别结果。

这组类型是 OCR / Vision 适配器与业务层之间的唯一接口。适配器负责把各家厂商的
响应翻译成这里的结构，业务层只认这里的结构，于是换厂商不需要改业务代码
（步骤 07 评估候选、步骤 08 接入真实服务时就按这份契约写适配器）。

契约要守住的几件事：

1. **保留原文。** 每个字段都带 `raw_text`——识别到的字符串原样保留，不做清洗、
   不做格式化。人工复核时对照的是原文，而不是程序理解后的版本。
2. **保留页码。** 多页单据必须能说清某一行来自第几页，否则无法回到原件核对。
3. **保留可获得的区域证据。** 厂商给了坐标就记下来（`FieldEvidence.box`），
   复核界面可以高亮原图对应位置；**没给就是 None，不编造坐标**。
4. **不把「没认出来」变成数值。** 字段状态分开表示：`RECOGNIZED` / `UNREADABLE`
   / `MISSING`。未识别的数量和金额不会变成 0——0 是一个具体的业务含义，
   "没认出来" 不是（见 docs/imp_notes.md 4.1、4.6）。
5. **假数据必须自报。** `is_fake` 为真表示结果来自 Fake 识别器，不是真实识别。
   契约层强制校验这一点，业务层与接口层因此可以可靠地拒绝把假数据当真。

本契约**不做数值解析**：`quantity`、`unit_price`、`line_amount` 在这里都还是原文。
把原文变成 Decimal 属于步骤 04（采购记录的精确数值），校验「数量 × 单价 = 行金额」
属于步骤 05（行级校验），判断单位能否换算属于步骤 09（换算）。识别层多猜一步，
错误就会被当成事实存下来。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Sequence

FAKE_PROVIDER = "fake"
"""Fake 识别器的 provider 标识。契约层据此强制 is_fake=True；
src/integrations/ocr.py 从这里导入，保证两边永远是同一个值。"""

# 行内字段的标准名。与 docs/imp_notes.md 4.5 的结构化提取字段一致；
# 厂商自己的字段名由适配器映射到这些名字上。
FIELD_ITEM_DESCRIPTION = "item_description"
FIELD_QUANTITY = "quantity"
FIELD_UNIT = "unit"
FIELD_UNIT_PRICE = "unit_price"
FIELD_LINE_AMOUNT = "line_amount"

LINE_FIELDS: tuple[str, ...] = (
    FIELD_ITEM_DESCRIPTION,
    FIELD_QUANTITY,
    FIELD_UNIT,
    FIELD_UNIT_PRICE,
    FIELD_LINE_AMOUNT,
)

# 单据级字段的标准名。
FIELD_SUPPLIER = "supplier"
FIELD_INVOICE_DATE = "invoice_date"
FIELD_INVOICE_NUMBER = "invoice_number"
FIELD_INVOICE_TOTAL = "invoice_total"
FIELD_CURRENCY = "currency"

DOCUMENT_FIELDS: tuple[str, ...] = (
    FIELD_SUPPLIER,
    FIELD_INVOICE_DATE,
    FIELD_INVOICE_NUMBER,
    FIELD_INVOICE_TOTAL,
    FIELD_CURRENCY,
)


class ContractError(ValueError):
    """契约本身被违反：构造出的识别结果不满足上述约定。

    这是程序错误（适配器写错了），不是用户输入错误，因此不映射成 HTTP 400。
    """


class FieldStatus(StrEnum):
    """字段的识别状态。"""

    RECOGNIZED = "recognized"
    """识别出了内容。`raw_text` 一定非空，内容是否正确仍待人工确认。"""

    UNREADABLE = "unreadable"
    """这个位置有内容但认不出来（字迹模糊、涂改、反光）。必须人工补。"""

    MISSING = "missing"
    """单据上没有这个字段（例如手写采购单没写单价）。"""


class CoordinateSpace(StrEnum):
    """区域坐标的解释方式。厂商返回什么就记什么，不做换算。"""

    RATIO = "ratio"
    """相对页面宽高的比例，取值 0—1。与页面像素尺寸无关。"""

    PIXEL = "pixel"
    """页面像素坐标。需要配合 `RecognizedPage` 的宽高才能定位。"""


@dataclass(frozen=True, slots=True)
class BoundingBox:
    """字段在页面上的矩形区域。

    坐标系由 `space` 指明。无法从厂商响应里得到区域时，不要构造一个猜测的框——
    把 `FieldEvidence.box` 留成 None。
    """

    left: float
    top: float
    width: float
    height: float
    space: CoordinateSpace = CoordinateSpace.RATIO

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ContractError(
                f"区域宽高必须为正：width={self.width}, height={self.height}"
            )
        if self.left < 0 or self.top < 0:
            raise ContractError(f"区域坐标不能为负：left={self.left}, top={self.top}")
        if self.space is CoordinateSpace.RATIO:
            if self.left + self.width > 1.0000001 or self.top + self.height > 1.0000001:
                raise ContractError(
                    "比例坐标超出页面范围："
                    f"left+width={self.left + self.width}, top+height={self.top + self.height}"
                )


@dataclass(frozen=True, slots=True)
class FieldEvidence:
    """一个字段的来源证据：在第几页、页面上的哪块区域。

    page 从 1 开始，与人翻单据时说的「第几页」一致。
    box 为 None 表示厂商没有给出区域，不表示区域在原点。
    """

    page: int
    box: BoundingBox | None = None

    def __post_init__(self) -> None:
        if self.page < 1:
            raise ContractError(f"页码从 1 开始，收到 {self.page}")


@dataclass(frozen=True, slots=True)
class RecognizedField:
    """一个字段的识别结果。"""

    name: str
    """标准字段名，取自本模块的 FIELD_* 常量。"""

    status: FieldStatus
    raw_text: str | None = None
    """识别到的原文，原样保留。UNREADABLE / MISSING 时为 None。"""

    confidence: float | None = None
    """厂商自报的置信度（0—1）。可能没有；**不作为唯一判据**——
    是否需要人工确认由业务层结合计算校验与历史数据判断（见 imp_notes 4.6）。"""

    evidence: FieldEvidence | None = None
    """页码与区域证据。厂商没给就是 None。"""

    def __post_init__(self) -> None:
        if not self.name:
            raise ContractError("字段缺少名字")
        if self.status is FieldStatus.RECOGNIZED:
            if self.raw_text is None or not self.raw_text.strip():
                raise ContractError(
                    f"字段 {self.name} 标为已识别，却没有原文；"
                    "认不出来请用 UNREADABLE，单据上没有请用 MISSING"
                )
        elif self.raw_text is not None:
            raise ContractError(
                f"字段 {self.name} 状态为 {self.status}，不应带原文 {self.raw_text!r}"
            )
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ContractError(f"字段 {self.name} 的置信度超出 0—1：{self.confidence}")

    @property
    def is_recognized(self) -> bool:
        return self.status is FieldStatus.RECOGNIZED

    @property
    def needs_human_input(self) -> bool:
        """认不出来的字段必须由人补；缺失字段由业务层决定是否必填。"""
        return self.status is FieldStatus.UNREADABLE

    @property
    def page(self) -> int | None:
        return self.evidence.page if self.evidence else None

    @property
    def box(self) -> BoundingBox | None:
        return self.evidence.box if self.evidence else None

    @classmethod
    def recognized(
        cls,
        name: str,
        raw_text: str,
        *,
        confidence: float | None = None,
        evidence: FieldEvidence | None = None,
    ) -> "RecognizedField":
        return cls(
            name=name,
            status=FieldStatus.RECOGNIZED,
            raw_text=raw_text,
            confidence=confidence,
            evidence=evidence,
        )

    @classmethod
    def unreadable(
        cls,
        name: str,
        *,
        evidence: FieldEvidence | None = None,
        confidence: float | None = None,
    ) -> "RecognizedField":
        """这个位置有东西但认不出来。注意：不返回 0，也不返回空字符串。"""
        return cls(
            name=name,
            status=FieldStatus.UNREADABLE,
            confidence=confidence,
            evidence=evidence,
        )

    @classmethod
    def missing(cls, name: str) -> "RecognizedField":
        """单据上没有这个字段。"""
        return cls(name=name, status=FieldStatus.MISSING)


def _freeze_fields(
    fields: Mapping[str, RecognizedField] | Sequence[RecognizedField] | None,
) -> Mapping[str, RecognizedField]:
    """把字段集合统一成只读映射，并检查名字与键一致。"""
    if fields is None:
        return MappingProxyType({})
    if isinstance(fields, Mapping):
        for key, value in fields.items():
            if key != value.name:
                raise ContractError(f"字段键 {key!r} 与字段名 {value.name!r} 不一致")
        return MappingProxyType(dict(fields))
    collected: dict[str, RecognizedField] = {}
    for item in fields:
        if item.name in collected:
            raise ContractError(f"字段 {item.name} 重复")
        collected[item.name] = item
    return MappingProxyType(collected)


@dataclass(frozen=True)
class RecognizedLine:
    """单据上的一行（一个商品）。"""

    index: int
    """行号，从 1 开始，按单据上的顺序。"""

    fields: Mapping[str, RecognizedField] = field(default_factory=dict)
    raw_text: str | None = None
    """整行原文，保留换行与分隔符原样。"""

    page: int | None = None
    """该行所在页码；跨页行取起始页。未知为 None。"""

    def __post_init__(self) -> None:
        if self.index < 1:
            raise ContractError(f"行号从 1 开始，收到 {self.index}")
        if self.page is not None and self.page < 1:
            raise ContractError(f"页码从 1 开始，收到 {self.page}")
        object.__setattr__(self, "fields", _freeze_fields(self.fields))

    def get(self, name: str) -> RecognizedField | None:
        return self.fields.get(name)

    @property
    def unreadable_fields(self) -> tuple[str, ...]:
        """认不出来的字段名。业务层据此决定这一行能否确认。"""
        return tuple(
            name for name, item in self.fields.items() if item.needs_human_input
        )


@dataclass(frozen=True, slots=True)
class RecognizedPage:
    """一页的识别结果。"""

    number: int
    text: str | None = None
    """整页原文。"""

    width: int | None = None
    height: int | None = None
    """页面像素尺寸。PIXEL 坐标需要它才能定位；厂商没给就是 None。"""

    def __post_init__(self) -> None:
        if self.number < 1:
            raise ContractError(f"页码从 1 开始，收到 {self.number}")
        for name, value in (("width", self.width), ("height", self.height)):
            if value is not None and value <= 0:
                raise ContractError(f"页面 {name} 必须为正，收到 {value}")


@dataclass(frozen=True, slots=True)
class RecognitionRequest:
    """一次识别请求。

    直接带文件字节，不带存储实现：适配器因此不需要知道文件存在本地还是对象存储。
    `source_key` 只用于日志与结果追溯，不参与读取。
    """

    content: bytes
    content_type: str | None = None
    original_name: str | None = None
    source_key: str | None = None
    """文件在存储中的引用（LocalFileStorage 的 key），用于把结果关联回原始单据。"""

    pages: tuple[int, ...] | None = None
    """只识别指定页（从 1 开始）；None 表示全部页。"""

    def __post_init__(self) -> None:
        if not self.content:
            raise ContractError("识别请求没有文件内容")
        if self.pages is not None:
            if not self.pages:
                raise ContractError("指定了 pages 却是空的；要识别全部页请用 None")
            if any(number < 1 for number in self.pages):
                raise ContractError(f"页码从 1 开始，收到 {self.pages}")

    @property
    def size(self) -> int:
        return len(self.content)


@dataclass(frozen=True)
class RecognitionResult:
    """一次识别的完整结果。"""

    provider: str
    """适配器标识，如 "fake"。真实厂商在步骤 08 接入后使用各自的标识。"""

    is_fake: bool
    """True 表示这是 Fake 识别器造的数据，不是真实识别结果。"""

    recognized_at: datetime
    model: str | None = None
    """厂商模型标识，便于日后复查「这批结果是哪个模型出的」。"""

    pages: tuple[RecognizedPage, ...] = ()
    document_fields: Mapping[str, RecognizedField] = field(default_factory=dict)
    """单据级字段：供应商、日期、单号、总额、币种。"""

    lines: tuple[RecognizedLine, ...] = ()
    raw_response: str | None = None
    """厂商原始响应（序列化后）。保留它才能在结果可疑时回看模型到底返回了什么。"""

    warnings: tuple[str, ...] = ()
    """识别过程中的提示：页面质量差、部分页未处理、结果为假数据等。"""

    source_key: str | None = None
    """被识别文件在存储中的引用，与 RecognitionRequest.source_key 对应。"""

    def __post_init__(self) -> None:
        if not self.provider:
            raise ContractError("识别结果缺少 provider 标识")
        if self.provider == FAKE_PROVIDER and not self.is_fake:
            raise ContractError(
                f"provider={FAKE_PROVIDER!r} 的结果必须标记 is_fake=True，不得冒充真实识别"
            )
        object.__setattr__(self, "document_fields", _freeze_fields(self.document_fields))
        numbers = [page.number for page in self.pages]
        if len(set(numbers)) != len(numbers):
            raise ContractError(f"页码重复：{numbers}")
        indexes = [line.index for line in self.lines]
        if len(set(indexes)) != len(indexes):
            raise ContractError(f"行号重复：{indexes}")
        known_pages = set(numbers)
        for line in self.lines:
            if line.page is not None and known_pages and line.page not in known_pages:
                raise ContractError(f"行 {line.index} 指向不存在的页 {line.page}")

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def document_field(self, name: str) -> RecognizedField | None:
        return self.document_fields.get(name)

    def line(self, index: int) -> RecognizedLine | None:
        for item in self.lines:
            if item.index == index:
                return item
        return None

    @property
    def has_unreadable_fields(self) -> bool:
        return any(line.unreadable_fields for line in self.lines) or any(
            item.needs_human_input for item in self.document_fields.values()
        )

    def summary(self) -> dict[str, Any]:
        """可写进日志的摘要：只有计数与标识，不含识别到的业务内容。"""
        return {
            "provider": self.provider,
            "is_fake": self.is_fake,
            "model": self.model,
            "pages": self.page_count,
            "lines": len(self.lines),
            "unreadable": self.has_unreadable_fields,
            "source_key": self.source_key,
        }


__all__ = [
    "BoundingBox",
    "ContractError",
    "CoordinateSpace",
    "DOCUMENT_FIELDS",
    "FAKE_PROVIDER",
    "FieldEvidence",
    "FieldStatus",
    "LINE_FIELDS",
    "RecognitionRequest",
    "RecognitionResult",
    "RecognizedField",
    "RecognizedLine",
    "RecognizedPage",
    "FIELD_CURRENCY",
    "FIELD_INVOICE_DATE",
    "FIELD_INVOICE_NUMBER",
    "FIELD_INVOICE_TOTAL",
    "FIELD_ITEM_DESCRIPTION",
    "FIELD_LINE_AMOUNT",
    "FIELD_QUANTITY",
    "FIELD_SUPPLIER",
    "FIELD_UNIT",
    "FIELD_UNIT_PRICE",
]
