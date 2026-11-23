"""采购记录的业务服务（步骤 04）。

职责与边界：

- **事务边界在本层**，与 `services/ingredients.py` 一致。`create_purchase()` 另外
  接受一个 `session` 参数：步骤 05 的逐行确认要把发票行与采购记录放在同一个事务里，
  传入外层 Session 即可加入（见 `src/db.py` 的嵌套说明），不会提前提交。
- **数值解析只有这一处实现**：`parse_decimal()` 把字符串/整数解析成 `Decimal`，
  并在这里拒绝 `float`、科学计数法、千分位、NaN、负数与超位数的输入。模型层的
  `DecimalAsInteger` 是最后一道防线，不负责给出可读的错误。
- **ORM 对象不流出本层**，返回下面的只读视图。

本模块**不做**单位换算，`purchase_unit` 按原文保留、另存一个可空的规范化 token；
换算、标准化价格与月度统计属于步骤 09 的 `services/units.py` 与 `services/prices.py`。

三条关键口径（决定见 `docs/stru_dev.md` 步骤 04）：

1. **行金额是权威。** 不拿单价反算金额。`|数量 × 单价 − 行金额| > $0.05` 时抛
   `AmountMismatchError`，调用方带 `accept_amount_mismatch=True` 重试才能入账，
   存下来的行 `amount_mismatch=True`。容差只盖供应商按分舍入那个量级。
2. **采购日期可空。** 识别不出就允许留空，不编造。代价是这些记录进不了步骤 09 的
   月度均价，所以查询支持 `undated=True` 筛出「待补日期」清单。
3. **纠错是直接修改 + 修改日志。** `update_purchase()` 可以改数值，但每次都往
   `purchase_revisions` 追加一行改动前后的值。统计按当前值算，所以均价会随修正
   变化——能不能解释清楚全靠这份日志。
4. **调整行（退货 / 折让 / 其它调整）金额可以为负。** `line_kind="adjustment"` 的行
   放宽了符号限制，留在库里是为了对账；它们**默认不出现在查询结果里**，也不该参与
   步骤 09 的价格统计。默认排除而不是默认包含，是因为两种错法的代价不对称：对账时
   漏了调整行，发票各行相加对不上总额，一眼就看得出；而把一笔退货混进加权均价只会
   让价格悄悄偏低，没人会发现。要对账就显式传 `kind="all"`。

`_require_text` / `_optional_text` / `_require_choice` 现在只是
`services/validation.py` 的薄包装：步骤 05 的发票服务成了第三个调用方，按 rule of
three 把实现抽了过去（原来刻意保留的那份重复到此结束）。调用点与错误文案没有变。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, Iterable

from sqlalchemy.orm import Session

from src.db import Database
from src.errors import AmountMismatchError, ConflictError, NotFoundError, ValidationError
from src.models import (
    AMOUNT_SCALE,
    DESCRIPTION_MAX_LENGTH,
    LINE_KIND_ADJUSTMENT,
    LINE_KIND_PURCHASE,
    MAX_INTEGER_DIGITS,
    PURCHASE_UNIT_MAX_LENGTH,
    QUANTITY_SCALE,
    SOURCE_NAME_MAX_LENGTH,
    UNIT_PRICE_SCALE,
    Purchase,
    PurchaseRevision,
    utcnow,
)
from src.repositories.ingredients import FoodItemRepository
from src.repositories.purchases import PurchaseRepository, PurchaseRevisionRepository
from src.services.ingredients import (
    BASE_UNITS,
    DEFAULT_LIMIT,
    MAX_LIMIT,
    PACKAGING_UNITS,
    UNSET,
    FoodItemView,
    normalize_name,
    normalize_unit_token,
)
from src.services.validation import (
    optional_text,
    require_choice,
    require_limit,
    require_offset,
    require_text,
)

# --- 口径常量 ---------------------------------------------------------------

# 数量 × 单价 与行金额的容差。供应商按分舍入最多就差几分（11.3 × $6.35 = $71.755，
# 单据上印 $71.76，差 $0.005）；真印错的那种（3 × $10 印成 $35）差得远不止这个量级。
AMOUNT_TOLERANCE: Final[Decimal] = Decimal("0.05")

# 币种白名单。只是标识，本轮不做汇率换算；不同币种不混算由步骤 09 负责。
DEFAULT_CURRENCY: Final[str] = "USD"
CURRENCIES: Final[tuple[str, ...]] = ("USD", "CNY")

# 行类别。`purchase` 正常采购行，`adjustment` 退货 / 折让 / 其它调整行。
# 取值常量定义在 `src/models.py`：迁移给既有行回填的也是这个字符串。
LINE_KINDS: Final[tuple[str, ...]] = (LINE_KIND_PURCHASE, LINE_KIND_ADJUSTMENT)

# 查询时的 `kind` 取值。`all` 不是一种行类别，只是「两种都要」的筛选选项。
KIND_ALL: Final[str] = "all"
SEARCH_KINDS: Final[tuple[str, ...]] = LINE_KINDS + (KIND_ALL,)

# 记录来源。手工（04）、发票确认（05）、表格导入（06）写同一张表，步骤 11 要按
# 三条入口分别核对，所以必须能分得出来。
ENTRY_SOURCE_MANUAL: Final[str] = "manual"
ENTRY_SOURCE_INVOICE: Final[str] = "invoice"
ENTRY_SOURCE_IMPORT: Final[str] = "import"
ENTRY_SOURCES: Final[tuple[str, ...]] = (
    ENTRY_SOURCE_MANUAL,
    ENTRY_SOURCE_INVOICE,
    ENTRY_SOURCE_IMPORT,
)

REVISION_ACTION_UPDATE: Final[str] = "update"
REVISION_ACTION_VOID: Final[str] = "void"
REVISION_REASON_MAX_LENGTH: Final[int] = 300

# 采购日期的合理范围。OCR 很容易把 2026 读成 2062，手输也会敲错年份；放过这种值
# 会让月度统计里凭空出现一个几十年后的月份。+1 天是给时区留的余量。
EARLIEST_PURCHASE_YEAR: Final[int] = 2000
FUTURE_DATE_SLACK: Final[timedelta] = timedelta(days=1)

# 包装单位的规范写法 → token。步骤 03 的 `PACKAGING_UNITS` 只是一个「这些是包装单位」
# 的集合（它在那里用于**拒绝**把 case 当基础单位）；采购单位本来就可以是 case，
# 所以这里需要的是归一映射而不是拒绝。
# `tests/test_purchases_service.py::test_packaging_synonyms_cover_step_03_set`
# 断言本表的键与那个集合完全一致，避免两处各自演化。
PACKAGING_UNIT_SYNONYMS: Final[dict[str, str]] = {
    "case": "case",
    "cases": "case",
    "cs": "case",
    "箱": "case",
    "件": "case",
    "pack": "pack",
    "packs": "pack",
    "pk": "pack",
    "package": "pack",
    "包": "pack",
    "box": "box",
    "boxes": "box",
    "carton": "carton",
    "cartons": "carton",
    "bag": "bag",
    "bags": "bag",
    "袋": "bag",
    # 容器名词（步骤 09 按真实单据补）。理由见 `services/ingredients.py` 的
    # `PACKAGING_UNITS`：`48 cups 酸奶` 是 48 个杯装而不是 48 杯容积，
    # 一杯装里有多少必须由箱规说一次。
    "can": "can",
    "cans": "can",
    "罐": "can",
    "cup": "cup",
    "cups": "cup",
    "杯": "cup",
    "jar": "jar",
    "jars": "jar",
    "bottle": "bottle",
    "bottles": "bottle",
    "瓶": "bottle",
}

# 规范十进制：可选符号 + 整数部分 + 可选小数部分。刻意不接受科学计数法（`1e5`）、
# 千分位（`1,234.56`）、货币符号（`$6.00`）与 `NaN` / `Infinity`：
# 带符号和分隔符的清洗属于步骤 06 的表格导入，本层只收规范写法。
#
# `re.ASCII` 不是摆设：`\d` 默认匹配所有 Unicode 十进制数字，中文输入法下很容易
# 打出全角的 `６.００`，而 `Decimal` 认得它——不限定成 ASCII 的话，同一个数字会有
# 半角和全角两种写法混进数据库。
DECIMAL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[+-]?\d+(\.\d+)?$", re.ASCII
)


# --- 数值解析与格式化 -------------------------------------------------------


def parse_decimal(
    value: Any,
    *,
    field: str,
    scale: int,
    allow_zero: bool,
    allow_negative: bool = False,
) -> Decimal:
    """把请求里的数值解析成 `Decimal`。全程不经 `float`。

    接受字符串与整数；**拒绝 JSON 小数**——`12.345` 在 JSON 里是 double，Python 的
    解析器拿到的已经是浮点数，客户端的 JavaScript 在序列化之前也已经过了一遍 double。
    只有拒绝它才能真正做到步骤 04 要求的「不经 float / Number 中转」，所以这里给出
    明确的错误而不是悄悄接受。

    小数位数超过该列的 `scale` 时**拒绝而不是四舍五入**：舍入是无声地改数据，
    和「不要自动篡改原始识别结果」相抵（`docs/imp_notes.md` 4.5）。
    """
    # bool 是 int 的子类，不先挡住的话 `True` 会被当成 1。
    if isinstance(value, bool):
        raise ValidationError(
            f"字段 {field} 必须是数字（用字符串或整数表示），不能是布尔值。",
            details={"field": field},
        )
    if isinstance(value, float):
        raise ValidationError(
            f"字段 {field} 不能用 JSON 小数传递（收到 {value!r}）："
            "JSON 的小数是浮点数，会在传输途中丢精度。请用字符串，例如 \"12.345\"。",
            details={"field": field},
        )
    if isinstance(value, Decimal):
        # 已经是精确值：`update_purchase()` 把未修改的原值传回来时走这一支，
        # 不必绕一圈字符串。NaN / Infinity 仍然要挡住。
        if not value.is_finite():
            raise ValidationError(
                f"字段 {field} 不是有效数字（收到 {value!r}）。",
                details={"field": field},
            )
        number = value
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValidationError(f"字段 {field} 不能为空。", details={"field": field})
        if not DECIMAL_PATTERN.match(text):
            raise ValidationError(
                f"字段 {field} 不是规范的十进制数字（收到 {text!r}）。"
                "请用形如 \"12.345\" 的写法：不要带货币符号、千分位逗号或科学计数法。",
                details={"field": field},
            )
        number = Decimal(text)
    else:
        raise ValidationError(
            f"字段 {field} 必须是数字字符串或整数。", details={"field": field}
        )

    shown = format(number, "f")

    if number < 0 and not allow_negative:
        raise ValidationError(
            f"字段 {field} 不能为负数（收到 {shown!r}）。"
            "退货、折让与其它调整请用 line_kind=\"adjustment\" 录入，"
            "那种行的数量与金额可以为负。",
            details={"field": field, "hint_line_kind": LINE_KIND_ADJUSTMENT},
        )
    if number == 0 and not allow_zero:
        raise ValidationError(
            f"字段 {field} 必须大于 0（收到 {shown!r}）。", details={"field": field}
        )

    exponent = number.as_tuple().exponent
    assert isinstance(exponent, int)  # 前面已排除 NaN / Infinity
    if -exponent > scale:
        raise ValidationError(
            f"字段 {field} 的小数位数最多 {scale} 位（收到 {shown!r}）。"
            "超出的位数不会被四舍五入掉，请自己确认要存哪个值。",
            details={"field": field, "max_decimal_places": scale},
        )

    # 用 "f" 格式而不是 str()：`Decimal("1E+3")` 的 str() 是 "1E+3"，数不出位数。
    integer_digits = len(shown.lstrip("+-").split(".")[0])
    if integer_digits > MAX_INTEGER_DIGITS:
        raise ValidationError(
            f"字段 {field} 的整数部分最多 {MAX_INTEGER_DIGITS} 位（收到 {shown!r}）。",
            details={"field": field, "max_integer_digits": MAX_INTEGER_DIGITS},
        )
    return number


def format_decimal(value: Decimal, *, min_scale: int = 0) -> str:
    """把 `Decimal` 格式化成 API 要输出的字符串。

    数值列按固定 scale 存，所以 `12.345` 读回来是 `12.3450`。直接输出会让「原值进
    原值出」看起来不成立，因此这里去掉尾随零，再按列的性质补一个最小位数：
    数量输出 `12.345`、`10`，而金额与单价至少两位（`0.07`、`60.00`），不会出现
    `$5.8` 这种不像钱的写法。

    `normalize()` 会把 `100.00` 变成 `1E+2`，所以指数为正时要先拉回定点写法。
    """
    trimmed = value.normalize()
    if trimmed.as_tuple().exponent > 0:  # type: ignore[operator]
        trimmed = trimmed.quantize(Decimal(1))
    exponent = trimmed.as_tuple().exponent
    assert isinstance(exponent, int)
    if -exponent < min_scale:
        trimmed = trimmed.quantize(Decimal(1).scaleb(-min_scale))
    return str(trimmed)


def format_quantity(value: Decimal) -> str:
    return format_decimal(value)


def format_money(value: Decimal) -> str:
    return format_decimal(value, min_scale=2)


def normalize_purchase_unit(value: str) -> str | None:
    """把发票上的单位原文归一到一个 token；认不出返回 `None`。

    **认不出不是错误。** 单据上写着 `件/2` 或某个供应商自造的缩写时，记录仍然要能
    存下来——原文已经保住了，换算到步骤 09 再处理（`docs/imp_notes.md` Feature 2：
    缺少包装规格等换算依据时不生成伪标准价）。

    与步骤 03 的 `normalize_base_unit()` 的区别：那个函数对 `case` / `pack` **抛错**
    （它们不能当食材的基础单位），而采购单位本来就可以是 case，所以这里把包装单位
    归一成 token 并照常返回。
    """
    candidate = normalize_unit_token(value)
    if not candidate:
        return None
    if candidate in BASE_UNITS:
        return candidate
    # 包装单位的键里有 `fl oz` 这类带空格的写法吗？没有——所以用下划线前的写法
    # 查一次即可；`normalize_unit_token` 不会改写单字词。
    packaging = PACKAGING_UNIT_SYNONYMS.get(candidate.replace("_", " "))
    if packaging is not None:
        return packaging
    return PACKAGING_UNIT_SYNONYMS.get(candidate)


def parse_purchase_date(value: Any) -> date | None:
    """解析采购日期。`None` 与空字符串都表示「没有日期」，按用户确认允许留空。"""
    if value is None:
        return None
    # `update_purchase()` 把未修改的原值（一个 date）传回来时走这一支。
    # datetime 是 date 的子类，但「带时刻的采购日期」本身就是口径错误，挡住它。
    if isinstance(value, date) and not isinstance(value, datetime):
        return _check_date_range(value)
    if not isinstance(value, str):
        raise ValidationError(
            "purchase_date 必须是 YYYY-MM-DD 形式的字符串或 null。",
            details={"field": "purchase_date"},
        )
    text = value.strip()
    if not text:
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(
            f"purchase_date 必须是 YYYY-MM-DD 形式的日历日（收到 {text!r}）。",
            details={"field": "purchase_date"},
        ) from exc
    return _check_date_range(parsed)


def _check_date_range(parsed: date) -> date:
    """日期的合理范围。年份敲错或被 OCR 读错（2026 → 2062）会凭空多出一个月份。"""
    if parsed.year < EARLIEST_PURCHASE_YEAR:
        raise ValidationError(
            f"purchase_date 过早（{parsed.isoformat()}），请确认年份。",
            details={"field": "purchase_date"},
        )
    if parsed > date.today() + FUTURE_DATE_SLACK:
        raise ValidationError(
            f"purchase_date 不能是未来日期（收到 {parsed.isoformat()}）。"
            "年份容易敲错或被识别错，请确认后再录。",
            details={"field": "purchase_date"},
        )
    return parsed


def _require_text(value: Any, *, field: str, max_length: int) -> str:
    return require_text(value, field=field, max_length=max_length)


def _optional_text(value: Any, *, field: str, max_length: int) -> str | None:
    return optional_text(value, field=field, max_length=max_length)


def _require_choice(value: Any, *, field: str, allowed: Iterable[str]) -> str:
    """白名单取值。币种按大写比较（`usd` 与 `USD` 是同一个），其余按小写。"""
    return require_choice(
        value,
        field=field,
        allowed=allowed,
        case="upper" if field == "currency" else "lower",
    )


# --- 只读视图 ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PurchaseRevisionView:
    """一条修改日志的只读快照。`changed_fields` 已解析成字典。"""

    id: int
    changed_at: datetime
    action: str
    changed_fields: dict[str, dict[str, str | None]]
    reason: str | None

    @classmethod
    def of(cls, revision: PurchaseRevision) -> "PurchaseRevisionView":
        return cls(
            id=revision.id,
            changed_at=revision.changed_at,
            action=revision.action,
            changed_fields=json.loads(revision.changed_fields),
            reason=revision.reason,
        )


@dataclass(frozen=True, slots=True)
class SourceInvoiceView:
    """采购记录指回原始单据的最小引用（步骤 05 验收：能根据采购记录查到原始单据）。

    只给三个值：单据 id、行 id 与行号。够前端拼出一个链接去
    `GET /api/invoices/<invoice_id>` 看原件，也不用在每条采购记录里重复一份单据头。
    """

    invoice_id: int
    invoice_line_id: int
    line_no: int

    @classmethod
    def of(cls, purchase: Purchase) -> "SourceInvoiceView | None":
        line = purchase.invoice_line
        if line is None:
            return None
        return cls(
            invoice_id=line.invoice_id, invoice_line_id=line.id, line_no=line.line_no
        )


@dataclass(frozen=True, slots=True)
class PurchaseView:
    """采购记录的只读快照。

    数值保持 `Decimal`，由 API 层用 `format_quantity` / `format_money` 转成字符串；
    视图里不提前变成字符串，这样步骤 09 的统计可以直接拿它们做精确运算。
    """

    id: int
    line_kind: str
    # 调整行可以没有食材（整单折让不指向任何一种食材）。
    food_item: FoodItemView | None
    # 这条记录是从哪一张单据的哪一行确认出来的；手工录入与表格导入为 None。
    source_invoice: "SourceInvoiceView | None"
    purchase_date: date | None
    source_name: str | None
    original_description: str | None
    quantity: Decimal
    purchase_unit: str
    purchase_unit_normalized: str | None
    unit_price: Decimal
    line_amount: Decimal
    currency: str
    amount_mismatch: bool
    entry_source: str
    voided_at: datetime | None
    revision_count: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, purchase: Purchase) -> "PurchaseView":
        return cls(
            id=purchase.id,
            line_kind=purchase.line_kind,
            food_item=(
                FoodItemView.of(purchase.food_item)
                if purchase.food_item is not None
                else None
            ),
            source_invoice=SourceInvoiceView.of(purchase),
            purchase_date=purchase.purchase_date,
            source_name=purchase.source_name,
            original_description=purchase.original_description,
            quantity=purchase.quantity,
            purchase_unit=purchase.purchase_unit,
            purchase_unit_normalized=purchase.purchase_unit_normalized,
            unit_price=purchase.unit_price,
            line_amount=purchase.line_amount,
            currency=purchase.currency,
            amount_mismatch=purchase.amount_mismatch,
            entry_source=purchase.entry_source,
            voided_at=purchase.voided_at,
            revision_count=len(purchase.revisions),
            created_at=purchase.created_at,
            updated_at=purchase.updated_at,
        )


@dataclass(frozen=True, slots=True)
class PurchasePage:
    """一页采购记录。`total` 是符合条件的总条数，不是本页条数。"""

    items: tuple[PurchaseView, ...]
    total: int
    limit: int
    offset: int


# --- 服务 -------------------------------------------------------------------


class PurchaseService:
    """采购记录的录入、查询与纠错。无状态，可以按请求创建。"""

    def __init__(self, database: Database) -> None:
        self._database = database

    # --- 录入 ---

    def create_purchase(
        self,
        *,
        quantity: Any,
        purchase_unit: Any,
        unit_price: Any,
        line_amount: Any,
        food_item_id: Any = None,
        line_kind: Any = LINE_KIND_PURCHASE,
        purchase_date: Any = None,
        source_name: Any = None,
        original_description: Any = None,
        currency: Any = DEFAULT_CURRENCY,
        entry_source: str = ENTRY_SOURCE_MANUAL,
        invoice_line_id: int | None = None,
        accept_amount_mismatch: bool = False,
        session: Session | None = None,
    ) -> PurchaseView:
        """新增一条采购记录或调整行。每次采购追加，不覆盖旧价格。

        `line_kind="adjustment"` 用于退货、折让与其它调整：数量与金额可以为负，
        食材可以不填（整单折让）。调整行默认不出现在查询结果里，也不参与价格统计。

        `session` 供步骤 05 传入外层事务：发票行的状态变更与采购记录的写入必须
        共同提交或共同回滚，否则会出现「行标成已确认、采购却没存进去」。

        `invoice_line_id` 是这条记录的来源行。它上面有一条**排除已作废记录的局部
        唯一索引**（见 `models.Purchase.__table_args__`），所以同一行被确认两次时，
        第二次会撞约束而不是悄悄记两次价。
        """
        kind = _require_choice(line_kind, field="line_kind", allowed=LINE_KINDS)
        clean = self._validate_fields(
            line_kind=kind,
            quantity=quantity,
            purchase_unit=purchase_unit,
            unit_price=unit_price,
            line_amount=line_amount,
            purchase_date=purchase_date,
            source_name=source_name,
            original_description=original_description,
            currency=currency,
        )
        source = _require_choice(
            entry_source, field="entry_source", allowed=ENTRY_SOURCES
        )
        mismatch = self._check_amount(
            quantity=clean["quantity"],
            unit_price=clean["unit_price"],
            line_amount=clean["line_amount"],
            accept_amount_mismatch=accept_amount_mismatch,
        )

        with self._database.unit_of_work(session) as active:
            self._require_food_item_for_line(active, food_item_id, line_kind=kind)
            purchase = Purchase(
                line_kind=kind,
                food_item_id=food_item_id,
                purchase_date=clean["purchase_date"],
                source_name=clean["source_name"],
                source_name_normalized=clean["source_name_normalized"],
                original_description=clean["original_description"],
                original_description_normalized=clean[
                    "original_description_normalized"
                ],
                quantity=clean["quantity"],
                purchase_unit=clean["purchase_unit"],
                purchase_unit_normalized=clean["purchase_unit_normalized"],
                unit_price=clean["unit_price"],
                line_amount=clean["line_amount"],
                currency=clean["currency"],
                amount_mismatch=mismatch,
                entry_source=source,
                invoice_line_id=invoice_line_id,
            )
            PurchaseRepository(active).add(purchase)
            return PurchaseView.of(purchase)

    # --- 查询 ---

    def get_purchase(self, purchase_id: int) -> PurchaseView:
        with self._database.unit_of_work() as session:
            return PurchaseView.of(self._load_purchase(session, purchase_id))

    def search_purchases(
        self,
        *,
        food_item_id: int | None = None,
        source: str | None = None,
        date_from: Any = None,
        date_to: Any = None,
        undated: bool | None = None,
        kind: Any = LINE_KIND_PURCHASE,
        include_voided: bool = False,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
        session: Session | None = None,
    ) -> PurchasePage:
        """按食材、来源、日期区间查询采购历史（原始单位）。

        `undated=True` 返回「待补日期」清单：这些记录进不了步骤 09 的月度均价，
        不给一条路把它们找出来，老板就永远不知道它们存在。

        `kind` 默认只返回正常采购行。退货 / 折让等调整行要显式传 `"adjustment"`
        或 `"all"`——**对账必须用 `"all"`**，否则发票各行相加对不上单据总额。
        默认排除的理由见模块文档字符串第 4 条。
        """
        checked_kind = _require_choice(kind, field="kind", allowed=SEARCH_KINDS)
        checked_limit = self._require_limit(limit)
        checked_offset = self._require_offset(offset)
        parsed_from = parse_purchase_date(date_from) if date_from else None
        parsed_to = parse_purchase_date(date_to) if date_to else None
        if parsed_from and parsed_to and parsed_from > parsed_to:
            raise ValidationError(
                f"date_from（{parsed_from.isoformat()}）不能晚于 "
                f"date_to（{parsed_to.isoformat()}）。",
                details={"field": "date_from"},
            )
        source_normalized = (
            normalize_name(source) or None if source is not None else None
        )

        with self._database.unit_of_work(session) as active:
            if food_item_id is not None:
                self._require_existing_food_item(active, food_item_id)
            rows, total = PurchaseRepository(active).search(
                food_item_id=food_item_id,
                source_normalized=source_normalized,
                date_from=parsed_from,
                date_to=parsed_to,
                undated=undated,
                line_kind=None if checked_kind == KIND_ALL else checked_kind,
                include_voided=include_voided,
                limit=checked_limit,
                offset=checked_offset,
            )
            return PurchasePage(
                items=tuple(PurchaseView.of(row) for row in rows),
                total=total,
                limit=checked_limit,
                offset=checked_offset,
            )

    def export_purchases(self, **filters: Any) -> tuple[PurchaseView, ...]:
        """All matching current records in one snapshot; never just the UI page."""
        result: list[PurchaseView] = []
        with self._database.unit_of_work() as session:
            while True:
                page = self.search_purchases(**filters, limit=MAX_LIMIT,
                                             offset=len(result), session=session)
                if page.total > 10000:
                    raise ValidationError("一次最多导出 10000 条，请缩小日期或来源范围。")
                result.extend(page.items)
                if len(result) >= page.total:
                    return tuple(result)

    def list_revisions(self, purchase_id: int) -> list[PurchaseRevisionView]:
        with self._database.unit_of_work() as session:
            self._load_purchase(session, purchase_id)
            rows = PurchaseRevisionRepository(session).list_for_purchase(purchase_id)
            return [PurchaseRevisionView.of(row) for row in rows]

    # --- 纠错 ---

    def update_purchase(
        self,
        purchase_id: int,
        *,
        food_item_id: Any = UNSET,
        purchase_date: Any = UNSET,
        source_name: Any = UNSET,
        original_description: Any = UNSET,
        quantity: Any = UNSET,
        purchase_unit: Any = UNSET,
        unit_price: Any = UNSET,
        line_amount: Any = UNSET,
        currency: Any = UNSET,
        reason: Any = None,
        accept_amount_mismatch: bool = False,
    ) -> PurchaseView:
        """直接修改一条采购记录（含数值），并把改动前后的值写进修改日志。

        按 2026-11-10 的用户确认，纠错不强制作废重录：食材选错直接改，数字录错也
        直接改。**代价是上个月的月度均价会随修正变化**（修正后的值才是事实），所以
        每次修改都必须留下日志，否则老板下次看到不同的数字时无从解释。

        未出现的字段保持原值；显式传 `null` 表示清空可选字段。什么都没变时不写日志，
        也不更新 `updated_at`——「点了保存但没改东西」不该在历史里留一条空记录。
        """
        clean_reason = _optional_text(
            reason, field="reason", max_length=REVISION_REASON_MAX_LENGTH
        )

        with self._database.unit_of_work() as session:
            purchase = self._load_purchase(session, purchase_id)
            if purchase.voided_at is not None:
                raise ConflictError(
                    f"采购记录 {purchase_id} 已作废，不能再修改；请新录一条。",
                    details={"voided_at": purchase.voided_at.isoformat()},
                )

            target_food_item_id = purchase.food_item_id
            if food_item_id is not UNSET:
                self._require_food_item_for_line(
                    session, food_item_id, line_kind=purchase.line_kind
                )
                target_food_item_id = food_item_id

            clean = self._validate_fields(
                line_kind=purchase.line_kind,
                quantity=(
                    purchase.quantity if quantity is UNSET else quantity
                ),
                purchase_unit=(
                    purchase.purchase_unit
                    if purchase_unit is UNSET
                    else purchase_unit
                ),
                unit_price=(
                    purchase.unit_price if unit_price is UNSET else unit_price
                ),
                line_amount=(
                    purchase.line_amount if line_amount is UNSET else line_amount
                ),
                purchase_date=(
                    purchase.purchase_date
                    if purchase_date is UNSET
                    else purchase_date
                ),
                source_name=(
                    purchase.source_name if source_name is UNSET else source_name
                ),
                original_description=(
                    purchase.original_description
                    if original_description is UNSET
                    else original_description
                ),
                currency=(purchase.currency if currency is UNSET else currency),
            )
            mismatch = self._check_amount(
                quantity=clean["quantity"],
                unit_price=clean["unit_price"],
                line_amount=clean["line_amount"],
                accept_amount_mismatch=accept_amount_mismatch,
            )

            changed = self._diff(
                purchase,
                food_item_id=target_food_item_id,
                clean=clean,
                amount_mismatch=mismatch,
            )
            if not changed:
                return PurchaseView.of(purchase)

            purchase.food_item_id = target_food_item_id
            purchase.purchase_date = clean["purchase_date"]
            purchase.source_name = clean["source_name"]
            purchase.source_name_normalized = clean["source_name_normalized"]
            purchase.original_description = clean["original_description"]
            purchase.original_description_normalized = clean[
                "original_description_normalized"
            ]
            purchase.quantity = clean["quantity"]
            purchase.purchase_unit = clean["purchase_unit"]
            purchase.purchase_unit_normalized = clean["purchase_unit_normalized"]
            purchase.unit_price = clean["unit_price"]
            purchase.line_amount = clean["line_amount"]
            purchase.currency = clean["currency"]
            purchase.amount_mismatch = mismatch

            PurchaseRevisionRepository(session).add(
                PurchaseRevision(
                    purchase_id=purchase.id,
                    action=REVISION_ACTION_UPDATE,
                    changed_fields=json.dumps(changed, ensure_ascii=False),
                    reason=clean_reason,
                )
            )
            session.flush()
            session.refresh(purchase)
            return PurchaseView.of(purchase)

    def void_purchase(
        self,
        purchase_id: int,
        *,
        reason: Any = None,
        session: Session | None = None,
    ) -> None:
        """作废一条采购记录（软删）。

        不硬删：步骤 05 的发票行要能追溯到它，物理删掉就只剩一个悬空的行状态。
        已作废的记录默认不出现在查询结果里，也不该参与步骤 09 的统计。

        `session` 供步骤 05 的「撤销确认」传入外层事务：作废采购记录与把发票行退回
        待确认必须共同提交，否则会出现「行退回去了、采购记录还在」这种重复记价。
        """
        clean_reason = _optional_text(
            reason, field="reason", max_length=REVISION_REASON_MAX_LENGTH
        )
        with self._database.unit_of_work(session) as session:
            purchase = self._load_purchase(session, purchase_id)
            if purchase.voided_at is not None:
                raise ConflictError(
                    f"采购记录 {purchase_id} 已经作废过了。",
                    details={"voided_at": purchase.voided_at.isoformat()},
                )
            moment = utcnow()
            purchase.voided_at = moment
            PurchaseRevisionRepository(session).add(
                PurchaseRevision(
                    purchase_id=purchase.id,
                    action=REVISION_ACTION_VOID,
                    changed_fields=json.dumps(
                        {"voided_at": {"before": None, "after": moment.isoformat()}},
                        ensure_ascii=False,
                    ),
                    reason=clean_reason,
                )
            )
            session.flush()

    # --- 内部辅助 ---

    @staticmethod
    def _validate_fields(
        *,
        line_kind: str,
        quantity: Any,
        purchase_unit: Any,
        unit_price: Any,
        line_amount: Any,
        purchase_date: Any,
        source_name: Any,
        original_description: Any,
        currency: Any,
    ) -> dict[str, Any]:
        """校验并规范化所有业务字段。新增与修改共用，于是两条路口径一致。

        `line_kind` 只影响数值的**符号**规则：调整行的数量、单价与行金额可以为负，
        且行金额不能为 0（0 元的退货/折让没有意义）。其余字段的规则两类行一样。
        """
        is_adjustment = line_kind == LINE_KIND_ADJUSTMENT
        clean_source = _optional_text(
            source_name, field="source_name", max_length=SOURCE_NAME_MAX_LENGTH
        )
        clean_description = _optional_text(
            original_description,
            field="original_description",
            max_length=DESCRIPTION_MAX_LENGTH,
        )
        clean_unit = _require_text(
            purchase_unit,
            field="purchase_unit",
            max_length=PURCHASE_UNIT_MAX_LENGTH,
        )
        clean_amount = parse_decimal(
            line_amount,
            field="line_amount",
            scale=AMOUNT_SCALE,
            # 调整行的金额必须非 0：0 元的退货或折让没有意义，放过只会留下一条
            # 既不影响对账也说不清是什么的行。
            allow_zero=not is_adjustment,
            allow_negative=is_adjustment,
        )
        return {
            "quantity": parse_decimal(
                quantity,
                field="quantity",
                scale=QUANTITY_SCALE,
                allow_zero=False,
                allow_negative=is_adjustment,
            ),
            # 单价与行金额允许为 0：赠品、促销与「买十送一」的那一行就是 0 元
            # （数量仍然必须大于 0）。是否计入步骤 09 的加权均价由那一步决定。
            "unit_price": parse_decimal(
                unit_price,
                field="unit_price",
                scale=UNIT_PRICE_SCALE,
                allow_zero=True,
                allow_negative=is_adjustment,
            ),
            "line_amount": clean_amount,
            "purchase_date": parse_purchase_date(purchase_date),
            "source_name": clean_source,
            "source_name_normalized": (
                normalize_name(clean_source) if clean_source else None
            ),
            "original_description": clean_description,
            "original_description_normalized": (
                normalize_name(clean_description) if clean_description else None
            ),
            "purchase_unit": clean_unit,
            "purchase_unit_normalized": normalize_purchase_unit(clean_unit),
            "currency": _require_choice(
                currency, field="currency", allowed=CURRENCIES
            ),
        }

    @staticmethod
    def _check_amount(
        *,
        quantity: Decimal,
        unit_price: Decimal,
        line_amount: Decimal,
        accept_amount_mismatch: bool,
    ) -> bool:
        """校验「数量 × 单价 ≈ 行金额」。返回「是否带着差额入账」。

        乘法用 `Decimal`，结果是精确值（不量化），于是差额本身也是精确的。
        """
        expected = quantity * unit_price
        difference = abs(expected - line_amount)
        if difference <= AMOUNT_TOLERANCE:
            return False
        if accept_amount_mismatch:
            return True
        raise AmountMismatchError(
            f"数量 × 单价 = {format_money(expected)}，但行金额填的是 "
            f"{format_money(line_amount)}，差 {format_money(difference)}，"
            f"超过了允许的误差 {format_money(AMOUNT_TOLERANCE)}。"
            "请对照单据核对。如果单据上的金额确实如此，可以选择按填写的行金额入账。",
            details={
                "field": "line_amount",
                "expected": format_money(expected),
                "line_amount": format_money(line_amount),
                "difference": format_money(difference),
                "tolerance": format_money(AMOUNT_TOLERANCE),
            },
        )

    @staticmethod
    def _diff(
        purchase: Purchase,
        *,
        food_item_id: int,
        clean: dict[str, Any],
        amount_mismatch: bool,
    ) -> dict[str, dict[str, str | None]]:
        """算出哪些字段变了，值一律转成字符串。

        数值以字符串入日志，不经 JSON 的浮点数中转——否则日志自己就会丢精度，
        而它恰恰是用来解释精确数字的。
        """

        def text(value: Any) -> str | None:
            if value is None:
                return None
            if isinstance(value, Decimal):
                return format_decimal(value, min_scale=2)
            if isinstance(value, date):
                return value.isoformat()
            if isinstance(value, bool):
                return "true" if value else "false"
            return str(value)

        candidates: dict[str, tuple[Any, Any]] = {
            # line_kind 不在此列：行类别不可改。退货行录成了采购行（或反之）属于
            # 录错了类别，作废重录比原地改类别清楚——原地改会让同一条记录的历史
            # 跨越两种语义，统计口径也跟着变。
            "food_item_id": (purchase.food_item_id, food_item_id),
            "purchase_date": (purchase.purchase_date, clean["purchase_date"]),
            "source_name": (purchase.source_name, clean["source_name"]),
            "original_description": (
                purchase.original_description,
                clean["original_description"],
            ),
            "quantity": (purchase.quantity, clean["quantity"]),
            "purchase_unit": (purchase.purchase_unit, clean["purchase_unit"]),
            "unit_price": (purchase.unit_price, clean["unit_price"]),
            "line_amount": (purchase.line_amount, clean["line_amount"]),
            "currency": (purchase.currency, clean["currency"]),
            "amount_mismatch": (purchase.amount_mismatch, amount_mismatch),
        }
        changed: dict[str, dict[str, str | None]] = {}
        for field, (before, after) in candidates.items():
            if before == after:
                continue
            changed[field] = {"before": text(before), "after": text(after)}
        return changed

    @staticmethod
    def _require_existing_food_item(session: Session, food_item_id: Any) -> None:
        """校验 food_item_id 的类型与存在性（不管行类别）。

        不存在时抛 400 而不是 404：被操作的资源是采购记录，食材只是请求数据里的一个
        引用，写错了属于请求不合法（与步骤 03 对 `category_id` 的处理一致）。

        查询的筛选参数也走这里——那里只有一个 id，没有「行」，谈不上行类别。
        """
        if not isinstance(food_item_id, int) or isinstance(food_item_id, bool):
            raise ValidationError(
                "food_item_id 必须是整数或 null。",
                details={"field": "food_item_id"},
            )
        if FoodItemRepository(session).get(food_item_id) is None:
            raise ValidationError(
                f"食材 {food_item_id} 不存在。", details={"field": "food_item_id"}
            )

    @staticmethod
    def _require_food_item_for_line(
        session: Session, food_item_id: Any, *, line_kind: str
    ) -> None:
        """一条记录的食材引用：**只有调整行可以不填**。

        整单折让在单据上不指向任何一种食材，强行挂到某个食材上会污染那个食材的采购
        历史。正常采购行必须指向食材——数据库那一列放宽成可空是为了能表达整单级调整，
        不是放弃这条规则，所以规则在这里强制。
        """
        if food_item_id is None:
            if line_kind == LINE_KIND_ADJUSTMENT:
                return
            raise ValidationError(
                "正常采购行必须指定 food_item_id。"
                f"不指向具体食材的整单折让请用 line_kind=\"{LINE_KIND_ADJUSTMENT}\"。",
                details={"field": "food_item_id"},
            )
        PurchaseService._require_existing_food_item(session, food_item_id)

    @staticmethod
    def _load_purchase(session: Session, purchase_id: int) -> Purchase:
        purchase = PurchaseRepository(session).get(purchase_id)
        if purchase is None:
            raise NotFoundError(f"采购记录 {purchase_id} 不存在。")
        return purchase

    @staticmethod
    def _require_limit(limit: Any) -> int:
        return require_limit(limit, max_limit=MAX_LIMIT)

    @staticmethod
    def _require_offset(offset: Any) -> int:
        return require_offset(offset)


__all__ = [
    "AMOUNT_TOLERANCE",
    "KIND_ALL",
    "LINE_KINDS",
    "LINE_KIND_ADJUSTMENT",
    "LINE_KIND_PURCHASE",
    "SEARCH_KINDS",
    "CURRENCIES",
    "DEFAULT_CURRENCY",
    "ENTRY_SOURCES",
    "ENTRY_SOURCE_MANUAL",
    "PACKAGING_UNIT_SYNONYMS",
    "ENTRY_SOURCE_INVOICE",
    "PurchasePage",
    "PurchaseRevisionView",
    "PurchaseService",
    "PurchaseView",
    "SourceInvoiceView",
    "format_decimal",
    "format_money",
    "format_quantity",
    "normalize_purchase_unit",
    "parse_decimal",
    "parse_purchase_date",
]
