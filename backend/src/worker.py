"""提取任务的后台 worker（步骤 08.3g：提取不挂在 HTTP 请求里）。

### 为什么必须有它

手写件一页约 15 秒（先问结构一轮、再多模型并行读一轮），多页单据更久。挂在 HTTP
请求里的后果是老板按下上传之后对着转圈等，而且浏览器一关、网络一抖，这次提取就
白花了钱却没有结果。步骤 08 因此明确「同步还是 worker：**定为 worker**；提取状态
持久化，老板拍完照可以去忙别的，回来再审」。

### 为什么是应用内线程，而不是 Celery / RQ

骨架基线写明「不引入通用 DI 容器、微服务、Redis 或 Celery 作为骨架前置条件」，
而当前承诺的也只是**本地单工作区演示**。一个进程内的线程池 + 一张落库的任务表
已经满足 08.5 的全部要求（状态持久化、失败可查询、不丢原文件与草稿）。

真要横向扩展时要改的是**取任务那一步**，不是整套设计：见下面「SQLite 下的并发」。

### SQLite 下的并发

`claim_next()` 用了 `SELECT ... FOR UPDATE`，但**SQLite 不支持行锁**，
SQLAlchemy 在 SQLite 上会把它忽略掉。所以本模块额外加一把**进程内互斥锁**：
所有 worker 线程都在同一个进程里，这把锁就够了。

⚠️ **如果以后把 worker 拆成独立进程，这把锁就不管用了**，那时要改成
`BEGIN IMMEDIATE`（SQLite）或真正的行锁（Postgres）。这不是现在的问题，
但必须写在这里——否则将来拆进程时，表现是"同一页图被调两遍"，花两份钱，
而且不报错。

### 独立运行

    python -m src.worker

同一套代码，只是不带 Flask。`--once` 跑完当前队列就退出（给脚本与验收用）。
"""

from __future__ import annotations

import argparse
import atexit
import logging
import signal
import sys
import threading
from types import FrameType

from dotenv import load_dotenv

from src.config import BASE_DIR, OCR_WORKER_THREAD, Config
from src.db import Database
from src.errors import AppError
from src.factory import configure_log_output
from src.integrations.file_storage import LocalFileStorage
from src.integrations.ocr import build_recognizer
from src.services.extraction import ExtractionService
from src.services.invoices import InvoiceService

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 2.0
"""队列空时等多久再看一次。2 秒足够让"上传完马上开始"感觉是即时的，
又不会把 SQLite 文件打扰到。"""

_CLAIM_LOCK = threading.Lock()
"""取任务的进程内互斥锁。见模块文档「SQLite 下的并发」——**拆进程时要换掉它**。"""


class ExtractionWorker:
    """按固定线程数轮询提取队列。"""

    def __init__(
        self,
        service: ExtractionService,
        *,
        threads: int = 2,
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self._service = service
        self._threads = max(1, threads)
        self._poll_interval = poll_interval
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []

    @property
    def running(self) -> bool:
        return any(thread.is_alive() for thread in self._workers)

    def start(self) -> None:
        """起线程开始轮询。先把卡住的任务退回排队。

        **启动时恢复卡住的任务**是必须的：进程上次被 kill 时正在 `running` 的
        任务没人改状态，表现是「那张单一直在识别中」——没有任何错误信息，
        最难发现。
        """
        if self.running:
            return
        try:
            recovered = self._service.recover_stale()
            if recovered:
                logger.warning(
                    "worker recovered %s stale running jobs: %s",
                    len(recovered),
                    [job.id for job in recovered],
                )
        except AppError:
            logger.exception("worker could not recover stale jobs; continuing")

        self._stop.clear()
        for index in range(self._threads):
            thread = threading.Thread(
                target=self._loop, name=f"extraction-worker-{index + 1}", daemon=True
            )
            thread.start()
            self._workers.append(thread)
        logger.info("extraction worker started threads=%s", self._threads)

    def stop(self, *, timeout: float = 10.0) -> None:
        """要求停下并等线程退出。正在跑的那一条会跑完，不中途掐断。

        中途掐断的代价是：钱已经花了（模型已经调过），结果却没写进草稿。
        """
        self._stop.set()
        for thread in self._workers:
            thread.join(timeout=timeout)
        self._workers.clear()
        logger.info("extraction worker stopped")

    def run_once(self) -> int:
        """把当前队列里的任务全部跑完，返回跑了几条。给 `--once` 与测试用。"""
        total = 0
        while True:
            done = self._claim_and_run()
            if not done:
                return total
            total += done

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                done = self._claim_and_run()
            except Exception:  # noqa: BLE001 - worker 线程不能因为一条任务而死掉
                logger.exception("extraction worker iteration failed")
                done = 0
            if not done:
                # 用 Event.wait 而不是 sleep：停止信号能立刻把它唤醒。
                self._stop.wait(self._poll_interval)

    def _claim_and_run(self) -> int:
        with _CLAIM_LOCK:
            return self._service.run_pending(limit=1)


def start_in_process(app, config: Config) -> "ExtractionWorker | None":
    """在 Flask 应用所在的进程里起一个 worker，并登记退出时的收尾。

    为什么放在这里而不是 `app.py`：入口要保持薄（`tests/test_entry.py` 守着这一条），
    而「怎么起一个 worker」是 worker 自己的知识。

    为什么**不放进 `create_app()`**：创建应用不该产生副作用（见 `src/factory.py`
    的组装规则），否则每个测试用例都会悄悄起几个后台线程。

    `OCR_WORKER_MODE` 不是 `thread`、或识别器是 Fake 时返回 None——Fake 是本地瞬时
    的，没有网络调用要等，不值得为它起线程。
    """
    if config.ocr_worker_mode != OCR_WORKER_THREAD or config.ocr_is_fake:
        return None
    with app.app_context():
        from src.factory import build_services  # 延迟导入：避免与工厂互相 import

        _, extraction = build_services(app)
    worker = ExtractionWorker(extraction, threads=config.ocr_worker_threads)
    worker.start()
    # 停下时让正在跑的那一条跑完：中途掐断的代价是钱已经花了（模型已经调过），
    # 结果却没写进草稿。
    atexit.register(worker.stop)
    app.logger.info(
        "extraction worker started in-process threads=%s", config.ocr_worker_threads
    )
    return worker


def start_intake_ingest(app) -> "BackgroundIngest":
    """给整理批次的切页装一个后台线程池（多文件计划 M03）。

    装上它之后，上传请求只做「存字节 + 登记」就返回，切页在请求之外进行：浏览器
    关掉、网络断掉都不影响已经收下的字节。

    为什么**不放进 `create_app()`**：创建应用不该产生副作用（见 `src/factory.py`
    的组装规则）。没装调度器时 `build_intake_services` 用的是 `InlineIngest`
    ——当场切完，测试因此不会悄悄起线程，也不会出现"切页跑在哪一刻说不清"。

    为什么**不是一个轮询 worker**：`intake_batch_files.status` 的 `pending` 本身
    就是队列，切页是本地计算（数页数、解一张图），没有 `running` 中间态，重跑也
    不花钱。所以这里只把切页挪出请求，不做轮询、也**不在启动时扫一遍库**——
    启动不连数据库是本项目守着的一条性质（README 第 10 节）。上次没切完的文件
    停在 `pending`，一个 `POST /api/intake-batches/<id>/process` 就接着切，
    恢复批次时顺手调一次即可。
    """
    from src.factory import INGEST_SCHEDULER_KEY  # 延迟导入：避免与工厂互相 import
    from src.services.intake_ingest import BackgroundIngest

    scheduler = BackgroundIngest()
    app.config[INGEST_SCHEDULER_KEY] = scheduler
    # 停下时让正在切的那一个切完：切到一半被掐断会留下 `pending`，下次照样能接着切。
    atexit.register(scheduler.shutdown)
    app.logger.info("intake ingest scheduler installed (background thread)")
    return scheduler


def build_service(config: Config) -> ExtractionService:
    """按配置组装一套提取服务（与应用工厂同一套依赖，只是不带 Flask）。"""
    database = Database.from_config(config)
    storage = LocalFileStorage(config.file_storage_dir)
    recognizer = build_recognizer(config)
    invoices = InvoiceService(
        database,
        file_storage=storage,
        recognizer=recognizer,
        upload_max_bytes=config.upload_max_bytes,
    )
    return ExtractionService(
        database,
        file_storage=storage,
        recognizer=recognizer,
        invoice_service=invoices,
        config=config,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="提取任务后台 worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="把当前队列跑完就退出（脚本与验收用），不常驻",
    )
    parser.add_argument("--threads", type=int, default=None, help="线程数，默认取配置")
    args = parser.parse_args(argv)

    load_dotenv(BASE_DIR / ".env")
    config = Config.from_env()
    configure_log_output(config)

    if config.ocr_is_fake:
        logger.warning(
            "当前 OCR_PROVIDER=fake：worker 会跑，但产出的是**明确标识的假数据**，"
            "不是真实识别结果。"
        )
    elif not config.has_ocr_credentials:  # pragma: no cover - 配置解析已挡住
        logger.error("缺少 OCR 凭据，worker 无事可做。")
        return 2

    service = build_service(config)
    worker = ExtractionWorker(
        service, threads=args.threads or config.ocr_worker_threads
    )

    if args.once:
        count = worker.run_once()
        logger.info("worker --once finished jobs=%s", count)
        return 0

    worker.start()

    stopping = threading.Event()

    def handle(signum: int, _frame: FrameType | None) -> None:
        logger.info("worker received signal %s; finishing current job", signum)
        stopping.set()

    for name in ("SIGINT", "SIGTERM"):
        received = getattr(signal, name, None)
        if received is not None:
            signal.signal(received, handle)

    try:
        while not stopping.wait(0.5):
            pass
    finally:
        worker.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "ExtractionWorker",
    "POLL_INTERVAL_SECONDS",
    "build_service",
    "main",
    "start_in_process",
    "start_intake_ingest",
]
