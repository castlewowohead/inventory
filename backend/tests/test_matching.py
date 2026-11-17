"""文本匹配原语的测试（步骤 10 的 `services/matching.py`）。

**用例里的写法全部来自真实样本**，不是构造出来的：`samples/real/ground_truth/`
的 48 行真实商品描述（NewMoys 肉铺 13 行、Kunkee 1 行、4 张零售小票 34 行）。
每一组都注明出处——词表与阈值是照着这些写法量出来的，换一组数据要重新量。
"""

from __future__ import annotations

import pytest

from src.services.matching import (
    SUGGEST_MIN_SCORE,
    content_tokens,
    coverage,
    prepare,
    similarity,
    token_ratio,
    tokenize,
)


# --- 分词 -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 05.6 留给步骤 10 的那个例子：标点不同，词一样。
        ("CHIX-BRST", ("chix", "brst")),
        ("CHIX BRST", ("chix", "brst")),
        # samples/real/ground_truth/04_NewMoys_Meat_5p__p04.json：
        # 中文名与包装规格之间**一个空格都没有**。
        (
            "Beef Stomach - 牛肚6X44.10",
            ("beef", "stomach", "牛肚", "6", "x", "44", "10"),
        ),
        # samples/real/ground_truth/03_Kunkee_Trading_1p__p01.json：开头是
        # 「箱」+ 全角空格（NFKC 折叠掉）。
        (
            "箱　麻薯粉 WHEAT FLOUR 1000g x 10",
            ("箱", "麻薯粉", "wheat", "flour", "1000", "g", "x", "10"),
        ),
        # samples/real/ground_truth/08_Receipt_Target_EN__p01.json
        ("GAIN 2X", ("gain", "2", "x")),
    ],
)
def test_tokenize_splits_at_script_boundaries(raw: str, expected: tuple[str, ...]) -> None:
    """⚠️ 这一组是本模块最要紧的地方。

    只按空白与标点切词的话，`牛肚6x44` 会变成一个词，于是食材库里的 `牛肚`
    **永远**对不上真实单据上的那一行——不报错，只是一条也匹配不到。
    """
    assert tokenize(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 包装规格原文整段是噪声：`6X30` 在真实单据上是「6 箱 × 每箱 30 磅」，
        # `1000g x 10` 是「每箱 10 × 1000g」——同形不同义、差 6 倍（步骤 09）。
        ("1000g x 10", ()),
        ("Beef Stomach - 牛肚6X44.10", ("beef", "stomach", "牛肚")),
        ("箱　麻薯粉 WHEAT FLOUR 1000g x 10", ("麻薯粉", "wheat", "flour")),
        # 缩写展开成规范词，复数折叠（`Duck Feet` 与 `Duck Foot` 是同一样东西）。
        ("Duck Feet", ("duck", "foot")),
        ("CHIX BRST", ("chicken", "brst")),
    ],
)
def test_content_tokens_drop_packaging_and_units(
    raw: str, expected: tuple[str, ...]
) -> None:
    assert content_tokens(raw) == expected


# --- 词相似度 ---------------------------------------------------------------


def test_identical_and_prefix_tokens_rank_above_everything_else() -> None:
    assert token_ratio("beef", "beef") == 1.0
    assert token_ratio("beef", "beefsteak") == 0.95


def test_vowel_dropped_abbreviations_match_their_full_word() -> None:
    """两个例子都来自真实样本：`CNTRY CROCK`（Target 小票）与行业写法 `BRST`。"""
    assert token_ratio("cntry", "country") >= 0.9
    assert token_ratio("brst", "breast") >= 0.9


def test_short_or_sparse_subsequences_are_not_treated_as_abbreviations() -> None:
    """⚠️ 不卡长度与覆盖率的后果有真实实例。

    `Pork - CT` 在 NewMoys 的单据上出现了 3 次；`c…t` 确实是 `condiment` 的有序
    子序列，放开这条规则会把它**稳定地**认成调味品。
    """
    assert token_ratio("ct", "condiment") < SUGGEST_MIN_SCORE
    assert token_ratio("at", "anything") < SUGGEST_MIN_SCORE


def test_misspellings_still_get_a_usable_score() -> None:
    assert token_ratio("beff", "beef") >= SUGGEST_MIN_SCORE


# --- 单向覆盖（搜索框那一侧）-----------------------------------------------


@pytest.mark.parametrize(
    ("query", "target"),
    [
        ("beef", "Beef Stomach - 牛肚"),
        ("牛肚", "Beef Stomach - 牛肚"),
        ("cntry crock", "Country Crock"),
        ("chix brst", "Chicken Breast"),
    ],
)
def test_coverage_finds_the_item_the_boss_typed_a_fragment_of(
    query: str, target: str
) -> None:
    assert coverage(prepare(query), prepare(target)) >= SUGGEST_MIN_SCORE


def test_chinese_is_compared_by_character_bigrams_not_single_characters() -> None:
    """⚠️ 按单字算重合会让 `牛肉` 与 `牛肚` 得 50%（都有「牛」）。

    它们是两样东西，而且在同一家供应商的同一张单据上都出现过
    （`samples/real/ground_truth/` 里有 `牛柏叶` / `牛肚`）。
    """
    assert coverage(prepare("牛肉"), prepare("牛肚")) == 0.0


# --- 双向相似度（描述 ↔ 描述那一侧）---------------------------------------


def test_the_same_item_written_with_and_without_its_pack_spec_is_one_item() -> None:
    """真实实例：NewMoys 的同一个商品，一次写了箱规一次没写。"""
    score = similarity(
        prepare("Beef Stomach - 牛肚6X44.10"), prepare("beef stomach 牛肚")
    )
    assert score >= SUGGEST_MIN_SCORE


def test_a_short_description_is_not_judged_the_same_as_a_longer_one_containing_it() -> None:
    """⚠️ 单向分会把这两条判成完全命中（1.0），而它们各有自己的价格。

    `Pork - CT` 与 `Pork Spare Rib(Cube) - 靓排粒` 在 NewMoys 的 5 页上都出现过，
    混成一条食材会让两个价格进同一个加权均价。
    """
    assert (
        coverage(prepare("pork"), prepare("Pork Spare Rib(Cube) - 靓排粒")) == 1.0
    )
    assert (
        similarity(prepare("pork"), prepare("Pork Spare Rib(Cube) - 靓排粒"))
        < SUGGEST_MIN_SCORE
    )


def test_a_description_with_no_content_words_matches_nothing() -> None:
    """`1000g x 10` 全是数字与单位；真实小票上还有 `LCC SYSTEM USE ONLY` 这种。

    这种描述不该拿到任何候选——给一条"分数最高的"凑数，等于让老板把一行采购
    挂到一个随机食材上。
    """
    assert prepare("1000g x 10").is_empty
    assert similarity(prepare("1000g x 10"), prepare("beef stomach")) == 0.0
    assert coverage(prepare("1000g x 10"), prepare("beef stomach")) == 0.0
