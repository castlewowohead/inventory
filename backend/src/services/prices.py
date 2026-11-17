"""标准化价格、月度加权均价、来源比价与涨价提醒（步骤 09）。

职责与边界：

- **事务边界在本层**，与其它 Service 一致；ORM 对象不流出，返回下面的只读视图。
- **换算只调 `services/units.py`**，不在这里另写系数；哪条箱规适用于哪一行是本层
  的业务判断（`repositories/prices.py::resolve_from_index` 定顺序）。
- **只读。** 本模块不写采购记录——`purchases` 的数量、单位与金额是人工复核过的
  事实（`docs/stru_dev.md` 09.5）。唯一会写的是箱规与提醒设置这两张配置表。

### 七条口径（步骤 09 定下，响应里都能看出来）

1. **标准化价格是派生值，不落列、查询时算。** 箱规一改、采购记录一改，价格立刻
   跟着对；落一列回填就会出现"列里存着旧值，而它看起来仍然可信"。代价是统计要
   把窗口内的记录取回内存算——一家餐厅一个月几百条，这个量级完全够用，而且
   `STATISTICS_ROW_LIMIT` 触顶时**报错而不是算一个少了尾巴的均价**。
2. **只取 `line_kind='purchase'` 且未作废的记录。** 退货 / 折让 / 运费是调整行，
   混进加权均价会让价格**悄悄偏低**。这条由 `PurchaseRepository.for_statistics()`
   在 SQL 里保证，本层不再放宽。因此标准化价格是**商品行价格，不含运费与整单
   折让**——这句话随每个统计响应一起返回（`notes`），不能让人读成到手总成本。
3. **换不出来的记录不消失。** 每一行都有换算状态（`available` /
   `pending_pack_spec` / `unit_unknown` / `incompatible_unit`），每个统计响应都带
   一份「有几条没算进来、分别为什么」。`docs/imp_notes.md` 要求「待确认、不可换算
   和不可比数据不会被静默混算」，做法就是把它们**数出来摆在同一个响应里**。
4. **不同币种不相加。** 本轮不做汇率换算，所以同一食材既有 USD 又有 CNY 时，
   接口要求显式指定币种而不是挑一个——两种货币相加得到的数没有意义却长得像均价。
5. **本月与上月比同样的天数。** 本月没过完就拿「本月截至今天」比「上月同样天数」
   （`imp_notes.md` Feature 2 明确要求），期间口径随响应返回（`basis` 与 `days`）。
6. **缺基期不显示 0%。** 基期没有可换算的采购 → `change_percent` 是 `null` 并带
   原因；基期均价恰好是 0（全是赠品）→ 同样是 `null`，原因不同。把"没有基期"
   显示成"0% 变化"是在报告一个未发生的事实。
7. **0 元赠品行计入、金额异常行计入，但都数出来。** 0 元的赠品确实压低了到手
   成本，那是事实；而「均价怎么突然降了一半」必须解释得清，所以响应里单独报
   `zero_amount_count` 与 `amount_mismatch_count`。

### 本步骤刻意没做

- **汇率换算**：没有需求，也不想把一个会过期的汇率变成历史价格的一部分。
- **按生效日期分段的箱规**：同一家中途换包装的情况没有实测样本，先不造复杂度。
- **按食材的提醒阈值**：本轮只要求阈值可调可关，见 `models.PriceAlertSettings`。
- **跌价提醒**：老板要的是"别悄悄涨价"。
"""

from __future__ import annotations

import logging
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any, Final, Iterable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.db import Database
from src.errors import ConflictError, NotFoundError, ValidationError
from src.models import (
    DEFAULT_ALERT_THRESHOLD_PERCENT,
    PACK_SIZE_SCALE,
    PACK_SPEC_NOTE_MAX_LENGTH,
    PACK_SPEC_TEXT_MAX_LENGTH,
    PRICE_ALERT_SETTINGS_ID,
    QUANTITY_SCALE,
    SOURCE_NAME_MAX_LENGTH,
    THRESHOLD_PERCENT_SCALE,
    UNIT_PRICE_SCALE,
    PackSpec,
    PriceAlertSettings,
    Purchase,
)
from src.repositories.ingredients import FoodItemRepository
from src.repositories.prices import (
    PackSpecRepository,
    PriceAlertSettingsRepository,
    index_for_lookup,
    resolve_from_index,
)
from src.repositories.purchases import PurchaseRepository
from src.services.ingredients import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    FoodItemView,
    normalize_name,
)
from src.services.purchases import (
    CURRENCIES,
    EARLIEST_PURCHASE_YEAR,
    PurchaseView,
    format_decimal,
    parse_decimal,
)
from src.services.units import (
    CALCULATION_PRECISION,
    DIMENSIONS,
    PACKAGING_UNIT_TOKENS,
    UNIT_FACTORS,
    VOLUME_SYSTEM,
    UnitError,
    canonical_packaging_unit,
    canonical_unit,
    convert,
    dimension_of,
    find_pack_spec_in_description,
    is_packaging_unit,
    require_measurement_unit,
    units_of_dimension,
)
from src.services.validation import (
    optional_text,
    require_limit,
    require_offset,
)

logger = logging.getLogger(__name__)

# --- 口径常量 ---------------------------------------------------------------

# 标准化单价的小数位数：与步骤 04 的单价列同为 4 位。按 $/g 计价时 2 位不够
# （$0.0052/g 会被舍成 $0.01/g，差 92%）。
NORMALIZED_PRICE_SCALE: Final[int] = UNIT_PRICE_SCALE
# 百分比 2 位：`10.00`% 就是阈值那一列的精度。
PERCENT_SCALE: Final[int] = THRESHOLD_PERCENT_SCALE

# 一次统计最多取多少条采购记录。**触顶是报错，不是截断**：少了尾巴的均价看起来
# 完全正常，没人会发现。两万条对一家餐厅是几年的量。
STATISTICS_ROW_LIMIT: Final[int] = 20_000

# 月度趋势默认与上限的月份数。
DEFAULT_MONTHS: Final[int] = 6
MAX_MONTHS: Final[int] = 36

# 换算状态。`available` 之外的三种都意味着这一行**不参与**任何均价。
CONVERSION_AVAILABLE: Final[str] = "available"
CONVERSION_PENDING_PACK_SPEC: Final[str] = "pending_pack_spec"
CONVERSION_UNIT_UNKNOWN: Final[str] = "unit_unknown"
CONVERSION_INCOMPATIBLE: Final[str] = "incompatible_unit"
CONVERSION_STATUSES: Final[tuple[str, ...]] = (
    CONVERSION_AVAILABLE,
    CONVERSION_PENDING_PACK_SPEC,
    CONVERSION_UNIT_UNKNOWN,
    CONVERSION_INCOMPATIBLE,
)

# 期间口径。
BASIS_FULL_MONTH: Final[str] = "full_month"
BASIS_MONTH_TO_DATE: Final[str] = "month_to_date"

# 比较结果的状态。**没有 `zero_change` 这一项**：没有基期就是没有基期，
# 不是"没变化"（09.11）。
COMPARISON_AVAILABLE: Final[str] = "available"
COMPARISON_NO_BASE_DATA: Final[str] = "no_base_data"
COMPARISON_NO_CURRENT_DATA: Final[str] = "no_current_data"
COMPARISON_BASE_PRICE_IS_ZERO: Final[str] = "base_price_is_zero"

# 箱规的来源：精确命中这家供应商，还是退到「不限来源」那条。
PACK_SPEC_MATCH_EXACT: Final[str] = "exact_source"
PACK_SPEC_MATCH_ANY_SOURCE: Final[str] = "any_source"

# 随统计响应一起返回的固定说明。**不是装饰**：步骤 09 的交付说明里写明「不要把
# 『发票能录进去』说成『价格分析可用』」，这里是同一条——不要把"商品行均价"
# 读成"到手总成本"。
NOTE_EXCLUDES_ADJUSTMENTS: Final[str] = (
    "标准化价格只含商品行（line_kind=purchase）且未作废的记录："
    "退货、折让、运费与税费是调整行，不参与均价，对账请用 /api/purchases?kind=all。"
)
NOTE_NO_FX: Final[str] = (
    "不同币种分开统计，本轮不做汇率换算。"
)


# --- 期间 -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Period:
    """一个闭区间的统计期间（含首尾两天）。"""

    key: str
    date_from: date
    date_to: date
    basis: str

    @property
    def days(self) -> int:
        return (self.date_to - self.date_from).days + 1

    def contains(self, day: date) -> bool:
        return self.date_from <= day <= self.date_to


def month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def month_start(day: date) -> date:
    return day.replace(day=1)


def month_end(day: date) -> date:
    return day.replace(day=monthrange(day.year, day.month)[1])


def previous_month_start(day: date) -> date:
    first = month_start(day)
    if first.month == 1:
        return first.replace(year=first.year - 1, month=12)
    return first.replace(month=first.month - 1)


def month_period(day: date, *, today: date) -> Period:
    """`day` 所在的那个月作为一个统计期间。

    当月未过完时只到 `today`，并标成 `month_to_date`——拿 13 天的本月去跟 31 天的
    上月比，均价差异里有一部分只是"天数不一样"。标出来，前端才不会把它画成
    一个可直接比较的点。
    """
    start = month_start(day)
    end = month_end(day)
    if start <= today <= end:
        if today >= end:
            return Period(month_key(day), start, end, BASIS_FULL_MONTH)
        return Period(month_key(day), start, today, BASIS_MONTH_TO_DATE)
    return Period(month_key(day), start, end, BASIS_FULL_MONTH)


def comparable_periods(today: date) -> tuple[Period, Period]:
    """返回（本期, 基期）。

    `imp_notes.md` Feature 2 的要求照抄：完整月份之间可以直接比较；本月没过完时
    用「本月截至今天」与「上月相同天数」比较，**避免用不完整月份与完整月份直接
    比较**。

    上月天数不够时（1/31 的上月是 2 月）取上月最后一天，于是基期可能比本期短一天
    两天。这一点不藏着：两个期间的 `days` 都在响应里。
    """
    current_start = month_start(today)
    current_end = month_end(today)
    if today >= current_end:
        current = Period(month_key(today), current_start, current_end, BASIS_FULL_MONTH)
    else:
        current = Period(
            month_key(today), current_start, today, BASIS_MONTH_TO_DATE
        )

    base_start = previous_month_start(today)
    base_last_day = monthrange(base_start.year, base_start.month)[1]
    base_end_day = min(current.date_to.day, base_last_day)
    base_end = base_start.replace(day=base_end_day)
    base_basis = (
        BASIS_FULL_MONTH if base_end_day == base_last_day else BASIS_MONTH_TO_DATE
    )
    base = Period(month_key(base_start), base_start, base_end, base_basis)
    return current, base


# --- 数值 -------------------------------------------------------------------


def _parse_window_date(value: Any, *, field: str) -> date | None:
    """解析**统计窗口**的端点。`None` / 空字符串表示"这一端不限"。

    ⚠️ **刻意不复用步骤 04 的 `parse_purchase_date`**：那个函数拒绝未来日期，
    理由是「年份容易敲错或被 OCR 读错（2026 → 2062），放过会让月度统计里凭空
    多出一个几十年后的月份」。那条规则管的是**被记录下来的采购日期**。

    查询窗口是另一回事：`date_to=2026-12-31` 这种"查到年底"完全正常，窗口的
    右端本来就可以在今天之后，只是那段时间还没有采购记录。拿录入的规则去卡查询，
    会把一个合理的请求判成 400——本次步骤 09 的真实数据实跑就撞到了这一条。

    仍然保留过早日期的下限：`0026-12-31` 这类明显打错的年份挡住，报错比返回
    一张空表好懂。
    """
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = date.fromisoformat(text)
        except ValueError as exc:
            raise ValidationError(
                f"{field} 必须是 YYYY-MM-DD 形式的日历日（收到 {text!r}）。",
                details={"field": field},
            ) from exc
    else:
        raise ValidationError(
            f"{field} 必须是 YYYY-MM-DD 形式的字符串或 null。",
            details={"field": field},
        )
    if parsed.year < EARLIEST_PURCHASE_YEAR:
        raise ValidationError(
            f"{field} 过早（{parsed.isoformat()}），请确认年份。",
            details={"field": field},
        )
    return parsed


def quantize_price(value: Decimal) -> Decimal:
    """标准化单价的输出舍入：4 位，四舍五入。

    **只在输出这一步舍入。** 加权均价是「总金额 ÷ 总基础数量」，不是把各行的
    标准化单价平均——后者会把每行的舍入误差累积进结果，而且与权重无关。
    """
    return value.quantize(
        Decimal(1).scaleb(-NORMALIZED_PRICE_SCALE), rounding=ROUND_HALF_UP
    )


def quantize_quantity(value: Decimal) -> Decimal:
    return value.quantize(
        Decimal(1).scaleb(-QUANTITY_SCALE), rounding=ROUND_HALF_UP
    )


def quantize_percent(value: Decimal) -> Decimal:
    return value.quantize(
        Decimal(1).scaleb(-PERCENT_SCALE), rounding=ROUND_HALF_UP
    )


def format_price(value: Decimal) -> str:
    """标准化单价的字符串形式。至少 2 位小数，最多 4 位，去掉多余的尾零。"""
    return format_decimal(quantize_price(value), min_scale=2)


def format_percent(value: Decimal) -> str:
    return format_decimal(quantize_percent(value), min_scale=2)


# --- 只读视图 ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PackSpecView:
    """一条箱规的只读快照。"""

    id: int
    food_item: FoodItemView | None
    food_item_id: int
    source_name: str | None
    source_name_normalized: str | None
    purchase_unit: str
    pack_size: Decimal
    pack_unit: str
    spec_text: str | None
    note: str | None
    created_at: Any
    updated_at: Any

    @classmethod
    def of(cls, spec: PackSpec, *, with_food_item: bool = True) -> "PackSpecView":
        return cls(
            id=spec.id,
            food_item=(
                FoodItemView.of(spec.food_item)
                if with_food_item and spec.food_item is not None
                else None
            ),
            food_item_id=spec.food_item_id,
            source_name=spec.source_name,
            source_name_normalized=spec.source_name_normalized,
            purchase_unit=spec.purchase_unit_normalized,
            pack_size=spec.pack_size,
            pack_unit=spec.pack_unit,
            spec_text=spec.spec_text,
            note=spec.note,
            created_at=spec.created_at,
            updated_at=spec.updated_at,
        )


@dataclass(frozen=True, slots=True)
class PackSpecPage:
    items: tuple[PackSpecView, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class ConversionView:
    """一行采购记录换算到基础单位的结果，或者换不出来的原因。

    `status != "available"` 时 `base_quantity` 与 `normalized_unit_price` 都是
    `None`——**不是 0**。0 是一个具体的价格（赠品行就是 0），拿它表示"不知道"
    就会让"没算出来"和"真的免费"变成同一个数（契约层 `FieldStatus` 的三态同理）。
    """

    status: str
    base_unit: str
    base_quantity: Decimal | None
    normalized_unit_price: Decimal | None
    pack_spec: PackSpecView | None
    pack_spec_match: str | None
    reason: str | None

    @property
    def available(self) -> bool:
        return self.status == CONVERSION_AVAILABLE


@dataclass(frozen=True, slots=True)
class ExclusionSummary:
    """这次统计有几条记录没算进来，分别为什么。"""

    undated: int
    unit_unknown: int
    pending_pack_spec: int
    incompatible_unit: int
    other_currency: int

    @property
    def total(self) -> int:
        return (
            self.undated
            + self.unit_unknown
            + self.pending_pack_spec
            + self.incompatible_unit
            + self.other_currency
        )

    @property
    def any(self) -> bool:
        return self.total > 0


EMPTY_EXCLUSIONS: Final[ExclusionSummary] = ExclusionSummary(0, 0, 0, 0, 0)


@dataclass(frozen=True, slots=True)
class PeriodStats:
    """一个期间的加权均价与它的构成。

    `weighted_average_unit_price` 为 `None` 表示这个期间**没有**可换算的采购记录，
    与"均价是 0"不同（后者是真的全部免费）。
    """

    period: Period
    weighted_average_unit_price: Decimal | None
    total_amount: Decimal
    total_base_quantity: Decimal
    purchase_count: int
    zero_amount_count: int
    amount_mismatch_count: int
    purchase_ids: tuple[int, ...]

    @property
    def has_data(self) -> bool:
        return self.purchase_count > 0


@dataclass(frozen=True, slots=True)
class ComparisonView:
    """本期与基期的比较。"""

    current: PeriodStats
    base: PeriodStats
    change_amount: Decimal | None
    change_percent: Decimal | None
    status: str
    reason: str | None


@dataclass(frozen=True, slots=True)
class MonthlyPriceReport:
    food_item: FoodItemView
    currency: str
    base_unit: str
    months: tuple[PeriodStats, ...]
    comparison: ComparisonView
    excluded: ExclusionSummary
    available_currencies: tuple[str, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PriceHistoryRow:
    purchase: PurchaseView
    conversion: ConversionView


@dataclass(frozen=True, slots=True)
class PriceHistoryPage:
    food_item: FoodItemView
    currency: str | None
    base_unit: str
    items: tuple[PriceHistoryRow, ...]
    total: int
    limit: int
    offset: int
    convertible_count: int
    excluded: ExclusionSummary
    available_currencies: tuple[str, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceStats:
    """一个采购来源在窗口内的加权均价。"""

    source_name: str | None
    source_name_normalized: str | None
    weighted_average_unit_price: Decimal
    total_amount: Decimal
    total_base_quantity: Decimal
    purchase_count: int
    first_purchase_date: date
    latest_purchase_date: date
    descriptions: tuple[str, ...]
    pack_spec: PackSpecView | None


@dataclass(frozen=True, slots=True)
class SourceComparisonReport:
    food_item: FoodItemView
    currency: str
    base_unit: str
    period: Period
    sources: tuple[SourceStats, ...]
    cheapest_source_normalized: str | None
    spread_amount: Decimal | None
    spread_percent: Decimal | None
    excluded: ExclusionSummary
    available_currencies: tuple[str, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AlertSettingsView:
    enabled: bool
    threshold_percent: Decimal
    updated_at: Any


@dataclass(frozen=True, slots=True)
class AlertRow:
    food_item: FoodItemView
    currency: str
    base_unit: str
    comparison: ComparisonView


@dataclass(frozen=True, slots=True)
class AlertReport:
    settings: AlertSettingsView
    threshold_percent: Decimal
    current: Period
    base: Period
    alerts: tuple[AlertRow, ...]
    compared_count: int
    skipped: dict[str, int]
    excluded: ExclusionSummary
    notes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PackSpecSuggestion:
    """从采购记录的原始描述里读出来的箱规候选。**必须人确认，不自动生效。**"""

    pack_size: Decimal
    pack_unit: str
    spec_text: str
    from_description: str


@dataclass(frozen=True, slots=True)
class PendingPackSpecRow:
    """一组「要补箱规才能算价格」的采购记录。"""

    food_item: FoodItemView
    source_name: str | None
    source_name_normalized: str | None
    purchase_unit: str
    purchase_count: int
    total_amount: Decimal
    latest_purchase_date: date | None
    descriptions: tuple[str, ...]
    suggestion: PackSpecSuggestion | None


# --- 服务 -------------------------------------------------------------------


class PriceService:
    """标准化价格与价格统计。无状态，可以按请求创建。"""

    def __init__(self, database: Database) -> None:
        self._database = database

    # --- 箱规维护 ---

    def list_pack_specs(
        self, *, food_item_id: Any = None, limit: Any = DEFAULT_LIMIT, offset: Any = 0
    ) -> PackSpecPage:
        clean_limit = require_limit(limit, max_limit=MAX_LIMIT)
        clean_offset = require_offset(offset)
        with self._database.unit_of_work() as session:
            item_id = (
                None
                if food_item_id is None
                else self._require_food_item(session, food_item_id).id
            )
            specs, total = PackSpecRepository(session).list_all(
                food_item_id=item_id, limit=clean_limit, offset=clean_offset
            )
            return PackSpecPage(
                items=tuple(PackSpecView.of(spec) for spec in specs),
                total=total,
                limit=clean_limit,
                offset=clean_offset,
            )

    def save_pack_spec(
        self,
        *,
        food_item_id: Any,
        purchase_unit: Any,
        pack_size: Any,
        pack_unit: Any,
        source_name: Any = None,
        spec_text: Any = None,
        note: Any = None,
    ) -> tuple[PackSpecView, bool, int]:
        """新增或更新一条箱规。返回（视图, 是否新建, 受影响的采购记录数）。

        «新增或更新» 是刻意的：键就是（食材, 来源, 包装单位），同一个键再提交一次
        表达的是"这个箱规我填错了/变了"，而不是"再来一条"。重复键报 409 会让老板
        必须先去找到那条旧的再改——而他看到的现象只是"保存不了"。

        ⚠️ **受影响的采购记录数要返回给调用方**：改箱规会让**历史**标准化价格跟着
        变（口径与步骤 04 的「统计按当前值算」一致）。把"这一下会影响 37 条采购
        记录"摆出来，是让这件事可见的最低成本做法。
        """
        unit_token = self._require_packaging_unit(purchase_unit)
        size = parse_decimal(
            pack_size, field="pack_size", scale=PACK_SIZE_SCALE, allow_zero=False
        )
        inner_unit = require_measurement_unit(pack_unit, field="pack_unit")
        clean_source = optional_text(
            source_name, field="source_name", max_length=SOURCE_NAME_MAX_LENGTH
        )
        clean_spec_text = optional_text(
            spec_text, field="spec_text", max_length=PACK_SPEC_TEXT_MAX_LENGTH
        )
        clean_note = optional_text(
            note, field="note", max_length=PACK_SPEC_NOTE_MAX_LENGTH
        )
        source_normalized = (
            normalize_name(clean_source) if clean_source else None
        )

        with self._database.unit_of_work() as session:
            food_item = self._require_food_item(session, food_item_id)
            # 箱规的内部单位必须能换到食材的基础单位，否则这条箱规存下来也算不出
            # 价格——而那时错误会表现为"价格还是出不来"，查起来要绕一圈。
            self._require_compatible(inner_unit, food_item.base_unit)

            repository = PackSpecRepository(session)
            existing = repository.find_exact(
                food_item_id=food_item.id,
                source_name_normalized=source_normalized,
                purchase_unit_normalized=unit_token,
            )
            created = existing is None
            if existing is None:
                spec = PackSpec(
                    food_item_id=food_item.id,
                    source_name=clean_source,
                    source_name_normalized=source_normalized,
                    purchase_unit_normalized=unit_token,
                    pack_size=size,
                    pack_unit=inner_unit,
                    spec_text=clean_spec_text,
                    note=clean_note,
                )
                try:
                    repository.add(spec)
                except IntegrityError as exc:  # pragma: no cover - 并发兜底
                    raise ConflictError(
                        "这条箱规刚被另一个请求建出来了，请重试。",
                        details={"food_item_id": food_item.id},
                    ) from exc
            else:
                spec = existing
                spec.source_name = clean_source
                spec.pack_size = size
                spec.pack_unit = inner_unit
                spec.spec_text = clean_spec_text
                spec.note = clean_note
                session.flush()

            affected = self._count_affected_purchases(
                session,
                food_item_id=food_item.id,
                source_name_normalized=source_normalized,
                purchase_unit=unit_token,
            )
            logger.info(
                "pack spec saved food_item=%s source=%s unit=%s size=%s %s "
                "created=%s affected_purchases=%s",
                food_item.id,
                source_normalized,
                unit_token,
                size,
                inner_unit,
                created,
                affected,
            )
            return PackSpecView.of(spec), created, affected

    def delete_pack_spec(self, pack_spec_id: int) -> int:
        """删掉一条箱规。返回因此**失去**标准化价格的采购记录数。"""
        with self._database.unit_of_work() as session:
            repository = PackSpecRepository(session)
            spec = repository.get(pack_spec_id)
            if spec is None:
                raise NotFoundError(
                    f"箱规 {pack_spec_id} 不存在。",
                    details={"pack_spec_id": pack_spec_id},
                )
            affected = self._count_affected_purchases(
                session,
                food_item_id=spec.food_item_id,
                source_name_normalized=spec.source_name_normalized,
                purchase_unit=spec.purchase_unit_normalized,
            )
            repository.delete(spec)
            logger.info(
                "pack spec deleted id=%s affected_purchases=%s",
                pack_spec_id,
                affected,
            )
            return affected

    def pending_pack_specs(self, *, limit: Any = DEFAULT_LIMIT) -> tuple[
        tuple[PendingPackSpecRow, ...], int
    ]:
        """「待补箱规」清单：按包装单位计价、但还算不出标准化价格的那些记录。

        返回（当页分组, 分组总数）。按「涉及金额」从大到小排——先补金额大的那组，
        老板的时间花在影响最大的地方。
        """
        clean_limit = require_limit(limit, max_limit=MAX_LIMIT)
        with self._database.unit_of_work() as session:
            rows = PurchaseRepository(session).for_price_review(
                limit=STATISTICS_ROW_LIMIT
            )
            index = self._pack_spec_index(session, rows)
            groups: dict[tuple[int, str | None, str], dict[str, Any]] = {}
            for purchase in rows:
                conversion = self._resolve_conversion(purchase, index)
                if conversion.status != CONVERSION_PENDING_PACK_SPEC:
                    continue
                token = canonical_packaging_unit(purchase.purchase_unit)
                assert token is not None  # pending_pack_spec 的前提
                key = (
                    int(purchase.food_item_id or 0),
                    purchase.source_name_normalized,
                    token,
                )
                group = groups.setdefault(
                    key,
                    {
                        "food_item": purchase.food_item,
                        "source_name": purchase.source_name,
                        "purchase_count": 0,
                        "total_amount": Decimal("0"),
                        "latest_purchase_date": None,
                        "descriptions": [],
                        "suggestion": None,
                    },
                )
                group["purchase_count"] += 1
                group["total_amount"] += abs(purchase.line_amount)
                if purchase.purchase_date is not None:
                    current_latest = group["latest_purchase_date"]
                    if current_latest is None or purchase.purchase_date > current_latest:
                        group["latest_purchase_date"] = purchase.purchase_date
                if (
                    purchase.original_description
                    and purchase.original_description not in group["descriptions"]
                    and len(group["descriptions"]) < 3
                ):
                    group["descriptions"].append(purchase.original_description)
                if group["suggestion"] is None:
                    group["suggestion"] = self._suggest_pack_spec(purchase)

            ordered = sorted(
                groups.items(),
                key=lambda item: (-item[1]["total_amount"], item[0]),
            )
            total = len(ordered)
            page = tuple(
                PendingPackSpecRow(
                    food_item=FoodItemView.of(payload["food_item"]),
                    source_name=payload["source_name"],
                    source_name_normalized=key[1],
                    purchase_unit=key[2],
                    purchase_count=payload["purchase_count"],
                    total_amount=payload["total_amount"],
                    latest_purchase_date=payload["latest_purchase_date"],
                    descriptions=tuple(payload["descriptions"]),
                    suggestion=payload["suggestion"],
                )
                for key, payload in ordered[:clean_limit]
            )
            return page, total

    # --- 价格历史（逐条，含换算状态）---

    def price_history(
        self,
        food_item_id: int,
        *,
        currency: Any = None,
        limit: Any = DEFAULT_LIMIT,
        offset: Any = 0,
    ) -> PriceHistoryPage:
        """一条食材的采购历史，每行带标准化价格或"为什么算不出来"。

        与 `GET /api/purchases` 的区别：那个接口回答「这条记录是什么」，这里回答
        「这条记录折成基础单位是多少钱、能不能进统计」。两个问题的答案形状不同，
        所以没有把换算塞进步骤 04 的响应里（那会改动已经稳定的契约）。

        整体取回再在内存里分页：这样**页上的状态**与**整体的汇总**用的是同一次
        换算，不会出现"汇总说有 3 条缺箱规、翻页却找不到它们"。
        """
        clean_limit = require_limit(limit, max_limit=MAX_LIMIT)
        clean_offset = require_offset(offset)
        with self._database.unit_of_work() as session:
            food_item = self._require_food_item(session, food_item_id)
            rows = PurchaseRepository(session).for_price_review(
                food_item_id=food_item.id, limit=STATISTICS_ROW_LIMIT
            )
            self._guard_row_limit(len(rows))
            available = self._currencies_of(rows)
            selected = self._select_currency(currency, available, required=False)

            index = self._pack_spec_index(session, rows)
            undated = PurchaseRepository(session).count_statistics_candidates(
                food_item_id=food_item.id
            )["undated"]

            items: list[PriceHistoryRow] = []
            counts = {status: 0 for status in CONVERSION_STATUSES}
            other_currency = 0
            for purchase in rows:
                if selected is not None and purchase.currency != selected:
                    other_currency += 1
                    continue
                conversion = self._resolve_conversion(purchase, index)
                counts[conversion.status] += 1
                items.append(
                    PriceHistoryRow(
                        purchase=PurchaseView.of(purchase), conversion=conversion
                    )
                )

            excluded = ExclusionSummary(
                undated=undated,
                unit_unknown=counts[CONVERSION_UNIT_UNKNOWN],
                pending_pack_spec=counts[CONVERSION_PENDING_PACK_SPEC],
                incompatible_unit=counts[CONVERSION_INCOMPATIBLE],
                other_currency=other_currency,
            )
            return PriceHistoryPage(
                food_item=FoodItemView.of(food_item),
                currency=selected,
                base_unit=food_item.base_unit,
                items=tuple(items[clean_offset : clean_offset + clean_limit]),
                total=len(items),
                limit=clean_limit,
                offset=clean_offset,
                convertible_count=counts[CONVERSION_AVAILABLE],
                excluded=excluded,
                available_currencies=available,
                notes=self._notes(excluded, extra=()),
            )

    # --- 月度加权均价与期间比较 ---

    def monthly_prices(
        self,
        food_item_id: int,
        *,
        currency: Any = None,
        months: Any = DEFAULT_MONTHS,
        today: date | None = None,
    ) -> MonthlyPriceReport:
        """月度加权均价序列 + 本期/基期比较。

        加权均价 = 期间采购总金额 ÷ 期间按基础单位换算后的总数量
        （`imp_notes.md` Feature 2 的定义，不是把各行单价简单平均）。
        """
        month_count = self._require_months(months)
        reference = today or date.today()
        with self._database.unit_of_work() as session:
            food_item = self._require_food_item(session, food_item_id)
            window_start = reference
            for _ in range(month_count - 1):
                window_start = previous_month_start(window_start)
            window_start = month_start(window_start)
            # 基期可能早于趋势窗口的第一个月（本月是窗口第一个月时），所以窗口
            # 要往前多盖一个月，否则比较用的基期数据根本没取回来。
            fetch_start = previous_month_start(window_start)

            rows = PurchaseRepository(session).for_statistics(
                food_item_id=food_item.id,
                date_from=fetch_start,
                date_to=month_end(reference),
                limit=STATISTICS_ROW_LIMIT,
            )
            self._guard_row_limit(len(rows))
            available = self._currencies_of(rows)
            selected = self._select_currency(currency, available, required=True)
            index = self._pack_spec_index(session, rows)

            resolved, excluded = self._partition(rows, index, currency=selected)
            undated = PurchaseRepository(session).count_statistics_candidates(
                food_item_id=food_item.id
            )["undated"]
            excluded = ExclusionSummary(
                undated=undated,
                unit_unknown=excluded.unit_unknown,
                pending_pack_spec=excluded.pending_pack_spec,
                incompatible_unit=excluded.incompatible_unit,
                other_currency=excluded.other_currency,
            )

            series: list[PeriodStats] = []
            cursor = month_start(window_start)
            for _ in range(month_count):
                period = month_period(cursor, today=reference)
                series.append(self._aggregate(resolved, period))
                cursor = self._next_month_start(cursor)

            current_period, base_period = comparable_periods(reference)
            comparison = self._compare(
                self._aggregate(resolved, current_period),
                self._aggregate(resolved, base_period),
            )
            notes = self._notes(
                excluded,
                extra=self._period_notes(comparison),
            )
            return MonthlyPriceReport(
                food_item=FoodItemView.of(food_item),
                currency=selected or "",
                base_unit=food_item.base_unit,
                months=tuple(series),
                comparison=comparison,
                excluded=excluded,
                available_currencies=available,
                notes=notes,
            )

    # --- 来源比价 ---

    def source_comparison(
        self,
        food_item_id: int,
        *,
        currency: Any = None,
        date_from: Any = None,
        date_to: Any = None,
        today: date | None = None,
    ) -> SourceComparisonReport:
        """同一食材在不同采购来源之间的加权均价比较。

        默认窗口是最近 3 个月（含本月），避免拿一年前的价格跟这个月比。

        ⚠️ **比价只在同一条食材、同一个基础单位、同一种币种内进行。**
        `imp_notes.md` 要求「比较前需要确保商品规格、质量和基础计量单位相同或具有
        可比性」——系统能保证后者（单位），**不能**保证前者（规格与质量），所以
        每个来源都把原始商品描述与用到的箱规一起返回，让人自己判断可比性。
        不同食材之间不自动比较：那是步骤 10 的食材合并要先解决的事
        （`POST /api/food-items/<id>/merge`，合并之后它们本来就是同一条）。
        """
        reference = today or date.today()
        start = _parse_window_date(date_from, field="date_from")
        end = _parse_window_date(date_to, field="date_to")
        if start is None:
            start = month_start(previous_month_start(previous_month_start(reference)))
        if end is None:
            end = reference
        if start > end:
            raise ValidationError(
                f"date_from（{start.isoformat()}）不能晚于 date_to（{end.isoformat()}）。",
                details={"field": "date_from"},
            )

        with self._database.unit_of_work() as session:
            food_item = self._require_food_item(session, food_item_id)
            rows = PurchaseRepository(session).for_statistics(
                food_item_id=food_item.id,
                date_from=start,
                date_to=end,
                limit=STATISTICS_ROW_LIMIT,
            )
            self._guard_row_limit(len(rows))
            available = self._currencies_of(rows)
            selected = self._select_currency(currency, available, required=True)
            index = self._pack_spec_index(session, rows)
            resolved, excluded = self._partition(rows, index, currency=selected)
            undated = PurchaseRepository(session).count_statistics_candidates(
                food_item_id=food_item.id
            )["undated"]
            excluded = ExclusionSummary(
                undated=undated,
                unit_unknown=excluded.unit_unknown,
                pending_pack_spec=excluded.pending_pack_spec,
                incompatible_unit=excluded.incompatible_unit,
                other_currency=excluded.other_currency,
            )

            grouped: dict[str | None, list[tuple[Purchase, ConversionView]]] = {}
            for purchase, conversion in resolved:
                grouped.setdefault(purchase.source_name_normalized, []).append(
                    (purchase, conversion)
                )

            sources: list[SourceStats] = []
            for source_normalized, entries in grouped.items():
                total_amount = sum(
                    (purchase.line_amount for purchase, _ in entries), Decimal("0")
                )
                total_quantity = sum(
                    (
                        conversion.base_quantity or Decimal("0")
                        for _, conversion in entries
                    ),
                    Decimal("0"),
                )
                if total_quantity == 0:  # pragma: no cover - 数量恒大于 0
                    continue
                dates = [
                    purchase.purchase_date
                    for purchase, _ in entries
                    if purchase.purchase_date is not None
                ]
                descriptions: list[str] = []
                for purchase, _ in entries:
                    text = purchase.original_description
                    if text and text not in descriptions and len(descriptions) < 3:
                        descriptions.append(text)
                with localcontext() as context:
                    context.prec = CALCULATION_PRECISION
                    average = total_amount / total_quantity
                sources.append(
                    SourceStats(
                        source_name=entries[0][0].source_name,
                        source_name_normalized=source_normalized,
                        weighted_average_unit_price=quantize_price(average),
                        total_amount=total_amount,
                        total_base_quantity=quantize_quantity(total_quantity),
                        purchase_count=len(entries),
                        first_purchase_date=min(dates),
                        latest_purchase_date=max(dates),
                        descriptions=tuple(descriptions),
                        pack_spec=entries[0][1].pack_spec,
                    )
                )

            sources.sort(key=lambda stats: stats.weighted_average_unit_price)
            cheapest = sources[0] if sources else None
            spread_amount: Decimal | None = None
            spread_percent: Decimal | None = None
            if len(sources) >= 2 and cheapest is not None:
                dearest = sources[-1]
                spread_amount = (
                    dearest.weighted_average_unit_price
                    - cheapest.weighted_average_unit_price
                )
                if cheapest.weighted_average_unit_price != 0:
                    with localcontext() as context:
                        context.prec = CALCULATION_PRECISION
                        spread_percent = quantize_percent(
                            spread_amount
                            / cheapest.weighted_average_unit_price
                            * Decimal("100")
                        )

            extra_notes: list[str] = [
                "只保证基础计量单位可比；规格与品质要靠原始商品描述自己判断，"
                "系统不会把不同食材放在一起比较。"
            ]
            if len(sources) == 1:
                extra_notes.append(
                    "窗口内只有一个采购来源，没有可比对象——这不等于「价格最优」。"
                )
            return SourceComparisonReport(
                food_item=FoodItemView.of(food_item),
                currency=selected or "",
                base_unit=food_item.base_unit,
                period=Period(
                    key=f"{start.isoformat()}..{end.isoformat()}",
                    date_from=start,
                    date_to=end,
                    basis=BASIS_MONTH_TO_DATE,
                ),
                sources=tuple(sources),
                cheapest_source_normalized=(
                    cheapest.source_name_normalized if cheapest else None
                ),
                spread_amount=spread_amount,
                spread_percent=spread_percent,
                excluded=excluded,
                available_currencies=available,
                notes=self._notes(excluded, extra=tuple(extra_notes)),
            )

    # --- 涨价提醒 ---

    def get_alert_settings(self) -> AlertSettingsView:
        with self._database.unit_of_work() as session:
            return self._settings_view(self._load_or_create_settings(session))

    def update_alert_settings(
        self, *, enabled: Any = None, threshold_percent: Any = None
    ) -> AlertSettingsView:
        """改阈值或开关。两个都不传就是没改任何东西，直接报错而不是静默成功。"""
        if enabled is None and threshold_percent is None:
            raise ValidationError(
                "请至少提供 enabled 或 threshold_percent 之一。",
                details={"fields": ["enabled", "threshold_percent"]},
            )
        if enabled is not None and not isinstance(enabled, bool):
            raise ValidationError(
                "字段 enabled 必须是 true 或 false。", details={"field": "enabled"}
            )
        threshold: Decimal | None = None
        if threshold_percent is not None:
            threshold = parse_decimal(
                threshold_percent,
                field="threshold_percent",
                scale=PERCENT_SCALE,
                allow_zero=True,
            )
            if threshold > Decimal("1000"):
                raise ValidationError(
                    f"阈值 {format_percent(threshold)}% 过大，超过 1000% 的"
                    "涨幅更可能是录错了数量或单位。",
                    details={"field": "threshold_percent"},
                )
        with self._database.unit_of_work() as session:
            settings = self._load_or_create_settings(session)
            if enabled is not None:
                settings.enabled = enabled
            if threshold is not None:
                settings.threshold_percent = threshold
            session.flush()
            logger.info(
                "price alert settings updated enabled=%s threshold=%s",
                settings.enabled,
                settings.threshold_percent,
            )
            return self._settings_view(settings)

    def alerts(
        self,
        *,
        threshold_percent: Any = None,
        today: date | None = None,
        limit: Any = MAX_LIMIT,
    ) -> AlertReport:
        """涨得超过阈值的食材清单。

        `threshold_percent` 可以临时覆盖设置里的阈值（界面上拖一下看看的那种），
        **不写库**。设置里关掉提醒时返回空清单并说明原因——关掉就是关掉，不要
        "虽然关了但还是给你看一眼"。

        ⚠️ **按（食材, 币种）分组比较**，所以不会出现把 USD 与 CNY 相加的均价。
        """
        clean_limit = require_limit(limit, max_limit=MAX_LIMIT)
        reference = today or date.today()
        override: Decimal | None = None
        if threshold_percent is not None:
            override = parse_decimal(
                threshold_percent,
                field="threshold_percent",
                scale=PERCENT_SCALE,
                allow_zero=True,
            )
        current_period, base_period = comparable_periods(reference)

        with self._database.unit_of_work() as session:
            settings = self._load_or_create_settings(session)
            settings_view = self._settings_view(settings)
            threshold = override if override is not None else settings.threshold_percent

            if not settings.enabled and override is None:
                return AlertReport(
                    settings=settings_view,
                    threshold_percent=threshold,
                    current=current_period,
                    base=base_period,
                    alerts=(),
                    compared_count=0,
                    skipped={},
                    excluded=EMPTY_EXCLUSIONS,
                    notes=("涨价提醒已关闭（settings.enabled=false），本次不做比较。",),
                )

            rows = PurchaseRepository(session).for_statistics(
                date_from=base_period.date_from,
                date_to=current_period.date_to,
                limit=STATISTICS_ROW_LIMIT,
            )
            self._guard_row_limit(len(rows))
            index = self._pack_spec_index(session, rows)
            resolved, excluded = self._partition(rows, index, currency=None)
            undated = PurchaseRepository(session).count_statistics_candidates()[
                "undated"
            ]
            excluded = ExclusionSummary(
                undated=undated,
                unit_unknown=excluded.unit_unknown,
                pending_pack_spec=excluded.pending_pack_spec,
                incompatible_unit=excluded.incompatible_unit,
                other_currency=0,
            )

            grouped: dict[tuple[int, str], list[tuple[Purchase, ConversionView]]] = {}
            for purchase, conversion in resolved:
                key = (int(purchase.food_item_id or 0), purchase.currency)
                grouped.setdefault(key, []).append((purchase, conversion))

            alerts: list[AlertRow] = []
            skipped: dict[str, int] = {}
            compared = 0
            for (item_id, item_currency), entries in grouped.items():
                comparison = self._compare(
                    self._aggregate(entries, current_period),
                    self._aggregate(entries, base_period),
                )
                if comparison.status != COMPARISON_AVAILABLE:
                    skipped[comparison.status] = skipped.get(comparison.status, 0) + 1
                    continue
                compared += 1
                assert comparison.change_percent is not None
                if comparison.change_percent < threshold:
                    continue
                food_item = entries[0][0].food_item
                assert food_item is not None
                alerts.append(
                    AlertRow(
                        food_item=FoodItemView.of(food_item),
                        currency=item_currency,
                        base_unit=food_item.base_unit,
                        comparison=comparison,
                    )
                )

            alerts.sort(
                key=lambda row: (
                    -(row.comparison.change_percent or Decimal("0")),
                    row.food_item.id,
                )
            )
            notes = self._notes(
                excluded,
                extra=(
                    f"比较期间：{current_period.date_from.isoformat()}…"
                    f"{current_period.date_to.isoformat()}（{current_period.days} 天）"
                    f" vs {base_period.date_from.isoformat()}…"
                    f"{base_period.date_to.isoformat()}（{base_period.days} 天）。",
                    "只提示上涨：跌价不提醒。",
                ),
            )
            return AlertReport(
                settings=settings_view,
                threshold_percent=threshold,
                current=current_period,
                base=base_period,
                alerts=tuple(alerts[:clean_limit]),
                compared_count=compared,
                skipped=skipped,
                excluded=excluded,
                notes=notes,
            )

    # --- 单位目录（给前端列可选单位用）---

    @staticmethod
    def units_catalog() -> dict[str, Any]:
        """支持的单位、量纲与两条拒绝规则。纯静态，不查库。"""
        return {
            "volume_system": VOLUME_SYSTEM,
            "dimensions": [
                {
                    "name": dimension,
                    "units": [
                        {
                            "unit": token,
                            "per_reference_unit": format_decimal(
                                UNIT_FACTORS[token][1]
                            ),
                        }
                        for token in units_of_dimension(dimension)
                    ],
                }
                for dimension in DIMENSIONS
            ],
            "packaging_units": sorted(PACKAGING_UNIT_TOKENS),
            "rules": [
                "体积与重量之间不换算：没有密度，gallon 与 lb 不能互转。",
                "包装单位（case / pack / box / carton / bag）必须先有一条箱规"
                "说明「1 个包装单位 = 多少基础单位」才能算标准化价格。",
                f"体积单位按{VOLUME_SYSTEM}制（1 gallon = 3.785411784 L）。",
            ],
        }

    # --- 内部：换算 ---

    def _resolve_conversion(
        self, purchase: Purchase, index: dict[Any, PackSpec]
    ) -> ConversionView:
        """一行采购记录 → 基础单位数量与标准化单价，或者换不出来的原因。

        ⚠️ **单位 token 从 `purchase_unit` 原文现算，不读 `purchase_unit_normalized`。**
        原文是单据上的事实，那一列是步骤 04 写入时的便利列；单位同义词表后来补全
        （例如认出发票上印的 `lbs.`）时，现算能让**历史记录**跟着受益，而读旧列
        会让一批老记录永远停在"单位认不出"。两者的归一逻辑是同一个函数
        （`services/ingredients.py::normalize_unit_token`），不会给出不同答案。
        """
        food_item = purchase.food_item
        assert food_item is not None  # 调用方只传有食材的采购行
        base_unit = food_item.base_unit

        measurement = canonical_unit(purchase.purchase_unit)
        if measurement is not None:
            try:
                base_quantity = convert(purchase.quantity, measurement, base_unit)
            except UnitError as exc:
                return ConversionView(
                    status=CONVERSION_INCOMPATIBLE,
                    base_unit=base_unit,
                    base_quantity=None,
                    normalized_unit_price=None,
                    pack_spec=None,
                    pack_spec_match=None,
                    reason=str(exc),
                )
            return self._priced(purchase, base_unit, base_quantity, None, None)

        packaging = canonical_packaging_unit(purchase.purchase_unit)
        if packaging is None:
            return ConversionView(
                status=CONVERSION_UNIT_UNKNOWN,
                base_unit=base_unit,
                base_quantity=None,
                normalized_unit_price=None,
                pack_spec=None,
                pack_spec_match=None,
                reason=(
                    f"采购单位 {purchase.purchase_unit!r} 既不是计量单位也不是"
                    "包装单位，无法换算。请把这条记录的单位改成单据上实际的计量单位。"
                ),
            )

        spec = resolve_from_index(
            index,
            food_item_id=food_item.id,
            source_name_normalized=purchase.source_name_normalized,
            purchase_unit_normalized=packaging,
        )
        if spec is None:
            return ConversionView(
                status=CONVERSION_PENDING_PACK_SPEC,
                base_unit=base_unit,
                base_quantity=None,
                normalized_unit_price=None,
                pack_spec=None,
                pack_spec_match=None,
                reason=(
                    f"按 {packaging} 计价，但还没有「1 {packaging} = 多少 "
                    f"{base_unit}」的包装规格，所以不生成标准化价格。"
                ),
            )
        match = (
            PACK_SPEC_MATCH_EXACT
            if spec.source_name_normalized is not None
            else PACK_SPEC_MATCH_ANY_SOURCE
        )
        try:
            per_unit = convert(spec.pack_size, spec.pack_unit, base_unit)
        except UnitError as exc:  # 箱规的单位与食材基础单位不同量纲
            return ConversionView(
                status=CONVERSION_INCOMPATIBLE,
                base_unit=base_unit,
                base_quantity=None,
                normalized_unit_price=None,
                pack_spec=PackSpecView.of(spec, with_food_item=False),
                pack_spec_match=match,
                reason=str(exc),
            )
        with localcontext() as context:
            context.prec = CALCULATION_PRECISION
            base_quantity = purchase.quantity * per_unit
        return self._priced(
            purchase,
            base_unit,
            base_quantity,
            PackSpecView.of(spec, with_food_item=False),
            match,
        )

    @staticmethod
    def _priced(
        purchase: Purchase,
        base_unit: str,
        base_quantity: Decimal,
        pack_spec: PackSpecView | None,
        pack_spec_match: str | None,
    ) -> ConversionView:
        """算标准化单价 = 行金额 ÷ 基础单位数量。

        用**行金额**而不是单价 × 数量：行金额是权威（那是实付的钱），步骤 04 已经
        定下这条口径，这里不另立一套。
        """
        if base_quantity == 0:  # pragma: no cover - 数量与箱规都恒大于 0
            return ConversionView(
                status=CONVERSION_INCOMPATIBLE,
                base_unit=base_unit,
                base_quantity=None,
                normalized_unit_price=None,
                pack_spec=pack_spec,
                pack_spec_match=pack_spec_match,
                reason="换算后的基础单位数量为 0，无法得出单位价格。",
            )
        with localcontext() as context:
            context.prec = CALCULATION_PRECISION
            price = purchase.line_amount / base_quantity
        return ConversionView(
            status=CONVERSION_AVAILABLE,
            base_unit=base_unit,
            base_quantity=quantize_quantity(base_quantity),
            normalized_unit_price=quantize_price(price),
            pack_spec=pack_spec,
            pack_spec_match=pack_spec_match,
            reason=None,
        )

    def _partition(
        self,
        rows: Iterable[Purchase],
        index: dict[Any, PackSpec],
        *,
        currency: str | None,
    ) -> tuple[list[tuple[Purchase, ConversionView]], ExclusionSummary]:
        """把记录分成「能算的」与「算不了的（按原因计数）」。"""
        resolved: list[tuple[Purchase, ConversionView]] = []
        counts = {status: 0 for status in CONVERSION_STATUSES}
        other_currency = 0
        for purchase in rows:
            if currency is not None and purchase.currency != currency:
                other_currency += 1
                continue
            conversion = self._resolve_conversion(purchase, index)
            counts[conversion.status] += 1
            if conversion.available:
                resolved.append((purchase, conversion))
        return resolved, ExclusionSummary(
            undated=0,  # 由调用方补：那是一次独立的计数查询
            unit_unknown=counts[CONVERSION_UNIT_UNKNOWN],
            pending_pack_spec=counts[CONVERSION_PENDING_PACK_SPEC],
            incompatible_unit=counts[CONVERSION_INCOMPATIBLE],
            other_currency=other_currency,
        )

    # --- 内部：聚合 ---

    @staticmethod
    def _aggregate(
        resolved: Iterable[tuple[Purchase, ConversionView]], period: Period
    ) -> PeriodStats:
        """期间加权均价 = 总金额 ÷ 总基础数量。

        ⚠️ 不是把各行的标准化单价平均：10 lb × \\$5 与 100 lb × \\$6 的加权均价是
        \\$5.91/lb，简单平均是 \\$5.50/lb，差 7%，而且总是偏向买得少的那一次。
        `imp_notes.md` Feature 2 明确要求加权。
        """
        total_amount = Decimal("0")
        total_quantity = Decimal("0")
        count = 0
        zero_amount = 0
        mismatch = 0
        ids: list[int] = []
        for purchase, conversion in resolved:
            if purchase.purchase_date is None or not period.contains(
                purchase.purchase_date
            ):
                continue
            assert conversion.base_quantity is not None
            total_amount += purchase.line_amount
            total_quantity += conversion.base_quantity
            count += 1
            if purchase.line_amount == 0:
                zero_amount += 1
            if purchase.amount_mismatch:
                mismatch += 1
            ids.append(purchase.id)
        average: Decimal | None = None
        if count and total_quantity != 0:
            with localcontext() as context:
                context.prec = CALCULATION_PRECISION
                average = quantize_price(total_amount / total_quantity)
        return PeriodStats(
            period=period,
            weighted_average_unit_price=average,
            total_amount=total_amount,
            total_base_quantity=quantize_quantity(total_quantity),
            purchase_count=count,
            zero_amount_count=zero_amount,
            amount_mismatch_count=mismatch,
            purchase_ids=tuple(ids),
        )

    @staticmethod
    def _compare(current: PeriodStats, base: PeriodStats) -> ComparisonView:
        """本期与基期比较。**没有数据时返回 `null`，不返回 0%。**

        三种"算不出变化"分开标，因为处置方式不同：
        - `no_current_data`：这个月还没买过——等着就行。
        - `no_base_data`：上月没有可换算的采购——可能是没买，也可能是缺箱规。
        - `base_price_is_zero`：上月全是赠品，涨幅在数学上没有定义。
        """
        if not current.has_data:
            return ComparisonView(
                current=current,
                base=base,
                change_amount=None,
                change_percent=None,
                status=COMPARISON_NO_CURRENT_DATA,
                reason="本期没有可换算的采购记录，无法计算变化。",
            )
        if not base.has_data:
            return ComparisonView(
                current=current,
                base=base,
                change_amount=None,
                change_percent=None,
                status=COMPARISON_NO_BASE_DATA,
                reason=(
                    "基期没有可换算的采购记录：缺少基期时不显示 0% 变化，"
                    "那会把「没有可比数据」说成「价格没变」。"
                ),
            )
        assert current.weighted_average_unit_price is not None
        assert base.weighted_average_unit_price is not None
        change = (
            current.weighted_average_unit_price - base.weighted_average_unit_price
        )
        if base.weighted_average_unit_price == 0:
            return ComparisonView(
                current=current,
                base=base,
                change_amount=change,
                change_percent=None,
                status=COMPARISON_BASE_PRICE_IS_ZERO,
                reason=(
                    "基期均价为 0（赠品或 0 元入账），涨幅百分比没有定义；"
                    "金额变化仍然给出。"
                ),
            )
        with localcontext() as context:
            context.prec = CALCULATION_PRECISION
            percent = quantize_percent(
                change / base.weighted_average_unit_price * Decimal("100")
            )
        return ComparisonView(
            current=current,
            base=base,
            change_amount=change,
            change_percent=percent,
            status=COMPARISON_AVAILABLE,
            reason=None,
        )

    # --- 内部：杂项 ---

    @staticmethod
    def _next_month_start(day: date) -> date:
        if day.month == 12:
            return day.replace(year=day.year + 1, month=1, day=1)
        return day.replace(month=day.month + 1, day=1)

    @staticmethod
    def _notes(excluded: ExclusionSummary, *, extra: tuple[str, ...]) -> tuple[str, ...]:
        notes = [NOTE_EXCLUDES_ADJUSTMENTS, NOTE_NO_FX]
        notes.extend(extra)
        if excluded.pending_pack_spec:
            notes.append(
                f"有 {excluded.pending_pack_spec} 条按包装单位计价的记录还缺箱规，"
                "没有参与均价；清单见 GET /api/prices/pack-specs/pending。"
            )
        if excluded.undated:
            notes.append(
                f"有 {excluded.undated} 条采购记录没有日期，进不了任何月度统计；"
                "清单见 GET /api/purchases?undated=true。"
            )
        if excluded.unit_unknown:
            notes.append(
                f"有 {excluded.unit_unknown} 条记录的采购单位认不出来，没有参与均价。"
            )
        if excluded.incompatible_unit:
            notes.append(
                f"有 {excluded.incompatible_unit} 条记录的单位与食材基础单位不是"
                "同一类计量（例如体积对重量），没有参与均价。"
            )
        if excluded.other_currency:
            notes.append(
                f"有 {excluded.other_currency} 条记录是其它币种，没有参与本次统计。"
            )
        return tuple(notes)

    @staticmethod
    def _period_notes(comparison: ComparisonView) -> tuple[str, ...]:
        current = comparison.current.period
        base = comparison.base.period
        note = (
            f"比较期间：{current.key} 的 {current.date_from.isoformat()}…"
            f"{current.date_to.isoformat()}（{current.days} 天，{current.basis}）"
            f" vs {base.key} 的 {base.date_from.isoformat()}…"
            f"{base.date_to.isoformat()}（{base.days} 天，{base.basis}）。"
        )
        notes = [note]
        if current.days != base.days:
            notes.append(
                f"两个期间天数不同（{current.days} vs {base.days}）：上月没有那么多天，"
                "已取到上月最后一天。"
            )
        if comparison.current.zero_amount_count:
            notes.append(
                f"本期含 {comparison.current.zero_amount_count} 条 0 元行（赠品/促销），"
                "它们会压低加权均价——这是实付成本的事实，但看数字时要知道。"
            )
        if comparison.current.amount_mismatch_count:
            notes.append(
                f"本期含 {comparison.current.amount_mismatch_count} 条"
                "「数量 × 单价 与行金额对不上但已确认」的记录，按行金额计入。"
            )
        return tuple(notes)

    def _pack_spec_index(
        self, session: Session, rows: Iterable[Purchase]
    ) -> dict[Any, PackSpec]:
        item_ids = sorted(
            {int(row.food_item_id) for row in rows if row.food_item_id is not None}
        )
        return index_for_lookup(
            PackSpecRepository(session).list_for_food_items(item_ids)
        )

    @staticmethod
    def _currencies_of(rows: Iterable[Purchase]) -> tuple[str, ...]:
        return tuple(sorted({row.currency for row in rows}))

    @staticmethod
    def _select_currency(
        currency: Any, available: tuple[str, ...], *, required: bool
    ) -> str | None:
        """定下这次统计用哪种币种。**多币种时不猜。**

        `required=False`（价格历史）时可以不选：那是一张逐条清单，不做加总，
        混着列出来不会算出一个无意义的数。`required=True`（均价、比价、提醒）时
        必须单一币种，否则 1 美元 + 1 人民币 = 2 会变成一个"均价"。
        """
        if currency is not None:
            if not isinstance(currency, str):
                raise ValidationError(
                    "currency 必须是字符串。", details={"field": "currency"}
                )
            token = currency.strip().upper()
            if token not in CURRENCIES:
                raise ValidationError(
                    f"币种 {currency!r} 不在支持范围内，可选值："
                    f"{', '.join(CURRENCIES)}。",
                    details={"field": "currency", "allowed": list(CURRENCIES)},
                )
            return token
        if not available:
            return None
        if len(available) == 1:
            return available[0]
        if not required:
            return None
        raise ValidationError(
            f"这条食材的采购记录里有 {len(available)} 种币种"
            f"（{', '.join(available)}），不能混算成一个均价。"
            "请用 currency 参数指定一种——本轮不做汇率换算。",
            details={"field": "currency", "available": list(available)},
        )

    @staticmethod
    def _require_months(months: Any) -> int:
        if isinstance(months, bool) or not isinstance(months, int):
            raise ValidationError(
                "months 必须是整数。", details={"field": "months"}
            )
        if months < 1 or months > MAX_MONTHS:
            raise ValidationError(
                f"months 必须在 1—{MAX_MONTHS} 之间（收到 {months}）。",
                details={"field": "months", "max": MAX_MONTHS},
            )
        return months

    @staticmethod
    def _require_packaging_unit(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(
                "purchase_unit 不能为空。", details={"field": "purchase_unit"}
            )
        token = canonical_packaging_unit(value)
        if token is None or not is_packaging_unit(token):
            measurement = canonical_unit(value)
            if measurement is not None:
                raise ValidationError(
                    f"{value!r} 本身就是计量单位（{measurement}），不需要箱规："
                    "按它计价的采购记录已经能直接换算。箱规只用于 "
                    f"{', '.join(sorted(PACKAGING_UNIT_TOKENS))} 这类包装单位。",
                    details={"field": "purchase_unit", "unit": measurement},
                )
            raise ValidationError(
                f"{value!r} 不是支持的包装单位，可选值："
                f"{', '.join(sorted(PACKAGING_UNIT_TOKENS))}（可以用 箱 / CS / "
                "cases 等写法）。",
                details={
                    "field": "purchase_unit",
                    "allowed": sorted(PACKAGING_UNIT_TOKENS),
                },
            )
        return token

    @staticmethod
    def _require_compatible(pack_unit: str, base_unit: str) -> None:
        if dimension_of(pack_unit) != dimension_of(base_unit):
            raise ValidationError(
                f"箱规的单位 {pack_unit} 与食材的基础单位 {base_unit} 不是同一类"
                f"计量（{dimension_of(pack_unit)} vs {dimension_of(base_unit)}）："
                "没有密度不能把体积换成重量，这条箱规存下来也算不出价格。",
                details={"field": "pack_unit", "base_unit": base_unit},
            )

    @staticmethod
    def _suggest_pack_spec(purchase: Purchase) -> PackSpecSuggestion | None:
        """从原始商品描述里读一个箱规候选出来。**只是候选。**

        实测依据：Kun Kee 的包装规格没有独立列，就写在描述里（`1000g x 10`）。
        而 New Moy's 的 `6X30` 这种裸数字写法读不出唯一结论，`units.py` 对它返回
        不可用，所以这里不会给出候选——**宁可不给，也不给一个会被当成事实的数。**
        """
        reading = find_pack_spec_in_description(purchase.original_description)
        if reading is None or not reading.usable:
            return None
        assert reading.pack_size is not None and reading.pack_unit is not None
        return PackSpecSuggestion(
            pack_size=reading.pack_size,
            pack_unit=reading.pack_unit,
            spec_text=reading.raw_text,
            from_description=purchase.original_description or "",
        )

    @staticmethod
    def _count_affected_purchases(
        session: Session,
        *,
        food_item_id: int,
        source_name_normalized: str | None,
        purchase_unit: str,
    ) -> int:
        """这条箱规会影响多少条采购记录（当前值变、历史也跟着变）。"""
        rows = PurchaseRepository(session).for_price_review(
            food_item_id=food_item_id, limit=STATISTICS_ROW_LIMIT
        )
        affected = 0
        for purchase in rows:
            if canonical_packaging_unit(purchase.purchase_unit) != purchase_unit:
                continue
            if (
                source_name_normalized is not None
                and purchase.source_name_normalized != source_name_normalized
            ):
                continue
            affected += 1
        return affected

    @staticmethod
    def _guard_row_limit(count: int) -> None:
        if count >= STATISTICS_ROW_LIMIT:
            raise ValidationError(
                f"这次统计要读的采购记录超过 {STATISTICS_ROW_LIMIT} 条上限。"
                "请缩小日期范围——截断之后算出来的均价看起来完全正常，"
                "所以这里报错而不是少算一部分。",
                details={"limit": STATISTICS_ROW_LIMIT},
            )

    @staticmethod
    def _load_or_create_settings(session: Session) -> PriceAlertSettings:
        """读提醒设置，没有就按默认值建出来（开启、10%）。

        默认值刻意不放在迁移里：老板把提醒关掉之后再跑一次迁移，那条 INSERT 不会
        重跑，于是"默认"会有两个位置却只有一个生效。放在这里，默认值只有一处。
        """
        repository = PriceAlertSettingsRepository(session)
        settings = repository.get()
        if settings is None:
            settings = repository.add(
                PriceAlertSettings(
                    id=PRICE_ALERT_SETTINGS_ID,
                    enabled=True,
                    threshold_percent=DEFAULT_ALERT_THRESHOLD_PERCENT,
                )
            )
        return settings

    @staticmethod
    def _settings_view(settings: PriceAlertSettings) -> AlertSettingsView:
        return AlertSettingsView(
            enabled=settings.enabled,
            threshold_percent=settings.threshold_percent,
            updated_at=settings.updated_at,
        )

    @staticmethod
    def _require_food_item(session: Session, food_item_id: Any):
        if isinstance(food_item_id, bool) or not isinstance(food_item_id, int):
            raise ValidationError(
                "food_item_id 必须是整数。", details={"field": "food_item_id"}
            )
        food_item = FoodItemRepository(session).get(food_item_id)
        if food_item is None:
            raise NotFoundError(
                f"食材 {food_item_id} 不存在。",
                details={"food_item_id": food_item_id},
            )
        return food_item


__all__ = [
    "AlertReport",
    "AlertRow",
    "AlertSettingsView",
    "BASIS_FULL_MONTH",
    "BASIS_MONTH_TO_DATE",
    "COMPARISON_AVAILABLE",
    "COMPARISON_BASE_PRICE_IS_ZERO",
    "COMPARISON_NO_BASE_DATA",
    "COMPARISON_NO_CURRENT_DATA",
    "CONVERSION_AVAILABLE",
    "CONVERSION_INCOMPATIBLE",
    "CONVERSION_PENDING_PACK_SPEC",
    "CONVERSION_STATUSES",
    "CONVERSION_UNIT_UNKNOWN",
    "ComparisonView",
    "ConversionView",
    "DEFAULT_MONTHS",
    "ExclusionSummary",
    "MAX_MONTHS",
    "MonthlyPriceReport",
    "NORMALIZED_PRICE_SCALE",
    "PACK_SPEC_MATCH_ANY_SOURCE",
    "PACK_SPEC_MATCH_EXACT",
    "PERCENT_SCALE",
    "PackSpecPage",
    "PackSpecSuggestion",
    "PackSpecView",
    "PendingPackSpecRow",
    "Period",
    "PeriodStats",
    "PriceHistoryPage",
    "PriceHistoryRow",
    "PriceService",
    "STATISTICS_ROW_LIMIT",
    "SourceComparisonReport",
    "SourceStats",
    "comparable_periods",
    "format_percent",
    "format_price",
    "month_end",
    "month_key",
    "month_period",
    "month_start",
    "previous_month_start",
    "quantize_percent",
    "quantize_price",
    "quantize_quantity",
]
