"""文本匹配的共用原语（步骤 10）。

**本模块只回答「这两段文本有多像」，不回答「要不要用它」。** 谁算命中、命中之后
预填还是直接选中、分数多少才够，全部属于调用方的业务判断——与
`services/units.py` 只做换算、不做业务判断是同一条分层约定（见 `docs/struct.md`）。

三个调用方（rule of three，所以才独立成模块）：

1. `services/ingredients.py` 的建议搜索：在步骤 03 的「包含匹配」之上补模糊命中，
   容忍缩写与拼写差异（10.1）；
2. `services/invoices.py` 的行匹配：05.6 的「原始描述规范化后精确命中」之上补模糊层
   （10.4），让 `CHIX-BRST` 能对上历史里确认过的 `CHIX BRST`；
3. 步骤 10.C 的关键词提取：从商品描述里剥出关键词再去认食材，用的是同一套分词、
   同一套噪声词表（`stru_dev.md` 10.C 原文：「词表是本步骤模糊匹配要建的那套规范化
   词表的副产品」）。

⚠️ **分词与词表只有这一处实现。** 三份各自演化的后果不是报错，是「录入时按一套词
归一、匹配时按另一套」——同一个描述在两条通路里命中不同的食材，而两边看起来都对。

**不引入的东西**（`stru_dev.md` 步骤 10「外部准备」原文）：向量数据库、Embedding
API、模型微调。本模块只用标准库：`unicodedata` 与 `difflib`。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Final, Iterable

from src.services.ingredients import (
    BASE_UNIT_SYNONYMS,
    BASE_UNITS,
    PACKAGING_UNITS,
    normalize_name,
)

# --- 分数阈值 ---------------------------------------------------------------
#
# ⚠️ **两个阈值刻意分开，不要合成一个。** 它们的代价不对称：
#
# - `SUGGEST_MIN_SCORE` 用在**人看着选**的场合（搜索框、候选列表）。多给一条不相关
#   的候选，代价是老板多看一眼；少给一条，代价是他搜不到自己库里有的东西，然后
#   建一条重复食材——而重复食材要靠 10.3 的合并才能收拾。所以这一档可以宽。
# - 「分数够高就替老板选中」那一档（10.C 的零点击）**不在本模块定**，而且不走模糊：
#   `pork` 与 `park` 的 `SequenceMatcher` 比值是 0.75，模糊层给候选没问题，拿来
#   自动选中就会把一条采购记录静默挂到错误的食材上——不报错、不可察觉，正是
#   步骤 09「算不出来时标准化价格是 null 而不是 0」要防的那一类错误。

SUGGEST_MIN_SCORE: Final[float] = 0.70
"""候选列表的入选线。低于这个分数的不进候选，而不是排在后面——排在后面的候选
老板仍然会读一遍，那是在浪费他最贵的那段时间。"""

MIN_FUZZY_TOKEN_LENGTH: Final[int] = 3
"""短于这个长度的词不参与缩写推断。`at` 是 `anything` 的子序列，`in` 是 `invoice`
的子序列——两个字母的词当缩写用，命中的几乎全是噪声。"""

_SUBSEQUENCE_MIN_COVERAGE: Final[float] = 0.5
"""缩写至少要覆盖目标词一半的字母。`brst`/`breast` = 0.67、`cntry`/`country` = 0.71
（两个都来自真实样本），而 `ct`/`condiment` = 0.22 这种不算缩写，只是碰巧有序。"""


# --- 词表 -------------------------------------------------------------------
#
# 下面两张表是**本模块唯一的"知识"**，其余全是算法。两张表都按真实样本建，
# 来源写在各自说明里：凭想象列缩写表的结果是表很长、命中很少。

ABBREVIATIONS: Final[dict[str, str]] = {
    # 肉类的行业缩写。来源：行业通用写法 + `samples/real/ground_truth/` 的英文描述。
    # ⚠️ 只收**推不出来的**那些：`cntry → country`、`brst → breast` 这类"抽掉元音"
    # 的写法由下面的子序列规则自动处理，不必列进来；列进来只会让这张表变长而行为
    # 不变。`chix` 要列——`x` 不在 `chicken` 里，任何通用规则都推不出它。
    "chix": "chicken",
    "chx": "chicken",
    "chkn": "chicken",
    "bf": "beef",
    "pk": "pork",
    "veg": "vegetable",
    "vegs": "vegetable",
    "veggies": "vegetable",
    "shrp": "shrimp",
    "tom": "tomato",
    "pot": "potato",
    "onn": "onion",
    "btr": "butter",
    "chz": "cheese",
    "chse": "cheese",
    # 复数折叠。`normalize_name` 不做词干化（它只管大小写与空白），而
    # `Duck Feet` 与 `Duck Foot` 在真实单据上是同一样东西。
    "feet": "foot",
    "leaves": "leaf",
    "loaves": "loaf",
}

# 噪声词：参与不了"这是什么东西"的判断，所以不进相似度，也不进关键词。
#
# ⚠️ **刻意从步骤 03 的单位表推导，不另抄一份**。单位写法的事实来源是
# `services/ingredients.py` 的 `BASE_UNITS` / `BASE_UNIT_SYNONYMS` /
# `PACKAGING_UNITS`；在这里重抄一遍，等于给「什么写法算一个单位」开出第二份定义，
# 以后补一个单位写法就会出现「录入认得、匹配不认得」。
_UNIT_NOISE: Final[frozenset[str]] = frozenset(
    {normalize_name(token) for token in BASE_UNITS}
    | {normalize_name(token) for token in BASE_UNIT_SYNONYMS}
    | {normalize_name(token) for token in PACKAGING_UNITS}
)

# 版式噪声：单据上到处都有、但不指向任何商品的词。
_LAYOUT_NOISE: Final[frozenset[str]] = frozenset(
    {
        "x",
        "ea",
        "per",
        "of",
        "and",
        "the",
        "no",
        "item",
        "total",
        "qty",
        "约",
        "共",
        "每",
    }
)

NOISE_TOKENS: Final[frozenset[str]] = _UNIT_NOISE | _LAYOUT_NOISE


# --- 分词 -------------------------------------------------------------------


def _char_class(char: str) -> str:
    """字符的"书写系统"类别。分词在类别边界处切开。

    为什么要按类别切而不只按空白与标点切：真实单据上**中英文与数字是连在一起写
    的**——`samples/real/ground_truth/04_NewMoys_Meat_5p__p04.json` 的
    `Beef Stomach - 牛肚6X44.10`，中文名与包装规格之间一个空格都没有。只按空白切，
    `牛肚6x44` 会变成一个词，于是库里的 `牛肚` 永远对不上它。
    """
    if char.isdigit():
        return "digit"
    if not char.isalnum():
        return "other"
    # CJK 统一汉字、兼容汉字与扩展区都归一类；日文假名与韩文谚文同样按"表意"处理，
    # 它们和拉丁字母的切词规则不同（没有词间空格）。
    name = unicodedata.name(char, "")
    if name.startswith(("CJK", "HIRAGANA", "KATAKANA", "HANGUL")):
        return "han"
    return "latin"


def tokenize(text: str) -> tuple[str, ...]:
    """文本 → 词序列。输入可以是原文，本函数内部先做 `normalize_name`。

    切分规则：先规范化（NFKC + 折叠空白 + casefold），再在**书写系统边界**与
    非字母数字处切开。于是：

    - `CHIX-BRST` → `chix` / `brst`（标点被切掉，05.6 的那个例子因此能对上
      `CHIX BRST`）；
    - `Beef Stomach - 牛肚6X44.10` → `beef` / `stomach` / `牛肚` / `6` / `x` / `44` / `10`；
    - `箱　麻薯粉 WHEAT FLOUR 1000g x 10`（Kunkee 真实样本，中间是全角空格）
      → `箱` / `麻薯粉` / `wheat` / `flour` / `1000` / `g` / `x` / `10`。

    **不在这里过滤噪声**：分词只负责切，什么算噪声由 `content_tokens()` 决定。
    两件事分开才能让「原文切出了什么」在排查时看得见。
    """
    normalized = normalize_name(text)
    tokens: list[str] = []
    current: list[str] = []
    current_class = ""
    for char in normalized:
        klass = _char_class(char)
        if klass == "other":
            if current:
                tokens.append("".join(current))
                current = []
                current_class = ""
            continue
        if klass != current_class and current:
            tokens.append("".join(current))
            current = []
        current_class = klass
        current.append(char)
    if current:
        tokens.append("".join(current))
    return tuple(tokens)


def expand(token: str) -> str:
    """缩写 → 规范词。表里没有就原样返回。"""
    return ABBREVIATIONS.get(token, token)


def content_tokens(text: str) -> tuple[str, ...]:
    """文本 → **有内容的**词序列：去掉纯数字、单字母与噪声词，缩写展开成规范词。

    丢掉纯数字是有依据的，不是图省事：`6X30` 在真实单据上是「6 箱 × 每箱 30 磅」，
    `1000g x 10` 是「每箱 10 × 1000g」——**同形不同义、差 6 倍**（步骤 09 的结论）。
    既然连人都要看上下文才知道这串数字是什么意思，它就不该参与"这是什么东西"的
    判断。包装规格的正经出路是 `pack_specs` 表，由人确认一次。

    ⚠️ 单字母也丢：`1000g x 10` 里的 `g` 与 `x` 是版式，不是内容。代价是单字母
    商品名（真实样本里没有）匹配不到。
    """
    result: list[str] = []
    for token in tokenize(text):
        if token.isdigit():
            continue
        if _char_class(token[0]) == "latin" and len(token) < 2:
            continue
        if token in NOISE_TOKENS:
            continue
        result.append(expand(token))
    return tuple(result)


# --- 相似度 -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Prepared:
    """一段文本预处理后的形态。比一次的两边都先过这里，避免重复分词。

    拉丁词与表意文字**分开算**：拉丁语言有词间空格，适合按词对齐；中文没有，
    适合按字二元组（bigram）算重合度。混在一起算会两头都不准——`牛肉` 与 `牛肚`
    按单字算重合 50%（都有「牛」），按二元组算重合 0%，而后者才是对的。
    """

    text: str
    latin: tuple[str, ...]
    han: str

    @property
    def is_empty(self) -> bool:
        return not self.latin and not self.han


def prepare(text: str) -> Prepared:
    """把文本切成「拉丁词 + 表意文字」两摊，供相似度计算复用。"""
    latin: list[str] = []
    han: list[str] = []
    for token in content_tokens(text):
        if _char_class(token[0]) == "han":
            han.append(token)
        else:
            latin.append(token)
    return Prepared(text=normalize_name(text), latin=tuple(latin), han="".join(han))


def _bigrams(text: str) -> frozenset[str]:
    """表意文字的字二元组。单字文本退化成它自己（否则「鸡」永远算不出相似度）。"""
    if len(text) < 2:
        return frozenset({text}) if text else frozenset()
    return frozenset(text[index : index + 2] for index in range(len(text) - 1))


def _is_subsequence(short: str, long: str) -> bool:
    iterator = iter(long)
    return all(char in iterator for char in short)


def token_ratio(query: str, target: str) -> float:
    """两个词的相似度，0—1。

    四档，从严到宽：

    1. 完全相同 → `1.0`；
    2. 目标以查询开头 → `0.95`。`beef` 对 `beefsteak` 几乎一定是想找它；
    3. 查询是目标的**子序列**且够长 → `0.85—0.95`。这一档专门吃"抽掉元音"的缩写，
       两个例子都来自真实样本：`cntry` ⊂ `country`（Target 小票的 `CNTRY CROCK`）、
       `brst` ⊂ `breast`。覆盖率越高分越高；
    4. 其余 → `difflib.SequenceMatcher` 的比值，吃拼写错误（`beff` 对 `beef` 得 0.75）。

    ⚠️ **第 3 档一定要卡长度与覆盖率**。不卡的话 `ct` 会命中 `condiment`（c…t 确实
    有序），而 `Pork - CT` 这个写法在 NewMoys 的真实单据上出现了 3 次——那条规则会
    把它稳定地认成调味品。
    """
    if query == target:
        return 1.0
    if not query or not target:
        return 0.0
    if target.startswith(query):
        return 0.95
    if (
        len(query) >= MIN_FUZZY_TOKEN_LENGTH
        and len(query) < len(target)
        and len(query) / len(target) >= _SUBSEQUENCE_MIN_COVERAGE
        and query[0] == target[0]
        and _is_subsequence(query, target)
    ):
        return 0.85 + 0.10 * (len(query) / len(target))
    return SequenceMatcher(None, query, target).ratio()


def _latin_coverage(query: tuple[str, ...], target: tuple[str, ...]) -> float:
    """查询里的每个词在目标里能找到多像的对应词，按词长加权平均。

    按词长加权而不是简单平均：`ground beef 80` 里 `beef` 比 `80` 重要，而简单平均
    会让一个两字母的噪声词和一个六字母的主词一样重。
    """
    if not query:
        return 0.0
    if not target:
        return 0.0
    total_weight = 0
    total_score = 0.0
    for token in query:
        best = max(token_ratio(token, candidate) for candidate in target)
        weight = len(token)
        total_weight += weight
        total_score += best * weight
    return total_score / total_weight if total_weight else 0.0


def _han_coverage(query: str, target: str) -> float:
    if not query:
        return 0.0
    query_grams = _bigrams(query)
    target_grams = _bigrams(target)
    if not query_grams or not target_grams:
        return 0.0
    return len(query_grams & target_grams) / len(query_grams)


def coverage(query: Prepared, target: Prepared) -> float:
    """**单向**相似度：查询这段文本有多少被目标涵盖，0—1。

    用在「搜索框 / 候选列表」那一侧：老板打 `beef` 想找 `Beef Stomach`，这时要的
    不是两段文本整体有多像（那样长名字会被短查询拖垮），而是"我打的这些字，那条
    食材里有没有"。

    拉丁与表意两摊分别算，再按**查询里各占多少字**加权——查询里中文多就以中文的
    结果为主，不是固定五五分。
    """
    if query.is_empty:
        return 0.0
    latin_weight = sum(len(token) for token in query.latin)
    han_weight = len(query.han)
    if latin_weight and han_weight:
        latin = _latin_coverage(query.latin, target.latin)
        han = _han_coverage(query.han, target.han)
        return (latin * latin_weight + han * han_weight) / (latin_weight + han_weight)
    if latin_weight:
        return _latin_coverage(query.latin, target.latin)
    return _han_coverage(query.han, target.han)


def similarity(left: Prepared, right: Prepared) -> float:
    """**双向**相似度：两段文本整体有多像，0—1（两个单向分数的调和平均）。

    用在「描述 ↔ 描述」那一侧（10.4）：两边都是完整的商品描述，长度相当，
    这时单向分会偏向把短描述判成"完全被涵盖"——`pork` 对
    `pork spare rib(cube) 靓排粒` 单向是 1.0，而它们是两样东西。

    取调和平均（F1）而不是算术平均：一边高一边低时调和平均更靠近低的那个，
    正好对应"其中一边有大量没被对上的词"这件事。
    """
    if left.is_empty or right.is_empty:
        return 0.0
    forward = coverage(left, right)
    backward = coverage(right, left)
    if forward <= 0.0 or backward <= 0.0:
        return 0.0
    return 2 * forward * backward / (forward + backward)


# --- 命中理由 ---------------------------------------------------------------
#
# 分数本身不足以让人判断该不该用这条候选，所以每条候选都带一个**理由**。
# 这与步骤 09 每个统计响应都带 `notes` 是同一条原则：数字要跟着"怎么来的"一起读。

REASON_EXACT: Final[str] = "exact"
"""规范化后完全相同。"""

REASON_PREFIX: Final[str] = "prefix"
"""名称以查询开头。"""

REASON_CONTAINS: Final[str] = "contains"
"""查询作为子串出现在检索文本里（步骤 03 的基础搜索那一层）。"""

REASON_FUZZY: Final[str] = "fuzzy"
"""靠本模块的模糊比较命中：缩写、拼写差异或词序不同。"""

REASON_RECENT: Final[str] = "recent"
"""没有查询词时的「最近用过」。"""

REASON_FREQUENT: Final[str] = "frequent"
"""没有查询词时的「常用」。"""

REASON_DESCRIPTION_EXACT: Final[str] = "description_exact"
"""这句原始描述以前确认到过这条食材，规范化后完全相同（05.6 那一层）。"""

REASON_DESCRIPTION_FUZZY: Final[str] = "description_fuzzy"
"""以前确认过的某句描述与这一句**相似**（10.4 新增的那一层）。"""

REASON_SUPPLIER_HISTORY: Final[str] = "supplier_history"
"""这家供应商历史上买过的食材（08.3i，手写件唯一能用的那条）。"""

REASON_MERGED_FROM: Final[str] = "merged_from"
"""搜的是一个**被合并掉的旧叫法**，给出的是它现在归属的那条食材（10.3 / 10.6）。

⚠️ 界面必须把这条和普通命中区分开：老板打的那个名字在库里已经不存在了，
不说清楚的话他会以为自己记错了名字，然后再建一条重复的——正好回到合并要解决的
问题。"""

MATCH_REASONS: Final[tuple[str, ...]] = (
    REASON_EXACT,
    REASON_PREFIX,
    REASON_CONTAINS,
    REASON_FUZZY,
    REASON_RECENT,
    REASON_FREQUENT,
    REASON_DESCRIPTION_EXACT,
    REASON_DESCRIPTION_FUZZY,
    REASON_SUPPLIER_HISTORY,
    REASON_MERGED_FROM,
)


# --- 关键词提取（步骤 10.9）------------------------------------------------


@dataclass(frozen=True, slots=True)
class Keywords:
    """从一句商品描述里剥出来的关键词。

    `name` 与 `name_zh` 分开，因为 `food_items` 本来就是这么存的；真实单据上
    两者也确实是分开写的（`samples/real/ground_truth/` 的 NewMoys 写成
    `Beef Stomach - 牛肚6X44.10`，Kunkee 写成 `箱　麻薯粉 WHEAT FLOUR 1000g x 10`）。

    ⚠️ **保留原文的大小写**，不用规范化后的形式。规范化是给比较用的；拿
    `beef stomach` 去当食材名，老板看到的是一个他自己绝不会这么写的名字。
    """

    name: str | None
    name_zh: str | None

    @property
    def is_empty(self) -> bool:
        return not self.name and not self.name_zh


def extract_keywords(description: str | None) -> Keywords:
    """商品描述 → 关键词（10.9）。

    剥掉的三类东西，依据都在真实样本里：

    1. **包装规格原文**：`6X44.10`、`1000g x 10`、`2X60.1`、`4X30`。它们拆开之后
       全是纯数字与单字母，所以不需要为这类写法单独写正则——⚠️ 也**不应该**写：
       `6X30`（6 箱 × 每箱 30 磅）与 `1000g x 10`（每箱 10 × 1000 g）同形不同义、
       差 6 倍，连人都要看上下文才知道哪种读法对。认出它、丢掉它就够了，
       别顺手把它解析成换算依据（那是 `pack_specs` 的事，由人确认一次）。
    2. **单位字**：`lbs.`、`箱`、`克`。词表从步骤 03 的单位表推导，不另抄一份。
    3. **纯数字与单字母**：`600`、`g`、`x`。

    ⚠️ **不做缩写展开**。`content_tokens()` 会把 `Duck Feet` 展成 `duck foot`，
    那是为了比较；拿来当名字就变成了一个老板没写过的词。

    ⚠️ **它不判断剥出来的东西是不是食材**。Target 小票上剥出来的是
    `BOUNTY`、`COLGATE` 这些品牌名，Lowe's 上是 `LCC SYSTEM USE ONLY`——
    本函数照样返回它们。要不要据此建一条食材，由调用方按**单位能不能归一**
    决定（10.11），不是按"这个词看起来像不像食材"决定。
    """
    if not description:
        return Keywords(name=None, name_zh=None)
    # 只做 NFKC 与折叠空白，**不 casefold**：大小写是要留给老板看的。
    text = " ".join(unicodedata.normalize("NFKC", description).split())
    latin: list[str] = []
    han: list[str] = []
    for token in tokenize_preserving_case(text):
        folded = token.casefold()
        if token.isdigit():
            continue
        klass = _char_class(token[0])
        if klass == "latin" and len(token) < 2:
            continue
        if folded in NOISE_TOKENS:
            continue
        (han if klass == "han" else latin).append(token)
    return Keywords(
        name=" ".join(latin) or None,
        # 中文词之间不加空格：`麻薯粉` 不该变成 `麻 薯 粉`。
        name_zh="".join(han) or None,
    )


def tokenize_preserving_case(text: str) -> tuple[str, ...]:
    """与 `tokenize()` 同一套切分规则，但**不折叠大小写**。

    两个函数共用 `_char_class` 的边界判断，所以「切在哪里」只有一处定义；
    差别只在要不要 casefold。
    """
    tokens: list[str] = []
    current: list[str] = []
    current_class = ""
    for char in text:
        klass = _char_class(char)
        if klass == "other":
            if current:
                tokens.append("".join(current))
                current = []
                current_class = ""
            continue
        if klass != current_class and current:
            tokens.append("".join(current))
            current = []
        current_class = klass
        current.append(char)
    if current:
        tokens.append("".join(current))
    return tuple(tokens)


# --- 分类的本地词表（步骤 10.12）-------------------------------------------
#
# ⚠️ **分类是关键词的副产品，不是输入。** 词表只回答「这个头词八成属于哪一类」，
# 认不出就落「未分类」——而不是按描述去造一个新类别。
#
# 为什么不按描述造类别（2026-11-14 用真实样本定的）：`samples/real/ground_truth/`
# 的 48 行里，若按关键词新建类别，**NewMoys 一家肉铺 5 页 13 行就会造出 7 个类别**
# （beef eye round / beef stomach / beef tripe / duck feet / pork / pork backfat /
# pork spare rib），而这 7 个全部属于「肉类」；Target 小票给出的 13 个关键词全是
# 品牌名。类别一按描述增长，10.A 的收口面板就退化成行列表的重印——而那个面板的
# 全部价值正是把 15 行压成 4 个数。
#
# ⚠️ **键是分类的 `name_normalized`，而且查不到就落未分类**。老板可以改分类名
# （`imp_notes.md`：不强制所有餐厅采用同一套分类体系），改完这里就对不上了——
# 那时落「未分类」是正确行为，不是故障。绝不能反过来按词表去新建分类。

_LATIN_CATEGORY_WORDS: Final[dict[str, str]] = {
    # 肉类与禽类
    **{
        word: "meat & poultry"
        for word in (
            "beef", "pork", "chicken", "duck", "lamb", "mutton", "veal", "turkey",
            "bacon", "ham", "sausage", "rib", "ribs", "tripe", "stomach", "brisket",
            "sirloin", "tenderloin", "breast", "thigh", "wing", "wings", "drumstick",
            "backfat", "lard", "offal", "gizzard", "liver", "oxtail", "meat", "poultry",
        )
    },
    # 海鲜
    **{
        word: "seafood"
        for word in (
            "fish", "salmon", "tuna", "cod", "tilapia", "shrimp", "prawn", "crab",
            "lobster", "squid", "octopus", "clam", "clams", "oyster", "oysters",
            "mussel", "scallop", "seafood", "eel",
        )
    },
    # 蔬菜
    **{
        word: "vegetables"
        for word in (
            "lettuce", "cabbage", "tomato", "tomatoes", "onion", "onions", "potato",
            "potatoes", "carrot", "carrots", "celery", "broccoli", "spinach",
            "cucumber", "pepper", "peppers", "mushroom", "mushrooms", "garlic",
            "ginger", "scallion", "bok", "choy", "eggplant", "zucchini", "vegetable",
            "greens", "sprouts", "tofu",
        )
    },
    # 乳制品与蛋类
    **{
        word: "dairy & eggs"
        for word in (
            "milk", "cream", "butter", "cheese", "yogurt", "yoghurt", "egg", "eggs",
            "dairy",
        )
    },
    # 干货
    **{
        word: "dry goods"
        for word in (
            "flour", "rice", "noodle", "noodles", "pasta", "bean", "beans", "lentil",
            "starch", "sesame", "peanut", "peanuts", "nut", "nuts", "dried",
        )
    },
    # 冷冻食品
    **{word: "frozen food" for word in ("frozen", "ice")},
    # 油类与调味品
    **{
        word: "oils & condiments"
        for word in (
            "oil", "sauce", "vinegar", "salt", "sugar", "soy", "soya", "syrup",
            "ketchup", "mayonnaise", "mustard", "spice", "spices", "seasoning",
            "paste", "msg", "wine", "cooking",
        )
    },
}

# 中文按**子串**匹配，因为中文没有词间空格：`牛柏叶` 里的「牛」切不出来。
# 顺序有意义——长的先比，否则「油」会在「酱油」之前命中，把酱油判成油类
# （同一类，无害；但换成「鱼」与「鱿鱼」就不是无害的了）。
_HAN_CATEGORY_WORDS: Final[tuple[tuple[str, str], ...]] = (
    ("鱿鱼", "seafood"),
    ("带鱼", "seafood"),
    ("三文鱼", "seafood"),
    ("酱油", "oils & condiments"),
    ("芝麻", "oils & condiments"),
    ("土豆", "vegetables"),
    ("西红柿", "vegetables"),
    ("牛", "meat & poultry"),
    ("猪", "meat & poultry"),
    ("鸭", "meat & poultry"),
    ("鸡", "meat & poultry"),
    ("羊", "meat & poultry"),
    ("肉", "meat & poultry"),
    ("排", "meat & poultry"),
    ("肚", "meat & poultry"),
    ("肠", "meat & poultry"),
    ("蹄", "meat & poultry"),
    ("鱼", "seafood"),
    ("虾", "seafood"),
    ("蟹", "seafood"),
    ("贝", "seafood"),
    ("菜", "vegetables"),
    ("葱", "vegetables"),
    ("蒜", "vegetables"),
    ("姜", "vegetables"),
    ("菇", "vegetables"),
    ("瓜", "vegetables"),
    ("豆", "vegetables"),
    ("奶", "dairy & eggs"),
    ("蛋", "dairy & eggs"),
    ("芝士", "dairy & eggs"),
    ("粉", "dry goods"),
    ("米", "dry goods"),
    ("面", "dry goods"),
    ("油", "oils & condiments"),
    ("酱", "oils & condiments"),
    ("醋", "oils & condiments"),
    ("盐", "oils & condiments"),
    ("糖", "oils & condiments"),
)

UNCATEGORIZED_NAME_NORMALIZED: Final[str] = "uncategorized"
"""「未分类」那条分类的锚点。

⚠️ **判定用它，不拿显示名做判据**——老板改个名（`imp_notes.md` 允许他改）
就全乱了。这个常量同时是迁移 seed 写进去的 `name_normalized`。
"""


def guess_category_key(keywords: Keywords) -> str | None:
    """关键词 → 分类的 `name_normalized`；认不出返回 `None`（调用方落未分类）。

    英文**按词**扫，中文**按子串**扫，英文优先：真实样本里英文名是供应商自己
    印的标准写法，中文名常常是手写的俗名（`靓排粒`、`老牛`）。

    ⚠️ 本函数**不碰数据库**，返回的只是一个待查的键。那条分类可能已经被改名或
    删掉，查不到就落未分类——那是正确行为，不是故障。
    """
    if keywords.name:
        for token in tokenize(keywords.name):
            hit = _LATIN_CATEGORY_WORDS.get(expand(token))
            if hit is not None:
                return hit
    if keywords.name_zh:
        folded = normalize_name(keywords.name_zh)
        for word, category in _HAN_CATEGORY_WORDS:
            if word in folded:
                return category
    return None


def best_coverage(query: Prepared, targets: Iterable[str]) -> float:
    """查询对一组目标文本里最像的那一个的单向分数。"""
    best = 0.0
    for target in targets:
        score = coverage(query, prepare(target))
        if score > best:
            best = score
    return best


__all__ = [
    "ABBREVIATIONS",
    "Keywords",
    "UNCATEGORIZED_NAME_NORMALIZED",
    "extract_keywords",
    "guess_category_key",
    "tokenize_preserving_case",
    "MATCH_REASONS",
    "MIN_FUZZY_TOKEN_LENGTH",
    "NOISE_TOKENS",
    "Prepared",
    "REASON_CONTAINS",
    "REASON_DESCRIPTION_EXACT",
    "REASON_DESCRIPTION_FUZZY",
    "REASON_EXACT",
    "REASON_FREQUENT",
    "REASON_FUZZY",
    "REASON_MERGED_FROM",
    "REASON_PREFIX",
    "REASON_RECENT",
    "REASON_SUPPLIER_HISTORY",
    "SUGGEST_MIN_SCORE",
    "best_coverage",
    "content_tokens",
    "coverage",
    "expand",
    "prepare",
    "similarity",
    "token_ratio",
    "tokenize",
]
