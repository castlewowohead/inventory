"""识别服务的边界：统一接口 + Fake 实现 + 步骤 08 接入的真实适配器。

业务层只依赖 `Recognizer` 这个接口和 `src/contracts.py` 的结果类型，不认识任何厂商。

三个实现：

- `FakeRecognizer`：不访问网络、不读凭据，没有 API Key 也能启动和跑测试。它返回的
  每一条文本都带 `FAKE` 标记，`provider="fake"`、`is_fake=True`，并在 `warnings`
  里明说这是假数据——**不冒充真实识别结果**。它刻意留下一个 `UNREADABLE` 字段和
  一个缺失字段，让审核流程从一开始就必须面对这两种情况。
- `DashScopeRecognizer`：单模型调用（步骤 07 选出的主模型 `qwen3.5-ocr`）。
  印刷件走这条——实测印刷件上单模型字段准确率 96.6%、行匹配 100%。
- `CrossCheckRecognizer`：**并行**跑配置里的全部模型（生产配置是 2 个：
  `qwen3.5-ocr` + `qwen3-vl-plus`），逐字段裁决，一致才预填、分歧置
  `UNREADABLE`。并行发请求，总耗时等于最慢的那个，不是相加。

### 请求里的 `content_type` 是**权威**媒体类型

⚠️ 调用方（`services/extraction.py`）负责先把文件解码好：HEIC 转成 JPEG、
EXIF 方向摆正，然后把**解码后的字节**与**嗅探出来的真实媒体类型**放进
`RecognitionRequest`。适配器不再自己猜格式，也**绝不**统一写死 `image/jpeg`——
步骤 07 实测写死会让 PNG 被按错格式解码。认不出的媒体类型直接拒绝调用。

这样安排还有一个原因：调用方本来就要把图解出来做格线检测与格子裁剪，解一次用两处，
而「这张图到底是什么」只有一个地方说得准。

`build_recognizer()` 按配置选择实现。配置里写了未实现的厂商时，`Config` 在启动
解析阶段就会报错（见 src/config.py 的 IMPLEMENTED_OCR_PROVIDERS）；本模块再做一道
防御性检查，保证即使绕过配置校验直接调用，也不会静默退回假数据。
"""

from __future__ import annotations

import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from src.config import OCR_PROVIDER_DASHSCOPE, Config
from src.contracts import (
    FAKE_PROVIDER,
    FIELD_CURRENCY,
    FIELD_INVOICE_DATE,
    FIELD_INVOICE_NUMBER,
    FIELD_INVOICE_TOTAL,
    FIELD_ITEM_DESCRIPTION,
    FIELD_LINE_AMOUNT,
    FIELD_QUANTITY,
    FIELD_SUPPLIER,
    FIELD_UNIT,
    FIELD_UNIT_PRICE,
    BoundingBox,
    FieldEvidence,
    RecognitionRequest,
    RecognitionResult,
    RecognizedField,
    RecognizedLine,
    RecognizedPage,
)
from src.errors import FeatureNotImplementedError
from src.integrations.crosscheck import CrossCheckOutcome, combine
from src.integrations.dashscope import (
    CONTENT_PROMPT,
    HEADER_PROMPT,
    ROW_NUMBER_PROMPT,
    STRUCTURE_PROMPT,
    DashScopeClient,
    ModelCall,
    OcrServiceError,
    StructureAnswer,
    parse_headers,
    parse_row_numbers,
    parse_structure,
    to_recognition_result,
)

logger = logging.getLogger(__name__)

# 允许送给识别服务的媒体类型。HEIC 不在其中：调用方必须先转成 JPEG
# （见 documents.files.decode_image），这里不接受一个"看起来能用"的类型。
SUPPORTED_MEDIA_TYPES = ("image/jpeg", "image/png")

ANCHOR_TIMEOUT_SECONDS = 60
"""锚定调用（读行号列、读表头）的单次等待上限。

实测这两种调用 2—5 秒就返回，输出只有十几个 token。它们**不沿用**整页识别那条
240 秒的上限，因为 `qwen3.5-ocr` 会偶发挂住不返回、耗到超时才失败、重试一次 2 秒
就成功：沿用 240 秒时一次挂起让单页多花 4 分钟（实测 ChangShun p01 跑了 300 秒，
其中 480 秒耗在两次挂起上）。压到 60 秒后，一次挂起的代价回到 60 秒，而重试还在。"""

# 所有假文本都带这个前缀。日志、接口响应与人工复核界面因此一眼能看出数据来源。
FAKE_MARKER = "FAKE"

FAKE_MODEL = "fake-fixture-v1"

FAKE_WARNING = (
    "本结果由 Fake 识别器生成，不是真实识别结果；"
    "真实 OCR 在步骤 07 评估、步骤 08 接入后才可用。"
)


@runtime_checkable
class Recognizer(Protocol):
    """识别器接口。真实适配器与 Fake 实现都满足它。

    实现约定：
    - `recognize()` 不抛厂商专有异常；网络、限流与超时失败要转成本项目的异常类型，
      并保留原始单据与已有草稿（步骤 08 的验收条件）。
    - 认不出来的字段用 `FieldStatus.UNREADABLE`，不要填 0 或空字符串。
    - 厂商没给坐标就把 `evidence.box` 留成 None，不要编造。
    """

    @property
    def provider(self) -> str:
        """适配器标识，写进识别结果与日志。"""

    @property
    def is_fake(self) -> bool:
        """是否为假数据实现。真实适配器返回 False。"""

    def recognize(self, request: RecognitionRequest) -> RecognitionResult:
        """识别一份单据。"""


class FakeRecognizer:
    """明确标识的假识别器：结构真实、内容是假的。

    用途是让上传 → 草稿 → 逐行确认这条流程在没有 OCR 账号时也能开发和测试。
    它不看文件内容（除了算一个摘要用于追溯），因此对任何输入都返回同样的结构。
    """

    provider = FAKE_PROVIDER
    is_fake = True

    def __init__(self, *, now: datetime | None = None) -> None:
        # 注入时间便于测试断言；默认取当前 UTC 时间。
        self._now = now

    def recognize(self, request: RecognitionRequest) -> RecognitionResult:
        digest = hashlib.sha256(request.content).hexdigest()
        recognized_at = self._now or datetime.now(timezone.utc)
        logger.info(
            "fake recognition source_key=%s size=%s sha256=%s",
            request.source_key,
            request.size,
            digest[:12],
        )
        result = RecognitionResult(
            provider=self.provider,
            is_fake=True,
            model=FAKE_MODEL,
            recognized_at=recognized_at,
            pages=self._pages(),
            document_fields=self._document_fields(),
            lines=self._lines(),
            # 原始响应同样标明是假数据，并带上输入摘要，便于把结果关联回文件。
            raw_response=(
                f'{{"{FAKE_MARKER}": true, "model": "{FAKE_MODEL}", '
                f'"source_sha256": "{digest}", "size": {request.size}}}'
            ),
            warnings=(FAKE_WARNING,),
            source_key=request.source_key,
        )
        return result

    @staticmethod
    def _pages() -> tuple[RecognizedPage, ...]:
        return (
            RecognizedPage(
                number=1,
                text=f"{FAKE_MARKER} 第 1 页原文占位",
                width=1700,
                height=2200,
            ),
        )

    @staticmethod
    def _document_fields() -> tuple[RecognizedField, ...]:
        evidence = FieldEvidence(page=1, box=BoundingBox(0.08, 0.05, 0.40, 0.04))
        return (
            RecognizedField.recognized(
                FIELD_SUPPLIER,
                f"{FAKE_MARKER} 示例供应商 / Sample Supplier",
                confidence=0.95,
                evidence=evidence,
            ),
            RecognizedField.recognized(
                FIELD_INVOICE_DATE,
                f"{FAKE_MARKER} 2026-11-09",
                confidence=0.90,
                evidence=FieldEvidence(page=1, box=BoundingBox(0.62, 0.05, 0.22, 0.04)),
            ),
            # 单号没给区域：厂商常常给了文字却没给坐标，契约允许 box 为 None，
            # 这里刻意保留这种情况，不编造坐标。
            RecognizedField.recognized(
                FIELD_INVOICE_NUMBER,
                f"{FAKE_MARKER}-INV-0001",
                confidence=0.80,
                evidence=FieldEvidence(page=1),
            ),
            RecognizedField.recognized(
                FIELD_INVOICE_TOTAL,
                f"{FAKE_MARKER} 286.00",
                confidence=0.70,
                evidence=FieldEvidence(page=1, box=BoundingBox(0.70, 0.82, 0.18, 0.04)),
            ),
            # 币种：假单据上没写。缺失就是缺失，不默认成 USD
            # （步骤 05 明确「不强制供应商/发票号/USD」）。
            RecognizedField.missing(FIELD_CURRENCY),
        )

    @staticmethod
    def _lines() -> tuple[RecognizedLine, ...]:
        return (
            RecognizedLine(
                index=1,
                page=1,
                raw_text=f"{FAKE_MARKER} 大米 Rice 10 lb 6.00 60.00",
                fields=(
                    RecognizedField.recognized(
                        FIELD_ITEM_DESCRIPTION,
                        f"{FAKE_MARKER} 大米 Rice",
                        confidence=0.93,
                        evidence=FieldEvidence(
                            page=1, box=BoundingBox(0.08, 0.30, 0.34, 0.03)
                        ),
                    ),
                    RecognizedField.recognized(
                        FIELD_QUANTITY,
                        "10",
                        confidence=0.91,
                        evidence=FieldEvidence(
                            page=1, box=BoundingBox(0.46, 0.30, 0.07, 0.03)
                        ),
                    ),
                    RecognizedField.recognized(
                        FIELD_UNIT,
                        "lb",
                        confidence=0.88,
                        evidence=FieldEvidence(
                            page=1, box=BoundingBox(0.54, 0.30, 0.07, 0.03)
                        ),
                    ),
                    RecognizedField.recognized(
                        FIELD_UNIT_PRICE,
                        "6.00",
                        confidence=0.89,
                        evidence=FieldEvidence(
                            page=1, box=BoundingBox(0.63, 0.30, 0.10, 0.03)
                        ),
                    ),
                    RecognizedField.recognized(
                        FIELD_LINE_AMOUNT,
                        "60.00",
                        confidence=0.89,
                        evidence=FieldEvidence(
                            page=1, box=BoundingBox(0.75, 0.30, 0.12, 0.03)
                        ),
                    ),
                ),
            ),
            RecognizedLine(
                index=2,
                page=1,
                raw_text=f"{FAKE_MARKER} 冷冻虾 Frozen Shrimp 2 case ? 226.00",
                fields=(
                    RecognizedField.recognized(
                        FIELD_ITEM_DESCRIPTION,
                        f"{FAKE_MARKER} 冷冻虾 Frozen Shrimp",
                        confidence=0.85,
                        evidence=FieldEvidence(
                            page=1, box=BoundingBox(0.08, 0.35, 0.34, 0.03)
                        ),
                    ),
                    RecognizedField.recognized(
                        FIELD_QUANTITY,
                        "2",
                        confidence=0.84,
                        evidence=FieldEvidence(
                            page=1, box=BoundingBox(0.46, 0.35, 0.07, 0.03)
                        ),
                    ),
                    # case 没有包装规格，步骤 09 规定这种行不能换算成基础数量；
                    # 这里照原样返回，由业务层判断，不在识别层猜。
                    RecognizedField.recognized(
                        FIELD_UNIT,
                        "case",
                        confidence=0.82,
                        evidence=FieldEvidence(
                            page=1, box=BoundingBox(0.54, 0.35, 0.07, 0.03)
                        ),
                    ),
                    # 单价这一格字迹模糊：标为认不出来，**不是 0**。
                    RecognizedField.unreadable(
                        FIELD_UNIT_PRICE,
                        evidence=FieldEvidence(
                            page=1, box=BoundingBox(0.63, 0.35, 0.10, 0.03)
                        ),
                    ),
                    RecognizedField.recognized(
                        FIELD_LINE_AMOUNT,
                        "226.00",
                        confidence=0.78,
                        evidence=FieldEvidence(
                            page=1, box=BoundingBox(0.75, 0.35, 0.12, 0.03)
                        ),
                    ),
                ),
            ),
        )


def _require_supported_media_type(request: RecognitionRequest) -> str:
    """取出请求里的权威媒体类型，不认的直接拒绝。

    ⚠️ 这里**不做兜底猜测**。缺类型或类型不支持时宁可让提取任务失败并说清原因，
    也不要默认成 `image/jpeg`——步骤 07 实测写死媒体类型会让 PNG 被按错格式解码，
    而那种错误的表现是"识别结果变差"，不是报错，所以没人会发现。
    """
    media_type = (request.content_type or "").split(";")[0].strip().lower()
    if media_type not in SUPPORTED_MEDIA_TYPES:
        raise OcrServiceError(
            f"识别服务不接受媒体类型 {request.content_type!r}（只接受 "
            f"{'、'.join(SUPPORTED_MEDIA_TYPES)}）。HEIC 要先由 "
            "documents.files.decode_image 转成 JPEG 再送过来。",
            code="unsupported_media_type",
            retryable=False,
        )
    return media_type


class DashScopeRecognizer:
    """单模型识别（步骤 07 选出的主模型）。印刷件走这条，约 5 秒一页。"""

    provider = OCR_PROVIDER_DASHSCOPE
    is_fake = False

    def __init__(self, client: DashScopeClient, *, model: str) -> None:
        self._client = client
        self._model = model

    @property
    def model(self) -> str:
        return self._model

    @property
    def models(self) -> tuple[str, ...]:
        return (self._model,)

    def recognize(self, request: RecognitionRequest) -> RecognitionResult:
        media_type = _require_supported_media_type(request)
        call = self._client.recognize(
            image=request.content,
            media_type=media_type,
            model=self._model,
            prompt=CONTENT_PROMPT,
        )
        return to_recognition_result(call, source_key=request.source_key)

    def recognize_structure(self, request: RecognitionRequest) -> StructureAnswer:
        """第 1 层：先问「哪几行写了东西」，不读内容（步骤 08.3b）。"""
        media_type = _require_supported_media_type(request)
        call = self._client.recognize(
            image=request.content,
            media_type=media_type,
            model=self._model,
            prompt=STRUCTURE_PROMPT,
        )
        return parse_structure(call)

    def read_row_numbers(self, request: RecognitionRequest) -> list[tuple[str, object]]:
        """读行号列拼图（08.3d 锚定）。单模型配置下**只有一个证人**。"""
        return [_read_cells(self._client, request, self._model, ROW_NUMBER_PROMPT, parse_row_numbers)]

    def read_headers(self, request: RecognitionRequest) -> list[tuple[str, object]]:
        """读表头拼图（08.3d 列映射）。"""
        return [_read_cells(self._client, request, self._model, HEADER_PROMPT, parse_headers)]


class CrossCheckRecognizer:
    """多模型**并行**识别 + 逐字段交叉裁决（步骤 08.3c）。

    生产配置是 2 个模型：`qwen3.5-ocr` + `qwen3-vl-plus`。并行发请求，
    总耗时等于最慢的那个，不是相加——步骤 07 实测单页中位 5—7 秒、最慢 19.6 秒。

    **一个模型失败不让整次提取失败**：还有结果的那些照常裁决，并在 `warnings`
    里写明哪个模型失败了。只剩一个模型时，所有字段都没有"一致"可言，于是全部
    按分歧处理——这是保守方向，不是降级后照常预填。
    """

    provider = OCR_PROVIDER_DASHSCOPE
    is_fake = False

    def __init__(self, client: DashScopeClient, *, models: tuple[str, ...]) -> None:
        if not models:
            raise ValueError("至少要有一个模型")
        self._client = client
        self._models = models

    @property
    def model(self) -> str:
        return self._models[0]

    @property
    def models(self) -> tuple[str, ...]:
        return self._models

    def recognize(self, request: RecognitionRequest) -> RecognitionResult:
        return self.cross_check(request).result

    def cross_check(self, request: RecognitionRequest) -> CrossCheckOutcome:
        """跑全部模型并裁决，连逐字段的裁决明细一起返回。"""
        media_type = _require_supported_media_type(request)

        def run(model: str) -> ModelCall | OcrServiceError:
            try:
                return self._client.recognize(
                    image=request.content,
                    media_type=media_type,
                    model=model,
                    prompt=CONTENT_PROMPT,
                )
            except OcrServiceError as error:
                return error

        with ThreadPoolExecutor(max_workers=len(self._models)) as pool:
            outcomes = list(pool.map(run, self._models))

        results = []
        models: list[str] = []
        failures: list[str] = []
        for model, outcome in zip(self._models, outcomes):
            if isinstance(outcome, OcrServiceError):
                failures.append(f"[{model}] 调用失败（{outcome.code}）：{outcome}")
                continue
            results.append(to_recognition_result(outcome, source_key=request.source_key))
            models.append(model)

        if not results:
            # 全部失败：把第一个错误抛出去，由提取任务按 retryable 决定是否重排。
            first = next(item for item in outcomes if isinstance(item, OcrServiceError))
            raise first

        combined = combine(results, models=models)
        if failures:
            logger.warning("crosscheck partial failure: %s", failures)
            extended = RecognitionResult(
                provider=combined.result.provider,
                is_fake=False,
                model=combined.result.model,
                recognized_at=combined.result.recognized_at,
                pages=combined.result.pages,
                document_fields=dict(combined.result.document_fields),
                lines=combined.result.lines,
                raw_response=combined.result.raw_response,
                warnings=combined.result.warnings
                + tuple(failures)
                + (
                    "⚠️ 有模型没有返回结果，因此「多模型一致」这个条件对本页不成立；"
                    "所有字段按需人填处理，不预填。",
                ),
                source_key=combined.result.source_key,
            )
            return CrossCheckOutcome(
                result=extended,
                agreements=combined.agreements,
                line_agreements=combined.line_agreements,
            )
        return combined

    def recognize_structure(self, request: RecognitionRequest) -> StructureAnswer:
        """第 1 层用**主模型一个**问结构。

        理由：这一层的产出只被当作行数上限之一使用，而真正的硬上限是
        `documents.grid` 的行容量（可以逐像素复核）。为一个advisory 的判断题
        再多花一次调用不值得。步骤 07 实测判断题模型答得准。
        """
        media_type = _require_supported_media_type(request)
        call = self._client.recognize(
            image=request.content,
            media_type=media_type,
            model=self._models[0],
            prompt=STRUCTURE_PROMPT,
        )
        return parse_structure(call)

    def read_row_numbers(self, request: RecognitionRequest) -> list[tuple[str, object]]:
        """读行号列拼图，**全部模型并行**（08.3d 锚定）。

        ⚠️ 这一层与第 1 层不同，**必须多模型**。第 1 层的产出只是一个上限，错了
        无非少几行；锚定错了却是静默的——老板会照着**相邻那一行**的字迹选食材，
        不会有任何报错。2026-11-11 实测到过两份各自都连号、却整体差一行的读数，
        正是两个模型比对把它挡下来的（见 `documents.anchor`）。
        """
        return self._read_all(request, ROW_NUMBER_PROMPT, parse_row_numbers)

    def read_headers(self, request: RecognitionRequest) -> list[tuple[str, object]]:
        """读表头拼图，全部模型并行（08.3d 列映射）。"""
        return self._read_all(request, HEADER_PROMPT, parse_headers)

    def _read_all(self, request, prompt, parser) -> list[tuple[str, object]]:
        media_type = _require_supported_media_type(request)

        def run(model: str) -> tuple[str, object]:
            try:
                call = self._client.recognize(
                    image=request.content,
                    media_type=media_type,
                    model=model,
                    prompt=prompt,
                    timeout_seconds=ANCHOR_TIMEOUT_SECONDS,
                )
            except OcrServiceError as error:
                logger.warning("anchor read failed model=%s code=%s", model, error.code)
                return (model, None)
            reading = parser(call)
            return (model, reading.cells if reading.parsed else None)

        with ThreadPoolExecutor(max_workers=len(self._models)) as pool:
            return list(pool.map(run, self._models))


def _read_cells(client, request, model: str, prompt: str, parser) -> tuple[str, object]:
    """调一次模型读拼图。失败或解析不出来就返回 `None`——由 `documents.anchor`
    判成「锚定不成立」，而不是在这里编一个读数。"""
    media_type = _require_supported_media_type(request)
    try:
        call = client.recognize(
            image=request.content,
            media_type=media_type,
            model=model,
            prompt=prompt,
            timeout_seconds=ANCHOR_TIMEOUT_SECONDS,
        )
    except OcrServiceError as error:
        logger.warning("anchor read failed model=%s code=%s", model, error.code)
        return (model, None)
    reading = parser(call)
    return (model, reading.cells if reading.parsed else None)


def build_client(config: Config) -> DashScopeClient:
    """按配置造一个传输客户端。**不发网络请求**（见 src/factory.py 的组装规则）。"""
    return DashScopeClient(
        base_url=config.ocr_base_url,
        api_key=config.ocr_api_key,
        timeout_seconds=config.ocr_timeout_seconds,
        max_attempts=config.ocr_max_attempts,
    )


def build_recognizer(config: Config) -> Recognizer:
    """按配置选择识别器实现。

    `fake` 之外的取值在配置解析阶段就已校验过（含凭据齐备与型号未落选），
    这里再拦一次：宁可明确报错，也不要静默退回假数据——那会让人以为真实识别
    已经接通。

    配了多个型号就用交叉裁决，配一个就用单模型。生产配置是 2 个。
    """
    if config.ocr_provider == FAKE_PROVIDER:
        return FakeRecognizer()
    if config.ocr_provider == OCR_PROVIDER_DASHSCOPE:
        client = build_client(config)
        if len(config.ocr_models) > 1:
            return CrossCheckRecognizer(client, models=config.ocr_models)
        return DashScopeRecognizer(client, model=config.ocr_models[0])
    raise FeatureNotImplementedError(
        f"识别服务 {config.ocr_provider!r} 尚未实现。",
        details={"provider": config.ocr_provider},
    )


__all__ = [
    "ANCHOR_TIMEOUT_SECONDS",
    "FAKE_MARKER",
    "FAKE_MODEL",
    "FAKE_WARNING",
    "SUPPORTED_MEDIA_TYPES",
    "CrossCheckRecognizer",
    "DashScopeRecognizer",
    "FakeRecognizer",
    "Recognizer",
    "build_client",
    "build_recognizer",
]
