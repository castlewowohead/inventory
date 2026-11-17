"""真实 OCR 适配器与交叉裁决的测试（步骤 08.1 / 08.3c）。

**不访问网络**：传输层用假响应注入。真实接口的行为由
`tools/eval_adapter.py live` 与 `tools/e2e_extraction.py` 验，结论记在实施记录里。

这组用例里的每一条几乎都对应步骤 07 实测到的一件事。改动前请先读
`docs/impl_record.md` 步骤 07——这些不是凭空想出来的边界。
"""

from __future__ import annotations

import hashlib
import json

import pytest

from src.config import Config
from src.contracts import FieldStatus, RecognitionRequest
from src.integrations.crosscheck import AGREEMENT_NOTE, combine, judge
from src.integrations.dashscope import (
    CONTENT_PROMPT,
    CONTENT_PROMPT_SHA12,
    NOT_ASKED,
    DashScopeClient,
    ModelCall,
    OcrServiceError,
    classify_value,
    parse_model_output,
    parse_structure,
    to_recognition_result,
)
from src.integrations.ocr import (
    SUPPORTED_MEDIA_TYPES,
    CrossCheckRecognizer,
    DashScopeRecognizer,
    build_recognizer,
)


def _call(content: str, model: str = "qwen3.5-ocr") -> ModelCall:
    return ModelCall(
        model=model,
        prompt_sha12=CONTENT_PROMPT_SHA12,
        content=content,
        elapsed_seconds=1.0,
    )


class _FakeClient:
    """假传输层：按模型名返回预置文本，记下每次调用的参数。"""

    def __init__(self, replies: dict[str, str], *, fail: dict[str, OcrServiceError] | None = None):
        self.replies = replies
        self.fail = fail or {}
        self.calls: list[dict] = []

    def recognize(self, *, image: bytes, media_type: str, model: str, prompt: str) -> ModelCall:
        self.calls.append(
            {"image": image, "media_type": media_type, "model": model, "prompt": prompt}
        )
        if model in self.fail:
            raise self.fail[model]
        return _call(self.replies[model], model=model)


# --- 提示词的身份 -----------------------------------------------------------


def test_content_prompt_is_byte_identical_to_the_evaluated_one() -> None:
    """⚠️ **提示词是被测配置的一部分。**

    步骤 07 的 234 份结果全是这个提示词跑出来的（sha256 前 12 位
    `12e0246b0377`）。改一个字就等于换一个被测系统，那批准确率数字立刻失效，
    必须重跑 `tools/eval_adapter.py`。这条断言就是那个配置的身份证。
    """
    digest = hashlib.sha256(CONTENT_PROMPT.encode("utf-8")).hexdigest()[:12]

    assert digest == CONTENT_PROMPT_SHA12 == "12e0246b0377"


# --- 三态归类 ---------------------------------------------------------------


def test_empty_string_means_needs_a_human_not_absent_from_the_page() -> None:
    """⚠️ 步骤 07 实测：三个模型**从不**输出 JSON null，空缺一律是空字符串。

    也就是说我们分不开「纸上没这一项」和「模型没读出来」。两者里选保守的那个：
    `UNREADABLE` 要求人来填，`MISSING` 会让界面以为这里本来就没东西。
    """
    assert classify_value("").status == "unreadable"
    assert classify_value("   ").status == "unreadable"


def test_self_reported_unreadable_is_recorded() -> None:
    """`qwen3-vl-plus` 用过 141 次。方向对，但**不能当裁判**——没 GT 验证不了。"""
    assert classify_value("UNREADABLE").status == "unreadable"


def test_a_dash_keeps_its_raw_text_instead_of_becoming_missing() -> None:
    """`-` 是纸面上真实存在的字符，不该由适配器替它下「纸上没有」的结论。

    实测 `qwen3-vl-plus` 在 10 个 line_amount 上给了 `-`。保留原文之后，
    老板看到的就是单据上那个破折号，数值那边由规范化明确拒绝并留说明。
    """
    value = classify_value("-")

    assert value.status == "recognized"
    assert value.raw_text == "-"


def test_a_key_the_prompt_never_asked_for_is_not_reported_as_a_model_null() -> None:
    """⚠️ `supplier` / `currency` 不在 CONTENT_PROMPT 的要求里。

    把"没问"报成"模型给了 null"会与步骤 07 的实测记录直接冲突，
    下一个排查的人会以为那条记录错了。诊断信息说错话比不说话更糟。
    """
    not_asked = classify_value(NOT_ASKED)
    real_null = classify_value(None)

    assert not_asked.status == "missing"
    assert "没有要求" in (not_asked.note or "")
    assert real_null.status == "missing"
    assert "从不" in (real_null.note or "")


# --- 输出解析 ---------------------------------------------------------------


def test_fenced_and_bare_json_both_parse() -> None:
    """`qwen3.5-ocr` 一律带 ```json 围栏，`qwen3-vl-plus` 给裸 JSON。"""
    assert parse_model_output('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_model_output('{"a": 1}') == {"a": 1}
    assert parse_model_output('前面有解释\n{"a": 1}\n后面还有') == {"a": 1}


def test_unparseable_output_yields_no_lines_rather_than_a_guess() -> None:
    """234 份里有 1 份怎么都解析不出来——**解析失败是必须有的分支**。"""
    assert parse_model_output("这是一段纯文字，没有 JSON") is None

    result = to_recognition_result(_call("这是一段纯文字，没有 JSON"))

    assert result.lines == ()
    # 原始输出仍然保留：结果可疑时要能回看模型到底返回了什么。
    assert result.raw_response == "这是一段纯文字，没有 JSON"
    assert any("解析不出 JSON" in note for note in result.warnings)


def test_result_never_claims_to_be_fake() -> None:
    result = to_recognition_result(_call('{"lines": []}'))

    assert result.is_fake is False
    assert result.provider == "dashscope"


def test_structure_answer_is_parsed_but_marked_advisory() -> None:
    answer = parse_structure(
        _call('{"written_rows": [1, 3], "printed_row_slots": 15, "handwritten": true}')
    )

    assert answer.parsed
    assert answer.written_rows == (1, 3)
    assert answer.printed_row_slots == 15
    assert answer.handwritten is True


def test_unparseable_structure_answer_produces_no_cap() -> None:
    answer = parse_structure(_call("看不懂"))

    assert not answer.parsed
    assert answer.written_rows == ()
    assert answer.note


# --- 传输层 -----------------------------------------------------------------


def test_media_type_is_never_defaulted_to_jpeg() -> None:
    """⚠️ 步骤 07 实测：写死 image/jpeg 会让 PNG 被按错格式解码。

    而那种错误的表现是"识别结果变差"，不是报错，所以没人会发现。
    """
    client = _FakeClient({"qwen3.5-ocr": '{"lines": []}'})
    recognizer = DashScopeRecognizer(client, model="qwen3.5-ocr")

    with pytest.raises(OcrServiceError, match="媒体类型"):
        recognizer.recognize(
            RecognitionRequest(content=b"x", content_type=None, source_key="k")
        )


def test_heic_must_be_converted_before_it_reaches_the_service() -> None:
    client = _FakeClient({"qwen3.5-ocr": '{"lines": []}'})
    recognizer = DashScopeRecognizer(client, model="qwen3.5-ocr")

    with pytest.raises(OcrServiceError) as excinfo:
        recognizer.recognize(
            RecognitionRequest(content=b"x", content_type="image/heic", source_key="k")
        )

    assert "decode_image" in str(excinfo.value)
    assert "image/jpeg" in str(excinfo.value) or "image/png" in str(excinfo.value)


def test_supported_media_types_are_exactly_what_the_service_accepts() -> None:
    assert SUPPORTED_MEDIA_TYPES == ("image/jpeg", "image/png")


def test_retryable_errors_are_retried_and_others_are_not() -> None:
    """限流值得再试一次；凭据无效再试一百次也一样。"""
    import urllib.error

    attempts = {"count": 0}

    class _Flaky(DashScopeClient):
        def _post(self, body: bytes, timeout_seconds=None):  # type: ignore[override]
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise OcrServiceError(
                    "限流", code="http_429", retryable=True, status=429
                )
            return '{"lines": []}', {"total_tokens": 10}, "req-1"

    client = _Flaky(
        base_url="https://example/v1",
        api_key="sk-test",
        max_attempts=3,
        sleep=lambda _seconds: None,
    )

    call = client.recognize(
        image=b"x", media_type="image/png", model="m", prompt="p"
    )

    assert attempts["count"] == 3
    assert call.attempts == 3


def test_a_non_retryable_error_fails_immediately() -> None:
    attempts = {"count": 0}

    class _Dead(DashScopeClient):
        def _post(self, body: bytes, timeout_seconds=None):  # type: ignore[override]
            attempts["count"] += 1
            raise OcrServiceError(
                "凭据无效", code="http_401", retryable=False, status=401
            )

    client = _Dead(
        base_url="https://example/v1",
        api_key="sk-test",
        max_attempts=3,
        sleep=lambda _seconds: None,
    )

    with pytest.raises(OcrServiceError):
        client.recognize(image=b"x", media_type="image/png", model="m", prompt="p")

    assert attempts["count"] == 1


def test_the_api_key_never_appears_in_the_endpoint_or_errors() -> None:
    client = DashScopeClient(base_url="https://example/v1", api_key="sk-secret-value")

    assert "sk-secret-value" not in client.endpoint
    assert client.endpoint.endswith("/chat/completions")


# --- 交叉裁决 ---------------------------------------------------------------


def _field(name: str, text: str | None, status: str = "recognized"):
    from src.contracts import RecognizedField

    if status == "recognized":
        return RecognizedField.recognized(name, text or "")
    if status == "unreadable":
        return RecognizedField.unreadable(name)
    return RecognizedField.missing(name)


def test_agreement_prefills_and_is_labelled() -> None:
    verdict = judge("quantity", [_field("quantity", "8"), _field("quantity", "8")])

    assert verdict.agreed
    assert verdict.field.status is FieldStatus.RECOGNIZED
    assert verdict.note == AGREEMENT_NOTE


def test_disagreement_becomes_unreadable_and_keeps_both_readings() -> None:
    """⚠️ 这是步骤 07 的 `5 85` 那一格。

    两种读法都记下来，摆在裁剪图旁边让人**看图判断**，
    而不是在两个数字之间二选一。
    """
    verdict = judge(
        "unit_price", [_field("unit_price", "5.85"), _field("unit_price", "58")]
    )

    assert not verdict.agreed
    assert verdict.field.status is FieldStatus.UNREADABLE
    assert verdict.values == ("5.85", "58")


def test_numeric_agreement_ignores_formatting_differences() -> None:
    """`6.00` 与 `6` 是同一个数；当成分歧会白白把能预填的值推给人填。"""
    verdict = judge(
        "unit_price", [_field("unit_price", "$6.00"), _field("unit_price", "6")]
    )

    assert verdict.agreed


def test_one_model_reading_and_one_not_is_also_a_disagreement() -> None:
    verdict = judge(
        "quantity",
        [_field("quantity", "8"), _field("quantity", None, status="unreadable")],
    )

    assert not verdict.agreed
    assert verdict.field.status is FieldStatus.UNREADABLE


def test_row_counts_differing_keeps_the_larger_count() -> None:
    """⚠️ **漏行比多一行空行危险得多。**

    各行金额之和对不上单据总额只能抓到一部分，而手写单常常连总额都没有。
    多出来的空行是看得见的噪声，漏掉的一行是看不见的损失。
    """
    first = to_recognition_result(
        _call(json.dumps({"lines": [{"item_description": "A"}, {"item_description": "B"}]}))
    )
    second = to_recognition_result(
        _call(json.dumps({"lines": [{"item_description": "A"}]}), model="qwen3-vl-plus")
    )

    outcome = combine([first, second], models=["qwen3.5-ocr", "qwen3-vl-plus"])

    assert len(outcome.result.lines) == 2
    assert any("行数不一致" in note for note in outcome.result.warnings)


def test_cross_check_runs_every_model_and_combines() -> None:
    client = _FakeClient(
        {
            "qwen3.5-ocr": json.dumps({"invoice_total": "100.00", "lines": []}),
            "qwen3-vl-plus": json.dumps({"invoice_total": "100.00", "lines": []}),
        }
    )
    recognizer = CrossCheckRecognizer(client, models=("qwen3.5-ocr", "qwen3-vl-plus"))

    outcome = recognizer.cross_check(
        RecognitionRequest(content=b"x", content_type="image/png", source_key="k")
    )

    assert {call["model"] for call in client.calls} == {"qwen3.5-ocr", "qwen3-vl-plus"}
    assert all(call["prompt"] == CONTENT_PROMPT for call in client.calls)
    assert outcome.result.document_field("invoice_total").raw_text == "100.00"


def test_one_model_failing_does_not_fail_the_whole_extraction() -> None:
    """但剩一个模型时「一致」不成立，于是**所有字段按分歧处理**，不降级预填。"""
    client = _FakeClient(
        {"qwen3.5-ocr": json.dumps({"invoice_total": "100.00", "lines": []})},
        fail={
            "qwen3-vl-plus": OcrServiceError(
                "限流", code="http_429", retryable=True, status=429
            )
        },
    )
    recognizer = CrossCheckRecognizer(client, models=("qwen3.5-ocr", "qwen3-vl-plus"))

    outcome = recognizer.cross_check(
        RecognitionRequest(content=b"x", content_type="image/png", source_key="k")
    )

    assert any("调用失败" in note for note in outcome.result.warnings)
    assert any("不预填" in note for note in outcome.result.warnings)


def test_all_models_failing_raises_so_the_job_can_be_retried() -> None:
    error = OcrServiceError("限流", code="http_429", retryable=True, status=429)
    client = _FakeClient({}, fail={"qwen3.5-ocr": error, "qwen3-vl-plus": error})
    recognizer = CrossCheckRecognizer(client, models=("qwen3.5-ocr", "qwen3-vl-plus"))

    with pytest.raises(OcrServiceError) as excinfo:
        recognizer.cross_check(
            RecognitionRequest(content=b"x", content_type="image/png", source_key="k")
        )

    assert excinfo.value.retryable is True


# --- 按配置选实现 -----------------------------------------------------------


def test_two_models_gives_cross_check_one_gives_single() -> None:
    env = {
        "OCR_PROVIDER": "dashscope",
        "OCR_API_KEY": "sk-test",
        "OCR_BASE_URL": "https://example/v1",
    }

    assert isinstance(build_recognizer(Config.from_env(env)), CrossCheckRecognizer)
    assert isinstance(
        build_recognizer(Config.from_env(dict(env, OCR_MODELS="qwen3.5-ocr"))),
        DashScopeRecognizer,
    )


def test_fake_stays_the_default_so_no_credentials_are_needed_to_start() -> None:
    """无密钥时手工与表格通路仍然可用（08.7 的验收条件）。"""
    recognizer = build_recognizer(Config.from_env({}))

    assert recognizer.is_fake
