"""识别器边界的测试（步骤 02）。

两件事：Fake 识别器必须把自己标清楚；没有 API Key 时启动与测试仍然可用，
且正常启动不连接任何外部服务。
"""

from __future__ import annotations

import socket
from datetime import datetime, timezone

import pytest
from flask import Flask
from flask.testing import FlaskClient

from src.config import Config
from src.contracts import (
    FAKE_PROVIDER,
    FIELD_CURRENCY,
    FIELD_INVOICE_NUMBER,
    FIELD_SUPPLIER,
    FIELD_UNIT_PRICE,
    FieldStatus,
    RecognitionRequest,
    RecognitionResult,
)
from src.errors import FeatureNotImplementedError
from src.factory import create_app, get_recognizer
from src.integrations.file_storage import LocalFileStorage
from src.integrations.ocr import (
    FAKE_MARKER,
    FAKE_MODEL,
    FakeRecognizer,
    Recognizer,
    build_recognizer,
)

REQUEST = RecognitionRequest(
    content=b"%PDF-1.4 fake bytes",
    content_type="application/pdf",
    original_name="送货单.pdf",
    source_key="2026/11/09/abc.pdf",
)


@pytest.fixture
def fake_result(recognizer: FakeRecognizer) -> RecognitionResult:
    return recognizer.recognize(REQUEST)


# --- Fake 必须自报 ---


def test_fake_result_declares_itself(fake_result: RecognitionResult) -> None:
    assert fake_result.provider == FAKE_PROVIDER
    assert fake_result.is_fake is True
    assert fake_result.model == FAKE_MODEL


def test_fake_result_warns_in_plain_language(fake_result: RecognitionResult) -> None:
    assert fake_result.warnings
    assert any("Fake" in warning for warning in fake_result.warnings)
    assert any("不是真实识别结果" in warning for warning in fake_result.warnings)


def test_every_fake_text_carries_the_marker(fake_result: RecognitionResult) -> None:
    """文本内容一眼可辨：不会被误当成真实单据上的字。"""
    texts = [page.text for page in fake_result.pages]
    texts += [
        field.raw_text
        for field in fake_result.document_fields.values()
        if field.raw_text
    ]
    texts += [line.raw_text for line in fake_result.lines]

    assert texts
    for text in texts:
        assert text is not None and FAKE_MARKER in text


def test_fake_raw_response_is_marked_and_traceable(
    fake_result: RecognitionResult,
) -> None:
    """原始响应同样标记为假，并带输入摘要，便于把结果关联回具体文件。"""
    assert fake_result.raw_response is not None
    assert FAKE_MARKER in fake_result.raw_response
    assert "source_sha256" in fake_result.raw_response
    assert fake_result.source_key == REQUEST.source_key


def test_fake_recognizer_satisfies_the_interface(recognizer: FakeRecognizer) -> None:
    assert isinstance(recognizer, Recognizer)
    assert recognizer.is_fake is True


def test_fake_recognizer_is_deterministic(recognizer: FakeRecognizer) -> None:
    """结构固定，便于把它当回归基准。"""
    first = recognizer.recognize(REQUEST)
    second = recognizer.recognize(REQUEST)

    assert first.lines == second.lines
    assert first.document_fields == second.document_fields


def test_recognised_at_can_be_injected() -> None:
    moment = datetime(2026, 11, 9, 8, 30, tzinfo=timezone.utc)

    result = FakeRecognizer(now=moment).recognize(REQUEST)

    assert result.recognized_at == moment


# --- Fake 保留了审核流程必须面对的几种情况 ---


def test_fake_result_keeps_page_and_region_evidence(
    fake_result: RecognitionResult,
) -> None:
    supplier = fake_result.document_field(FIELD_SUPPLIER)

    assert fake_result.page_count == 1
    assert supplier is not None
    assert supplier.page == 1
    assert supplier.box is not None


def test_fake_result_includes_a_field_without_region(
    fake_result: RecognitionResult,
) -> None:
    """厂商给文字不给坐标是常见情况，Fake 刻意保留一个，不编造坐标。"""
    number = fake_result.document_field(FIELD_INVOICE_NUMBER)

    assert number is not None
    assert number.page == 1
    assert number.box is None


def test_fake_result_keeps_an_unreadable_value_instead_of_zero(
    fake_result: RecognitionResult,
) -> None:
    """模糊单价标为 UNREADABLE，不变成 0——0 是一个具体金额，「没认出来」不是。"""
    line = fake_result.line(2)

    assert line is not None
    assert line.unreadable_fields == (FIELD_UNIT_PRICE,)
    price = line.get(FIELD_UNIT_PRICE)
    assert price is not None
    assert price.status is FieldStatus.UNREADABLE
    assert price.raw_text is None
    assert price.box is not None, "认不出来也要留下区域，复核时要能定位原图"


def test_fake_result_leaves_absent_currency_missing(
    fake_result: RecognitionResult,
) -> None:
    """币种单据上没写就是 MISSING，不默认成 USD。"""
    currency = fake_result.document_field(FIELD_CURRENCY)

    assert currency is not None
    assert currency.status is FieldStatus.MISSING


def test_fake_result_has_both_clean_and_dirty_lines(
    fake_result: RecognitionResult,
) -> None:
    """第一行干净、第二行有认不出的字段：步骤 05 的逐行确认从一开始就有真实场景。"""
    assert len(fake_result.lines) == 2
    assert fake_result.line(1) is not None and not fake_result.line(1).unreadable_fields
    assert fake_result.has_unreadable_fields is True


def test_fake_recognizer_does_not_read_the_file_contents() -> None:
    """它只对输入算摘要，不解析内容，因此任何字节都能用来开发流程。"""
    other = FakeRecognizer().recognize(RecognitionRequest(content=b"not a pdf at all"))

    assert other.lines
    assert other.is_fake is True


# --- 按配置选择实现 ---


def test_build_recognizer_returns_the_fake_implementation(config: Config) -> None:
    built = build_recognizer(config)

    assert isinstance(built, FakeRecognizer)
    assert built.provider == FAKE_PROVIDER


def test_build_recognizer_refuses_unimplemented_providers(config: Config) -> None:
    """不静默退回假数据：那会让人以为真实识别已经接通。"""
    pretend = object.__new__(Config)
    object.__setattr__(pretend, "ocr_provider", "qwen")

    with pytest.raises(FeatureNotImplementedError) as excinfo:
        build_recognizer(pretend)

    assert excinfo.value.details == {"provider": "qwen"}
    assert excinfo.value.status_code == 501


def test_configuration_rejects_real_providers_before_startup(
    test_env: dict[str, str],
) -> None:
    """配置层就拦住了：真实厂商在步骤 07 评估、步骤 08 接入后才能配。"""
    from src.errors import ConfigError

    test_env["OCR_PROVIDER"] = "qwen"

    with pytest.raises(ConfigError):
        Config.from_env(test_env)


# --- 无 API Key 也能启动；启动不连外部服务 ---


def test_app_starts_without_any_api_key(
    database_free_env: dict[str, str], storage: LocalFileStorage
) -> None:
    """环境里没有任何厂商凭据，应用照样创建成功。"""
    config = Config.from_env(database_free_env)

    app = create_app(config, file_storage=storage)

    assert get_recognizer(app).is_fake is True


def test_startup_and_health_make_no_outbound_connection(
    config: Config, storage: LocalFileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """创建应用与访问 /health 都不应该连接任何外部服务。"""
    attempts: list[object] = []

    def refuse(self: socket.socket, address: object) -> None:
        attempts.append(address)
        raise AssertionError(f"启动阶段不应发起网络连接：{address}")

    monkeypatch.setattr(socket.socket, "connect", refuse)

    app = create_app(config, file_storage=storage)
    response = app.test_client().get("/health")

    assert response.status_code == 200
    assert attempts == []


def test_recognition_itself_makes_no_outbound_connection(
    recognizer: FakeRecognizer, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: socket.socket, address: object) -> None:
        raise AssertionError(f"Fake 识别器不应发起网络连接：{address}")

    monkeypatch.setattr(socket.socket, "connect", refuse)

    assert recognizer.recognize(REQUEST).is_fake is True


def test_health_does_not_report_ocr_state(client: FlaskClient) -> None:
    """没做的检查不写进响应：/health 不声称识别服务可用。"""
    payload = client.get("/health").get_json()

    assert "ocr" not in str(payload).lower()


def test_injected_recognizer_is_used(config: Config, storage: LocalFileStorage) -> None:
    sentinel = FakeRecognizer()

    app: Flask = create_app(config, file_storage=storage, recognizer=sentinel)

    assert get_recognizer(app) is sentinel


@pytest.fixture
def database_free_env() -> dict[str, str]:
    """只给最低限度配置的环境映射：没有任何 API Key。"""
    return {
        "APP_ENV": "testing",
        "SECRET_KEY": "test-only-secret-not-a-real-key",
        "DATABASE_URL": "sqlite://",
    }
