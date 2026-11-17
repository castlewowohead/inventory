"""食材与分类的业务服务（步骤 03）。

职责与边界：

- **事务边界在本层**：每个公开方法用 `Database.unit_of_work()` 包住整次操作
  （见 src/db.py 与 README 第 10 节）。Repository 只拿 Session 做读写。
- **名称规范化只有这一处实现**：写入时落到 `*_normalized` 与 `search_text` 列，
  查询时对关键字用同一个函数，于是「大小写不同、全角半角不同、空格多少不同」
  不会变成两条食材，也不会搜不到。
- **ORM 对象不流出本层**：方法返回下面的只读视图（`CategoryView` / `FoodItemView`），
  在事务内构造完成。API 层因此拿不到游离的 ORM 对象，也不会触发延迟加载。

本模块**不做**单位换算，只校验「基础单位是不是白名单里的那几个」。换算与包装规格属于
步骤 09 的 `services/units.py`（金额与数量的 Decimal 精度属于步骤 04 的采购记录）。

⚠️ **模糊匹配不在这里，而且不能在这里**（步骤 10）。本模块是单位与名称**词表的
owner**，`services/units.py` 与 `services/matching.py` 都 import 它；反向再 import
回来就成了循环。所以步骤 10 的模糊匹配、最近/常用与候选排序落在
`services/suggestions.py`，它依赖本模块，本模块对它一无所知——与 `units.py` /
`prices.py` 的依赖方向一致。本模块在步骤 10 只多了一个 `never_purchased` 筛选
（10.15，用来回收孤立食材）。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.db import Database
from src.errors import ConflictError, NotFoundError, ValidationError
from src.models import (
    BASE_UNIT_MAX_LENGTH,
    CATEGORY_NAME_MAX_LENGTH,
    NAME_MAX_LENGTH,
    SPECIFICATION_MAX_LENGTH,
    Category,
    FoodItem,
)
from src.repositories.ingredients import CategoryRepository, FoodItemRepository
from src.services.validation import (
    collapse_whitespace,
    optional_text,
    require_limit,
    require_offset,
    require_text,
)

# --- 基础单位 ---------------------------------------------------------------
#
# 步骤 03 只需要「这条食材按什么单位计量」这一个标识，因此用固定白名单校验：
# 写错的单位在录入时就被拒绝，数据库里不会留下 lb / lbs / LB / 磅 四种写法。
#
# 步骤 09 的 `services/units.py` 会接管单位体系（换算系数、包装规格、制式），
# 届时以它为准；食材的 `base_unit` 取值必须始终是那套规范单位的子集。

BASE_UNITS: Final[tuple[str, ...]] = (
    "lb",
    "oz",
    "g",
    "kg",
    "gallon",
    "qt",
    "pt",
    "fl_oz",
    "l",
    "ml",
    "each",
    "dozen",
)

# 常见同义写法 → 规范 token。刻意保持短小：它只负责消除同一单位的拼写分叉，
# 不负责解析发票上的任意单位文本（那是步骤 09 的事）。
BASE_UNIT_SYNONYMS: Final[dict[str, str]] = {
    "lbs": "lb",
    "pound": "lb",
    "pounds": "lb",
    "磅": "lb",
    "ounce": "oz",
    "ounces": "oz",
    "gram": "g",
    "grams": "g",
    "gm": "g",
    "克": "g",
    "kgs": "kg",
    "kilogram": "kg",
    "kilograms": "kg",
    "公斤": "kg",
    "千克": "kg",
    "gal": "gallon",
    "gallons": "gallon",
    "quart": "qt",
    "quarts": "qt",
    "pint": "pt",
    "pints": "pt",
    "fl oz": "fl_oz",
    "floz": "fl_oz",
    "fluid ounce": "fl_oz",
    "fluid ounces": "fl_oz",
    "liter": "l",
    "liters": "l",
    "litre": "l",
    "litres": "l",
    "升": "l",
    "milliliter": "ml",
    "milliliters": "ml",
    "millilitre": "ml",
    "毫升": "ml",
    "ea": "each",
    "pc": "each",
    "pcs": "each",
    "piece": "each",
    "pieces": "each",
    "个": "each",
    # ⚠️ 这四个是步骤 09 按真实单据补的：`samples/real/sample_grocery_supplier_invoices.pdf`
    # 上写着 `72 heads`（球生菜）与 `24 bunches`（香菜）。一颗生菜、一把香菜就是
    # 一件，按「个」计价是有意义的；不补的话这些行的单位永远认不出来，老板得逐行
    # 改单位。⚠️ 容器名词（can / cup / jar / bottle）**不在这里**——那些是包装单位，
    # 一罐里有多少必须先有箱规，见下面 PACKAGING_UNITS 的说明。
    "head": "each",
    "heads": "each",
    "bunch": "each",
    "bunches": "each",
    "颗": "each",
    "把": "each",
    "doz": "dozen",
    "dozens": "dozen",
    "打": "dozen",
}

# 包装单位：它们不是基础单位。必须先知道「1 case = 多少 lb」才能算出标准化价格，
# 包装规格与换算由步骤 09 的 `services/units.py` + `pack_specs` 表承担。单独识别
# 它们是为了给出能看懂的错误，而不是笼统地说「单位无效」
# （docs/imp_notes.md Feature 2：没有包装规格不能换算）。
#
# ⚠️ **容器名词（can / cup / jar / bottle）是步骤 09 按真实单据补的，而且刻意归到
# 这里而不是基础单位**。依据与理由：
#
# - `samples/real/sample_grocery_supplier_invoices.pdf` 上写着 `48 cups`（原味酸奶）
#   与 `48 cans`（甜玉米罐头）。
# - `cup` 在美制里**也是一个体积单位**（1 US cup = 236.588 mL）。把它当体积单位
#   加进 `BASE_UNITS` 会出大错：单据上的「48 cups 酸奶」指的是 **48 个杯装**，
#   不是 48 杯容积。两种读法差的正好是一杯装里有多少——而那恰恰是箱规要回答的
#   问题。所以 `cup` 走箱规（「1 cup = 170 g」或「1 cup = 1 each」），由人说一次。
# - 同理 `can`：一罐玉米是 15 oz 还是一件，取决于老板怎么盘货，系统不猜。
PACKAGING_UNITS: Final[frozenset[str]] = frozenset(
    {
        "case",
        "cases",
        "cs",
        "pack",
        "packs",
        "pk",
        "package",
        "box",
        "boxes",
        "carton",
        "cartons",
        "bag",
        "bags",
        "can",
        "cans",
        "cup",
        "cups",
        "jar",
        "jars",
        "bottle",
        "bottles",
        "箱",
        "包",
        "袋",
        "件",
        "罐",
        "杯",
        "瓶",
    }
)

# --- 搜索与分页 -------------------------------------------------------------

DEFAULT_LIMIT: Final[int] = 20
MAX_LIMIT: Final[int] = 100
MAX_QUERY_LENGTH: Final[int] = 100

# 「未分类」那条分类的锚点（步骤 10.8）。⚠️ 判定用它，**不拿显示名做判据**——
# `imp_notes.md` 允许老板改分类名，按显示名找这条分类的话他改个名全系统就乱了。
#
# ⚠️ 这个字面值在三处各写了一次：这里、`services/matching.py` 的
# `UNCATEGORIZED_NAME_NORMALIZED`、以及 seed 它的那条迁移。不互相 import 是有
# 原因的——`matching.py` import 本模块（词表在这里），迁移是历史快照不 import
# 应用代码。一致性由 `tests/` 断言，不靠人记得（同 `normalization.py` 的
# `_TRAILING_UNIT_WORDS`）。
UNCATEGORIZED_NAME_NORMALIZED: Final[str] = "uncategorized"


class _Unset:
    """「这个字段没有出现在请求里」的标记。

    局部更新必须区分「没传这个字段」（保持原值）与「显式传了 null」（清空），
    用 None 无法表达这个差别。
    """

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试输出
        return "UNSET"


UNSET: Final[Any] = _Unset()


def normalize_name(value: str) -> str:
    """名称的规范化形式：用于唯一判定与搜索匹配。

    三步，顺序有意义：

    1. NFKC —— 把全角字母数字、全角空格、兼容字符折叠成标准形式，于是
       `Ｇｒｏｕｎｄ　Ｂｅｅｆ`（中文输入法下很常见）与 `Ground Beef` 相同；
    2. 按空白切分再用单个空格拼接 —— 吸收首尾空格、重复空格、制表符与换行；
    3. casefold —— 比 `lower()` 更彻底的大小写折叠，对非英文字母同样有效。

    返回值只用于比较与检索，**不用于展示**：展示一律用用户输入的原值。
    """
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def build_search_text(name: str, name_zh: str | None, specification: str | None) -> str:
    """基础搜索的检索目标：英文名 + 中文名 + 规格的规范化拼接。

    拼成一列，搜索就只需要一个 LIKE 条件，一条食材也只会出现一次。代价是跨字段的
    连续匹配有限（例如同时跨「中文名尾部 + 规格开头」的关键字匹配不到），
    跨字段的连续匹配因此有限（例如同时跨「中文名尾部 + 规格开头」的关键字），
    步骤 10 的模糊层改成把这一列取回内存按相似度打分，绕开了这个限制
    （`services/suggestions.py`）；本列的生成规则没变。
    """
    parts = [normalize_name(part) for part in (name, name_zh, specification) if part]
    return " ".join(part for part in parts if part)


def normalize_unit_token(value: str | None) -> str:
    """单位原文 → 候选 token。**只做归一，不判断合法性**，空输入返回空串。

    四步：`normalize_name` → 去掉尾部句点 → 同义词映射 → 空格换下划线
    （让 `fl oz` 与 `fl_oz` 落到同一个 token）。

    ⚠️ **为什么要去尾部句点**：真实供应商发票上印的是 `lbs.`——步骤 07 的 6 张
    印刷件 Ground Truth 里**每一行**的单位列都带这个句点。不去掉的话 `lbs.` 既
    不在同义词表里也不在白名单里，这些行的单位全部"认不出"，于是步骤 09 一条
    价格也算不出来，老板得为每一行重新敲一次单位。句点是排版习惯，不是单位的
    一部分。

    三个调用方共用本函数（基础单位校验、采购单位归一、步骤 09 的换算层），
    于是「什么写法算同一个单位」只有一处定义。分成三份的后果是同一个单位在录入时
    和统计时归到不同 token——这种错不报错，只会让一部分采购记录静默算不进均价。
    """
    candidate = normalize_name(value or "")
    if not candidate:
        return ""
    candidate = candidate.rstrip(".。").strip()
    candidate = BASE_UNIT_SYNONYMS.get(candidate, candidate)
    return candidate.replace(" ", "_")


def normalize_base_unit(value: str) -> str:
    """把用户输入的单位归一到白名单 token；不认识就抛 ValidationError。"""
    candidate = normalize_unit_token(value)
    if not candidate:
        raise ValidationError("基础单位不能为空。", details={"field": "base_unit"})
    if candidate in BASE_UNITS:
        return candidate
    if candidate in PACKAGING_UNITS or candidate.replace("_", " ") in PACKAGING_UNITS:
        raise ValidationError(
            f"{value!r} 是包装单位，不能作为基础单位："
            "必须先知道一箱/一包折合多少基础单位，才能比较价格。"
            f"请选择基础单位（{', '.join(BASE_UNITS)}）；包装规格另行维护，"
            "确定一箱/一包折合多少之后才能比较价格。",
            details={"field": "base_unit", "allowed": list(BASE_UNITS)},
        )
    raise ValidationError(
        f"基础单位 {value!r} 不在支持范围内，可选值：{', '.join(BASE_UNITS)}。",
        details={"field": "base_unit", "allowed": list(BASE_UNITS)},
    )


# --- 只读视图 ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CategoryView:
    """分类的只读快照。在事务内构造，可以安全地交给 API 层序列化。"""

    id: int
    name: str
    name_zh: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, category: Category) -> "CategoryView":
        return cls(
            id=category.id,
            name=category.name,
            name_zh=category.name_zh,
            created_at=category.created_at,
            updated_at=category.updated_at,
        )


@dataclass(frozen=True, slots=True)
class FoodItemView:
    """标准食材的只读快照，含所属分类（搜索结果要直接显示分类与基础单位）。

    `needs_review` 为真表示**这条是系统按关键词自动建出来的，还没人确认过**
    （步骤 10.13）：名字是从供应商描述里剥的、分类是本地词表猜的。界面要把它和
    老板自己建的食材区分开——不标出来的话，一个猜错的分类会一直装成"他自己选的"。
    """

    id: int
    name: str
    name_zh: str | None
    specification: str | None
    base_unit: str
    category: CategoryView
    needs_review: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, food_item: FoodItem) -> "FoodItemView":
        return cls(
            id=food_item.id,
            name=food_item.name,
            name_zh=food_item.name_zh,
            specification=food_item.specification,
            base_unit=food_item.base_unit,
            category=CategoryView.of(food_item.category),
            needs_review=food_item.needs_review,
            created_at=food_item.created_at,
            updated_at=food_item.updated_at,
        )


@dataclass(frozen=True, slots=True)
class FoodItemPage:
    """一页搜索结果。`total` 是符合条件的总条数，不是本页条数。"""

    items: tuple[FoodItemView, ...]
    total: int
    limit: int
    offset: int


# --- 服务 -------------------------------------------------------------------


class IngredientService:
    """食材与分类的维护与查询。

    无状态：只持有 `Database`，可以按请求创建。
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    # --- 分类 ---

    def list_categories(self) -> list[CategoryView]:
        with self._database.unit_of_work() as session:
            repository = CategoryRepository(session)
            return [CategoryView.of(row) for row in repository.list_all()]

    def create_category(self, *, name: str, name_zh: str | None = None) -> CategoryView:
        clean_name = self._require_text(
            name, field="name", max_length=CATEGORY_NAME_MAX_LENGTH
        )
        clean_name_zh = self._optional_text(
            name_zh, field="name_zh", max_length=CATEGORY_NAME_MAX_LENGTH
        )
        normalized = normalize_name(clean_name)

        with self._database.unit_of_work() as session:
            repository = CategoryRepository(session)
            existing = repository.find_by_normalized_name(normalized)
            if existing is not None:
                raise self._category_conflict(existing)
            category = Category(
                name=clean_name, name_normalized=normalized, name_zh=clean_name_zh
            )
            try:
                repository.add(category)
            except IntegrityError as exc:
                raise self._integrity_conflict("分类名称已存在。", "name") from exc
            return CategoryView.of(category)

    def update_category(
        self,
        category_id: int,
        *,
        name: Any = UNSET,
        name_zh: Any = UNSET,
    ) -> CategoryView:
        with self._database.unit_of_work() as session:
            repository = CategoryRepository(session)
            category = repository.get(category_id)
            if category is None:
                raise NotFoundError(f"分类 {category_id} 不存在。")

            if not isinstance(name, _Unset):
                clean_name = self._require_text(
                    name, field="name", max_length=CATEGORY_NAME_MAX_LENGTH
                )
                normalized = normalize_name(clean_name)
                conflict = repository.find_by_normalized_name(normalized)
                if conflict is not None and conflict.id != category.id:
                    raise self._category_conflict(conflict)
                category.name = clean_name
                category.name_normalized = normalized
            if not isinstance(name_zh, _Unset):
                category.name_zh = self._optional_text(
                    name_zh, field="name_zh", max_length=CATEGORY_NAME_MAX_LENGTH
                )

            try:
                session.flush()
            except IntegrityError as exc:
                raise self._integrity_conflict("分类名称已存在。", "name") from exc
            return CategoryView.of(category)

    def delete_category(self, category_id: int) -> None:
        """删除分类。仍被食材引用时拒绝，不做级联删除。"""
        with self._database.unit_of_work() as session:
            repository = CategoryRepository(session)
            category = repository.get(category_id)
            if category is None:
                raise NotFoundError(f"分类 {category_id} 不存在。")
            if category.name_normalized == UNCATEGORIZED_NAME_NORMALIZED:
                # ⚠️ 「未分类」是系统锚点，不是普通分类（步骤 10.8）。删掉它之后，
                # 10.C 的关键词猜不出分类时就无处可落，而 `category_id` 是 NOT NULL
                # 外键——表现是发票逐行确认那一屏突然不能自动建食材了，而且不报
                # 「分类没了」，只报「建不出来」。**改名随便改**，判定走的是
                # `name_normalized`，不是显示名。
                raise ConflictError(
                    f"{category.name!r} 是系统保留的「未分类」，不能删除："
                    "发票里认不出分类的食材要落在它上面。想换个说法的话改名即可，"
                    "系统不按显示名识别它。",
                    details={"field": "id", "reserved": True},
                )
            in_use = repository.count_food_items(category_id)
            if in_use:
                raise ConflictError(
                    f"分类 {category.name!r} 下还有 {in_use} 种食材，不能删除；"
                    "请先把这些食材改到其它分类。",
                    details={"food_item_count": in_use},
                )
            try:
                repository.delete(category)
            except IntegrityError as exc:
                # 外键兜底：计数与删除之间若有并发写入，由数据库拦住。
                raise ConflictError(f"分类 {category.name!r} 仍被引用，不能删除。") from exc

    # --- 食材 ---

    def search_food_items(
        self,
        *,
        query: str | None = None,
        category_id: int | None = None,
        never_purchased: bool | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> FoodItemPage:
        """按名称（中/英/规格）搜索并可按分类筛选。关键字为空时就是分页列表。

        `never_purchased=True` 只留下**一条采购记录都没有引用过**的食材（步骤 10.15）。
        它的用处是回收孤立食材：10.D 把「新建食材」从"确认那一刻才落库"改成"填完即
        落库"之后，中途放弃的新建会在库里留下没人买过的主数据。它们**不进价格历史**
        （统计只读 `purchases`），所以留着不污染任何数字，但该有个地方能把它们找出来。

        ⚠️ **「从没买过」不等于「一定删得掉」**：食材还可能被**发票草稿行**引用，
        那种情况下删除仍然会按步骤 03 的口径返回 409。这个筛选是清理用的入口，
        不是一张"可删清单"——把它说成后者，老板点下去会撞一串 409。
        """
        checked_limit = self._require_limit(limit)
        checked_offset = self._require_offset(offset)
        normalized_query = self._normalize_query(query)

        with self._database.unit_of_work() as session:
            if category_id is not None:
                self._require_category(session, category_id)
            repository = FoodItemRepository(session)
            rows, total = repository.search(
                query_normalized=normalized_query,
                category_id=category_id,
                never_purchased=never_purchased,
                limit=checked_limit,
                offset=checked_offset,
            )
            return FoodItemPage(
                items=tuple(FoodItemView.of(row) for row in rows),
                total=total,
                limit=checked_limit,
                offset=checked_offset,
            )

    def get_food_item(self, food_item_id: int) -> FoodItemView:
        with self._database.unit_of_work() as session:
            return FoodItemView.of(self._load_food_item(session, food_item_id))

    def create_food_item(
        self,
        *,
        name: str,
        category_id: int,
        base_unit: str,
        name_zh: str | None = None,
        specification: str | None = None,
        needs_review: bool = False,
        allow_purchase_unit_base: bool = False,
        session: Session | None = None,
    ) -> FoodItemView:
        """快速新增：只要求名称、分类与基础单位，其余可选。

        `session` 供步骤 05 的「确认发票行时当场新建食材」传入外层事务：新建食材与
        确认那一行必须共同提交，否则确认失败会留下一条谁也没要的食材。

        `needs_review=True` **只给步骤 10.C 的自动建食材用**，不开放给接口：
        那条路径上名字是从供应商描述里剥的、分类是词表猜的，必须标出来等人确认。
        老板自己在界面上建的食材是他亲手填的，不需要再确认一次。
        """
        clean_name = self._require_text(name, field="name", max_length=NAME_MAX_LENGTH)
        clean_name_zh = self._optional_text(
            name_zh, field="name_zh", max_length=NAME_MAX_LENGTH
        )
        clean_specification = self._optional_text(
            specification, field="specification", max_length=SPECIFICATION_MAX_LENGTH
        )
        clean_base = self._require_text(
            base_unit, field="base_unit", max_length=BASE_UNIT_MAX_LENGTH
        )
        # Invoice confirmation has already normalized its purchase unit. A package
        # token can be the first observed unit, but stays unpriced until its pack
        # size is known; the ordinary food-item form keeps the stricter rule.
        unit = normalize_unit_token(clean_base) if allow_purchase_unit_base else normalize_base_unit(clean_base)
        if allow_purchase_unit_base and unit not in BASE_UNITS and unit not in PACKAGING_UNITS:
            raise ValidationError("采购单位不能用作食材的首个单位。", details={"field": "base_unit"})

        with self._database.unit_of_work(session) as session:
            self._require_category(session, category_id)
            repository = FoodItemRepository(session)
            self._ensure_names_available(repository, name=clean_name, name_zh=clean_name_zh)
            food_item = FoodItem(
                name=clean_name,
                name_normalized=normalize_name(clean_name),
                name_zh=clean_name_zh,
                name_zh_normalized=(
                    normalize_name(clean_name_zh) if clean_name_zh else None
                ),
                specification=clean_specification,
                base_unit=unit,
                category_id=category_id,
                needs_review=bool(needs_review),
                search_text=build_search_text(
                    clean_name, clean_name_zh, clean_specification
                ),
            )
            try:
                repository.add(food_item)
            except IntegrityError as exc:
                raise self._integrity_conflict(
                    "该名称已被另一条食材使用。", "name"
                ) from exc
            return FoodItemView.of(food_item)

    def update_food_item(
        self,
        food_item_id: int,
        *,
        name: Any = UNSET,
        name_zh: Any = UNSET,
        specification: Any = UNSET,
        base_unit: Any = UNSET,
        category_id: Any = UNSET,
    ) -> FoodItemView:
        """局部更新。未出现的字段保持原值，显式传 null 表示清空可选字段。"""
        with self._database.unit_of_work() as session:
            repository = FoodItemRepository(session)
            food_item = self._load_food_item(session, food_item_id)

            # 先算出目标值并查完重复，**之后**才改对象。顺序反了的话，改完对象再查重复
            # 会触发 SQLAlchemy 的 autoflush：唯一约束先以 IntegrityError 抛出来，
            # 调用方就拿不到「和哪一条冲突」这个信息了。
            new_name = (
                food_item.name
                if isinstance(name, _Unset)
                else self._require_text(name, field="name", max_length=NAME_MAX_LENGTH)
            )
            new_name_zh = (
                food_item.name_zh
                if isinstance(name_zh, _Unset)
                else self._optional_text(
                    name_zh, field="name_zh", max_length=NAME_MAX_LENGTH
                )
            )
            new_specification = (
                food_item.specification
                if isinstance(specification, _Unset)
                else self._optional_text(
                    specification,
                    field="specification",
                    max_length=SPECIFICATION_MAX_LENGTH,
                )
            )
            new_base_unit = (
                food_item.base_unit
                if isinstance(base_unit, _Unset)
                else normalize_base_unit(
                    self._require_text(
                        base_unit, field="base_unit", max_length=BASE_UNIT_MAX_LENGTH
                    )
                )
            )
            new_category_id = food_item.category_id
            if not isinstance(category_id, _Unset):
                self._require_category(session, category_id)
                new_category_id = category_id

            self._ensure_names_available(
                repository,
                name=new_name,
                name_zh=new_name_zh,
                exclude_id=food_item.id,
            )

            food_item.name = new_name
            food_item.name_zh = new_name_zh
            food_item.specification = new_specification
            food_item.base_unit = new_base_unit
            food_item.category_id = new_category_id
            # 老板动过它，「待确认」就结束了（步骤 10.13）。他改的可能只是名字，
            # 但那说明他看过这一条了——而这个标记问的正是"有没有人看过"。
            food_item.needs_review = False
            # 名称可能变了：唯一键与检索列一并重算。
            food_item.name_normalized = normalize_name(new_name)
            food_item.name_zh_normalized = (
                normalize_name(new_name_zh) if new_name_zh else None
            )
            food_item.search_text = build_search_text(
                new_name, new_name_zh, new_specification
            )

            try:
                session.flush()
            except IntegrityError as exc:
                raise self._integrity_conflict(
                    "该名称已被另一条食材使用。", "name"
                ) from exc
            # 换过分类时重新读一次，保证返回的视图里是新分类而不是缓存的旧对象。
            session.refresh(food_item)
            return FoodItemView.of(food_item)

    def delete_food_item(self, food_item_id: int) -> None:
        """删除食材。被采购记录或发票行引用时拒绝（引用从步骤 04 起出现）。"""
        with self._database.unit_of_work() as session:
            repository = FoodItemRepository(session)
            food_item = self._load_food_item(session, food_item_id)
            name = food_item.name
            try:
                repository.delete(food_item)
            except IntegrityError as exc:
                # 步骤 04/05 的采购记录与发票行引用本表时，由数据库外键拦住删除
                # （SQLite 的 PRAGMA foreign_keys=ON 已在 src/db.py 统一打开）。
                raise ConflictError(
                    f"食材 {name!r} 已被采购记录或发票引用，不能删除；"
                    "重复食材用 POST /api/food-items/<保留的那条>/merge 合并。"
                ) from exc

    # --- 内部辅助 ---

    # 下面五个都是 services/validation.py 的薄包装：步骤 05 的发票服务成了第三个
    # 调用方，按 rule of three 把实现抽到了那里（见那个模块的文档）。签名与错误
    # 文案没有变，所以调用点不动。

    @staticmethod
    def _collapse(value: str) -> str:
        return collapse_whitespace(value)

    @staticmethod
    def _require_text(value: Any, *, field: str, max_length: int) -> str:
        return require_text(value, field=field, max_length=max_length)

    @staticmethod
    def _optional_text(value: Any, *, field: str, max_length: int) -> str | None:
        return optional_text(value, field=field, max_length=max_length)

    @staticmethod
    def _require_limit(limit: Any) -> int:
        return require_limit(limit, max_limit=MAX_LIMIT)

    @staticmethod
    def _require_offset(offset: Any) -> int:
        return require_offset(offset)

    @staticmethod
    def _normalize_query(query: Any) -> str | None:
        """关键字规范化。去空白后为空等于「不筛选」。"""
        if query is None:
            return None
        if not isinstance(query, str):
            raise ValidationError("q 必须是字符串。", details={"field": "q"})
        if len(query) > MAX_QUERY_LENGTH:
            raise ValidationError(
                f"搜索关键字不能超过 {MAX_QUERY_LENGTH} 个字符。",
                details={"field": "q", "max_length": MAX_QUERY_LENGTH},
            )
        return normalize_name(query) or None

    @staticmethod
    def _require_category(session: Session, category_id: Any) -> Category:
        """校验 category_id 的类型与存在性。

        不存在时抛 400 而不是 404：被请求的资源是食材，分类只是请求数据里的一个
        引用，写错了属于请求不合法。
        """
        if not isinstance(category_id, int) or isinstance(category_id, bool):
            raise ValidationError(
                "category_id 必须是整数。", details={"field": "category_id"}
            )
        category = CategoryRepository(session).get(category_id)
        if category is None:
            raise ValidationError(
                f"分类 {category_id} 不存在。", details={"field": "category_id"}
            )
        return category

    @staticmethod
    def _load_food_item(session: Session, food_item_id: int) -> FoodItem:
        food_item = FoodItemRepository(session).get(food_item_id)
        if food_item is None:
            raise NotFoundError(f"食材 {food_item_id} 不存在。")
        return food_item

    def _ensure_names_available(
        self,
        repository: FoodItemRepository,
        *,
        name: str,
        name_zh: str | None,
        exclude_id: int | None = None,
    ) -> None:
        """同名（规范化后）食材已存在就抛 409，并指向已存在的那一条。

        前端据此可以直接改用已有食材，而不是再建一条重复记录
        （docs/imp_notes.md Feature 1：不要让同一种食材产生两条记录）。
        """
        existing = repository.find_by_normalized_name(normalize_name(name))
        if existing is not None and existing.id != exclude_id:
            raise self._food_item_conflict(existing, field="name")
        if name_zh:
            existing_zh = repository.find_by_normalized_name_zh(normalize_name(name_zh))
            if existing_zh is not None and existing_zh.id != exclude_id:
                raise self._food_item_conflict(existing_zh, field="name_zh")

    @staticmethod
    def _food_item_conflict(existing: FoodItem, *, field: str) -> ConflictError:
        zh_part = f"（{existing.name_zh}）" if existing.name_zh else ""
        return ConflictError(
            f"已存在同名食材：{existing.name}{zh_part}。可以直接使用它，或改用其它名称。",
            details={
                "field": field,
                "existing": {
                    "id": existing.id,
                    "name": existing.name,
                    "name_zh": existing.name_zh,
                },
            },
        )

    @staticmethod
    def _category_conflict(existing: Category) -> ConflictError:
        return ConflictError(
            f"已存在同名分类：{existing.name}。",
            details={
                "field": "name",
                "existing": {
                    "id": existing.id,
                    "name": existing.name,
                    "name_zh": existing.name_zh,
                },
            },
        )

    @staticmethod
    def _integrity_conflict(message: str, field: str) -> ConflictError:
        """唯一约束兜底。

        前面已经查过重复，这里只处理「查询与写入之间又被别人插了一条」的情况：
        数据库约束是最终防线，不是注释。
        """
        return ConflictError(message, details={"field": field})
