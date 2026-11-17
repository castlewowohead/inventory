"""识别契约的测试（步骤 02）。

重点是契约自己能挡住哪些错误：假数据冒充真实结果、未识别值变成 0、
编造的区域坐标、页码与行号对不上。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from src.contracts import (
    DOCUMENT_FIELDS,
    FAKE_PROVIDER,
    FIELD_ITEM_DESCRIPTION,
    FIELD_LINE_AMOUNT,
    FIELD_QUANTITY,
    FIELD_UNIT_PRICE,
    LINE_FIELDS,
    BoundingBox,
    ContractError,
    CoordinateSpace,
    FieldEvidence,
    FieldStatus,
    RecognitionRequest,
    RecognitionResult,
    RecognizedField,
    RecognizedLine,
    RecognizedPage,
)

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def result(**overrides: object) -> RecognitionResult:
    defaults: dict[str, object] = {
        "provider": "example-vendor",
        "is_fake": False,
        "recognized_at": NOW,
    }
    defaults.update(overrides)
    return RecognitionResult(**defaults)  # type: ignore[arg-type]


# --- 原文、页码、区域证据 ---


def test_recognized_field_keeps_raw_text_verbatim() -> None:
    """原文不清洗、不格式化：人工复核对照的是原文。"""
    field = RecognizedField.recognized(FIELD_QUANTITY, "  10 .5 lb ")

    assert field.raw_text == "  10 .5 lb "


def test_evidence_carries_page_and_region() -> None:
    field = RecognizedField.recognized(
        FIELD_QUANTITY,
        "10",
        evidence=FieldEvidence(page=2, box=BoundingBox(0.1, 0.2, 0.3, 0.05)),
    )

    assert field.page == 2
    assert field.box is not None
    assert field.box.space is CoordinateSpace.RATIO


def test_region_can_be_absent_without_losing_the_page() -> None:
    """厂商给了文字没给坐标是常见情况：box 为 None，不编造坐标。"""
    field = RecognizedField.recognized(
        FIELD_QUANTITY, "10", evidence=FieldEvidence(page=1)
    )

    assert field.page == 1
    assert field.box is None


def test_field_without_evidence_reports_no_page() -> None:
    field = RecognizedField.recognized(FIELD_QUANTITY, "10")

    assert field.page is None
    assert field.box is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"left": 0.1, "top": 0.1, "width": 0.0, "height": 0.1},
        {"left": 0.1, "top": 0.1, "width": 0.1, "height": -0.1},
        {"left": -0.1, "top": 0.1, "width": 0.1, "height": 0.1},
        {"left": 0.9, "top": 0.1, "width": 0.2, "height": 0.1},
        {"left": 0.1, "top": 0.95, "width": 0.1, "height": 0.2},
    ],
)
def test_impossible_regions_are_rejected(kwargs: dict[str, float]) -> None:
    with pytest.raises(ContractError):
        BoundingBox(**kwargs)


def test_pixel_space_is_not_bound_by_the_ratio_range() -> None:
    box = BoundingBox(120, 400, 180, 36, CoordinateSpace.PIXEL)

    assert box.space is CoordinateSpace.PIXEL
    assert box.width == 180


def test_page_numbers_start_at_one() -> None:
    with pytest.raises(ContractError, match="页码"):
        FieldEvidence(page=0)
    with pytest.raises(ContractError, match="页码"):
        RecognizedPage(number=0)


def test_page_dimensions_must_be_positive_when_given() -> None:
    with pytest.raises(ContractError):
        RecognizedPage(number=1, width=0, height=100)
    assert RecognizedPage(number=1).width is None


# --- 未识别 ≠ 0 ---


def test_unreadable_field_has_no_value_at_all() -> None:
    """认不出来就是认不出来：不是 0，也不是空字符串。"""
    field = RecognizedField.unreadable(FIELD_UNIT_PRICE)

    assert field.status is FieldStatus.UNREADABLE
    assert field.raw_text is None
    assert field.is_recognized is False
    assert field.needs_human_input is True


def test_missing_field_is_distinct_from_unreadable() -> None:
    """单据上没写（MISSING）与写了但认不出（UNREADABLE）是两件事。"""
    missing = RecognizedField.missing(FIELD_UNIT_PRICE)

    assert missing.status is FieldStatus.MISSING
    assert missing.needs_human_input is False
    assert missing.raw_text is None


def test_recognized_status_requires_raw_text() -> None:
    """不允许「标为已识别但没有原文」这种会被当成空值使用的状态。"""
    with pytest.raises(ContractError, match="没有原文"):
        RecognizedField(name=FIELD_QUANTITY, status=FieldStatus.RECOGNIZED)
    with pytest.raises(ContractError, match="没有原文"):
        RecognizedField(
            name=FIELD_QUANTITY, status=FieldStatus.RECOGNIZED, raw_text="   "
        )


def test_unreadable_status_must_not_carry_text() -> None:
    with pytest.raises(ContractError, match="不应带原文"):
        RecognizedField(
            name=FIELD_QUANTITY, status=FieldStatus.UNREADABLE, raw_text="10"
        )


def test_confidence_must_be_a_probability() -> None:
    with pytest.raises(ContractError, match="置信度"):
        RecognizedField.recognized(FIELD_QUANTITY, "10", confidence=1.5)


def test_unreadable_fields_are_listed_per_line() -> None:
    line = RecognizedLine(
        index=1,
        fields=(
            RecognizedField.recognized(FIELD_ITEM_DESCRIPTION, "大米"),
            RecognizedField.unreadable(FIELD_QUANTITY),
            RecognizedField.unreadable(FIELD_UNIT_PRICE),
            RecognizedField.missing(FIELD_LINE_AMOUNT),
        ),
    )

    assert line.unreadable_fields == (FIELD_QUANTITY, FIELD_UNIT_PRICE)


def test_result_reports_whether_anything_needs_human_input() -> None:
    clean = result(
        lines=(
            RecognizedLine(
                index=1, fields=(RecognizedField.recognized(FIELD_QUANTITY, "10"),)
            ),
        )
    )
    dirty = result(
        lines=(
            RecognizedLine(index=1, fields=(RecognizedField.unreadable(FIELD_QUANTITY),)),
        )
    )

    assert clean.has_unreadable_fields is False
    assert dirty.has_unreadable_fields is True


# --- 假数据必须自报 ---


def test_fake_provider_must_declare_is_fake() -> None:
    with pytest.raises(ContractError, match="不得冒充真实识别"):
        RecognitionResult(provider=FAKE_PROVIDER, is_fake=False, recognized_at=NOW)


def test_real_provider_may_declare_is_fake_false() -> None:
    assert result().is_fake is False


def test_provider_is_required() -> None:
    with pytest.raises(ContractError, match="provider"):
        RecognitionResult(provider="", is_fake=False, recognized_at=NOW)


# --- 结构一致性 ---


def test_duplicate_page_numbers_are_rejected() -> None:
    with pytest.raises(ContractError, match="页码重复"):
        result(pages=(RecognizedPage(number=1), RecognizedPage(number=1)))


def test_duplicate_line_indexes_are_rejected() -> None:
    with pytest.raises(ContractError, match="行号重复"):
        result(lines=(RecognizedLine(index=1), RecognizedLine(index=1)))


def test_line_cannot_point_at_a_page_that_does_not_exist() -> None:
    with pytest.raises(ContractError, match="不存在的页"):
        result(pages=(RecognizedPage(number=1),), lines=(RecognizedLine(index=1, page=2),))


def test_line_indexes_start_at_one() -> None:
    with pytest.raises(ContractError, match="行号"):
        RecognizedLine(index=0)


def test_field_key_must_match_field_name() -> None:
    with pytest.raises(ContractError, match="不一致"):
        RecognizedLine(
            index=1, fields={"quantity": RecognizedField.recognized("unit", "lb")}
        )


def test_duplicate_field_names_are_rejected() -> None:
    with pytest.raises(ContractError, match="重复"):
        RecognizedLine(
            index=1,
            fields=(
                RecognizedField.recognized(FIELD_QUANTITY, "10"),
                RecognizedField.recognized(FIELD_QUANTITY, "11"),
            ),
        )


def test_fields_are_read_only_after_construction() -> None:
    """契约对象不可改：原始识别结果不能被后续流程悄悄改掉。"""
    line = RecognizedLine(
        index=1, fields=(RecognizedField.recognized(FIELD_QUANTITY, "10"),)
    )

    with pytest.raises(TypeError):
        line.fields[FIELD_QUANTITY] = RecognizedField.recognized(FIELD_QUANTITY, "99")  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        line.index = 2  # type: ignore[misc]


def test_lookup_helpers(result_with_lines: RecognitionResult) -> None:
    assert result_with_lines.line(1) is not None
    assert result_with_lines.line(99) is None
    assert result_with_lines.document_field("nope") is None


@pytest.fixture
def result_with_lines() -> RecognitionResult:
    return result(
        pages=(RecognizedPage(number=1),),
        lines=(RecognizedLine(index=1, page=1),),
    )


# --- 请求 ---


def test_request_requires_content() -> None:
    with pytest.raises(ContractError, match="没有文件内容"):
        RecognitionRequest(content=b"")


def test_request_page_selection_is_validated() -> None:
    with pytest.raises(ContractError, match="空的"):
        RecognitionRequest(content=b"x", pages=())
    with pytest.raises(ContractError, match="页码"):
        RecognitionRequest(content=b"x", pages=(0,))
    assert RecognitionRequest(content=b"x", pages=(1, 3)).pages == (1, 3)


def test_request_reports_size_and_keeps_source_key() -> None:
    request = RecognitionRequest(
        content=b"1234", source_key="2026/11/09/abc.pdf", content_type="application/pdf"
    )

    assert request.size == 4
    assert request.source_key == "2026/11/09/abc.pdf"


# --- 字段名清单 ---


def test_standard_field_names_cover_the_required_extraction_fields() -> None:
    """与 docs/imp_notes.md 4.5 的结构化提取字段对齐。"""
    assert LINE_FIELDS == (
        "item_description",
        "quantity",
        "unit",
        "unit_price",
        "line_amount",
    )
    assert set(DOCUMENT_FIELDS) >= {"supplier", "invoice_date", "invoice_total"}


def test_summary_contains_no_business_content() -> None:
    """日志摘要只写计数与标识，不写识别到的商品名与金额。"""
    data = result(
        lines=(
            RecognizedLine(
                index=1,
                fields=(
                    RecognizedField.recognized(FIELD_ITEM_DESCRIPTION, "机密商品名"),
                ),
            ),
        )
    ).summary()

    assert "机密商品名" not in str(data)
    assert data["lines"] == 1
