"""提取 worker 的测试（步骤 08.3g：提取不挂在 HTTP 请求里）。

不访问网络：识别器用桩件。这里验的是**调度**，不是识别。
"""

from __future__ import annotations

import io
import threading
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from src.config import Config
from src.db import Database
from src.contracts import RecognitionResult, RecognizedPage
from src.integrations.file_storage import LocalFileStorage
from src.models import (
    EXTRACTION_STATUS_QUEUED,
    EXTRACTION_STATUS_RUNNING,
    EXTRACTION_STATUS_SUCCEEDED,
    ExtractionJob,
)
from src.repositories.extraction import ExtractionJobRepository
from src.services.extraction import ExtractionService
from src.services.invoices import InvoiceService, UploadedFile
from src.worker import ExtractionWorker


def _page() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (400, 300), (255, 255, 255)).save(buffer, format="JPEG")
    return buffer.getvalue()


class _QuietRecognizer:
    provider = "dashscope"
    is_fake = False
    models = ("qwen3.5-ocr",)

    def __init__(self) -> None:
        self.calls = 0
        self.lock = threading.Lock()

    def recognize(self, request) -> RecognitionResult:
        with self.lock:
            self.calls += 1
        return RecognitionResult(
            provider=self.provider,
            is_fake=False,
            model="qwen3.5-ocr",
            recognized_at=datetime.now(timezone.utc),
            pages=(RecognizedPage(number=1),),
            document_fields={},
            lines=(),
            raw_response="{}",
            warnings=(),
            source_key=request.source_key,
        )


@pytest.fixture
def thread_config() -> Config:
    """worker 模式：提取**不在**当前请求里跑完，而是入队。"""
    return Config.from_env(
        {
            "APP_ENV": "testing",
            "SECRET_KEY": "test-only",
            "OCR_PROVIDER": "dashscope",
            "OCR_API_KEY": "sk-test",
            "OCR_BASE_URL": "https://example/v1",
            "OCR_WORKER_MODE": "thread",
        }
    )


def _services(database: Database, storage: LocalFileStorage, config: Config):
    recognizer = _QuietRecognizer()
    invoices = InvoiceService(
        database,
        file_storage=storage,
        recognizer=recognizer,
        upload_max_bytes=4 * 1024 * 1024,
    )
    extraction = ExtractionService(
        database,
        file_storage=storage,
        recognizer=recognizer,
        invoice_service=invoices,
        config=config,
    )
    invoices.attach_extraction(extraction)
    return invoices, extraction, recognizer


def test_thread_mode_queues_instead_of_blocking_the_upload(
    migrated_database: Database, storage: LocalFileStorage, thread_config: Config
) -> None:
    """⚠️ 08.3g：手写件约 15 秒，不能让老板对着转圈等。

    上传请求**只负责把单据存住**；识别排进队列，老板可以去忙别的，回来再审。
    """
    invoices, extraction, recognizer = _services(migrated_database, storage, thread_config)

    view = invoices.create_invoice(
        [UploadedFile(content=_page(), original_name="a.jpg", content_type=None)]
    )

    job = extraction.jobs_for(view.id)[0]
    assert job.status == EXTRACTION_STATUS_QUEUED
    assert recognizer.calls == 0, "上传请求里不该已经调过模型"


def test_the_worker_picks_the_job_up_and_finishes_it(
    migrated_database: Database, storage: LocalFileStorage, thread_config: Config
) -> None:
    invoices, extraction, recognizer = _services(migrated_database, storage, thread_config)
    view = invoices.create_invoice(
        [UploadedFile(content=_page(), original_name="a.jpg", content_type=None)]
    )

    done = ExtractionWorker(extraction).run_once()

    assert done == 1
    assert recognizer.calls == 1
    assert extraction.jobs_for(view.id)[0].status == EXTRACTION_STATUS_SUCCEEDED


def test_a_claimed_job_is_not_handed_out_twice(
    migrated_database: Database, storage: LocalFileStorage, thread_config: Config
) -> None:
    """⚠️ 占用与执行必须分开提交。

    两个 worker 线程取到同一条任务 = 同一页图调两遍 = 花两份钱，
    还可能产出两套草稿行。
    """
    invoices, extraction, _ = _services(migrated_database, storage, thread_config)
    invoices.create_invoice(
        [UploadedFile(content=_page(), original_name="a.jpg", content_type=None)]
    )

    with migrated_database.unit_of_work() as session:
        first = ExtractionJobRepository(session).claim_next()
        assert first is not None
        assert first.status == EXTRACTION_STATUS_RUNNING

    with migrated_database.unit_of_work() as session:
        second = ExtractionJobRepository(session).claim_next()

    assert second is None


def test_a_job_stuck_in_running_is_recovered_on_start(
    migrated_database: Database, storage: LocalFileStorage, thread_config: Config
) -> None:
    """进程被 kill 时正在跑的任务没人改状态。

    表现是「那张单一直在识别中」——没有任何错误信息，最难发现，所以这条出口必须有。
    """
    invoices, extraction, _ = _services(migrated_database, storage, thread_config)
    view = invoices.create_invoice(
        [UploadedFile(content=_page(), original_name="a.jpg", content_type=None)]
    )
    with migrated_database.unit_of_work() as session:
        job = ExtractionJobRepository(session).claim_next()
        assert job is not None
        job.started_at = datetime.now(timezone.utc) - timedelta(hours=3)

    recovered = extraction.recover_stale()

    assert len(recovered) == 1
    assert extraction.jobs_for(view.id)[0].status == EXTRACTION_STATUS_QUEUED


def test_a_fresh_running_job_is_not_treated_as_stale(
    migrated_database: Database, storage: LocalFileStorage, thread_config: Config
) -> None:
    invoices, extraction, _ = _services(migrated_database, storage, thread_config)
    invoices.create_invoice(
        [UploadedFile(content=_page(), original_name="a.jpg", content_type=None)]
    )
    with migrated_database.unit_of_work() as session:
        ExtractionJobRepository(session).claim_next()

    assert extraction.recover_stale() == []


def test_running_a_job_that_is_already_running_is_refused(
    migrated_database: Database, storage: LocalFileStorage, thread_config: Config
) -> None:
    from src.errors import ConflictError

    invoices, extraction, _ = _services(migrated_database, storage, thread_config)
    invoices.create_invoice(
        [UploadedFile(content=_page(), original_name="a.jpg", content_type=None)]
    )
    with migrated_database.unit_of_work() as session:
        job = ExtractionJobRepository(session).claim_next()
        job_id = job.id

    with pytest.raises(ConflictError, match="白花钱"):
        extraction.run_job(job_id)


def test_an_empty_queue_is_not_an_error(
    migrated_database: Database, storage: LocalFileStorage, thread_config: Config
) -> None:
    _, extraction, _ = _services(migrated_database, storage, thread_config)

    assert ExtractionWorker(extraction).run_once() == 0
