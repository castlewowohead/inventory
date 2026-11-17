"""锚定的本地校验（步骤 08.3d，批次 2）。

这些用例守的是**闸门**，不是模型。模型读出来什么由它去，这里验的是「什么样的读数
会被放行、什么样的会被挡下来」——因为锚定错一行是**静默**错误：老板照着相邻那一行
的字迹选食材，不会有任何报错。
"""

from __future__ import annotations

from src.documents.anchor import (
    MIN_ANCHOR_BANDS,
    column_map_from,
    row_anchor_from,
)

BANDS_25 = tuple(range(1, 26))
"""TangFa 那一版：25 条行带 = NAME + ADDRESS + 列标题 + 21 个数据行 + 签收。"""

TANGFA_READING = (None, None, None, *range(1, 22), None)


def _two(cells, other=None):
    return [("qwen3.5-ocr", cells), ("qwen3-vl-plus", other if other is not None else cells)]


# --- 放行 -------------------------------------------------------------------


def test_two_models_reading_the_same_row_numbers_establishes_the_anchor() -> None:
    anchor = row_anchor_from(_two(TANGFA_READING), band_indexes=BANDS_25)

    assert anchor.established
    assert anchor.source == "row_numbers"
    # 第 1 号商品行落在第 4 条行带——前三条是 NAME / ADDRESS / 列标题。
    assert anchor.band_of(1) == 4
    assert anchor.band_of(21) == 24
    assert anchor.printed_rows == 21
    assert anchor.header_band == 3


def test_a_missing_band_shifts_the_anchor_instead_of_breaking_it() -> None:
    """漏检一条横线时，锚定应当**跟着挪**，而不是照序号硬套。

    实测 TangFa p07 就是这样：NAME 那条带没被切出来，于是数据行从第 3 条行带开始。
    """
    reading = (None, None, *range(1, 22), None)
    anchor = row_anchor_from(_two(reading), band_indexes=tuple(range(1, 25)))

    assert anchor.established
    assert anchor.band_of(1) == 3
    assert anchor.header_band == 2


def test_the_anchor_is_a_number_to_band_map_not_a_band_index() -> None:
    """越界返回 None，不夹到边界——夹一下就是裁到相邻那一格。"""
    anchor = row_anchor_from(_two(TANGFA_READING), band_indexes=BANDS_25)

    assert anchor.band_of(0) is None
    assert anchor.band_of(22) is None


# --- 挡下来 -----------------------------------------------------------------


def test_two_models_disagreeing_by_one_row_is_refused() -> None:
    """⚠️ 这是实测抓到过的那一例，也是两个模型比对存在的理由。

    ChangShun p01 上一个模型答 `[null,1,…,15]`、另一个答 `[null,null,1,…,14]`，
    **两份各自都是连号、各自都能过长度与连续关**，却整体差了一行。
    """
    first = (None, *range(1, 16))
    second = (None, None, *range(1, 15))
    anchor = row_anchor_from(
        [("qwen3.5-ocr", first), ("qwen3-vl-plus", second)],
        band_indexes=tuple(range(1, 17)),
    )

    assert not anchor.established
    assert "对不上" in anchor.reason


def test_a_reading_with_the_wrong_length_is_refused() -> None:
    """格子数是**我们自己**裁出来的事实，对不上说明模型漏数或多数了一格。"""
    anchor = row_anchor_from(_two((None, *range(1, 16))), band_indexes=tuple(range(1, 18)))

    assert not anchor.established
    assert "格子" in anchor.reason


def test_row_numbers_that_skip_a_band_are_refused() -> None:
    """数据行在预印表格上是中间连续的一段，中间断开说明读错了格。"""
    reading = (None, 1, 2, None, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13)
    anchor = row_anchor_from(_two(reading), band_indexes=tuple(range(1, 16)))

    assert not anchor.established
    assert "连续" in anchor.reason


def test_row_numbers_that_are_not_consecutive_are_refused() -> None:
    """预印表格的行号一定是 1、2、3…，跳号说明中间漏读或多读了一格。"""
    reading = (None, 1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15)
    anchor = row_anchor_from(_two(reading), band_indexes=tuple(range(1, 16)))

    assert not anchor.established
    assert "连号" in anchor.reason


def test_a_page_without_printed_row_numbers_is_refused() -> None:
    """FengYuan 那一家纸上根本没印行号。**正确拒绝**，不是失败。"""
    anchor = row_anchor_from(_two((None,) * 18), band_indexes=tuple(range(1, 19)))

    assert not anchor.established
    assert "行号" in anchor.reason


def test_a_table_with_too_few_bands_is_not_even_attempted() -> None:
    """Kunkee / NewMoys 只切出 2—4 条行带。对它们发请求是白花钱。"""
    anchor = row_anchor_from(_two((None, None, None)), band_indexes=(1, 2, 3))

    assert not anchor.established
    assert str(MIN_ANCHOR_BANDS) in anchor.reason


def test_a_model_that_failed_to_answer_is_refused_not_ignored() -> None:
    """调用失败 / 解析失败时读数是 None，不能当成"这一列没有行号"。"""
    anchor = row_anchor_from(
        [("qwen3.5-ocr", TANGFA_READING), ("qwen3-vl-plus", None)],
        band_indexes=BANDS_25,
    )

    assert not anchor.established


# --- 列映射 -----------------------------------------------------------------


def test_header_labels_map_columns_to_contract_fields() -> None:
    reading = ("quantity", "weight", "item_description", "unit_price", "line_amount")
    mapping = column_map_from(_two(reading), column_indexes=(1, 2, 3, 4, 5))

    assert mapping.established
    assert mapping.column_of("quantity") == 1
    assert mapping.column_of("item_description") == 3
    assert mapping.column_of("line_amount") == 5
    # 重量不是契约字段（它属于步骤 09 的换算），不该被硬塞到某个字段上。
    assert mapping.column_of("weight") is None


def test_a_field_hitting_two_columns_is_dropped_rather_than_guessed() -> None:
    """两列里哪一列才是它，猜错就是裁错格子。"""
    reading = ("quantity", "quantity", "item_description", "unit_price", "line_amount")
    mapping = column_map_from(_two(reading), column_indexes=(1, 2, 3, 4, 5))

    assert mapping.established
    assert mapping.column_of("quantity") is None
    assert mapping.column_of("item_description") == 3
    assert "不止一列" in mapping.reason


def test_models_disagreeing_on_the_header_refuses_the_mapping() -> None:
    first = ("quantity", "item_description", "unit_price", "line_amount", "other")
    second = ("item_description", "quantity", "unit_price", "line_amount", "other")
    mapping = column_map_from(
        [("qwen3.5-ocr", first), ("qwen3-vl-plus", second)], column_indexes=(1, 2, 3, 4, 5)
    )

    assert not mapping.established


def test_a_header_reading_of_the_wrong_length_refuses_the_mapping() -> None:
    """实测过：一个模型只回了 4 个而表上有 5 条列带。"""
    mapping = column_map_from(
        _two(("item_description", "quantity", "unit_price", "line_amount")),
        column_indexes=(1, 2, 3, 4, 5),
    )

    assert not mapping.established
    assert "对不上" in mapping.reason


def test_a_header_with_nothing_recognisable_refuses_the_mapping() -> None:
    mapping = column_map_from(_two(("other", "other", None)), column_indexes=(1, 2, 3))

    assert not mapping.established
