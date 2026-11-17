"""原始单据、单据文件与行项目的数据访问（步骤 05）。

约定与 `repositories/purchases.py` 相同：只接收已有 Session，自己不提交、不回滚、
不做业务校验；返回 ORM 对象，由 Service 在事务内转成只读视图。

两处需要说明的地方：

1. **列表筛选用 `completed`，不用派生出来的 `status`。** 单据的整体状态
   （draft / in_review / ready_to_complete / completed）是由各行状态**算出来**的，
   数据库里没有这一列，拿它做 SQL 筛选就得在每次查询里重算一遍。老板实际需要的
   那个清单是「还没做完的单据」，而它等价于 `completed_at IS NULL`——一列、一个
   索引都不用、而且分页是准的。派生状态仍然照常返回给界面显示。
2. **一次把文件、行、行上的食材与采购记录都取出来。** 审核界面本来就要同时显示
   这些；分开查会在十几行的单据上变成几十次查询（N+1）。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Select, func, select, update
from sqlalchemy.orm import Session, joinedload, selectinload

from src.models import Invoice, InvoiceFile, InvoiceLine


def _detail_options() -> tuple[object, ...]:
    """详情与列表共用的预加载选项。见模块文档第 2 条。"""
    return (
        selectinload(Invoice.files),
        selectinload(Invoice.lines).joinedload(InvoiceLine.food_item),
        selectinload(Invoice.lines).selectinload(InvoiceLine.purchases),
    )


class InvoiceRepository:
    """单据的数据访问。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, invoice_id: int) -> Invoice | None:
        statement = (
            select(Invoice).options(*_detail_options()).where(Invoice.id == invoice_id)
        )
        return self._session.scalars(statement).unique().one_or_none()

    def search(
        self,
        *,
        completed: bool | None = None,
        source_normalized: str | None = None,
        include_voided: bool = False,
        limit: int,
        offset: int,
    ) -> tuple[list[Invoice], int]:
        """返回（当页单据, 符合条件的总条数）。

        `completed` 三态：`True` 只要已完成的，`False` 只要还没完成的（老板的待办
        清单），`None` 不筛。排序是上传时间倒序 + id 倒序——第二个键不是装饰，
        同一秒上传的多张单据必须有稳定次序，否则同一个 offset 两次请求可能漏行或
        重行。
        """
        conditions = []
        if completed is True:
            conditions.append(Invoice.completed_at.is_not(None))
        elif completed is False:
            conditions.append(Invoice.completed_at.is_(None))
        if source_normalized is not None:
            conditions.append(Invoice.source_name_normalized == source_normalized)
        if not include_voided:
            conditions.append(Invoice.voided_at.is_(None))

        total_statement = select(func.count()).select_from(Invoice).where(*conditions)
        total = int(self._session.scalars(total_statement).one())

        statement: Select[tuple[Invoice]] = (
            select(Invoice)
            .options(*_detail_options())
            .where(*conditions)
            .order_by(Invoice.created_at.desc(), Invoice.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(statement).unique()), total

    def add(self, invoice: Invoice) -> Invoice:
        self._session.add(invoice)
        self._session.flush()
        return invoice

    def find_possible_duplicates(
        self,
        *,
        exclude_invoice_id: int,
        source_normalized: str,
        number_normalized: str,
        total: Decimal,
    ) -> list[int]:
        """返回「来源 + 单号 + 总额」三者都相同的其它单据的 id。

        这是**软提示**用的：同一张单补拍的照片字节不同，摘要判重抓不到它，而三个
        字段都一样就很可能是同一张单。调用方只提示、不阻止——真有两张同号同额的单
        也是可能的（供应商自己开重了），拦住反而让人没法录入。

        三个值必须都有：缺一个就不判重（手写单常常连单号都没有，凭「来源 + 总额」
        相同就报重复会在同一家供应商的常规订货上天天误报）。
        """
        statement = select(Invoice.id).where(
            Invoice.id != exclude_invoice_id,
            Invoice.voided_at.is_(None),
            Invoice.source_name_normalized == source_normalized,
            Invoice.invoice_number_normalized == number_normalized,
            Invoice.invoice_total == total,
        )
        return [int(value) for value in self._session.scalars(statement)]

    def next_line_no(self, invoice_id: int) -> int:
        """下一个行号。手工加行时用，保证同一张单内不撞 `(invoice_id, line_no)`。"""
        statement = select(func.max(InvoiceLine.line_no)).where(
            InvoiceLine.invoice_id == invoice_id
        )
        current = self._session.scalars(statement).one()
        return int(current or 0) + 1


class InvoiceFileRepository:
    """单据文件的数据访问。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, file_id: int) -> InvoiceFile | None:
        statement = (
            select(InvoiceFile)
            .options(joinedload(InvoiceFile.invoice))
            .where(InvoiceFile.id == file_id)
        )
        return self._session.scalars(statement).one_or_none()

    def find_by_sha256(self, digest: str) -> InvoiceFile | None:
        """按内容摘要找已有文件：完全相同的文件不许重复上传，就靠这一查。"""
        statement = select(InvoiceFile).where(InvoiceFile.sha256 == digest)
        return self._session.scalars(statement).one_or_none()

    def has_storage_key(self, key: str) -> bool:
        return self._session.scalar(select(InvoiceFile.id).where(InvoiceFile.storage_key == key)) is not None

    def add(self, invoice_file: InvoiceFile) -> InvoiceFile:
        self._session.add(invoice_file)
        self._session.flush()
        return invoice_file

    def delete(self, invoice_file: InvoiceFile) -> None:
        """删掉文件记录。磁盘上那份字节由 Service 决定是否一并删除。"""
        self._session.delete(invoice_file)
        self._session.flush()


class InvoiceLineRepository:
    """行项目的数据访问。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, line_id: int) -> InvoiceLine | None:
        statement = (
            select(InvoiceLine)
            .options(
                joinedload(InvoiceLine.invoice),
                joinedload(InvoiceLine.food_item),
                selectinload(InvoiceLine.purchases),
            )
            .where(InvoiceLine.id == line_id)
        )
        return self._session.scalars(statement).unique().one_or_none()

    def add(self, line: InvoiceLine) -> InvoiceLine:
        self._session.add(line)
        self._session.flush()
        return line

    def delete(self, line: InvoiceLine) -> None:
        self._session.delete(line)
        self._session.flush()

    # --- 步骤 10.3：重复食材合并 ---

    def count_for_food_item(self, food_item_id: int) -> int:
        """有多少行草稿指向这条食材（含已确认与已跳过的行）。"""
        statement = (
            select(func.count())
            .select_from(InvoiceLine)
            .where(InvoiceLine.food_item_id == food_item_id)
        )
        return int(self._session.scalars(statement).one())

    def repoint_food_item(self, *, from_food_item_id: int, to_food_item_id: int) -> int:
        """把指向这条食材的草稿行改指向另一条，返回搬了几行（步骤 10.3）。

        ⚠️ **不碰 `version`，也不碰 `raw_fields`**。`version` 是步骤 05 的乐观锁，
        用来拦住并发重复确认；合并不是"人改了这一行"，把它加一会让老板正在审的
        那一页凭空收到 409。`raw_fields` 里的原始识别值**入账后永不改动**
        （步骤 05 的口径），合并同样不是改它的理由。

        已确认的行也跟着改指向：它那条采购记录在同一个事务里一起搬走，两边必须
        指向同一条食材，否则「从采购记录追溯到原始单据」会得到两个不同的食材。
        """
        statement = (
            update(InvoiceLine)
            .where(InvoiceLine.food_item_id == from_food_item_id)
            .values(food_item_id=to_food_item_id)
        )
        return int(self._session.execute(statement).rowcount or 0)
