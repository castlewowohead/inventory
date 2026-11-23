"""阿里云 Model Studio（百炼）识别适配器：调用、解析、转成统一契约。

步骤 07 用 74 张真实扫描件 + 4 张小票 × 3 个模型、234 次成功调用选出了这套配置。
**本模块的实现细节几乎每一条都对应那轮实测里踩到的一件事**，改动前请先读
`docs/impl_record.md` 步骤 07。

### 选型与接口（步骤 07 实测）

- 主模型 `qwen3.5-ocr`（印刷件字段准确率 96.6%、人工修正率 3.4%、行匹配 100%），
  第二意见 `qwen3-vl-plus`（92.0% / 8.0% / 100%），**并行跑这 2 个**。
- `qwen-vl-ocr` 已落选（48.9%），且是唯一一个把 15 行空表格填满的模型；
  `vanchin/deepseek-ocr` 调用返回「产品未开通」。两者都在 `config.py` 里被拒。
- OpenAI 兼容接口 `POST {base}/chat/completions`，图片走 `image_url` 的 base64
  data URL。endpoint 用工作空间专属域名（老域名 `dashscope.aliyuncs.com`
  自 2026-09-30 起不再支持新功能）。

### 四条必须照做的实测结论

1. ⚠️ **data URL 的媒体类型必须按真实格式给**，不能统一写死 `image/jpeg`——
   写死会让 PNG 被按错格式解码。媒体类型由 `documents.files` 嗅探出来。
2. ⚠️ **三个模型都从不输出 JSON `null`**，哪怕提示词第 3 条明确要求。空缺一律是
   **空字符串**（`qwen-vl-ocr` 1,859 个、`qwen3.5-ocr` 253 个、
   `qwen3-vl-plus` 39 个）。按 `null` 判空会漏掉**全部**空缺。
3. ⚠️ **输出格式不一致**：`qwen3.5-ocr` 一律用 ```json 围栏包裹，
   `qwen3-vl-plus` 给裸 JSON；234 份里有 1 份解析失败。
   **解析失败必须有处理分支**，不能假设一定是合法 JSON。
4. ⚠️ **提示词是被测配置的一部分**。步骤 07 的 234 份历史结果绑定旧
   SHA `12e0246b0377`；M01 将运行时提示词改为 `67fb7df1217b`，加入商家
   英文、中文及原文线索。旧准确率不能当成新配置结果；测试守住新 SHA。

### `UNREADABLE` 自报只能当旁证

`qwen3-vl-plus` 用过 141 次 `UNREADABLE`，且**全部落在 68 张中文手写件上、
10 张清晰页一次没有**——方向是对的。但没有 GT 就无法验证它说"认不出"的格子
是真认不出还是本来能读，所以**它不能当裁判**。本模块照原样记下这个状态，
判空仍然靠我们自己算出来的事实（行容量上限、第 0 层墨迹检测、多模型分歧）。

### 本模块不做业务判断

转成契约就交出去。「这个值能不能预填」「这一行能不能确认」属于
`services/extraction.py` 与步骤 05 的审核规则。识别层多猜一步，错误就会被当成
事实存下来。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.contracts import (
    FIELD_CURRENCY,
    FIELD_INVOICE_DATE,
    FIELD_INVOICE_NUMBER,
    FIELD_INVOICE_TOTAL,
    FIELD_ITEM_DESCRIPTION,
    FIELD_LINE_AMOUNT,
    FIELD_QUANTITY,
    FIELD_SUPPLIER,
    FIELD_SUPPLIER_ENGLISH,
    FIELD_SUPPLIER_CHINESE,
    FIELD_SUPPLIER_EVIDENCE,
    FIELD_UNIT,
    FIELD_UNIT_PRICE,
    FieldEvidence,
    FieldStatus,
    RecognitionResult,
    RecognizedField,
    RecognizedLine,
    RecognizedPage,
)

logger = logging.getLogger(__name__)

PROVIDER = "dashscope"

# 步骤 07 的旧配置保留在 tools/ocr_batch.py；其历史结果绑定 12e0246b0377。
# 本提示词是 M01 新配置，不能把旧评测准确率记到它名下。
CONTENT_PROMPT = """这是一张餐厅采购单据的扫描件。请只识别你在图上真正看到的内容。

以 JSON 输出，结构如下：
{
  "invoice_number": ...,
  "invoice_date": ...,
  "invoice_total": ...,
  "supplier_english": ...,
  "supplier_chinese": ...,
  "supplier_evidence": ...,
  "lines": [
    {"index": 1, "item_description": ..., "quantity": ..., "unit": ..., "unit_price": ..., "line_amount": ...}
  ]
}

必须遵守的规则：
1. 每个字段填你在图上看到的原文，保持原样：不要改写数字格式、不要补零、不要加千分位、不要把中文标点换成英文标点。
2. 看得见但认不出来（字迹潦草、涂改、反光、太淡）→ 该字段填字符串 "UNREADABLE"。
3. 单据上根本没有这一项 → 该字段填 null。
4. 不要做任何计算。invoice_total 只能来自单据上印着或写着的那个总额数字；单据上没有总额就填 null，**禁止把各行金额相加后填进去**。
5. 表格里没有填写内容的空白行不要输出。
6. supplier_english 和 supplier_chinese 只填纸面上作为卖方、开票方或商家印出的英文和中文名称；买方、收货方、付款方、商品品牌都不是商家。两种文字都出现就分别照抄；只出现一种就把另一项填 null，绝不翻译、扩写或根据商品推测。
7. supplier_evidence 填纸面上可见的卖方标签及名称原文片段；没有可引用的片段填 null，不要编造位置或文字。不能确定谁是卖方时，商家字段填 "UNREADABLE"，不要把买方填进去。纸面确实没有商家时填 null。
8. 只输出 JSON，不要解释。"""

CONTENT_PROMPT_SHA12 = "67fb7df1217b"
"""`CONTENT_PROMPT` 的 sha256 前 12 位。被测配置的身份证。"""

# 第 1 层「先定结构，不读内容」的提示词（步骤 08.3b）。
#
# 它**只问判断题**，一个字都不要求转录。理由是步骤 07 的两条实测：
# ① 结构化提问（"把这个表填满"）正是诱发编造的那一步——ChangShun p01 纸上只写了
#    4 行、第 5—15 行全空还画了斜线，`qwen-vl-ocr` 报出了 15 行，而那张表恰好
#    印着 15 个行格；
# ② 同一批结果里，判断题模型答得准。
#
# ⚠️ 这是一个**新的**提示词，没有经过步骤 07 那轮评测。它的产出只被当作
# **上限之一**使用（与行容量取较小者），不单独决定任何值。
STRUCTURE_PROMPT = """这是一张餐厅采购单据的扫描件。**不要转录任何文字或数字。**

只回答一个问题：表格里**哪几行写了东西**。

以 JSON 输出，结构如下：
{
  "written_rows": [1, 2, 5],
  "printed_row_slots": 15,
  "handwritten": true
}

规则：
1. written_rows 是从上往下数、**确实写了商品内容**的那些行的序号，从 1 开始。
2. 空白行、画了斜线作废的行、只有签名或备注的行**都不算**。
3. printed_row_slots 是这张表**印着多少个空行格**（数格子，不是数写了字的行）；数不出来就填 null。
4. handwritten 表示内容是手写的还是印刷的。
5. 不要输出任何商品名、数量或金额。
6. 只输出 JSON，不要解释。"""

# 批次 2 新增的两个提示词（08.3a / 08.3d 的锚定）。
#
# 它们是独立配置。步骤 07 的结果绑定旧内容提示词；M01 已另开新 SHA。
# 这两个提示词问的都是**印刷体**内容——行号与表头——而印刷体正是模型最准的那一类
# （步骤 07 实测字段准确率 96.6%）。它们都**不读手写内容**，也不产出任何草稿值。
ROW_NUMBER_PROMPT = """这是一张预印表格**最左侧一列**的格子，已经按从上到下的顺序一格一格裁下来，
再上下拼在一起，相邻两格之间留了空白、各描了一个淡灰色的框。

只回答一件事：**每一格里印着的行号数字是多少**。

以 JSON 输出，结构如下：
{"cells": [null, null, 1, 2, 3]}

规则：
1. cells 从上往下一个格子一个元素，不要合并也不要跳过。
2. 格子里印着阿拉伯数字行号 → 填那个数字（整数）。例如印着 "7." 就填 7。
3. 格子里是文字（例如 NAME、ADDRESS、品名、數量、RECEIVED BY）、是空白、或只有手写笔迹 → 填 null。
4. **只看印刷体数字，不要读手写内容**，手写的数量和金额一律忽略。
5. 认不出来的格子填 "UNREADABLE"。
6. 只输出 JSON，不要解释。"""

# ⚠️ **不要在这段里写明"图里有 N 个格子"。** 2026-11-11 实测：写明之后
# `qwen3-vl-plus` 会**整体平移一格去凑够 N**——ChangShun p01 上一个模型答
# `[null,1,2,…,15]`、另一个答 `[null,null,1,…,14]`，两份各自都是连号、各自都能过
# 本地校验，却差了一整行。**把期望长度写进提示词会诱发凑长度**；长度对不对交给
# 本地校验（`documents.anchor`）判，两个模型不一致就直接判锚定不成立。

HEADER_PROMPT = """这是一张预印表格**表头那一行**的格子，已经按从左到右的顺序一格一格裁下来，
再上下拼在一起，相邻两格之间留了空白、各描了一个淡灰色的框。

只回答一件事：**每一格的表头印的是哪一类**。

以 JSON 输出，结构如下：
{"columns": ["row_number", "item_description", "quantity", "unit_price", "line_amount"]}

每一格只能填下面这几个取值之一：
- "row_number"：行号列（印着 1、2、3… 的那一列，表头常常是空的）
- "item_description"：品名 / 名稱 / 貨品 / DESCRIPTION / ITEM
- "quantity"：數量 / 件數 / QTY / QUANTITY
- "weight"：重量 / WEIGHT
- "unit"：單位 / UNIT
- "unit_price"：價格 / 單價 / PRICE / U. PRICE / UNIT PRICE
- "line_amount"：金額 / 總數 / 該銀 / AMOUNT / TOTAL
- "other"：以上都不是，或者这一格是空的、认不出来

规则：
1. columns 从上往下一个格子一个元素（它们在原表里是从左到右的几列），不要合并也不要跳过。
2. **按表头印的字判**，不要去看下面写了什么。
3. 拿不准就填 "other"，不要猜。
4. 只输出 JSON，不要解释。"""


# 模型用来表示「看得见但认不出」的写法。
# ⚠️ 空字符串**也在这里**：步骤 07 实测三个模型从不输出 JSON null，空缺一律是空
# 字符串。但空字符串与 "UNREADABLE" 的含义不同，分别由下面两个集合区分。
UNREADABLE_MARKERS = frozenset({"unreadable", "unreadble", "无法识别", "认不出", "?", "？"})

# 模型用来表示「单据上没有这一项」的写法。
#
# ⚠️ **破折号 `-` 刻意不在这里。** 它是纸面上真实存在的一个字符，模型只是把它
# 转录了下来。把它翻译成「单据上没有这一项」是适配器替纸面下结论——而本模块的
# 职责是保留原文。实测 `qwen3-vl-plus` 在 10 个 `line_amount` 上给了 `-`：
# 保留原文之后，老板看到的就是单据上那个破折号，数值那边由
# `normalization.normalize_number_text` 明确拒绝并留一条说明，格子留空等他填。
MISSING_MARKERS = frozenset({"null", "none", "n/a", "na", "无", "没有"})

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

# 可以重试的 HTTP 状态：限流与服务端故障。
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

RETRY_BASE_SECONDS = 2.0
"""重试退避的基数。第 n 次重试等 RETRY_BASE_SECONDS × 2^(n-1) 秒。"""


class OcrServiceError(RuntimeError):
    """识别服务调用失败。

    `retryable` 区分「再试一次可能就好了」（限流、超时、502）与「再试一百次也
    一样」（Key 无效、型号未开通、图片格式不支持）。提取任务据此决定是不是重排。

    ⚠️ 失败**不丢原文件、不丢草稿**（步骤 08.5 的验收条件）：单据与草稿在调用
    之前就已经落库，这个异常只让提取任务失败，不回滚单据。
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool,
        status: int | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class ModelCall:
    """一次模型调用的原始结果。**原文一律保留**，解析失败也保留。"""

    model: str
    prompt_sha12: str
    content: str
    elapsed_seconds: float
    usage: dict[str, Any] | None = None
    request_id: str | None = None
    attempts: int = 1

    def summary(self) -> dict[str, Any]:
        """可写进日志与任务记录的摘要：**不含识别到的业务内容**，也不含凭据。"""
        return {
            "model": self.model,
            "prompt_sha12": self.prompt_sha12,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "total_tokens": (self.usage or {}).get("total_tokens"),
            "request_id": self.request_id,
            "attempts": self.attempts,
            "content_chars": len(self.content),
        }


class DashScopeClient:
    """OpenAI 兼容的 chat/completions 调用。

    构造它**不发网络请求**（src/factory.py 的组装规则）。凭据只从注入的配置来，
    不读环境变量，也**不进任何日志与返回值**。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: int = 240,
        max_attempts: int = 3,
        sleep=time.sleep,
    ) -> None:
        if not base_url:
            raise ValueError("base_url 不能为空：没有默认域名，必须显式给出工作空间专属域名")
        if not api_key:
            raise ValueError("api_key 不能为空")
        self._endpoint = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._max_attempts = max(1, max_attempts)
        self._sleep = sleep

    @property
    def endpoint(self) -> str:
        """调用地址。**不是秘密**，排查时第一个要看的就是它。"""
        return self._endpoint

    def recognize(
        self,
        *,
        image: bytes,
        media_type: str,
        model: str,
        prompt: str,
        timeout_seconds: float | None = None,
    ) -> ModelCall:
        """调一次模型。失败抛 `OcrServiceError`，重试已在内部做过。

        `timeout_seconds` 可以**为这一次调用**压低等待上限。批次 2 的锚定调用
        （读行号列、读表头）用得上：它们的图小、输出只有十几个 token，实测 2—5 秒
        就回来；而整页识别偶尔要 20 秒，所以不能共用一个上限。

        ⚠️ **为什么要压低**：实测 `qwen3.5-ocr` 会偶发地挂住不返回，一直耗到超时
        才失败，然后**重试一次就 2 秒成功**。锚定调用沿用整页那条 240 秒的上限时，
        一次偶发挂起就让这一页多花 4 分钟（实测 ChangShun p01 单页 300 秒，其中
        480 秒花在两次挂起上）。压到 60 秒之后，一次挂起的代价是 60 秒而不是 240 秒。
        """
        if not media_type:
            raise OcrServiceError(
                "没有媒体类型，拒绝调用。"
                "⚠️ 不能统一写死 image/jpeg——步骤 07 实测写死会让 PNG 被按错格式解码。",
                code="missing_media_type",
                retryable=False,
            )
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{media_type};base64,"
                                + base64.b64encode(image).decode()
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        }
        body = json.dumps(payload).encode()
        prompt_sha12 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]

        last: OcrServiceError | None = None
        for attempt in range(1, self._max_attempts + 1):
            started = time.monotonic()
            try:
                content, usage, request_id = self._post(body, timeout_seconds)
            except OcrServiceError as error:
                last = error
                if not error.retryable or attempt == self._max_attempts:
                    logger.warning(
                        "ocr call failed model=%s code=%s attempt=%s/%s retryable=%s",
                        model,
                        error.code,
                        attempt,
                        self._max_attempts,
                        error.retryable,
                    )
                    raise
                delay = RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                logger.info(
                    "ocr call retrying model=%s code=%s attempt=%s/%s in %.1fs",
                    model,
                    error.code,
                    attempt,
                    self._max_attempts,
                    delay,
                )
                self._sleep(delay)
                continue
            elapsed = time.monotonic() - started
            call = ModelCall(
                model=model,
                prompt_sha12=prompt_sha12,
                content=content,
                elapsed_seconds=elapsed,
                usage=usage,
                request_id=request_id,
                attempts=attempt,
            )
            logger.info("ocr call ok %s", call.summary())
            return call
        assert last is not None  # 循环要么 return 要么 raise
        raise last

    def _post(
        self, body: bytes, timeout_seconds: float | None = None
    ) -> tuple[str, dict[str, Any] | None, str | None]:
        request = urllib.request.Request(
            self._endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=timeout_seconds or self._timeout
            ) as response:
                parsed = json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:1000]
            retryable = error.code in RETRYABLE_STATUS
            raise OcrServiceError(
                _http_message(error.code, detail),
                code=f"http_{error.code}",
                retryable=retryable,
                status=error.code,
                detail=detail,
            ) from None
        except urllib.error.URLError as error:
            # 超时与连接失败都走这里。两者都值得再试一次。
            raise OcrServiceError(
                f"识别服务连不上：{error.reason}。原始单据与草稿都已保存，可以稍后重试提取。",
                code="network",
                retryable=True,
            ) from None
        except (TimeoutError, OSError) as error:
            raise OcrServiceError(
                f"识别服务超时或连接中断：{error}。原始单据与草稿都已保存，可以稍后重试提取。",
                code="timeout",
                retryable=True,
            ) from None
        except json.JSONDecodeError as error:
            raise OcrServiceError(
                f"识别服务返回的不是 JSON：{error}",
                code="bad_envelope",
                retryable=True,
            ) from None

        choices = parsed.get("choices") or []
        if not choices:
            raise OcrServiceError(
                "识别服务的响应里没有 choices。",
                code="no_choices",
                retryable=True,
                detail=json.dumps(parsed, ensure_ascii=False)[:400],
            )
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str):
            raise OcrServiceError(
                f"识别服务返回的 content 不是字符串（{type(content).__name__}）。",
                code="bad_content",
                retryable=True,
            )
        return content, parsed.get("usage"), parsed.get("id")


def _http_message(status: int, detail: str) -> str:
    """把 HTTP 错误翻成一句**能照着做下一步**的话。

    这几条都是步骤 07 真实遇到过的：`The product is not activated` 出现了 78 次
    （第三方市场模型要在控制台单独开通）。
    """
    if status in (401, 403):
        return (
            f"识别服务拒绝了凭据（HTTP {status}）。请检查 OCR_API_KEY 是否有效、"
            "以及它是否属于 OCR_BASE_URL 这个工作空间。"
        )
    if status == 429:
        return f"识别服务限流（HTTP 429）。已自动重试；仍然失败就稍后再提取。详情：{detail[:200]}"
    if status == 400 and "not activated" in detail.lower():
        return (
            "这个型号在当前账号下未开通（HTTP 400 product is not activated）。"
            "第三方市场模型需要先在百炼控制台单独开通产品。"
        )
    if status == 404:
        return (
            f"识别服务找不到这个接口（HTTP 404）。请检查 OCR_BASE_URL 是否是"
            "工作空间专属域名，且以 /v1 这类版本段结尾。"
        )
    return f"识别服务返回 HTTP {status}：{detail[:300]}"


def parse_model_output(content: str) -> dict[str, Any] | None:
    """把模型返回的文本解析成结构。解析不出来就返回 None，**不猜**。

    与 `tools/eval_ocr.py::parse_model_output()` 同一套逻辑：剥 ```json 围栏、
    容忍 JSON 前后的解释文字。步骤 07 实测 `qwen3.5-ocr` 一律带围栏、
    `qwen3-vl-plus` 给裸 JSON，234 份里有 1 份怎么都解析不出来——
    所以**解析失败是一个必须有的分支**，不是异常情况。
    """
    text = (content or "").strip()
    if not text:
        return None
    match = _FENCE.search(text)
    if match:
        text = match.group(1).strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < start:
            return None
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


@dataclass(frozen=True)
class RawValue:
    """模型给某个字段的原始取值，以及它属于三态里的哪一种。"""

    raw_text: str | None
    status: str
    """`recognized` / `unreadable` / `missing`，取值与 contracts.FieldStatus 一致。"""

    note: str | None = None


NOT_ASKED = object()
"""响应缺少提示词要求的键；与模型明确返回 JSON null 分开记录。

步骤 07 的历史提示词没有要求商家，旧结果必须继续按旧 SHA 解读。
"""


def classify_value(value: Any) -> RawValue:
    """把模型给的一个取值归到契约三态。

    ⚠️ **空字符串按「认不出」处理，不按「单据上没有」处理。** 步骤 07 实测三个
    模型从不输出 JSON `null`，空缺一律是空字符串——也就是说我们**分不开**
    「纸上没这一项」和「模型没读出来」。两者里选保守的那个：`UNREADABLE` 要求
    人来填，`MISSING` 会让审核界面以为这里本来就没东西。把「没读出来」错标成
    「纸上没有」，等于悄悄把一个要人确认的格子变成了不用管的格子。
    """
    if value is NOT_ASKED:
        return RawValue(
            None,
            "missing",
            "响应没有这个键；它不是模型明确返回的 JSON null。"
            "若当前提示词确实要求该字段，应检查模型是否漏答。",
        )
    if value is None:
        return RawValue(
            None,
            "missing",
            "模型给的是 JSON null。⚠️ 步骤 07 实测三个模型**从不**这么做"
            "（234 份结果里 0 次），出现这条说明模型行为变了，值得看一眼。",
        )
    if isinstance(value, bool):
        return RawValue(None, "unreadable", f"模型给了布尔值 {value!r}，不是一个取值")
    if isinstance(value, (int, float)):
        return RawValue(str(value), "recognized", None)
    if not isinstance(value, str):
        return RawValue(None, "unreadable", f"模型给了 {type(value).__name__}，不是文本")

    text = value.strip()
    if not text:
        return RawValue(
            None,
            "unreadable",
            "模型给的是空字符串。实测三个模型从不输出 null，空缺一律是空字符串，"
            "因此分不开「纸上没有」和「没读出来」——按需要人填处理。",
        )
    folded = text.casefold()
    if folded in UNREADABLE_MARKERS:
        return RawValue(None, "unreadable", f"模型自报认不出（{text!r}）")
    if folded in MISSING_MARKERS:
        return RawValue(None, "missing", f"模型自报单据上没有这一项（{text!r}）")
    return RawValue(text, "recognized", None)


def _field_of(name: str, value: Any, *, page: int) -> tuple[RecognizedField, str | None]:
    raw = classify_value(value)
    evidence = FieldEvidence(page=page)
    if raw.status == "recognized" and raw.raw_text:
        # ⚠️ 坐标**不编造**：提示词没有要求坐标，模型也没给。证据里只有页码。
        # 格子位置由 documents.grid 自己按印刷格线算，并在 raw_fields 里标
        # box_source=grid，与"厂商给的坐标"永远分得清。
        return RecognizedField.recognized(name, raw.raw_text, evidence=evidence), raw.note
    if raw.status == "missing":
        return RecognizedField.missing(name), raw.note
    return RecognizedField.unreadable(name, evidence=evidence), raw.note


def _supplier_from_names(fields: dict[str, RecognizedField], *, page: int) -> RecognizedField:
    """Choose only a paper reading, with English taking precedence over Chinese."""
    english = fields[FIELD_SUPPLIER_ENGLISH]
    chinese = fields[FIELD_SUPPLIER_CHINESE]
    if english.status is FieldStatus.RECOGNIZED:
        return RecognizedField.recognized(
            FIELD_SUPPLIER, english.raw_text or "", evidence=english.evidence
        )
    if english.status is FieldStatus.MISSING and chinese.status is FieldStatus.RECOGNIZED:
        return RecognizedField.recognized(
            FIELD_SUPPLIER, chinese.raw_text or "", evidence=chinese.evidence
        )
    if english.status is FieldStatus.MISSING and chinese.status is FieldStatus.MISSING:
        return RecognizedField.missing(FIELD_SUPPLIER)
    return RecognizedField.unreadable(FIELD_SUPPLIER, evidence=FieldEvidence(page=page))


@dataclass(frozen=True)
class StructureAnswer:
    """第 1 层「哪几行写了东西」的回答。

    ⚠️ 它**不是裁判**，只是一个上限来源。与 `documents.grid` 的行容量取较小者，
    而行容量才是那个可以逐像素复核的硬事实。
    """

    written_rows: tuple[int, ...] = field(default_factory=tuple)
    printed_row_slots: int | None = None
    handwritten: bool | None = None
    parsed: bool = False
    note: str | None = None


def parse_structure(call: ModelCall) -> StructureAnswer:
    """解析第 1 层的回答。解析不出来就返回 `parsed=False`，不编一个结构。"""
    data = parse_model_output(call.content)
    if data is None:
        return StructureAnswer(
            parsed=False,
            note=f"模型 {call.model} 的结构回答解析不出 JSON，本层不产出任何上限。",
        )
    rows = data.get("written_rows")
    written: list[int] = []
    if isinstance(rows, list):
        for item in rows:
            if isinstance(item, bool):
                continue
            if isinstance(item, int) and item >= 1:
                written.append(item)
            elif isinstance(item, str) and item.strip().isdigit():
                number = int(item.strip())
                if number >= 1:
                    written.append(number)
    slots = data.get("printed_row_slots")
    slot_count = slots if isinstance(slots, int) and not isinstance(slots, bool) and slots > 0 else None
    handwritten = data.get("handwritten")
    return StructureAnswer(
        written_rows=tuple(sorted(set(written))),
        printed_row_slots=slot_count,
        handwritten=handwritten if isinstance(handwritten, bool) else None,
        parsed=True,
    )


@dataclass(frozen=True)
class CellReading:
    """拼图里逐格读出来的一列取值（行号或表头类别）。

    ⚠️ 它**不是裁判**。`cells` 只是一个模型对一张**我们自己拼出来的图**的读数；
    它合不合法、两个模型对不对得上，由 `documents.anchor` 用本地规则判。
    """

    cells: tuple[int | str | None, ...] = field(default_factory=tuple)
    parsed: bool = False
    note: str | None = None


def parse_row_numbers(call: ModelCall) -> CellReading:
    """解析行号列的逐格读数。解析不出来就 `parsed=False`，不编一个序列。"""
    data = parse_model_output(call.content)
    if data is None:
        return CellReading(
            parsed=False,
            note=f"模型 {call.model} 的行号回答解析不出 JSON，本页不建立锚定。",
        )
    raw = data.get("cells") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return CellReading(
            parsed=False,
            note=f"模型 {call.model} 的行号回答里没有 cells 数组，本页不建立锚定。",
        )
    cells: list[int | None] = []
    for item in raw:
        if isinstance(item, bool) or item is None:
            cells.append(None)
        elif isinstance(item, int):
            cells.append(item if item >= 1 else None)
        elif isinstance(item, str):
            # ⚠️ 步骤 07 实测三个模型**从不输出 JSON null**，空缺一律是空字符串；
            # `qwen3.5-ocr` 还会把整数包成字符串（"1"）甚至把 null 写成 "null"。
            text = item.strip().rstrip(".。、").strip()
            cells.append(int(text) if text.isdigit() and int(text) >= 1 else None)
        else:
            cells.append(None)
    return CellReading(cells=tuple(cells), parsed=True)


def parse_headers(call: ModelCall) -> CellReading:
    """解析表头逐格读数。只保留字符串取值，合法性交给 `documents.anchor` 判。"""
    data = parse_model_output(call.content)
    if data is None:
        return CellReading(
            parsed=False,
            note=f"模型 {call.model} 的表头回答解析不出 JSON，本页不映射列。",
        )
    raw = data.get("columns") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return CellReading(
            parsed=False,
            note=f"模型 {call.model} 的表头回答里没有 columns 数组，本页不映射列。",
        )
    return CellReading(
        cells=tuple(
            item.strip().lower() if isinstance(item, str) and item.strip() else None
            for item in raw
        ),
        parsed=True,
    )


def to_recognition_result(
    call: ModelCall,
    *,
    page: int = 1,
    page_width: int | None = None,
    page_height: int | None = None,
    source_key: str | None = None,
    extra_warnings: tuple[str, ...] = (),
) -> RecognitionResult:
    """把一次模型调用转成统一契约。

    解析失败时返回一个**没有行**的结果，并在 `warnings` 里说明——原始输出仍然
    保留在 `raw_response` 里。这正是步骤 07 那 1/234 份解析失败要走的分支。
    """
    warnings: list[str] = list(extra_warnings)
    data = parse_model_output(call.content)
    recognized_at = datetime.now(timezone.utc)
    pages = (
        RecognizedPage(number=page, text=None, width=page_width, height=page_height),
    )

    if data is None:
        warnings.append(
            f"模型 {call.model} 的输出解析不出 JSON（{len(call.content)} 个字符已"
            "原样保留在 raw_response 里）。本页没有产出任何草稿行——"
            "**不猜**，请对着原件手工加行。"
        )
        return RecognitionResult(
            provider=PROVIDER,
            is_fake=False,
            model=call.model,
            recognized_at=recognized_at,
            pages=pages,
            document_fields={},
            lines=(),
            raw_response=call.content,
            warnings=tuple(warnings),
            source_key=source_key,
        )

    document_fields: list[RecognizedField] = []
    for name, key in (
        (FIELD_INVOICE_NUMBER, "invoice_number"),
        (FIELD_INVOICE_DATE, "invoice_date"),
        (FIELD_INVOICE_TOTAL, "invoice_total"),
        (FIELD_SUPPLIER_ENGLISH, "supplier_english"),
        (FIELD_SUPPLIER_CHINESE, "supplier_chinese"),
        (FIELD_SUPPLIER_EVIDENCE, "supplier_evidence"),
        (FIELD_CURRENCY, "currency"),
    ):
        # M01 商家键已被明确要求；模型省略时不能误判为纸面没有。
        value = data[key] if key in data else (
            "UNREADABLE" if name in {FIELD_SUPPLIER_ENGLISH, FIELD_SUPPLIER_CHINESE}
            else NOT_ASKED
        )
        recognized, note = _field_of(
            name, value, page=page
        )
        document_fields.append(recognized)
        if key not in data and name in {FIELD_SUPPLIER_ENGLISH, FIELD_SUPPLIER_CHINESE}:
            warnings.append(f"{name}：模型未返回提示词要求的键，按需人工确认处理。")
        if note:
            warnings.append(f"{name}：{note}")
    names = {item.name: item for item in document_fields}
    document_fields.append(_supplier_from_names(names, page=page))

    lines: list[RecognizedLine] = []
    raw_lines = data.get("lines")
    if not isinstance(raw_lines, list):
        warnings.append(
            f"模型 {call.model} 没有给出 lines 数组（给的是 "
            f"{type(raw_lines).__name__}），本页没有产出草稿行。"
        )
        raw_lines = []
    for offset, item in enumerate(raw_lines, start=1):
        if not isinstance(item, dict):
            warnings.append(f"第 {offset} 条行记录不是对象，已跳过。")
            continue
        fields: list[RecognizedField] = []
        for name, key in (
            (FIELD_ITEM_DESCRIPTION, "item_description"),
            (FIELD_QUANTITY, "quantity"),
            (FIELD_UNIT, "unit"),
            (FIELD_UNIT_PRICE, "unit_price"),
            (FIELD_LINE_AMOUNT, "line_amount"),
        ):
            recognized, note = _field_of(
                name, item[key] if key in item else NOT_ASKED, page=page
            )
            fields.append(recognized)
            if note:
                warnings.append(f"第 {len(lines) + 1} 行 {name}：{note}")
        lines.append(
            RecognizedLine(
                index=len(lines) + 1,
                page=page,
                raw_text=json.dumps(item, ensure_ascii=False),
                fields=fields,
            )
        )

    return RecognitionResult(
        provider=PROVIDER,
        is_fake=False,
        model=call.model,
        recognized_at=recognized_at,
        pages=pages,
        document_fields=document_fields,
        lines=tuple(lines),
        raw_response=call.content,
        warnings=tuple(warnings),
        source_key=source_key,
    )


__all__ = [
    "CONTENT_PROMPT",
    "CONTENT_PROMPT_SHA12",
    "CellReading",
    "DashScopeClient",
    "HEADER_PROMPT",
    "MISSING_MARKERS",
    "NOT_ASKED",
    "ModelCall",
    "OcrServiceError",
    "PROVIDER",
    "RETRYABLE_STATUS",
    "RawValue",
    "ROW_NUMBER_PROMPT",
    "STRUCTURE_PROMPT",
    "StructureAnswer",
    "UNREADABLE_MARKERS",
    "classify_value",
    "parse_headers",
    "parse_model_output",
    "parse_row_numbers",
    "parse_structure",
    "to_recognition_result",
]
