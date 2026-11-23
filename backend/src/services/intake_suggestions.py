"""商家记忆与相似堆：整理页上的商家建议（多文件计划 M07）。

M05 做完了 docs/multifiles.md 第 3.2 节的**第一层**（纸面直接读出）。本模块做剩下
的三层，一次模型调用都不发生——它们全部是**本地计算与数据库查询**：

| 层 | 依据 | 来源 | `basis` |
| --- | --- | --- | --- |
| 2 | 同一份文件里别的页已经被人确认过商家 | 数据库 | `same_file` |
| 3 | 历史来源与别名（`COSTCO` / `Costco` 不分成两组） | 数据库 | `history` |
| 4 | 相似外观的待办堆（版式指纹） | `documents/layout.py` 本地量 | `similar_layout` |

### 三条红线

1. **一个字都不会写进 `decided_*`。** 产出全部是
   `intake_item_candidates`（M02 的 `replace_candidates`），界面必须把「系统建议」
   与「人确认过」分开显示（第 4 节）。再像的版式也不会把一堆内容自动确认到某一家。
2. **纸面明确冲突的来源优先。** 这一页纸上读出的是 B，而同文件别的页确认成了 A
   时，**不给这一页 A 的建议**，只记一条「单页证据优先」的说明（第 3.2 节第 2 层
   的原话）。版式建议同理：有纸面读法的页根本不进堆。
3. **相似度只用于排序。** 评分是**档位**，不是概率；代码里没有任何一处拿它与阈值
   比较后自动确认。唯一的真阈值是 `matching.SUGGEST_MIN_SCORE`（步骤 10 的入选
   线，0.70）——它只决定「这条模糊写法值不值得摆出来给人看」。

### 什么落库、什么不落库

- **落库的只有别名映射**（`intake_source_aliases`）：「纸面上这种写法 = 这一家」
  是人的决定，推不出来，而且第 5 节明确要求它**可更正**。
- **「这个规范化键现在叫什么」不落库**：它是最近一次人工确认的写法聚合出来的
  派生值（与「日期/商家组不落表」同一条）。
- **「这个版式属于哪一家」不落库**：由 `intake_items.layout_fingerprint` 加那一页
  上的人工确认派生。要更正就改那一页的确认，不会出现两份打架的真相。
- **版式指纹本身落两列**，那是**本地测量的缓存**：一页约 0.3—0.6 秒，而输入
  （按 sha256 寻址的字节）永不改变，所以这份测量不会过期。

### 来源候选不是供应商模块

M07 的原话：「来源候选与商家主数据不是应付账款供应商模块」。所以这里没有联系人、
地址、账期、商品目录，也没有「新建供应商」这个动作——一家商家在系统里存在，就是
因为有人在某张卡片或某条采购记录上确认过这个名字。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime
from decimal import Decimal
from typing import Any, Final, Mapping, Sequence

from src.db import Database
from src.documents.layout import profile_page_layout
from src.errors import NotFoundError, ValidationError
from src.integrations.file_storage import LocalFileStorage
from src.models import (
    INTAKE_ALIAS_BASES,
    INTAKE_ALIAS_BASIS_CONFIRMED_READING,
    INTAKE_ALIAS_BASIS_PAPER_BILINGUAL,
    INTAKE_ALIAS_BASIS_USER,
    INTAKE_CANDIDATE_BASIS_HISTORY,
    INTAKE_CANDIDATE_BASIS_PAPER,
    INTAKE_CANDIDATE_BASIS_SAME_FILE,
    INTAKE_CANDIDATE_BASIS_SIMILAR_LAYOUT,
    INTAKE_CANDIDATE_FIELD_SOURCE_NAME,
    INTAKE_ITEM_KIND_PAGE,
    SOURCE_NAME_MAX_LENGTH,
    IntakeItem,
    IntakeSourceAlias,
)
from src.repositories.intake_suggestions import IntakeSourceMemoryRepository
from src.services.ingredients import normalize_name
from src.services.intake import (
    MAX_ITEMS_PER_PAGE,
    CandidateInput,
    IntakeService,
)
from src.services.matching import SUGGEST_MIN_SCORE, prepare, similarity
from src.services.validation import (
    require_limit,
    require_offset,
    require_text,
)

logger = logging.getLogger(__name__)

# 本模块产生的三层建议。第一层（纸面读出）归 M05，这里不碰它——否则重算建议会把
# 真正的纸面读法擦掉。
SUGGESTION_BASES: Final[tuple[str, ...]] = (
    INTAKE_CANDIDATE_BASIS_SAME_FILE,
    INTAKE_CANDIDATE_BASIS_HISTORY,
    INTAKE_CANDIDATE_BASIS_SIMILAR_LAYOUT,
)

# --- 排序档位 ---------------------------------------------------------------
#
# ⚠️ 这些数**不是概率，也不是阈值**：它们只决定同一张卡片上几条建议的先后。
# 代码里没有任何一处拿它们与某个线比较后自动确认（M07 第三条子项的原话：
# 「相似度阈值只用于候选排序，不自动把整堆确认到某商家」）。
#
# 档位的顺序有依据：同一份文件里**人已经确认过**的那一家，比任何文本或外观推断
# 都可靠；纸面写法与历史来源对得上，比写法相似可靠；写法相似又比「长得像」可靠。
SCORE_SAME_FILE: Final[Decimal] = Decimal("0.80")
SCORE_HISTORY_EXACT: Final[Decimal] = Decimal("0.75")
SCORE_HISTORY_ALIAS: Final[Decimal] = Decimal("0.70")
SCORE_HISTORY_SIMILAR_BASE: Final[Decimal] = Decimal("0.55")
SCORE_HISTORY_SIMILAR_SPAN: Final[Decimal] = Decimal("0.10")
"""模糊写法落在 0.55—0.65：始终低于别名（0.70），始终高于版式（0.40）。"""
SCORE_SIMILAR_LAYOUT: Final[Decimal] = Decimal("0.40")
"""版式是最弱的一层：第 2 节引的两篇研究说明同一模板常共享布局，而细小差异会
造成误配。它只够形成一堆待人确认的内容。"""

# 一次建议最多摆几条模糊写法。再多就是在浪费老板最贵的那段时间（与
# `services/suggestions.py` 的入选线同一条考虑）。
MAX_HISTORY_SUGGESTIONS: Final[int] = 3

# 一次驱动最多量几页版式。⚠️ 比切页的上限小一个数量级：量一页 0.3—0.6 秒，
# 78 页会把一个 HTTP 请求拖到半分钟以上。**指纹本身就是队列**（`layout_fingerprint`
# 为 NULL 且 `layout_features` 也为 NULL = 还没量过），所以没量完的下次接着量，
# 与切页 / 识别两条队列同源。
MAX_LAYOUT_PROFILES_PER_RUN: Final[int] = 20

# 记忆列表的上限。一家餐厅的供应商是几十家量级，500 足够而且防住了「历史表一大
# 就把整张表取回内存」。
MAX_MEMORY_SOURCES: Final[int] = 500
MAX_ALIASES: Final[int] = 500

# 堆视图里每堆最多列几张卡片的 id。整堆命名要的是 id 列表，但一堆几百页时不该
# 把它们全塞进一个响应——分页读内容走 M06 的 `/groups/items`。
MAX_PILE_MEMBERS: Final[int] = 200


# --- 只读视图 -----------------------------------------------------------------


@dataclass(frozen=True)
class AliasView:
    """一条别名映射：纸面上的一种写法指向人确认过的哪一家。"""

    id: int
    alias_key: str
    alias_text: str
    source_key: str
    source_name: str
    basis: str
    evidence: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class SourceMemoryEntry:
    """一家确认过的来源。

    `source_name` 是**最近一次人工确认的写法**，不是翻译、不是模型给的名字。
    `confirmed_items` / `purchase_records` 让人看出这条记忆有多少依据。
    """

    source_key: str
    source_name: str
    confirmed_items: int
    purchase_records: int
    last_confirmed_at: datetime | None
    aliases: tuple[AliasView, ...] = ()


@dataclass(frozen=True)
class SuggestionView:
    """写给某张卡片的一条建议。`score` 只用于排序。"""

    basis: str
    value: str
    value_normalized: str
    score: Decimal
    evidence: dict[str, Any]


@dataclass(frozen=True)
class ItemSuggestions:
    """一张卡片这一轮的建议结果。

    `paper_source` 是 M05 读出来的纸面写法（如果有）。`conflicts` 记下**因为纸面
    证据优先而被压住**的建议——它不是错误，而是第 3.2 节要求的行为，必须看得见。
    """

    item_id: int
    kind: str
    page_no: int | None
    row_no: int | None
    original_name: str
    paper_source: str | None
    suggestions: tuple[SuggestionView, ...]
    conflicts: tuple[dict[str, Any], ...] = ()
    notes: tuple[str, ...] = ()
    resolved: bool = False
    """这张卡片的商家已经由人确认过（或被排除出堆）：本轮只清掉过期建议。"""


@dataclass(frozen=True)
class PileView:
    """一堆版式相同、**读不出商家**的内容。

    堆是**算出来的**，不落表（与日期/商家组同一条）：它就是「同一个指纹的未决内容」
    这句查询的结果。`excluded_item_ids` 是人一张张排除掉的例外（M06 的
    `pile_excluded`），它们不在 `item_ids` 里，也不会拿到这一堆的建议。
    """

    fingerprint: str
    content_class: str | None
    basis_detail: str
    template_evidence: bool
    features: dict[str, Any]
    item_ids: tuple[int, ...]
    excluded_item_ids: tuple[int, ...]
    confirmed_sources: tuple[dict[str, Any], ...]
    notes: tuple[str, ...] = ()

    @property
    def item_count(self) -> int:
        return len(self.item_ids)


@dataclass(frozen=True)
class PileBoard:
    """整理页右侧那些「相似的 N 张」。"""

    batch_id: int
    piles: tuple[PileView, ...]
    unprofiled_item_ids: tuple[int, ...]
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SuggestionRun:
    """一次建议重算的全部产出。

    `model_calls` 恒为 0，而且是**结构上**的 0：本服务没有识别器这个依赖，
    连一个可以调用的对象都没有。
    """

    batch_id: int
    items: tuple[ItemSuggestions, ...]
    piles: tuple[PileView, ...]
    aliases_recorded: tuple[AliasView, ...]
    layout_profiled: int
    layout_pending: int
    total_items: int
    limit: int
    offset: int
    model_calls: int = 0
    notes: tuple[str, ...] = dataclass_field(default_factory=tuple)


# --- 内部事实 -----------------------------------------------------------------


@dataclass
class _ItemFacts:
    """一张卡片在事务外要用到的事实。ORM 行不出 Session（与别处同一条）。"""

    id: int
    batch_file_id: int
    kind: str
    page_no: int | None
    row_no: int | None
    original_name: str
    storage_key: str
    decided_key: str | None
    pile_excluded: bool
    layout_fingerprint: str | None
    layout_measured: bool
    layout_features: dict[str, Any]
    paper_sources: tuple[tuple[str, str, dict[str, Any]], ...]
    """纸面读出的商家候选：`(展示写法, 规范化键, 证据)`。"""


def _json_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class IntakeSuggestionService:
    """商家记忆与相似堆。

    落库一律经 M02 的 `IntakeService.replace_candidates`（候选）与本模块自己的
    Repository（别名、指纹），与 M03 / M05 同一条分层。**没有识别器依赖**：
    本模块不可能调用任何外部服务。
    """

    def __init__(
        self,
        intake: IntakeService,
        *,
        database: Database,
        file_storage: LocalFileStorage,
    ) -> None:
        self._intake = intake
        self._database = database
        self._storage = file_storage

    # --- 商家记忆 -----------------------------------------------------------

    def source_memory(self, *, limit: int = 100) -> tuple[SourceMemoryEntry, ...]:
        """人确认过的来源 + 它们的别名。

        这就是 M07 第一条子项里的「规范化名称」：键是规范化写法
        （`Costco` 与 `COSTCO` 同一个键），展示名是**最近一次人工确认的写法**。
        ⚠️ 不翻译：纸面英文由 M01/M05 在读纸面时就选好了，这里不把中文名自行
        译成英文，也不凭缩写扩写法定名称（第 3.1 节）。
        """
        clean_limit = require_limit(limit, max_limit=MAX_MEMORY_SOURCES)
        entries = self._memory_entries()
        return tuple(entries.values())[:clean_limit]

    def list_aliases(self, *, limit: int = 100, offset: int = 0) -> tuple[AliasView, ...]:
        clean_limit = require_limit(limit, max_limit=MAX_ALIASES)
        clean_offset = require_offset(offset)
        with self._database.unit_of_work() as session:
            rows = IntakeSourceMemoryRepository(session).list_aliases(
                limit=clean_limit, offset=clean_offset
            )
            return tuple(_alias_view(row) for row in rows)

    def set_alias(
        self,
        *,
        alias_text: Any,
        source_name: Any,
        basis: str = INTAKE_ALIAS_BASIS_USER,
        evidence: Mapping[str, Any] | None = None,
    ) -> AliasView:
        """记下或**更正**一条别名：这种写法就是那一家。

        这是第 5 节要求的「可更正」那一半：同一个 `alias_key` 再来一次就是改指向，
        `DELETE` 就是撤销。人改过的别名（`basis=user`）之后不会被自动重算覆盖——
        否则老板纠正过的东西下一批又被系统改回去。

        ⚠️ 它仍然**只影响建议**：别名不会把两家已有的采购记录合并，也不会去改任何
        `decided_*`。「相似名称只建议，不自动合并不同商家」（第 3.2 节第 3 层）。
        """
        clean_alias = require_text(
            alias_text, field="alias_text", max_length=SOURCE_NAME_MAX_LENGTH
        )
        clean_source = require_text(
            source_name, field="source_name", max_length=SOURCE_NAME_MAX_LENGTH
        )
        if basis not in INTAKE_ALIAS_BASES:
            raise ValidationError(
                f"字段 basis 只能是 {', '.join(INTAKE_ALIAS_BASES)} 之一"
                f"（收到 {basis!r}）。",
                details={"field": "basis", "allowed": list(INTAKE_ALIAS_BASES)},
            )
        alias_key = normalize_name(clean_alias)
        source_key = normalize_name(clean_source)
        if not alias_key or not source_key:
            raise ValidationError(
                "别名与商家名都不能是空白。", details={"field": "alias_text"}
            )
        if alias_key == source_key:
            raise ValidationError(
                f"「{clean_alias}」与「{clean_source}」规范化之后是同一个名字，"
                "不需要别名（规范化已经把大小写与空白差异吸收掉了）。",
                details={"field": "alias_text", "alias_key": alias_key},
            )
        with self._database.unit_of_work() as session:
            repository = IntakeSourceMemoryRepository(session)
            row = repository.alias_for(alias_key)
            payload = None if evidence is None else json.dumps(
                dict(evidence), ensure_ascii=False
            )
            if row is None:
                row = repository.add_alias(
                    IntakeSourceAlias(
                        alias_key=alias_key,
                        alias_text=clean_alias,
                        source_key=source_key,
                        source_name=clean_source,
                        basis=basis,
                        evidence=payload,
                    )
                )
            else:
                row.alias_text = clean_alias
                row.source_key = source_key
                row.source_name = clean_source
                row.basis = basis
                row.evidence = payload
                session.flush()
            logger.info(
                "intake source alias set alias=%s source=%s basis=%s",
                alias_key,
                source_key,
                basis,
            )
            return _alias_view(row)

    def delete_alias(self, alias_id: int) -> AliasView:
        """撤销一条别名。建议立刻不再出现，历史分类与采购记录一个字都不动。"""
        with self._database.unit_of_work() as session:
            repository = IntakeSourceMemoryRepository(session)
            row = repository.get_alias(alias_id)
            if row is None:
                raise NotFoundError(
                    f"找不到别名 #{alias_id}。", details={"alias_id": alias_id}
                )
            view = _alias_view(row)
            repository.delete_alias(row)
            logger.info("intake source alias deleted alias=%s", view.alias_key)
            return view

    # --- 一次建议重算 -------------------------------------------------------

    def suggest_batch(
        self, batch_id: int, *, limit: int = 50, offset: int = 0
    ) -> SuggestionRun:
        """为这个批次的卡片重算第 2—4 层建议。

        **重复驱动安全且免费**：建议是整组替换的派生结果，再跑一次只会得到同样的
        东西；一次模型调用都不会发生。大批次按 `limit` / `offset` 分几次跑
        （五千行的表格不该在一个请求里算完）。

        顺序是刻意的：先**记住这一批里人已经确认过的来源**（别名候选），再用记忆
        去给还没定的卡片提建议。于是「人确认一次之后，后续批次有建议」这条
        （第 3.2 节第 5 层）在同一个批次里也成立。
        """
        clean_limit = require_limit(limit, max_limit=MAX_ITEMS_PER_PAGE)
        clean_offset = require_offset(offset)
        self._intake.get_batch(batch_id)

        aliases_recorded = self._remember_confirmed(batch_id)
        facts, total = self._load_facts(
            batch_id, limit=clean_limit, offset=clean_offset
        )
        profiled, pending = self._ensure_layout_profiles(facts)

        memory = self._memory_entries()
        aliases = self._alias_map()
        fingerprints = sorted(
            {
                fact.layout_fingerprint
                for fact in facts
                if fact.layout_fingerprint and not fact.decided_key
            }
        )
        layout_memory = self._layout_memory(fingerprints)
        confirmed_in_file: dict[int, list[tuple[str, str, int | None]]] = {}

        results: list[ItemSuggestions] = []
        for fact in facts:
            if fact.decided_key or fact.pile_excluded:
                results.append(self._clear_suggestions(fact))
                continue
            siblings = confirmed_in_file.get(fact.batch_file_id)
            if siblings is None:
                siblings = self._confirmed_in_file(fact.batch_file_id)
                confirmed_in_file[fact.batch_file_id] = siblings
            results.append(
                self._suggest_item(
                    fact,
                    siblings=siblings,
                    memory=memory,
                    aliases=aliases,
                    layout_memory=layout_memory,
                )
            )

        board = self.piles(batch_id)
        notes = [
            "建议全部是**候选**：`decided_*` 一个字都没有被写（第 4 节）。",
            "一次模型调用都没有发生——本服务没有识别器依赖，三层线索都是本地"
            "计算与数据库查询。",
        ]
        if pending:
            notes.append(
                f"还有 {pending} 页没量版式（一次最多量 "
                f"{MAX_LAYOUT_PROFILES_PER_RUN} 页）。再调一次接着量，"
                "已量过的不会重量。"
            )
        logger.info(
            "intake suggestions recomputed batch=%s items=%s piles=%s profiled=%s",
            batch_id,
            len(results),
            len(board.piles),
            profiled,
        )
        return SuggestionRun(
            batch_id=batch_id,
            items=tuple(results),
            piles=board.piles,
            aliases_recorded=aliases_recorded,
            layout_profiled=profiled,
            layout_pending=pending,
            total_items=total,
            limit=clean_limit,
            offset=clean_offset,
            notes=tuple(notes),
        )

    # --- 相似堆 -------------------------------------------------------------

    def piles(self, batch_id: int) -> PileBoard:
        """这个批次里版式相同、读不出商家的内容分成哪几堆。

        只读：**不**量新的版式、不写候选（那是 `suggest_batch`）。所以刷新整理页
        不会悄悄花掉几十秒去量图像。
        """
        self._intake.get_batch(batch_id)
        with self._database.unit_of_work() as session:
            rows = IntakeSourceMemoryRepository(session).pile_rows(batch_id)
        members: dict[str, list[int]] = {}
        excluded: dict[str, list[int]] = {}
        features: dict[str, dict[str, Any]] = {}
        unprofiled: list[int] = []
        for fingerprint, item_id, is_excluded, decided, raw_features in rows:
            if fingerprint is None:
                if not decided:
                    unprofiled.append(item_id)
                continue
            features.setdefault(fingerprint, _json_object(raw_features))
            if decided:
                # 已经确认过商家的页不在堆里——它是**版式记忆的依据**，不是待办。
                continue
            (excluded if is_excluded else members).setdefault(fingerprint, []).append(
                item_id
            )

        active = [key for key in members if members[key]]
        layout_memory = self._layout_memory(sorted(active))
        piles: list[PileView] = []
        for fingerprint in active:
            feature_map = features.get(fingerprint, {})
            template = bool(feature_map.get("template_evidence"))
            confirmed = [
                {
                    "source_key": entry["source_key"],
                    "source_name": entry["source_name"],
                    "pages": entry["pages"],
                    "last_confirmed_at": entry["last_confirmed_at"],
                }
                for entry in layout_memory.get(fingerprint, [])
            ]
            notes = [
                "版式相同**不证明**商家相同（第 2 节的两篇研究）：这一堆只是"
                "「值得一起看」，不是「同一家」。",
            ]
            if not template:
                notes.append(
                    "⚠️ 这一堆的指纹里**没有模板信息**（照片、空白页，或格线量不"
                    "出来）：它只够让人扫视缩略图，系统不会据此给出商家建议。"
                )
            if len(confirmed) > 1:
                notes.append(
                    f"⚠️ 同一个版式下已经有 {len(confirmed)} 家被人确认过，"
                    "所以这几家会**同时**作为候选摆出来，由人挑——不同商家共用"
                    "版式是真实存在的情况，系统不替人选。"
                )
            piles.append(
                PileView(
                    fingerprint=fingerprint,
                    content_class=feature_map.get("content_class"),
                    basis_detail=(
                        "layout_template" if template else "layout_unknown"
                    ),
                    template_evidence=template,
                    features=_pile_features(feature_map),
                    item_ids=tuple(members[fingerprint][:MAX_PILE_MEMBERS]),
                    excluded_item_ids=tuple(excluded.get(fingerprint, [])),
                    confirmed_sources=tuple(confirmed),
                    notes=tuple(notes),
                )
            )
        piles.sort(key=lambda pile: (-pile.item_count, pile.fingerprint))
        board_notes = [
            "堆是**算出来的**（同一指纹的未决内容），没有「堆」这张表：排除一个"
            "例外只写那一张卡片，堆立刻就对了。",
        ]
        if unprofiled:
            board_notes.append(
                f"{len(unprofiled)} 页不在任何堆里，三种原因之一："
                "**纸面已经读出商家**（有名字可接受，不需要靠外观）、还没量版式"
                "（跑一次 `suggest-sources` 会接着量）、或者量不出来"
                "（原因写在这一页的 `layout_features.problem` 里）。"
            )
        return PileBoard(
            batch_id=batch_id,
            piles=tuple(piles),
            unprofiled_item_ids=tuple(unprofiled),
            notes=tuple(board_notes),
        )

    # --- 内部：记忆 ---------------------------------------------------------

    def _memory_entries(self) -> dict[str, SourceMemoryEntry]:
        """派生出「人确认过哪些来源」。整理阶段的确认与已入账采购记录都算。"""
        with self._database.unit_of_work() as session:
            repository = IntakeSourceMemoryRepository(session)
            intake_rows = repository.confirmed_intake_sources(limit=MAX_MEMORY_SOURCES)
            purchase_rows = repository.confirmed_purchase_sources(
                limit=MAX_MEMORY_SOURCES
            )
            alias_rows = [
                _alias_view(row)
                for row in repository.list_aliases(limit=MAX_ALIASES)
            ]
        aliases_by_source: dict[str, list[AliasView]] = {}
        for alias in alias_rows:
            aliases_by_source.setdefault(alias.source_key, []).append(alias)

        entries: dict[str, SourceMemoryEntry] = {}
        for key, name, last_at, count in intake_rows:
            entries[key] = SourceMemoryEntry(
                source_key=key,
                source_name=name,
                confirmed_items=count,
                purchase_records=0,
                last_confirmed_at=last_at,
                aliases=tuple(aliases_by_source.get(key, ())),
            )
        for key, name, last_at, count in purchase_rows:
            existing = entries.get(key)
            if existing is None:
                entries[key] = SourceMemoryEntry(
                    source_key=key,
                    source_name=name,
                    confirmed_items=0,
                    purchase_records=count,
                    last_confirmed_at=last_at,
                    aliases=tuple(aliases_by_source.get(key, ())),
                )
                continue
            # 整理阶段的写法优先：那是**最近一次**人工分类写下的名字，而采购记录
            # 这边取的是字典序最大的写法（历史记录没有「最后一次改名」这个事实）。
            entries[key] = SourceMemoryEntry(
                source_key=key,
                source_name=existing.source_name,
                confirmed_items=existing.confirmed_items,
                purchase_records=count,
                last_confirmed_at=_later(existing.last_confirmed_at, last_at),
                aliases=existing.aliases,
            )
        return dict(
            sorted(
                entries.items(),
                key=lambda pair: (
                    -(pair[1].confirmed_items + pair[1].purchase_records),
                    pair[0],
                ),
            )
        )

    def _alias_map(self) -> dict[str, AliasView]:
        with self._database.unit_of_work() as session:
            rows = IntakeSourceMemoryRepository(session).list_aliases(
                limit=MAX_ALIASES
            )
            return {row.alias_key: _alias_view(row) for row in rows}

    def _layout_memory(
        self, fingerprints: Sequence[str]
    ) -> dict[str, list[dict[str, Any]]]:
        if not fingerprints:
            return {}
        with self._database.unit_of_work() as session:
            rows = IntakeSourceMemoryRepository(session).layout_memory(
                list(fingerprints)
            )
        memory: dict[str, list[dict[str, Any]]] = {}
        for fingerprint, key, name, last_at, count in rows:
            memory.setdefault(fingerprint, []).append(
                {
                    "source_key": key,
                    "source_name": name,
                    "pages": count,
                    "last_confirmed_at": last_at.isoformat() if last_at else None,
                }
            )
        for entries in memory.values():
            entries.sort(key=lambda entry: (-entry["pages"], entry["source_key"]))
        return memory

    def _remember_confirmed(self, batch_id: int) -> tuple[AliasView, ...]:
        """把这一批里「人确认的商家 ≠ 纸面读出的写法」记成别名候选。

        两种来历：

        - `confirmed_paper_reading`：纸面读出 `COSTCO WHOLESALE #1162`，人确认成
          `Costco`。于是下次读到同样的写法就能建议同一家。
        - `paper_bilingual`：同一张纸上同时印了英文名与中文名，人确认了其中一个，
          另一个写法因此也是这一家。⚠️ **这不是翻译**——两个写法都印在纸上
          （第 3.1 节禁止把中文自行译成英文）。

        人手工改过的别名（`basis=user`）**不会被覆盖**：老板纠正过的东西，下一批
        不该被系统改回去。指向另一家的 `user` 别名只记一条日志，不动它。
        """
        facts, _ = self._load_facts(batch_id, limit=MAX_ITEMS_PER_PAGE, offset=0)
        recorded: list[AliasView] = []
        for fact in facts:
            if not fact.decided_key:
                continue
            for value, key, evidence in fact.paper_sources:
                writings = [(value, key, INTAKE_ALIAS_BASIS_CONFIRMED_READING)]
                for field in ("supplier_english", "supplier_chinese"):
                    other = evidence.get(field)
                    if isinstance(other, str) and other.strip():
                        writings.append(
                            (
                                other.strip(),
                                normalize_name(other),
                                INTAKE_ALIAS_BASIS_PAPER_BILINGUAL,
                            )
                        )
                for text, alias_key, basis in writings:
                    if not alias_key or alias_key == fact.decided_key:
                        continue
                    view = self._remember_alias(
                        alias_text=text,
                        alias_key=alias_key,
                        source_key=fact.decided_key,
                        basis=basis,
                        evidence={
                            "basis_detail": basis,
                            "item_id": fact.id,
                            "original_name": fact.original_name,
                            "page_no": fact.page_no,
                            "row_no": fact.row_no,
                            "paper_text": text,
                            "note": (
                                "人在这一页上确认了商家，而纸面上的写法是这个。"
                                "可以删掉或改指向。"
                            ),
                        },
                    )
                    if view is not None:
                        recorded.append(view)
        return tuple(recorded)

    def _remember_alias(
        self,
        *,
        alias_text: str,
        alias_key: str,
        source_key: str,
        basis: str,
        evidence: dict[str, Any],
    ) -> AliasView | None:
        """写一条自动记下来的别名。已有人工别名时让人工的那条赢。"""
        with self._database.unit_of_work() as session:
            repository = IntakeSourceMemoryRepository(session)
            existing = repository.alias_for(alias_key)
            source_name = self._display_name(session, source_key)
            if existing is not None:
                if existing.basis == INTAKE_ALIAS_BASIS_USER:
                    if existing.source_key != source_key:
                        logger.info(
                            "intake alias kept user mapping alias=%s user_source=%s "
                            "observed_source=%s",
                            alias_key,
                            existing.source_key,
                            source_key,
                        )
                    return None
                if existing.source_key == source_key:
                    existing.source_name = source_name
                    existing.evidence = json.dumps(evidence, ensure_ascii=False)
                    session.flush()
                    return _alias_view(existing)
                # 同一种写法以前被确认成另一家：**不静默改指向**。两家都是人确认
                # 过的，谁对只有人知道，所以留着旧的并记一条日志。
                logger.warning(
                    "intake alias conflict alias=%s existing=%s observed=%s",
                    alias_key,
                    existing.source_key,
                    source_key,
                )
                return None
            row = repository.add_alias(
                IntakeSourceAlias(
                    alias_key=alias_key,
                    alias_text=alias_text,
                    source_key=source_key,
                    source_name=source_name,
                    basis=basis,
                    evidence=json.dumps(evidence, ensure_ascii=False),
                )
            )
            return _alias_view(row)

    def _display_name(self, session: Any, source_key: str) -> str:
        """这个规范化键最近一次被人写成什么。查不到就退回键本身。"""
        rows = IntakeSourceMemoryRepository(session).confirmed_intake_sources(
            limit=MAX_MEMORY_SOURCES
        )
        for key, name, _last_at, _count in rows:
            if key == source_key:
                return name
        return source_key

    # --- 内部：逐卡片建议 ---------------------------------------------------

    def _suggest_item(
        self,
        fact: _ItemFacts,
        *,
        siblings: Sequence[tuple[str, str, int | None]],
        memory: Mapping[str, SourceMemoryEntry],
        aliases: Mapping[str, AliasView],
        layout_memory: Mapping[str, list[dict[str, Any]]],
    ) -> ItemSuggestions:
        paper_keys = {key for _value, key, _evidence in fact.paper_sources}
        paper_display = next(
            (value for value, _key, _evidence in fact.paper_sources), None
        )
        conflicts: list[dict[str, Any]] = []
        notes: list[str] = []

        same_file = self._same_file_suggestions(
            fact, siblings=siblings, paper_keys=paper_keys, conflicts=conflicts
        )
        history = self._history_suggestions(
            fact, memory=memory, aliases=aliases, notes=notes
        )
        layout = self._layout_suggestions(
            fact, paper_keys=paper_keys, layout_memory=layout_memory, notes=notes
        )

        for basis, suggestions in (
            (INTAKE_CANDIDATE_BASIS_SAME_FILE, same_file),
            (INTAKE_CANDIDATE_BASIS_HISTORY, history),
            (INTAKE_CANDIDATE_BASIS_SIMILAR_LAYOUT, layout),
        ):
            self._write(fact.id, basis, suggestions)

        ordered = sorted(
            [*same_file, *history, *layout], key=lambda item: -item.score
        )
        return ItemSuggestions(
            item_id=fact.id,
            kind=fact.kind,
            page_no=fact.page_no,
            row_no=fact.row_no,
            original_name=fact.original_name,
            paper_source=paper_display,
            suggestions=tuple(ordered),
            conflicts=tuple(conflicts),
            notes=tuple(notes),
        )

    def _same_file_suggestions(
        self,
        fact: _ItemFacts,
        *,
        siblings: Sequence[tuple[str, str, int | None]],
        paper_keys: set[str],
        conflicts: list[dict[str, Any]],
    ) -> list[SuggestionView]:
        """第二层：同一份文件里别的页已经确认过的商家。

        ⚠️ **纸面写着别的来源时不给这条建议**（第 3.2 节第 2 层的原话「若某页明确
        写着不同来源，单页证据优先」）。被压住的建议记进 `conflicts` 让人看得见，
        而不是悄悄不提。
        """
        grouped: dict[str, dict[str, Any]] = {}
        for name, key, page_no in siblings:
            entry = grouped.setdefault(
                key, {"name": name, "pages": [], "count": 0}
            )
            entry["count"] += 1
            if page_no is not None:
                entry["pages"].append(page_no)
        suggestions: list[SuggestionView] = []
        for key, entry in sorted(
            grouped.items(), key=lambda pair: (-pair[1]["count"], pair[0])
        ):
            if paper_keys and key not in paper_keys:
                conflicts.append(
                    {
                        "basis": INTAKE_CANDIDATE_BASIS_SAME_FILE,
                        "suppressed_value": entry["name"],
                        "suppressed_source_key": key,
                        "paper_sources": sorted(paper_keys),
                        "reason": (
                            "这一页纸面上读出的来源与同文件已确认的那一家不同："
                            "**单页证据优先**，所以不给这条建议。"
                        ),
                    }
                )
                continue
            suggestions.append(
                SuggestionView(
                    basis=INTAKE_CANDIDATE_BASIS_SAME_FILE,
                    value=entry["name"],
                    value_normalized=key,
                    score=SCORE_SAME_FILE,
                    evidence={
                        "basis_detail": "same_file_confirmed",
                        "batch_file_id": fact.batch_file_id,
                        "original_name": fact.original_name,
                        "confirmed_pages": sorted(entry["pages"])[:20],
                        "confirmed_items": entry["count"],
                        "note": (
                            "同一份文件里已经有人把这几页确认成这一家。"
                            "⚠️ 仍然要人点一下：一份文件里混着两家的单据是"
                            "真实存在的情况。"
                        ),
                    },
                )
            )
        return suggestions

    def _history_suggestions(
        self,
        fact: _ItemFacts,
        *,
        memory: Mapping[str, SourceMemoryEntry],
        aliases: Mapping[str, AliasView],
        notes: list[str],
    ) -> list[SuggestionView]:
        """第三层：历史来源与别名。

        ⚠️ **只从纸面读出的写法出发**。没有纸面写法的卡片一条历史建议都不给——
        凭「上次这家买得多」去填一个商家名，就是编造来源（M02 规则三）。
        ⚠️ 也**不看文件名**：M05 送识别时就把 `original_name` 置空了，商家名必须
        来自纸面而不是某人给文件起的名字。
        """
        if not fact.paper_sources:
            return []
        suggestions: list[SuggestionView] = []
        seen: set[str] = set()
        for value, key, _evidence in fact.paper_sources:
            entry = memory.get(key)
            if entry is not None:
                if key not in seen:
                    seen.add(key)
                    suggestions.append(
                        SuggestionView(
                            basis=INTAKE_CANDIDATE_BASIS_HISTORY,
                            value=entry.source_name,
                            value_normalized=key,
                            score=SCORE_HISTORY_EXACT,
                            evidence={
                                "basis_detail": "history_exact",
                                "paper_text": value,
                                "confirmed_items": entry.confirmed_items,
                                "purchase_records": entry.purchase_records,
                                "last_confirmed_at": (
                                    entry.last_confirmed_at.isoformat()
                                    if entry.last_confirmed_at
                                    else None
                                ),
                                "note": (
                                    "纸面写法规范化之后与确认过的来源完全相同，"
                                    "所以显示成人上次写的那个名字"
                                    "（`COSTCO` 与 `Costco` 不分成两组）。"
                                ),
                            },
                        )
                    )
                continue
            alias = aliases.get(key)
            if alias is not None:
                target = memory.get(alias.source_key)
                if alias.source_key not in seen:
                    seen.add(alias.source_key)
                    suggestions.append(
                        SuggestionView(
                            basis=INTAKE_CANDIDATE_BASIS_HISTORY,
                            value=(
                                target.source_name if target else alias.source_name
                            ),
                            value_normalized=alias.source_key,
                            score=SCORE_HISTORY_ALIAS,
                            evidence={
                                "basis_detail": "history_alias",
                                "paper_text": value,
                                "alias_id": alias.id,
                                "alias_basis": alias.basis,
                                "note": (
                                    "以前有人把纸面上这种写法确认成这一家。"
                                    "这条映射可以改指向或删掉"
                                    "（`/api/source-memory/aliases`）。"
                                ),
                            },
                        )
                    )
                continue
            # 既不相同也没有别名：才轮到模糊写法，而且只排序、不自动接受。
            prepared = prepare(value)
            scored = sorted(
                (
                    (similarity(prepared, prepare(entry.source_name)), entry)
                    for entry in memory.values()
                ),
                key=lambda pair: -pair[0],
            )
            for score, entry in scored[:MAX_HISTORY_SUGGESTIONS]:
                if score < SUGGEST_MIN_SCORE or entry.source_key in seen:
                    continue
                seen.add(entry.source_key)
                suggestions.append(
                    SuggestionView(
                        basis=INTAKE_CANDIDATE_BASIS_HISTORY,
                        value=entry.source_name,
                        value_normalized=entry.source_key,
                        score=(
                            SCORE_HISTORY_SIMILAR_BASE
                            + SCORE_HISTORY_SIMILAR_SPAN
                            * Decimal(str(round(score, 4)))
                        ).quantize(Decimal("0.0001")),
                        evidence={
                            "basis_detail": "history_similar",
                            "paper_text": value,
                            "similarity": round(score, 3),
                            "note": (
                                "写法相似，**不是**同一个名字："
                                "相似度只用来排这几条候选的先后，"
                                "不同商家的名字长得像是常事（`Cosco` / `Costco`）。"
                            ),
                        },
                    )
                )
            if scored and scored[0][0] >= SUGGEST_MIN_SCORE:
                notes.append(
                    f"纸面写法「{value}」与历史来源不完全相同，"
                    "只给了相似写法的候选。"
                )
        return suggestions

    def _layout_suggestions(
        self,
        fact: _ItemFacts,
        *,
        paper_keys: set[str],
        layout_memory: Mapping[str, list[dict[str, Any]]],
        notes: list[str],
    ) -> list[SuggestionView]:
        """第四层：相似外观的待办堆。

        三道闸门，缺一个就不给建议：

        1. **纸面读出了商家就不进堆**：纸面证据优先，外观不该去覆盖它。
        2. **指纹里必须有模板信息**（量到了格线，或文字层的字号集合够一份排版表）。
           两张都没量出格线的歪照片「指纹相同」，那不是证据。
        3. **同一指纹下必须已经有人确认过商家**，否则这一堆只是待办，没有可建议的
           名字——界面照样把它们摆成一堆让人一次命名（`piles()`）。
        """
        if fact.kind != INTAKE_ITEM_KIND_PAGE or not fact.layout_fingerprint:
            return []
        if paper_keys:
            return []
        if not fact.layout_features.get("template_evidence"):
            return []
        entries = layout_memory.get(fact.layout_fingerprint) or []
        if not entries:
            return []
        if len(entries) > 1:
            notes.append(
                f"这个版式下已经有 {len(entries)} 家被人确认过，"
                "所以几家**同时**作为候选摆出来——系统不替人选。"
            )
        return [
            SuggestionView(
                basis=INTAKE_CANDIDATE_BASIS_SIMILAR_LAYOUT,
                value=entry["source_name"],
                value_normalized=entry["source_key"],
                score=SCORE_SIMILAR_LAYOUT,
                evidence={
                    "basis_detail": "layout_memory",
                    "fingerprint": fact.layout_fingerprint,
                    "layout_features": _pile_features(fact.layout_features),
                    "pages_with_this_layout": entry["pages"],
                    "competing_sources": [
                        item["source_name"] for item in entries
                    ],
                    "note": (
                        "这一页与已经确认过商家的页**版式特征相同**。"
                        "⚠️ 版式相同不证明商家相同——这只是一条待确认的候选，"
                        "系统不会据此自动归组。"
                    ),
                },
            )
            for entry in entries
        ]

    def _clear_suggestions(self, fact: _ItemFacts) -> ItemSuggestions:
        """已确认或已排除的卡片：把三层建议清掉，纸面读法留着。

        为什么要清：建议是**派生**的，前提变了就该没了。人已经确认了商家之后还挂
        着「可能是另一家」的建议，只会让人怀疑自己点错了；被排除出堆的例外更不该
        继续拿到整堆的建议。⚠️ `basis=paper` 一个字都不动——那是纸面事实，不是建议。
        """
        for basis in SUGGESTION_BASES:
            self._write(fact.id, basis, ())
        reason = (
            "商家已由人确认，建议已清空（纸面读法保留）。"
            if fact.decided_key
            else "这张卡片被人移出了相似堆（例外），不再接收整堆的建议。"
        )
        return ItemSuggestions(
            item_id=fact.id,
            kind=fact.kind,
            page_no=fact.page_no,
            row_no=fact.row_no,
            original_name=fact.original_name,
            paper_source=next(
                (value for value, _key, _evidence in fact.paper_sources), None
            ),
            suggestions=(),
            notes=(reason,),
            resolved=True,
        )

    def _write(
        self, item_id: int, basis: str, suggestions: Sequence[SuggestionView]
    ) -> None:
        """整组替换某一层的候选。空列表 = 这一层这一轮什么都没得出。"""
        self._intake.replace_candidates(
            item_id,
            field=INTAKE_CANDIDATE_FIELD_SOURCE_NAME,
            basis=basis,
            candidates=[
                CandidateInput(
                    value=item.value,
                    confidence=item.score,
                    evidence=item.evidence,
                )
                for item in suggestions
            ],
        )

    # --- 内部：事实与版式 ---------------------------------------------------

    def _load_facts(
        self, batch_id: int, *, limit: int, offset: int
    ) -> tuple[list[_ItemFacts], int]:
        with self._database.unit_of_work() as session:
            rows, total = IntakeSourceMemoryRepository(session).batch_items(
                batch_id, limit=limit, offset=offset
            )
            facts = [
                _ItemFacts(
                    id=item.id,
                    batch_file_id=item.batch_file_id,
                    kind=item.kind,
                    page_no=item.page_no,
                    row_no=item.row_no,
                    original_name=item.batch_file.original_name,
                    storage_key=item.batch_file.file.storage_key,
                    decided_key=item.decided_source_name_normalized,
                    pile_excluded=bool(item.pile_excluded),
                    layout_fingerprint=item.layout_fingerprint,
                    layout_measured=(
                        item.layout_fingerprint is not None
                        or item.layout_features is not None
                    ),
                    layout_features=_json_object(item.layout_features),
                    paper_sources=tuple(
                        (
                            candidate.value,
                            candidate.value_normalized
                            or normalize_name(candidate.value),
                            _json_object(candidate.evidence),
                        )
                        for candidate in item.candidates
                        if candidate.field == INTAKE_CANDIDATE_FIELD_SOURCE_NAME
                        and candidate.basis == INTAKE_CANDIDATE_BASIS_PAPER
                    ),
                )
                for item in rows
            ]
            return facts, total

    def _confirmed_in_file(
        self, batch_file_id: int
    ) -> list[tuple[str, str, int | None]]:
        with self._database.unit_of_work() as session:
            rows = IntakeSourceMemoryRepository(session).confirmed_in_file(
                batch_file_id
            )
            return [
                (
                    item.decided_source_name or item.decided_source_name_normalized,
                    item.decided_source_name_normalized,
                    item.page_no,
                )
                for item in rows
                if item.decided_source_name_normalized
            ]

    def _ensure_layout_profiles(self, facts: Sequence[_ItemFacts]) -> tuple[int, int]:
        """给还没量过版式的页量一次，返回（这次量了几页, 还剩几页）。

        **指纹本身就是队列**：`layout_fingerprint` 与 `layout_features` 都为 NULL
        就是「还没量过」；量过但量不出来的页留下 `layout_features.problem`，于是
        不会被反复重量（与 M03 的切页队列、M05 的识别队列同源）。

        量**两类页**，顺序有理由：

        1. **需要进堆的页**（未决、纸面读不出商家、没被排除）——老板正等着它们
           分堆，所以先量。
        2. **已经确认过商家的页**——它们是「历史版式」这条线索**本身**
           （M07 第二条子项）。⚠️ 这一类不能省：真实实跑里发现，纸面能读出商家的
           页往往**一上来就被确认**，如果只量第一类，它们永远没有指纹，于是
           「未来相似单据出现候选」在最常见的那条路径上根本不成立
           （`samples/real/m07/m07_live_replay_layers.json` 的第一版实跑就是这样：
           `similar_layout=0`）。

        表格行（没有版式）、被排除的例外、以及有纸面读法但还没确认的页都不量：
        前两类白花本机算力，第三类已经有名字可接受，不需要靠外观。
        """
        pending_pile = [
            fact
            for fact in facts
            if fact.kind == INTAKE_ITEM_KIND_PAGE
            and not fact.layout_measured
            and not fact.decided_key
            and not fact.pile_excluded
            and not fact.paper_sources
        ]
        reference = [
            fact
            for fact in facts
            if fact.kind == INTAKE_ITEM_KIND_PAGE
            and not fact.layout_measured
            and fact.decided_key
        ]
        waiting = pending_pile + reference
        profiled = 0
        for fact in waiting[:MAX_LAYOUT_PROFILES_PER_RUN]:
            try:
                content = self._storage.read(fact.storage_key)
            except (NotFoundError, ValidationError, OSError) as error:
                self._save_profile(
                    fact,
                    fingerprint=None,
                    features={
                        "problem": f"原件读不出来：{error}",
                        "template_evidence": False,
                    },
                )
                profiled += 1
                continue
            profile = profile_page_layout(
                content,
                page_no=fact.page_no or 1,
                original_name=fact.original_name,
            )
            features = dict(profile.features)
            if profile.problem:
                features["problem"] = profile.problem
            if profile.notes:
                features["notes"] = list(profile.notes)
            self._save_profile(
                fact, fingerprint=profile.fingerprint, features=features
            )
            profiled += 1
        return profiled, max(0, len(waiting) - profiled)

    def _save_profile(
        self, fact: _ItemFacts, *, fingerprint: str | None, features: dict[str, Any]
    ) -> None:
        with self._database.unit_of_work() as session:
            item = session.get(IntakeItem, fact.id)
            if item is None:  # pragma: no cover - 刚读出来的行不该消失
                return
            IntakeSourceMemoryRepository(session).set_layout_profile(
                item,
                fingerprint=fingerprint,
                features=json.dumps(features, ensure_ascii=False),
            )
        fact.layout_fingerprint = fingerprint
        fact.layout_features = features
        fact.layout_measured = True


def _alias_view(row: IntakeSourceAlias) -> AliasView:
    return AliasView(
        id=row.id,
        alias_key=row.alias_key,
        alias_text=row.alias_text,
        source_key=row.source_key,
        source_name=row.source_name,
        basis=row.basis,
        evidence=_json_object(row.evidence) or None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _later(left: datetime | None, right: datetime | None) -> datetime | None:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


def _pile_features(features: Mapping[str, Any]) -> dict[str, Any]:
    """摆给人看的版式特征。

    只透出**能读懂的那几项**：笔迹风格、格线行数、有无竖线、长宽比，或文字层的
    页面尺寸与字号。表头列位置那种一长串小数留在库里，不堆在界面上。
    """
    keys = (
        "content_class",
        "ink_style",
        "ruled_rows",
        "ruled_columns",
        "aspect",
        "grid_detected",
        "page_width_points",
        "page_height_points",
        "font_sizes",
        "producer",
        "template_evidence",
        "problem",
    )
    return {key: features[key] for key in keys if key in features}


__all__ = [
    "MAX_ALIASES",
    "MAX_HISTORY_SUGGESTIONS",
    "MAX_LAYOUT_PROFILES_PER_RUN",
    "MAX_MEMORY_SOURCES",
    "MAX_PILE_MEMBERS",
    "SCORE_HISTORY_ALIAS",
    "SCORE_HISTORY_EXACT",
    "SCORE_SAME_FILE",
    "SCORE_SIMILAR_LAYOUT",
    "SUGGESTION_BASES",
    "AliasView",
    "IntakeSuggestionService",
    "ItemSuggestions",
    "PileBoard",
    "PileView",
    "SourceMemoryEntry",
    "SuggestionRun",
    "SuggestionView",
]
