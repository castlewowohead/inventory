"""「行带 ↔ 商品行」的锚定，以及历史列映射校验（步骤 08.3d，批次 2）。

当前提取流程只使用行号锚定，列映射不再为裁图调用。

### 批次 1 为什么没建立起来

`impl_record.md` 步骤 08 第 5.2 节记了四条试过并失败的路：按行带序号直接当商品行号、
按行带高度认表头、按行带墨迹密度认表头、按「竖线只在数据行里存在」认表头。前三条
是实测分不开；第四条的数据是在**镜像坐标**上量的（`grid._rule_columns` 的左右镜像
错误，见该函数的说明），所以才会「表头带与数据带的逐列覆盖率都是 0.04」。

### 批次 2 走的是另一条路：读印着的字

这批预印单据最左列印着**行号**、表头那一行印着**列名**。两者都是**印刷体**——步骤 07
实测模型在印刷体上的字段准确率 96.6%，是它最准的那一类。所以不再用像素阈值去猜，
而是把这两处裁成拼图交给模型读，**再用本地规则把读数卡死**。

### 本地校验才是闸门，不是模型

模型给的行号读数要同时过这四关，差一关就判「锚定不成立」，不生成逐行墨迹证据：

1. **长度**：读数的个数必须正好等于我们裁出来的格子数。格子是我们自己拼的，
   个数是我们自己的事实。
2. **连号**：有号的那些行带必须印着 1、2、3…M，一个不漏。预印表格的行号一定连号。
3. **连续**：有号的行带必须挨在一起。表头在上、签收在下，数据行是中间连续的一段。
4. **两个模型一致**：两份读数逐格相同才算数。

⚠️ **第 4 关不是多余的。** 2026-11-11 实测到一次**两份各自都合法、却整体差一行**的
读数（ChangShun p01：一个模型答 `[null,1,2,…,15]`，另一个答 `[null,null,1,…,14]`）。
单看任何一份都过得了 1—3 关。而锚定差一行的后果恰恰是批次 1 最怕的那种静默错误：
老板照着**相邻那一行**的字迹选食材，不会有任何报错。

### 2026-11-11 全量实测（74 页真实样本，每页 2 个模型）

| 单据 | 页数 | 锚定成立 | 说明 |
| --- | --- | --- | --- |
| TangFa（印着 1—21 行号） | 17 页判定 | 13 | 失败的都是漏检横线导致行带数对不上，被长度关挡下 |
| ChangShun（印着 1.—15.） | 27 页判定 | 22 | 同上，另有 3 页两模型不一致 |
| FengYuan（**纸上没有行号**） | 18 页 | 0 | **正确拒绝**：没有行号就没有锚定可言 |
| Kunkee / NewMoys（印刷件） | 6 页 | 0 | 行带只有 2—4 条，也没有行号列 |
| 合计 | 68 页判定 | 35 | **误锚 0 处**——不确定的一律落到「未建立」 |
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

MIN_ANCHOR_BANDS = 8
"""行带少于这么多就根本不去试锚定。

实测：Kunkee 只切出 4 条行带、NewMoys 2—3 条，那是印刷件发票，表格行格本来就少，
也没有行号列。对它们发两次请求是白花钱，而且只会得到一串 null。"""

MIN_ANCHORED_ROWS = 3
"""至少要读出这么多个行号才算数。一两个数字可能是手写的笔画被认成了数字。"""

# 表头类别 → 契约里的行内字段名。`weight` / `unit` / `row_number` / `other`
# 不落到契约字段上：重量与包装规格属于步骤 09 的换算，行号列本身不是数据。
HEADER_TO_FIELD = {
    "item_description": "item_description",
    "quantity": "quantity",
    "unit": "unit",
    "unit_price": "unit_price",
    "line_amount": "line_amount",
}

HEADER_IGNORED = frozenset({"row_number", "weight", "other"})


@dataclass(frozen=True)
class RowAnchor:
    """行带序号 ↔ 印刷行号 的对应关系。

    `established=False` 时 `bands` 是空的，`reason` 说明为什么没建立起来。
    """

    established: bool
    reason: str = ""
    source: str = "none"
    bands: tuple[int, ...] = field(default_factory=tuple)
    """第 k 个元素是印刷行号 k+1 所在的**行带序号**。"""

    header_band: int | None = None
    """数据行上面紧挨着的那条行带——通常就是列标题带。读列名用它。"""

    @property
    def printed_rows(self) -> int:
        return len(self.bands)

    def band_of(self, printed_row: int) -> int | None:
        """印刷行号 → 行带序号。越界返回 None，**不夹到边界**。"""
        if 1 <= printed_row <= len(self.bands):
            return self.bands[printed_row - 1]
        return None

    def summary(self) -> dict[str, object]:
        return {
            "established": self.established,
            "source": self.source,
            "reason": self.reason,
            "printed_rows": self.printed_rows,
            "bands": list(self.bands),
            "header_band": self.header_band,
        }


@dataclass(frozen=True)
class ColumnMap:
    """列带序号 ↔ 契约字段名。"""

    established: bool
    reason: str = ""
    fields: dict[str, int] = field(default_factory=dict)
    """字段名 → 列带序号。只收**唯一命中**的字段。"""

    def column_of(self, field_name: str) -> int | None:
        return self.fields.get(field_name)

    def summary(self) -> dict[str, object]:
        return {
            "established": self.established,
            "reason": self.reason,
            "fields": dict(self.fields),
        }


def _normalise(cells: object) -> list[int | None] | None:
    if not isinstance(cells, (list, tuple)):
        return None
    out: list[int | None] = []
    for item in cells:
        out.append(item if isinstance(item, int) and not isinstance(item, bool) else None)
    return out


def row_anchor_from(
    readings: list[tuple[str, object]], *, band_indexes: tuple[int, ...]
) -> RowAnchor:
    """把多个模型的行号读数变成锚定，或者说清楚为什么不成立。

    `readings` 是 (模型名, cells) 的列表；`band_indexes` 是我们裁出来的行带序号，
    顺序与拼图里的格子一一对应——**那是构造上的事实，不是模型给的**。
    """
    if len(band_indexes) < MIN_ANCHOR_BANDS:
        return RowAnchor(
            established=False,
            reason=(
                f"这一页只有 {len(band_indexes)} 条行带（少于 {MIN_ANCHOR_BANDS} 条），"
                "不是那种印着行号的预印表格，不去试锚定。"
            ),
        )
    if not readings:
        return RowAnchor(established=False, reason="没有任何模型给出行号读数。")

    normalised: list[list[int | None]] = []
    for model, cells in readings:
        values = _normalise(cells)
        if values is None:
            return RowAnchor(
                established=False, reason=f"模型 {model} 的行号读数不是一个数组。"
            )
        if len(values) != len(band_indexes):
            return RowAnchor(
                established=False,
                reason=(
                    f"模型 {model} 给了 {len(values)} 个读数，而这一页裁出来的是 "
                    f"{len(band_indexes)} 个格子。**格子数是我们自己的事实**，对不上"
                    "就说明它漏数或多数了一格，锚定不成立。"
                ),
            )
        normalised.append(values)

    first = normalised[0]
    for model, values in zip((name for name, _ in readings[1:]), normalised[1:]):
        if values != first:
            return RowAnchor(
                established=False,
                reason=(
                    f"两个模型的行号读数逐格对不上（在模型 {model} 上）。"
                    "⚠️ 实测出现过两份各自都连号、却整体差一行的读数——锚定差一行的"
                    "后果是老板照着**相邻那一行**的字迹选食材，而且不会有任何报错。"
                    "对不上就不锚定。"
                ),
            )

    positions = [i for i, value in enumerate(first) if value is not None]
    if len(positions) < MIN_ANCHORED_ROWS:
        return RowAnchor(
            established=False,
            reason=(
                f"只读出 {len(positions)} 个行号（需要 {MIN_ANCHORED_ROWS} 个）："
                "这一页的最左列多半没印行号。"
            ),
        )
    if positions != list(range(positions[0], positions[-1] + 1)):
        return RowAnchor(
            established=False,
            reason=(
                "有行号的行带不连续。预印表格的数据行是中间连续的一段"
                "（上面是表头、下面是签收栏），中间断开说明读错了格。"
            ),
        )
    numbers = [first[i] for i in positions]
    if numbers != list(range(1, len(numbers) + 1)):
        return RowAnchor(
            established=False,
            reason=(
                f"行号不是 1…{len(numbers)} 的连号（读到 {numbers[:8]}…）。"
                "预印表格的行号一定连号，不连号说明中间漏读或多读了一格。"
            ),
        )

    bands = tuple(band_indexes[i] for i in positions)
    header = band_indexes[positions[0] - 1] if positions[0] > 0 else None
    anchor = RowAnchor(
        established=True,
        source="row_numbers",
        bands=bands,
        header_band=header,
    )
    logger.info("row anchor established %s", anchor.summary())
    return anchor


def column_map_from(
    readings: list[tuple[str, object]], *, column_indexes: tuple[int, ...]
) -> ColumnMap:
    """把多个模型的表头读数变成「字段 → 列带序号」。

    同样是四关：长度对得上、两个模型一致、取值在允许的类别里、**同一个字段只能
    命中一列**。一个字段命中两列就把它丢掉——两列里哪一列才是它，猜错就是裁错格子。
    """
    if not column_indexes:
        return ColumnMap(established=False, reason="这一页没有列带，谈不上列映射。")
    if not readings:
        return ColumnMap(established=False, reason="没有任何模型给出表头读数。")

    lists: list[list[str | None]] = []
    for model, cells in readings:
        if not isinstance(cells, (list, tuple)):
            return ColumnMap(established=False, reason=f"模型 {model} 的表头读数不是数组。")
        values = [
            item.strip().lower() if isinstance(item, str) and item.strip() else None
            for item in cells
        ]
        if len(values) != len(column_indexes):
            return ColumnMap(
                established=False,
                reason=(
                    f"模型 {model} 给了 {len(values)} 个表头读数，而这一页裁出来的是 "
                    f"{len(column_indexes)} 个列格子，对不上就不映射。"
                ),
            )
        lists.append(values)

    first = lists[0]
    for model, values in zip((name for name, _ in readings[1:]), lists[1:]):
        if values != first:
            return ColumnMap(
                established=False,
                reason=(
                    f"两个模型的表头读数对不上（在模型 {model} 上）。"
                    "列映射错了就会把裁剪图摆到错的输入框旁边，对不上就不映射。"
                ),
            )

    hits: dict[str, list[int]] = {}
    for label, column in zip(first, column_indexes):
        if label is None or label in HEADER_IGNORED:
            continue
        name = HEADER_TO_FIELD.get(label)
        if name is None:
            continue
        hits.setdefault(name, []).append(column)

    fields = {name: columns[0] for name, columns in hits.items() if len(columns) == 1}
    dropped = sorted(name for name, columns in hits.items() if len(columns) > 1)
    if not fields:
        return ColumnMap(
            established=False,
            reason="表头没有读出任何能唯一对上契约字段的列。",
        )
    reason = ""
    if dropped:
        reason = (
            f"这些字段命中了不止一列，已丢弃不映射：{'、'.join(dropped)}。"
            "两列里哪一列才是它，猜错就是裁错格子。"
        )
    result = ColumnMap(established=True, reason=reason, fields=fields)
    logger.info("column map established %s", result.summary())
    return result


__all__ = [
    "ColumnMap",
    "HEADER_IGNORED",
    "HEADER_TO_FIELD",
    "MIN_ANCHORED_ROWS",
    "MIN_ANCHOR_BANDS",
    "RowAnchor",
    "column_map_from",
    "row_anchor_from",
]
