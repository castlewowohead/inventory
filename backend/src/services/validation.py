"""业务层共用的输入校验（文本、枚举取值与分页）。

**为什么现在才抽出来。** `services/purchases.py` 的模块文档里写着：这几个函数与
`services/ingredients.py` 的同名方法重复，"刻意没有提前抽公共模块：等步骤 05 的发票
服务成为第三个调用方时再抽（rule of three），那时才看得清真正共用的是哪几条规则"。
步骤 05 到了，于是抽在这里——第三份复制粘贴才是真正该避免的那一份。

共用的规则只有这几条，刻意不做成通用校验框架：

- 文本：必须是字符串、折叠空白、去首尾、长度不超限；可选文本把空白与 None 都归成
  NULL（"填了几个空格"和"没填"在业务上是一回事）。
- 枚举取值：只认白名单里的字符串，错了就把可选值列给调用方。
- 分页：`limit` 有上限、`offset` 不为负，两者都必须是真正的整数（`bool` 是 `int`
  的子类，不挡住的话 `True` 会被当成 1）。

数值解析**不在这里**：它只有采购相关的代码用，而且规则多（scale、符号、JSON 小数、
整数位上限），留在 `services/purchases.py` 的 `parse_decimal()` 一处实现。
"""

from __future__ import annotations

from typing import Any, Iterable

from src.errors import ValidationError


def collapse_whitespace(value: str) -> str:
    """折叠空白：去首尾、把连续空白压成一个空格；大小写与其它字符原样保留。

    存进去的是展示用的文本，`Ground   Beef`（粘贴时常见的多空格）应该存成
    `Ground Beef`，而不是把排版问题一直带在数据里。用于唯一判定与搜索的规范化形式
    另见 `services/ingredients.py` 的 `normalize_name()`。
    """
    return " ".join(value.split())


def require_text(value: Any, *, field: str, max_length: int) -> str:
    """必填文本：必须是字符串、去空白后非空、长度不超限。返回折叠空白后的原写法。"""
    if not isinstance(value, str):
        raise ValidationError(f"字段 {field} 必须是字符串。", details={"field": field})
    cleaned = collapse_whitespace(value)
    if not cleaned:
        raise ValidationError(f"字段 {field} 不能为空。", details={"field": field})
    if len(cleaned) > max_length:
        raise ValidationError(
            f"字段 {field} 长度不能超过 {max_length} 个字符。",
            details={"field": field, "max_length": max_length},
        )
    return cleaned


def optional_text(value: Any, *, field: str, max_length: int) -> str | None:
    """可选文本：None 或全空白都表示「没有」，统一存成 NULL。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(
            f"字段 {field} 必须是字符串或 null。", details={"field": field}
        )
    cleaned = collapse_whitespace(value)
    if not cleaned:
        return None
    if len(cleaned) > max_length:
        raise ValidationError(
            f"字段 {field} 长度不能超过 {max_length} 个字符。",
            details={"field": field, "max_length": max_length},
        )
    return cleaned


def require_choice(
    value: Any, *, field: str, allowed: Iterable[str], case: str = "lower"
) -> str:
    """白名单取值。`case` 决定比较前怎么折叠大小写（币种用 upper，其余用 lower）。"""
    options = tuple(allowed)
    if not isinstance(value, str):
        raise ValidationError(f"字段 {field} 必须是字符串。", details={"field": field})
    stripped = value.strip()
    candidate = stripped.upper() if case == "upper" else stripped.lower()
    if candidate not in options:
        raise ValidationError(
            f"字段 {field} 只能是 {', '.join(options)} 之一（收到 {value!r}）。",
            details={"field": field, "allowed": list(options)},
        )
    return candidate


def require_limit(limit: Any, *, max_limit: int) -> int:
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValidationError("limit 必须是整数。", details={"field": "limit"})
    if not 1 <= limit <= max_limit:
        raise ValidationError(
            f"limit 必须在 1—{max_limit} 之间。",
            details={"field": "limit", "max": max_limit},
        )
    return limit


def require_offset(offset: Any) -> int:
    if not isinstance(offset, int) or isinstance(offset, bool):
        raise ValidationError("offset 必须是整数。", details={"field": "offset"})
    if offset < 0:
        raise ValidationError("offset 不能为负。", details={"field": "offset"})
    return offset
