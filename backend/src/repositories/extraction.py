"""提取任务与供应商提取路径的数据访问（步骤 08）。

约定与 `repositories/invoices.py` 相同：只接收已有 Session，自己不提交、不回滚、
不做业务校验；返回 ORM 对象，由 Service 在事务内转成只读视图。

两处需要说明的地方：

1. **`claim_next()` 用「先占后跑」而不是「查出来再跑」。** worker 取任务时在同一个
   事务里把状态从 `queued` 改成 `running` 并加一次 `attempt`，提交之后才真的去调
   模型。否则两个 worker 线程会取到同一条任务，于是同一页图被调两遍——花两份钱，
   还可能产出两套草稿行。
2. **`stale_running()` 给「卡住的任务」留了出口。** 进程被 kill 时正在 `running`
   的任务不会有人去改它的状态，于是永远停在 running。按 `started_at` 判断超时，
   由调用方决定是重排还是标记失败——**不在这里自动改**，因为"超时多久算卡住"是
   业务判断。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from src.models import (
    EXTRACTION_STATUS_FAILED,
    EXTRACTION_STATUS_QUEUED,
    EXTRACTION_STATUS_RUNNING,
    EXTRACTION_STATUS_SUCCEEDED,
    ExtractionJob,
    OcrRoute,
    utcnow,
)


def _json_list(raw: str | None) -> list[str]:
    """读回一列 JSON 文本里的字符串清单。读不动就当空。"""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


class ExtractionJobRepository:
    """提取任务的数据访问。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, job: ExtractionJob) -> ExtractionJob:
        self._session.add(job)
        self._session.flush()
        return job

    def get(self, job_id: int) -> ExtractionJob | None:
        return self._session.get(ExtractionJob, job_id)

    def for_invoice(self, invoice_id: int) -> list[ExtractionJob]:
        """一张单据的全部提取任务，最近的在前。

        历史任务**不删**：重试过几次、每次为什么失败，是事后唯一能解释
        「这张单当初怎么没出行」的东西。
        """
        statement = (
            select(ExtractionJob)
            .where(ExtractionJob.invoice_id == invoice_id)
            .order_by(ExtractionJob.id.desc())
        )
        return list(self._session.scalars(statement))

    def latest_for_invoice(self, invoice_id: int) -> ExtractionJob | None:
        statement = (
            select(ExtractionJob)
            .where(ExtractionJob.invoice_id == invoice_id)
            .order_by(ExtractionJob.id.desc())
            .limit(1)
        )
        return self._session.scalars(statement).one_or_none()

    def active_for_invoice(self, invoice_id: int) -> ExtractionJob | None:
        """这张单据还有没有在排队或正在跑的提取任务。

        用来挡住「连点两次提取」——同一张图调两遍是白花钱。
        """
        statement = (
            select(ExtractionJob)
            .where(
                ExtractionJob.invoice_id == invoice_id,
                ExtractionJob.status.in_(
                    (EXTRACTION_STATUS_QUEUED, EXTRACTION_STATUS_RUNNING)
                ),
            )
            .order_by(ExtractionJob.id)
            .limit(1)
        )
        return self._session.scalars(statement).one_or_none()

    def for_file(self, file_id: int) -> list[ExtractionJob]:
        """指着某个原始文件的全部提取任务（最近的在前）。

        删一个文件之前必须先问这一句：`invoice_file_id` 上有外键，直接删文件行
        会被数据库拦下来（SQLite 报 `FOREIGN KEY constraint failed`），而那是一条
        500，不是一句人能看懂的话。
        """
        statement = (
            select(ExtractionJob)
            .where(ExtractionJob.invoice_file_id == file_id)
            .order_by(ExtractionJob.id.desc())
        )
        return list(self._session.scalars(statement))

    def detach_file(self, job: ExtractionJob, *, note: str) -> None:
        """解开任务与原始文件的关联，并把为什么解开写进 `warnings`。

        **任务行本身不删**（理由见 `for_invoice()`）：它记着这次提取用了哪个模型、
        哪版提示词、出了几行、为什么失败。但文件行没了之后 `invoice_file_id`
        不能再指向一个不存在的行，于是置空；文件的编号与名字留在 `warnings` 里，
        否则界面上只会多出一条既没有文件、也没有理由的任务。

        `warnings` 是只给人看的 JSON 文本（不参与查询），所以这里按整列重写，
        读不回来就当它是空的——一条坏数据不该让「删掉拍坏的照片」失败。
        """
        job.invoice_file_id = None
        job.warnings = json.dumps([*_json_list(job.warnings), note], ensure_ascii=False)
        self._session.flush()

    def claim_next(self) -> ExtractionJob | None:
        """占下最早的一条排队任务并置为 `running`。没有就返回 None。

        ⚠️ **占用必须和取出在同一个事务里**（调用方负责提交）。否则两个 worker
        线程会取到同一条任务，同一页图被调两遍——花两份钱，还可能产出两套草稿行。
        """
        statement = (
            select(ExtractionJob)
            .where(ExtractionJob.status == EXTRACTION_STATUS_QUEUED)
            .order_by(ExtractionJob.created_at, ExtractionJob.id)
            .limit(1)
            .with_for_update()
        )
        job = self._session.scalars(statement).one_or_none()
        if job is None:
            return None
        job.status = EXTRACTION_STATUS_RUNNING
        job.attempt += 1
        job.started_at = utcnow()
        self._session.flush()
        return job

    def queued_count(self) -> int:
        statement = select(ExtractionJob).where(
            ExtractionJob.status == EXTRACTION_STATUS_QUEUED
        )
        return len(list(self._session.scalars(statement)))

    def stale_running(self, *, older_than: timedelta, now: datetime | None = None) -> list[ExtractionJob]:
        """`running` 了太久的任务。

        进程被 kill 时正在跑的任务没人改状态，于是永远停在 running——表现是
        「那张单一直在识别中」，没有任何错误信息。按 `started_at` 把它们找出来，
        **怎么处理由调用方决定**（重排还是标记失败是业务判断，不在仓储里定）。
        """
        cutoff = (now or utcnow()) - older_than
        statement = (
            select(ExtractionJob)
            .where(
                ExtractionJob.status == EXTRACTION_STATUS_RUNNING,
                ExtractionJob.started_at.is_not(None),
                ExtractionJob.started_at < cutoff,
            )
            .order_by(ExtractionJob.id)
        )
        return list(self._session.scalars(statement))

    def finish(
        self,
        job: ExtractionJob,
        *,
        succeeded: bool,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> ExtractionJob:
        job.status = (
            EXTRACTION_STATUS_SUCCEEDED if succeeded else EXTRACTION_STATUS_FAILED
        )
        job.error_code = error_code
        job.error_message = error_message
        job.finished_at = utcnow()
        self._session.flush()
        return job

    def requeue(self, job: ExtractionJob, *, reason: str) -> ExtractionJob:
        """把一条任务退回排队。重试次数不清零——它是「试了几次」的事实。"""
        job.status = EXTRACTION_STATUS_QUEUED
        job.started_at = None
        job.error_code = None
        job.error_message = reason
        self._session.flush()
        return job


class OcrRouteRepository:
    """供应商 → 提取路径的数据访问。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, source_name_normalized: str) -> OcrRoute | None:
        statement = select(OcrRoute).where(
            OcrRoute.source_name_normalized == source_name_normalized
        )
        return self._session.scalars(statement).one_or_none()

    def list_all(self) -> list[OcrRoute]:
        return list(
            self._session.scalars(select(OcrRoute).order_by(OcrRoute.source_name_normalized))
        )

    def upsert(
        self,
        *,
        source_name_normalized: str,
        source_name: str | None,
        doc_class: str,
        decided_from: str,
        date_order: str | None = None,
        note: str | None = None,
    ) -> OcrRoute:
        """记下或更新一家供应商的路由。

        ⚠️ `date_order` 传 None 时**不清掉已有值**：那是人显式设过的偏好
        （「这家写月/日/年」），自动分流不该把它覆盖掉。要清就显式传空串。
        """
        route = self.get(source_name_normalized)
        if route is None:
            route = OcrRoute(
                source_name_normalized=source_name_normalized,
                source_name=source_name,
                doc_class=doc_class,
                decided_from=decided_from,
                date_order=date_order or None,
                note=note,
            )
            self._session.add(route)
            self._session.flush()
            return route
        route.doc_class = doc_class
        route.decided_from = decided_from
        if source_name:
            route.source_name = source_name
        if date_order is not None:
            route.date_order = date_order or None
        if note is not None:
            route.note = note or None
        self._session.flush()
        return route


__all__ = ["ExtractionJobRepository", "OcrRouteRepository"]
