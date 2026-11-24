"""上传整理批次的数据访问（多文件计划 M02）。

约定与 `repositories/invoices.py` 相同：只接收已有 Session，自己不提交、不回滚、
不做业务校验；返回 ORM 对象，由 Service 在事务内转成只读视图。

三处需要说明的地方：

1. **日期/商家组是一句 `GROUP BY`，不是一张表。** `group_rows()` 按
   `(decided_purchase_date, decided_source_name_normalized)` 聚合内容上的人工决定
   值。整理页的三个区域因此是同一个查询的三类结果：两列都空 = 左侧待归类；只有
   日期 = 那一天的「商家待定」；两列都有 = 一个真正的日期/商家组。另存一张组表就会
   出现「组表里的成员名单」和「内容上的决定值」两份能互相矛盾的真相。
2. **读取默认排除已回收的文件。** 用户把一个文件从批次里撤掉之后，它的页不该再出现
   在整理页上；但那些行**保留**着（痕迹与审计），所以排除是在查询条件里做的，不是
   删行。要看全部就传 `include_discarded=True`。
3. **`intake_files` 的行不随批次级联删除。** 一份字节可能有好几个批次在引用，
   所以这里提供 `active_reference_count()` 让 Service 判断「这份字节还有人用吗」，
   删不删那份字节由 Service 决定——级联删会把别的批次的原件一起删掉。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import Select, exists, func, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from src.models import (
    INTAKE_FILE_STATUS_DISCARDED,
    INTAKE_FILE_STATUS_FAILED,
    INTAKE_FILE_STATUS_PENDING,
    INTAKE_RECOGNITION_STATUS_FAILED,
    INTAKE_RECOGNITION_STATUS_PENDING,
    IntakeBatch,
    IntakeBatchFile,
    IntakeFile,
    IntakeItem,
    IntakeItemCandidate,
)


def _batch_options() -> tuple[object, ...]:
    """批次详情的预加载选项。

    整理页一次要显示「这个批次有哪些文件、每个文件切出了哪些内容、每个内容有哪些
    候选」，分开查会在十几个文件的批次上变成几十次查询（N+1）。
    """
    return (
        selectinload(IntakeBatch.files).joinedload(IntakeBatchFile.file),
        selectinload(IntakeBatch.files)
        .selectinload(IntakeBatchFile.items)
        .selectinload(IntakeItem.candidates),
    )


def _item_options() -> tuple[object, ...]:
    """内容详情的预加载选项：候选 + 回指的文件引用与那份字节。

    「回看原件」要的是文件名、存储引用与页码/行号，三者分别在三张表上，所以一次
    取齐。
    """
    return (
        selectinload(IntakeItem.candidates),
        joinedload(IntakeItem.batch_file).joinedload(IntakeBatchFile.file),
    )


class IntakeBatchRepository:
    """整理批次的数据访问。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, batch_id: int) -> IntakeBatch | None:
        statement = (
            select(IntakeBatch)
            .options(*_batch_options())
            .where(IntakeBatch.id == batch_id)
        )
        return self._session.scalars(statement).unique().one_or_none()

    def add(self, batch: IntakeBatch) -> IntakeBatch:
        self._session.add(batch)
        self._session.flush()
        return batch

    def latest_open(self) -> IntakeBatch | None:
        """最近一个有文件可继续处理的开放批次。

        「恢复批次」用它：浏览器关掉再打开，老板应该接着整理上次那一批，而不是又
        建一个空批次把同一天的内容分成两处。空批次没有可恢复的工作，不应让首页
        出现「接着整理」；等待切页或处理失败的文件仍算有工作。排序第二个键是 id。
        """
        statement = (
            select(IntakeBatch)
            .options(*_batch_options())
            .where(
                IntakeBatch.closed_at.is_(None),
                IntakeBatch.discarded_at.is_(None),
                exists(select(IntakeBatchFile.id).where(
                    IntakeBatchFile.batch_id == IntakeBatch.id,
                    IntakeBatchFile.status != INTAKE_FILE_STATUS_DISCARDED,
                )),
            )
            .order_by(IntakeBatch.created_at.desc(), IntakeBatch.id.desc())
            .limit(1)
        )
        return self._session.scalars(statement).unique().one_or_none()


class IntakeFileRepository:
    """原件字节的数据访问（按内容摘要一行一份）。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, intake_file_id: int) -> IntakeFile | None:
        return self._session.get(IntakeFile, intake_file_id)

    def find_by_sha256(self, digest: str) -> IntakeFile | None:
        """按内容摘要找已存的字节。

        这是「原件字节只存一次」的落点：同一份 PDF 第二次被传进来时，这里查得到，
        于是只加一条批次引用，不再写一份字节。
        """
        statement = select(IntakeFile).where(IntakeFile.sha256 == digest)
        return self._session.scalars(statement).one_or_none()

    def add(self, intake_file: IntakeFile) -> IntakeFile:
        self._session.add(intake_file)
        self._session.flush()
        return intake_file

    def batch_ids_for(
        self, intake_file_id: int, *, exclude_batch_id: int | None = None
    ) -> list[int]:
        """这份字节还被哪些批次引用着（不含已回收的引用）。

        用来提示「这个文件你上个月传过」，**不用来阻断**：同一份 CSV 下个月又传一次
        是正常操作，拦住反而让人没法录入。真正的硬拦截只有「同一批次里同一份字节
        只能进来一次」那条唯一约束。
        """
        conditions = [
            IntakeBatchFile.intake_file_id == intake_file_id,
            IntakeBatchFile.status != INTAKE_FILE_STATUS_DISCARDED,
        ]
        if exclude_batch_id is not None:
            conditions.append(IntakeBatchFile.batch_id != exclude_batch_id)
        statement = (
            select(IntakeBatchFile.batch_id)
            .where(*conditions)
            .distinct()
            .order_by(IntakeBatchFile.batch_id)
        )
        return [int(value) for value in self._session.scalars(statement)]

    def active_reference_count(self, intake_file_id: int) -> int:
        """还有多少条**未回收**的引用指向这份字节。

        文件回收靠它：数到 0 才能考虑删磁盘上那份字节，否则会把别的批次正在用的
        原件一起删掉。删不删由 Service 决定，本模块只回答数量。
        """
        statement = (
            select(func.count())
            .select_from(IntakeBatchFile)
            .where(
                IntakeBatchFile.intake_file_id == intake_file_id,
                IntakeBatchFile.status != INTAKE_FILE_STATUS_DISCARDED,
            )
        )
        return int(self._session.scalars(statement).one())


class IntakeBatchFileRepository:
    """批次对原件的引用的数据访问。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, batch_file_id: int) -> IntakeBatchFile | None:
        statement = (
            select(IntakeBatchFile)
            .options(
                joinedload(IntakeBatchFile.file),
                joinedload(IntakeBatchFile.batch),
                selectinload(IntakeBatchFile.items),
            )
            .where(IntakeBatchFile.id == batch_file_id)
        )
        return self._session.scalars(statement).unique().one_or_none()

    def find_in_batch(
        self, *, batch_id: int, intake_file_id: int
    ) -> IntakeBatchFile | None:
        """这份字节在这个批次里已经有引用了吗（含已回收的）。

        含已回收的是刻意的：撤掉之后又传同一个文件，撞的仍然是
        `(batch_id, intake_file_id)` 那条唯一约束，所以要能查到那条已回收的行，
        由 Service 决定是复活它还是报冲突。
        """
        statement = (
            select(IntakeBatchFile)
            .options(joinedload(IntakeBatchFile.file))
            .where(
                IntakeBatchFile.batch_id == batch_id,
                IntakeBatchFile.intake_file_id == intake_file_id,
            )
        )
        return self._session.scalars(statement).unique().one_or_none()

    def add(self, batch_file: IntakeBatchFile) -> IntakeBatchFile:
        self._session.add(batch_file)
        self._session.flush()
        return batch_file

    def list_waiting(
        self,
        *,
        batch_id: int | None = None,
        include_failed: bool = False,
        limit: int = 50,
    ) -> list[IntakeBatchFile]:
        """还等着切页/切行的文件引用，按上传顺序。

        **`status` 本身就是队列，没有另一张任务表**：`pending` 的含义就是「字节已经
        存好了，还没切出内容」。这与「单据状态由行状态派生」同一条——另存一张任务表
        就会出现「任务表说跑完了、文件还是 pending」这种两份真相。进程被 kill 时
        那一条仍然是 `pending`，下一次驱动队列时照样被取到，不需要「卡住的任务要退回
        排队」那套恢复逻辑（`extraction_jobs` 需要它，是因为那边有 `running`
        这个中间状态，而且重跑要再花一次钱；本地切页重跑不花钱）。

        `include_failed=True` 时连 `failed` 一起取，用于「失败的重试一遍」。
        已回收（`discarded`）的永远不取：用户已经把它撤出批次了。
        """
        wanted = [INTAKE_FILE_STATUS_PENDING]
        if include_failed:
            wanted.append(INTAKE_FILE_STATUS_FAILED)
        statement = (
            select(IntakeBatchFile)
            .options(
                joinedload(IntakeBatchFile.file),
                joinedload(IntakeBatchFile.batch),
                selectinload(IntakeBatchFile.items),
            )
            .where(IntakeBatchFile.status.in_(wanted))
            .order_by(IntakeBatchFile.batch_id, IntakeBatchFile.upload_order)
            .limit(limit)
        )
        if batch_id is not None:
            statement = statement.where(IntakeBatchFile.batch_id == batch_id)
        return list(self._session.scalars(statement).unique().all())

    def next_upload_order(self, batch_id: int) -> int:
        """下一个上传序号。页序默认按上传顺序，和 `invoice_files.page_order` 同理。"""
        statement = select(func.max(IntakeBatchFile.upload_order)).where(
            IntakeBatchFile.batch_id == batch_id
        )
        current = self._session.scalars(statement).one()
        return int(current or 0) + 1


class IntakeItemRepository:
    """可归类内容与分类候选的数据访问。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, item_id: int) -> IntakeItem | None:
        statement = (
            select(IntakeItem).options(*_item_options()).where(IntakeItem.id == item_id)
        )
        return self._session.scalars(statement).unique().one_or_none()

    def add(self, item: IntakeItem) -> IntakeItem:
        self._session.add(item)
        self._session.flush()
        return item

    def add_candidate(self, candidate: IntakeItemCandidate) -> IntakeItemCandidate:
        self._session.add(candidate)
        self._session.flush()
        return candidate

    def delete_candidates(self, *, item_id: int, field: str, basis: str) -> int:
        """删掉这个内容在这个维度上来自这一层依据的候选，返回删了几条。

        重算建议时整组替换，不叠加——否则同一个依据会在列表里出现三遍旧值，
        老板分不清哪个是最新的。
        """
        statement = select(IntakeItemCandidate).where(
            IntakeItemCandidate.item_id == item_id,
            IntakeItemCandidate.field == field,
            IntakeItemCandidate.basis == basis,
        )
        stale = list(self._session.scalars(statement))
        for row in stale:
            self._session.delete(row)
        self._session.flush()
        return len(stale)

    def list_recognition_waiting(
        self,
        *,
        batch_id: int | None = None,
        include_failed: bool = False,
        limit: int = 50,
    ) -> list[IntakeItem]:
        """还等着跑日期/商家识别的内容，按文件上传序与页号/行号。

        **`recognition_status` 本身就是队列**，与 `list_waiting` 的切页队列同一条
        （理由写在那个方法上）。这里的待办是两种：

        - `NULL`——还没排到它。刚切出来的页就是这个状态，所以**不需要**在摄入时
          额外写一次 `pending` 才能被捡起来。
        - `pending`——已经排进队列（M03 的处理器钩子写的），还没跑完。

        ⚠️ **`done` 永远不取**：那正是「重新打开批次不对同一页重复收费」的闸门。
        读出来是空的也算 `done`（纸面上本来就没有商家），所以不能用「有没有候选」
        代替这一列。`failed` 只在显式重试时取——识别失败常常是环境问题
        （凭据没配、整页图取不出来），装好之后不该逼人重传原件。
        已回收（`discarded`）文件的内容一概不取。
        """
        wanted_null_or = [
            IntakeItem.recognition_status.is_(None),
            IntakeItem.recognition_status == INTAKE_RECOGNITION_STATUS_PENDING,
        ]
        if include_failed:
            wanted_null_or.append(
                IntakeItem.recognition_status == INTAKE_RECOGNITION_STATUS_FAILED
            )
        conditions = [
            IntakeBatchFile.status != INTAKE_FILE_STATUS_DISCARDED,
            or_(*wanted_null_or),
        ]
        if batch_id is not None:
            conditions.append(IntakeBatchFile.batch_id == batch_id)
        statement = (
            select(IntakeItem)
            .options(*_item_options())
            .join(IntakeItem.batch_file)
            .where(*conditions)
            .order_by(
                IntakeBatchFile.batch_id,
                IntakeBatchFile.upload_order,
                IntakeItem.page_no,
                IntakeItem.row_no,
                IntakeItem.id,
            )
            .limit(limit)
        )
        return list(self._session.scalars(statement).unique().all())

    def list_for_batch(
        self,
        batch_id: int,
        *,
        include_discarded: bool = False,
        purchase_date: date | None = None,
        date_missing: bool = False,
        source_key: str | None = None,
        source_missing: bool = False,
        missing: str | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[IntakeItem], int]:
        """返回（当页内容, 符合条件的总条数）。

        排序是「文件上传序 → 页号/行号 → id」：整理页左侧那一列必须稳定，否则同一个
        offset 两次请求可能漏掉或重复一张卡片。大表分页就靠它（M04 的五千行 CSV
        不会一次返回五千张卡片）。
        """
        conditions = [IntakeBatchFile.batch_id == batch_id]
        if not include_discarded:
            conditions.append(
                IntakeBatchFile.status != INTAKE_FILE_STATUS_DISCARDED
            )
        if date_missing:
            conditions.append(IntakeItem.decided_purchase_date.is_(None))
        elif purchase_date is not None:
            conditions.append(IntakeItem.decided_purchase_date == purchase_date)
        if source_missing:
            conditions.append(IntakeItem.decided_source_name_normalized.is_(None))
        elif source_key is not None:
            conditions.append(IntakeItem.decided_source_name_normalized == source_key)
        if missing == "both":
            conditions.extend((IntakeItem.decided_purchase_date.is_(None),
                               IntakeItem.decided_source_name_normalized.is_(None)))
        elif missing == "date":
            conditions.extend((IntakeItem.decided_purchase_date.is_(None),
                               IntakeItem.decided_source_name_normalized.is_not(None)))
        elif missing == "source":
            conditions.extend((IntakeItem.decided_purchase_date.is_not(None),
                               IntakeItem.decided_source_name_normalized.is_(None)))
        elif missing == "any":
            conditions.append(or_(IntakeItem.decided_purchase_date.is_(None),
                                  IntakeItem.decided_source_name_normalized.is_(None)))
        elif missing == "recognition_failed":
            conditions.append(IntakeItem.recognition_status == INTAKE_RECOGNITION_STATUS_FAILED)
        elif missing == "date_any":
            conditions.append(IntakeItem.decided_purchase_date.is_(None))
        elif missing == "source_any":
            conditions.append(IntakeItem.decided_source_name_normalized.is_(None))

        total_statement = (
            select(func.count())
            .select_from(IntakeItem)
            .join(IntakeItem.batch_file)
            .where(*conditions)
        )
        total = int(self._session.scalars(total_statement).one())

        statement: Select[tuple[IntakeItem]] = (
            select(IntakeItem)
            .options(*_item_options())
            .join(IntakeItem.batch_file)
            .where(*conditions)
            .order_by(
                IntakeBatchFile.upload_order,
                IntakeItem.page_no,
                IntakeItem.row_no,
                IntakeItem.id,
            )
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(statement).unique()), total

    def group_rows(
        self, batch_id: int, *, include_discarded: bool = False
    ) -> list[tuple[date | None, str | None, str | None, int]]:
        """派生出日期/商家组：`(日期, 规范化来源, 展示用来源, 内容数)`。

        **按规范化来源分组**，不按展示写法：`Costco` 与 `COSTCO` 是同一家，按展示
        写法分组就会在整理页上长出两个组——那正是 docs/multifiles.md 第 3.2 节要避免
        的情况。

        展示名取同一个规范化键下**字典序最大**的那个写法。它是稳定的、但确实是任意
        的：老板在两张卡片上分别写了 `Costco` 和 `COSTCO` 时，组标题只能显示其中
        一个。把两种写法真正收敛到一个名字是 M07 的别名记忆要做的事，不是这句
        `GROUP BY` 能回答的，所以这里不假装它有答案。

        排序把「还没定日期」的放最后（整理页的待归类区在左边单独一列，不该插在
        11 月 8 日和 11 月 9 日之间），同一天内「商家待定」也放最后。
        """
        conditions = [IntakeBatchFile.batch_id == batch_id]
        if not include_discarded:
            conditions.append(
                IntakeBatchFile.status != INTAKE_FILE_STATUS_DISCARDED
            )

        statement = (
            select(
                IntakeItem.decided_purchase_date,
                IntakeItem.decided_source_name_normalized,
                func.max(IntakeItem.decided_source_name),
                func.count(),
            )
            .select_from(IntakeItem)
            .join(IntakeItem.batch_file)
            .where(*conditions)
            .group_by(
                IntakeItem.decided_purchase_date,
                IntakeItem.decided_source_name_normalized,
            )
            .order_by(
                IntakeItem.decided_purchase_date.is_(None),
                IntakeItem.decided_purchase_date,
                IntakeItem.decided_source_name_normalized.is_(None),
                IntakeItem.decided_source_name_normalized,
            )
        )
        return [
            (row[0], row[1], row[2], int(row[3]))
            for row in self._session.execute(statement)
        ]
