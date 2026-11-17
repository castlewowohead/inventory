"""采购记录与修改日志的数据访问（步骤 04）。

本层的约定与 `repositories/ingredients.py` 相同：只接收已有 Session，自己不提交、
不回滚、不做业务校验；返回 ORM 对象，由 Service 在事务内转成只读视图。

一处需要说明的排序：列表按 `purchase_date DESC, id DESC`。

- 第二个键不是装饰：同一天的多条采购必须有稳定次序，否则同一个 `offset` 两次请求
  可能返回不同的行（分页会漏行或重行）。
- SQLite 把 NULL 视为最小值，所以 `DESC` 下**未填日期的记录自然排在最后**，不会挤在
  最新采购前面。它们另有 `undated=True` 这条筛选，供「待补日期」清单使用。
"""

from __future__ import annotations

from datetime import date
from typing import NamedTuple

from sqlalchemy import Select, func, select, update
from sqlalchemy.orm import Session, joinedload, selectinload

from src.models import LINE_KIND_PURCHASE, FoodItem, Purchase, PurchaseRevision


class DescriptionHistoryRow(NamedTuple):
    """历史上「这句原始描述 → 这个食材」的一条证据。供行匹配排候选用。"""

    purchase_id: int
    food_item_id: int
    source_name_normalized: str | None
    purchase_date: date | None


class DescriptionMappingRow(NamedTuple):
    """已确认过的一条「原始描述 → 食材」映射，已按（描述, 食材, 来源）聚合（步骤 10.4）。

    和 `DescriptionHistoryRow` 的分工：那个按**精确命中**取回若干条原始记录，
    键是已经确定的那句描述；这个要把整份映射表取回内存做**模糊比较**，所以必须
    先在 SQL 里聚合——不聚合的话同一句描述买过 50 次就要比 50 遍。
    """

    description_normalized: str
    food_item_id: int
    source_name_normalized: str | None
    purchase_count: int
    last_purchase_id: int
    last_purchase_date: date | None


class FoodItemUsageRow(NamedTuple):
    """一条食材的采购使用情况（步骤 10.2 的最近 / 常用）。"""

    food_item_id: int
    purchase_count: int
    last_purchase_id: int
    last_purchase_date: date | None


class PurchaseRepository:
    """采购记录的数据访问。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, purchase_id: int) -> Purchase | None:
        statement = (
            select(Purchase)
            .options(
                joinedload(Purchase.food_item),
                joinedload(Purchase.invoice_line),
                selectinload(Purchase.revisions),
            )
            .where(Purchase.id == purchase_id)
        )
        return self._session.scalars(statement).unique().one_or_none()

    def search(
        self,
        *,
        food_item_id: int | None = None,
        source_normalized: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        undated: bool | None = None,
        line_kind: str | None = None,
        include_voided: bool = False,
        limit: int,
        offset: int,
    ) -> tuple[list[Purchase], int]:
        """返回（当页采购记录, 符合条件的总条数）。

        `undated` 三态：`True` 只要未填日期的（待补清单），`False` 只要填了日期的
        （能进月度统计的那些），`None` 不筛。

        `line_kind` 为 `None` 表示两类行都要（对账用）；服务层默认传
        `"purchase"`，把退货 / 折让等调整行排除在外。
        """
        conditions = []
        if food_item_id is not None:
            conditions.append(Purchase.food_item_id == food_item_id)
        if source_normalized is not None:
            conditions.append(Purchase.source_name_normalized == source_normalized)
        if date_from is not None:
            conditions.append(Purchase.purchase_date >= date_from)
        if date_to is not None:
            conditions.append(Purchase.purchase_date <= date_to)
        if undated is True:
            conditions.append(Purchase.purchase_date.is_(None))
        elif undated is False:
            conditions.append(Purchase.purchase_date.is_not(None))
        if line_kind is not None:
            conditions.append(Purchase.line_kind == line_kind)
        if not include_voided:
            conditions.append(Purchase.voided_at.is_(None))

        total_statement = select(func.count()).select_from(Purchase).where(*conditions)
        total = int(self._session.scalars(total_statement).one())

        statement: Select[tuple[Purchase]] = (
            select(Purchase)
            .options(
                joinedload(Purchase.food_item),
                joinedload(Purchase.invoice_line),
                selectinload(Purchase.revisions),
            )
            .where(*conditions)
            .order_by(Purchase.purchase_date.desc(), Purchase.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(statement).unique()), total

    def add(self, purchase: Purchase) -> Purchase:
        self._session.add(purchase)
        self._session.flush()
        return purchase

    # --- 步骤 05 用到的两个查询 ---

    def find_active_by_invoice_line(self, invoice_line_id: int) -> Purchase | None:
        """这个发票行现在有没有一条**未作废**的采购记录。

        重复确认保护的第一道：服务层先查一次，给出「这一行已经确认过了，采购记录
        是 #12」这种能看懂的 409。数据库上那条局部唯一索引是第二道，挡并发。
        """
        statement = (
            select(Purchase)
            .options(joinedload(Purchase.food_item))
            .where(
                Purchase.invoice_line_id == invoice_line_id,
                Purchase.voided_at.is_(None),
            )
        )
        return self._session.scalars(statement).unique().one_or_none()

    def description_history(
        self, description_normalized: str, *, limit: int
    ) -> list["DescriptionHistoryRow"]:
        """这条原始商品描述以前确认到过哪些食材（步骤 05 的行匹配）。

        只看**正常采购行**且**未作废**的记录：退货 / 折让行不代表「这句描述是这个
        食材」，已作废的记录是老板明确否掉的，拿它们当记忆只会把错的猜法再推一次。

        返回最近的若干行原始记录（不在 SQL 里聚合），由服务层按「同来源优先、最近
        优先」排出候选。聚合放在 Python 是刻意的：这里要的排序规则含「是否同一个
        来源」这种业务判断，写成 SQL 反而更难读，而条数本来就被 limit 压住了。
        """
        statement = (
            select(
                Purchase.id,
                Purchase.food_item_id,
                Purchase.source_name_normalized,
                Purchase.purchase_date,
            )
            .where(
                Purchase.original_description_normalized == description_normalized,
                Purchase.food_item_id.is_not(None),
                Purchase.line_kind == LINE_KIND_PURCHASE,
                Purchase.voided_at.is_(None),
            )
            .order_by(Purchase.id.desc())
            .limit(limit)
        )
        return [
            DescriptionHistoryRow(
                purchase_id=int(row.id),
                food_item_id=int(row.food_item_id),
                source_name_normalized=row.source_name_normalized,
                purchase_date=row.purchase_date,
            )
            for row in self._session.execute(statement)
        ]


    def supplier_history(
        self, source_name_normalized: str, *, limit: int
    ) -> list["DescriptionHistoryRow"]:
        """**这家供应商历史上买过哪些食材**（步骤 08.3i 的手写件候选）。

        与 `description_history` 同一张表、同一组过滤条件，只是键换成了来源。
        为什么需要它：步骤 05 的行匹配靠「这句原始描述以前确认到哪个食材」，
        而**手写件没有可信的描述文本**——步骤 07 实测中文商品名跨模型分歧率
        96.0%，等于随机。按描述去命中只会命中错的，或者什么都命中不到。

        改走供应商维度之后，数据是现成的：这家供应商买过什么、最近买的是哪些。
        老板看一眼裁剪图就知道是什么——**他认的是自己的货，不是认字**——
        所以要给他的不是一个猜出来的名字，而是一张「这家买过的食材」清单。

        不需要新表（步骤 05 刻意没建供应商表，这里也不建）。
        """
        statement = (
            select(
                Purchase.id,
                Purchase.food_item_id,
                Purchase.source_name_normalized,
                Purchase.purchase_date,
            )
            .where(
                Purchase.source_name_normalized == source_name_normalized,
                Purchase.food_item_id.is_not(None),
                Purchase.line_kind == LINE_KIND_PURCHASE,
                Purchase.voided_at.is_(None),
            )
            .order_by(Purchase.id.desc())
            .limit(limit)
        )
        return [
            DescriptionHistoryRow(
                purchase_id=int(row.id),
                food_item_id=int(row.food_item_id),
                source_name_normalized=row.source_name_normalized,
                purchase_date=row.purchase_date,
            )
            for row in self._session.execute(statement)
        ]


    # --- 步骤 10 用到的检索、记忆与合并 ---

    def count_for_food_item(self, food_item_id: int) -> int:
        """这条食材有多少条采购记录（**含已作废**）。

        合并预览要把"会搬动多少东西"摆给老板看，所以数的是**全部**引用而不是
        未作废那些：作废是软删，那些行照样指向这条食材，合并时同样要跟着搬走。
        少数了它们，预览上的数字就会比实际动的少，而那正是老板用来判断"要不要
        按下去"的依据。
        """
        statement = (
            select(func.count())
            .select_from(Purchase)
            .where(Purchase.food_item_id == food_item_id)
        )
        return int(self._session.scalars(statement).one())

    def repoint_food_item(self, *, from_food_item_id: int, to_food_item_id: int) -> int:
        """把这条食材的全部采购记录改指向另一条，返回搬了几条（步骤 10.3）。

        ⚠️ **一条都不删、一个数值都不改**：`original_description`、数量、单价、
        金额、日期、作废标记全部原样保留。合并改的只是「这些钱花在哪条食材上」
        这一个指向，所以 10.3 要求的「采购历史保持可追溯」在结构上就成立——
        供应商当初那句原话仍然在每一行上。
        """
        statement = (
            update(Purchase)
            .where(Purchase.food_item_id == from_food_item_id)
            .values(food_item_id=to_food_item_id)
        )
        return int(self._session.execute(statement).rowcount or 0)

    # --- 步骤 10 用到的检索与记忆查询 ---

    def description_mappings(self, *, limit: int) -> list["DescriptionMappingRow"]:
        """全部已确认的「原始描述 → 食材」映射，按（描述, 食材, 来源）聚合。

        供 10.4 的模糊层使用：05.6 那一层靠
        `original_description_normalized` 上的索引做**精确命中**，命中不了的要能
        按相似度再找一遍，而相似度算不进 SQL，只能把映射表取回内存。

        ⚠️ **按 `MAX(id) DESC` 排序再截断，不是随便截。** 截断意味着模糊层看不到
        老映射，所以必须保证**最近确认过的那些一定在里面**——老板刚纠正过的映射
        下一张单据就用不上，是这条功能最容易被察觉的失败方式。精确命中那一层
        不受 `limit` 影响（走索引，全表有效）。

        过滤条件与 `description_history` 完全一致（正常采购行、未作废、有食材、
        有描述）：换一套条件就等于让同一件事在两处有两个答案。
        """
        last_id = func.max(Purchase.id).label("last_purchase_id")
        statement = (
            select(
                Purchase.original_description_normalized,
                Purchase.food_item_id,
                Purchase.source_name_normalized,
                func.count().label("purchase_count"),
                last_id,
                func.max(Purchase.purchase_date).label("last_purchase_date"),
            )
            .where(
                Purchase.original_description_normalized.is_not(None),
                Purchase.food_item_id.is_not(None),
                Purchase.line_kind == LINE_KIND_PURCHASE,
                Purchase.voided_at.is_(None),
            )
            .group_by(
                Purchase.original_description_normalized,
                Purchase.food_item_id,
                Purchase.source_name_normalized,
            )
            .order_by(last_id.desc())
            .limit(limit)
        )
        return [
            DescriptionMappingRow(
                description_normalized=row.original_description_normalized,
                food_item_id=int(row.food_item_id),
                source_name_normalized=row.source_name_normalized,
                purchase_count=int(row.purchase_count),
                last_purchase_id=int(row.last_purchase_id),
                last_purchase_date=row.last_purchase_date,
            )
            for row in self._session.execute(statement)
        ]

    def food_item_usage(
        self,
        *,
        since: date | None = None,
        category_id: int | None = None,
        limit: int,
        order: str = "recent",
    ) -> list["FoodItemUsageRow"]:
        """按食材聚合的采购使用情况，供「最近 / 常用」入口（10.2）。

        - `order="recent"`：按**最近一次入账**（`MAX(purchases.id)`）排序；
        - `order="frequent"`：按采购次数排序，次数相同再按最近一次入账。

        ⚠️ **「最近」按 `id` 而不是按 `purchase_date`。** 步骤 04 定下 `purchase_date`
        可空（手写单上常常没写日期，认不出就留空、不编造），按日期排会让这些记录
        永远沉到最后——而"刚刚录进去的那一批"恰恰是老板下一分钟最可能再选的。
        `id` 单调递增，等于"最近录进来的"，这正是「最近用过」在这个产品里的含义。
        纸面日期照样返回（`last_purchase_date`）供展示。

        ⚠️ **`line_kind='purchase' AND voided_at IS NULL` 是必须的**，不是可选项
        （步骤 04 的口径）。漏掉前者会把一笔退货算成"用过这个食材"；漏掉后者会把
        老板明确否掉的记录当成记忆再推一次。

        `since` 只用于"常用"：它限定统计窗口。**未填日期的记录因此进不了常用榜**
        ——它们算不进任何一个月，这与步骤 09 的月度均价是同一条口径，不在这里
        另开例外。
        """
        last_id = func.max(Purchase.id).label("last_purchase_id")
        count = func.count().label("purchase_count")
        conditions = [
            Purchase.food_item_id.is_not(None),
            Purchase.line_kind == LINE_KIND_PURCHASE,
            Purchase.voided_at.is_(None),
        ]
        if since is not None:
            conditions.append(Purchase.purchase_date.is_not(None))
            conditions.append(Purchase.purchase_date >= since)
        ordering = (
            (count.desc(), last_id.desc())
            if order == "frequent"
            else (last_id.desc(),)
        )
        statement = select(
            Purchase.food_item_id,
            count,
            last_id,
            func.max(Purchase.purchase_date).label("last_purchase_date"),
        )
        if category_id is not None:
            # 在 SQL 里过滤而不是取回来再筛：取回来再筛会让 `limit` 的含义变成
            # 「筛之前的前 N 条」，于是分类一选，榜单就莫名其妙地短一截。
            statement = statement.join(FoodItem, FoodItem.id == Purchase.food_item_id)
            conditions.append(FoodItem.category_id == category_id)
        statement = (
            statement.where(*conditions)
            .group_by(Purchase.food_item_id)
            .order_by(*ordering)
            .limit(limit)
        )
        return [
            FoodItemUsageRow(
                food_item_id=int(row.food_item_id),
                purchase_count=int(row.purchase_count),
                last_purchase_id=int(row.last_purchase_id),
                last_purchase_date=row.last_purchase_date,
            )
            for row in self._session.execute(statement)
        ]

    # --- 步骤 09 用到的统计查询 ---

    def for_statistics(
        self,
        *,
        food_item_id: int | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int,
    ) -> list[Purchase]:
        """取出能参与价格统计的采购记录。**四个过滤条件不是可选项。**

        ⚠️ `line_kind = 'purchase'` 与 `voided_at IS NULL` 写在**这里**而不是交给
        调用方，正是因为漏写它们的后果是静默的：把一笔退货（调整行）或一条已作废
        的记录混进加权均价，只会让价格悄悄偏低，不报错、没人察觉
        （docs/stru_dev.md 步骤 04 与 09 都把这条列为硬口径）。本方法名字里的
        "for_statistics" 就是这个承诺，统计路径只许走这一个入口。

        `purchase_date IS NOT NULL`：没有日期的记录分不进任何一个月，无法参与
        期间比较。它们不是被忽略了——`search(undated=True)` 那条"待补日期"清单
        就是为它们准备的，统计响应里也会报出有几条因此没算进来。

        `food_item_id IS NOT NULL`：整单折让不指向食材，而它本来就是调整行，
        这里再写一次是因为「食材维度的统计」必须有食材。

        **刻意不按币种过滤**：服务层把窗口内的记录一次取回再按币种分组，这样
        「另一种币种有几条没算进来」是数出来的事实，而不是一个被 WHERE 悄悄
        吃掉的空洞。本轮不做汇率换算，所以不同币种只能分开统计，不能相加。

        `limit` 是**保护性上限**而不是分页：统计要对窗口内的全部记录求和，截断会
        给出一个看起来正常的错数。服务层取回后检查是否触顶，触顶就报错而不是
        算一个少了尾巴的均价。
        """
        conditions = [
            Purchase.line_kind == LINE_KIND_PURCHASE,
            Purchase.voided_at.is_(None),
            Purchase.purchase_date.is_not(None),
            Purchase.food_item_id.is_not(None),
        ]
        if food_item_id is not None:
            conditions.append(Purchase.food_item_id == food_item_id)
        if date_from is not None:
            conditions.append(Purchase.purchase_date >= date_from)
        if date_to is not None:
            conditions.append(Purchase.purchase_date <= date_to)

        statement = (
            select(Purchase)
            .options(joinedload(Purchase.food_item))
            .where(*conditions)
            .order_by(Purchase.purchase_date.asc(), Purchase.id.asc())
            .limit(limit)
        )
        return list(self._session.scalars(statement).unique())

    def count_statistics_candidates(
        self,
        *,
        food_item_id: int | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> dict[str, int]:
        """统计窗口内「本来算得上候选、却进不了统计」的记录数，按原因分。

        现在只有一个原因在数据库层面可查：**没有采购日期**。单位认不出、缺箱规、
        量纲不兼容都要换算之后才知道，由服务层在内存里数（见 `services/prices.py`）。

        为什么要单独数一遍：没有日期的记录被 `for_statistics` 的 WHERE 排除掉，
        如果不在这里数出来，它们就从统计里**无声消失**了——而「未填日期的采购」
        恰恰是最可能藏着真实涨价的那一批（手写单上常常不写日期）。
        """
        conditions = [
            Purchase.line_kind == LINE_KIND_PURCHASE,
            Purchase.voided_at.is_(None),
            Purchase.food_item_id.is_not(None),
            Purchase.purchase_date.is_(None),
        ]
        if food_item_id is not None:
            conditions.append(Purchase.food_item_id == food_item_id)
        # 日期区间对"没有日期"的记录无意义，刻意不加——加了永远是 0 条。
        undated = int(
            self._session.scalars(
                select(func.count()).select_from(Purchase).where(*conditions)
            ).one()
        )
        return {"undated": undated}

    def for_price_review(
        self, *, food_item_id: int | None = None, limit: int
    ) -> list[Purchase]:
        """取出采购行供「逐条看换算状态」用：价格历史与「待补箱规」清单。

        与 `for_statistics` 的两个区别都有理由：

        1. **不要求有采购日期**。缺箱规和缺日期是两件独立的待办，一条既没日期又
           没箱规的记录要同时出现在两张清单上；按日期过滤会让它从箱规清单里漏掉。
           价格历史同样要把没日期的那些列出来，否则它们会从界面上整体消失。
        2. **不限日期窗口**。箱规是长期事实，去年买过的那箱货现在补上箱规，
           去年的价格立刻就能算出来。

        仍然排除调整行与已作废记录：那两类不参与价格统计，给它们提示补箱规
        只是给老板派一件没用的活。

        排序是 `purchase_date DESC, id DESC`（与 `search()` 同一个口径，NULL 日期
        自然排在最后），服务层据此分页。
        """
        conditions = [
            Purchase.line_kind == LINE_KIND_PURCHASE,
            Purchase.voided_at.is_(None),
            Purchase.food_item_id.is_not(None),
        ]
        if food_item_id is not None:
            conditions.append(Purchase.food_item_id == food_item_id)
        statement = (
            select(Purchase)
            .options(joinedload(Purchase.food_item))
            .where(*conditions)
            .order_by(Purchase.purchase_date.desc(), Purchase.id.desc())
            .limit(limit)
        )
        return list(self._session.scalars(statement).unique())


class PurchaseRevisionRepository:
    """修改日志的数据访问。只追加与读取，没有修改和删除。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_for_purchase(self, purchase_id: int) -> list[PurchaseRevision]:
        statement = (
            select(PurchaseRevision)
            .where(PurchaseRevision.purchase_id == purchase_id)
            .order_by(PurchaseRevision.id)
        )
        return list(self._session.scalars(statement))

    def add(self, revision: PurchaseRevision) -> PurchaseRevision:
        self._session.add(revision)
        self._session.flush()
        return revision
