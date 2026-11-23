"""采购记录接口（步骤 04）。

本层只做三件事：解析请求、调用 Service、把结果或异常转成响应。没有业务判断，
也不碰 Session——事务边界在 Service（见 README 第 10 节）。

接口一览（前缀 `/api`）：

| 方法   | 路径                             | 说明                                       |
| ------ | -------------------------------- | ------------------------------------------ |
| GET    | /api/purchases                   | 按食材 / 来源 / 日期区间查询 + 分页        |
| POST   | /api/purchases                   | 手工录入一条采购                           |
| GET    | /api/purchases/<id>              | 单条详情                                   |
| PATCH  | /api/purchases/<id>              | 直接修改（含数值），自动记修改日志         |
| DELETE | /api/purchases/<id>              | 作废（软删，不物理删除）                   |
| GET    | /api/purchases/<id>/revisions    | 修改历史：每次改动前后的值                 |

两处与步骤 03 不同的约定：

1. **数值一律用字符串传递。** 响应里的 `quantity` / `unit_price` / `line_amount`
   是字符串，请求也应该用字符串；传 JSON 小数会被明确拒绝（见
   `services/purchases.py` 的 `parse_decimal`）。整数可以直接传。
2. **`DELETE` 是作废而不是删除**，所以返回 200 加一句说明，而不是 204：它改变了
   记录的状态（`voided_at`），记录本身还在，`include_voided=true` 能查到。

`POST` / `PATCH` 都接受 `accept_amount_mismatch`：数量 × 单价 与行金额差额超过
$0.05 时，第一次提交会收到 `amount_mismatch` 错误；核对单据后带上这个标记重发，
那一行按行金额入账并标记为金额异常。

**调整行（退货 / 折让）**：`POST` 时传 `line_kind: "adjustment"`，该行的数量与金额
可以为负，`food_item_id` 也可以不填（整单折让）。`GET /api/purchases` **默认不返回**
这类行，要 `kind=adjustment` 或 `kind=all`；**对账请用 `kind=all`**，否则各行相加
对不上单据总额。`line_kind` 不能用 `PATCH` 改——录错了类别请作废重录。
"""

from __future__ import annotations

from flask import Blueprint, Response, current_app, jsonify, request

from src.api.schemas import (
    field,
    json_object,
    purchase_page_payload,
    purchase_payload,
    purchase_revisions_payload,
    query_bool,
    query_int,
    query_kind,
    query_text,
    reject_unknown_fields,
    require_non_empty,
    require_present,
)
from src.factory import get_database
from src.services.purchases import (
    DEFAULT_LIMIT,
    LINE_KIND_PURCHASE,
    PurchaseService,
)

purchases_bp = Blueprint("purchases", __name__, url_prefix="/api")

# `accept_amount_mismatch` 与 `reason` 不是采购记录的字段：前者是「我核对过单据」
# 的确认，后者进修改日志。放在同一个请求体里，所以要一起列进白名单。
PURCHASE_FIELDS = (
    "line_kind",
    "food_item_id",
    "purchase_date",
    "source_name",
    "original_description",
    "quantity",
    "purchase_unit",
    "unit_price",
    "line_amount",
    "currency",
    "accept_amount_mismatch",
)
# `food_item_id` 不在必填里：调整行可以不指向食材（整单折让）。正常采购行必须有，
# 那条规则由 Service 判断（它才知道 line_kind）。
PURCHASE_REQUIRED_FIELDS = (
    "quantity",
    "purchase_unit",
    "unit_price",
    "line_amount",
)
# 修改时不能改 `line_kind`：录错了类别属于录错了行，作废重录比原地改类别清楚。
PURCHASE_PATCH_FIELDS = tuple(
    name for name in PURCHASE_FIELDS if name != "line_kind"
) + ("reason",)
VOID_FIELDS = ("reason",)


def service() -> PurchaseService:
    """按请求创建 Service。依赖来自应用工厂注入的 `Database`，不读模块级全局量。"""
    return PurchaseService(get_database(current_app))


def _flag(payload: dict, key: str) -> bool:
    """取布尔标记。缺省为 False；写成字符串或数字时明确拒绝，不做宽松解释。"""
    from src.errors import ValidationError

    value = payload.get(key, False)
    if not isinstance(value, bool):
        raise ValidationError(
            f"字段 {key} 必须是 true 或 false。", details={"field": key}
        )
    return value


@purchases_bp.get("/purchases")
def search_purchases() -> tuple[Response, int]:
    page = service().search_purchases(
        food_item_id=query_int(request.args, "food_item_id"),
        source=query_text(request.args, "source"),
        date_from=query_text(request.args, "date_from"),
        date_to=query_text(request.args, "date_to"),
        undated=query_bool(request.args, "undated"),
        kind=query_kind(request.args),
        include_voided=bool(query_bool(request.args, "include_voided")),
        limit=query_int(request.args, "limit", DEFAULT_LIMIT),
        offset=query_int(request.args, "offset", 0),
    )
    return jsonify(purchase_page_payload(page)), 200


@purchases_bp.post("/purchases")
def create_purchase() -> tuple[Response, int]:
    payload = json_object(request)
    reject_unknown_fields(payload, PURCHASE_FIELDS)
    require_present(payload, PURCHASE_REQUIRED_FIELDS)
    view = service().create_purchase(
        line_kind=payload.get("line_kind", LINE_KIND_PURCHASE),
        food_item_id=payload.get("food_item_id"),
        quantity=payload.get("quantity"),
        purchase_unit=payload.get("purchase_unit"),
        unit_price=payload.get("unit_price"),
        line_amount=payload.get("line_amount"),
        purchase_date=payload.get("purchase_date"),
        source_name=payload.get("source_name"),
        original_description=payload.get("original_description"),
        currency=payload.get("currency", "USD"),
        accept_amount_mismatch=_flag(payload, "accept_amount_mismatch"),
    )
    return jsonify(purchase_payload(view)), 201


@purchases_bp.get("/purchases/<int:purchase_id>")
def get_purchase(purchase_id: int) -> tuple[Response, int]:
    return jsonify(purchase_payload(service().get_purchase(purchase_id))), 200


@purchases_bp.patch("/purchases/<int:purchase_id>")
def update_purchase(purchase_id: int) -> tuple[Response, int]:
    payload = json_object(request)
    reject_unknown_fields(payload, PURCHASE_PATCH_FIELDS)
    require_non_empty(payload)
    view = service().update_purchase(
        purchase_id,
        food_item_id=field(payload, "food_item_id"),
        purchase_date=field(payload, "purchase_date"),
        source_name=field(payload, "source_name"),
        original_description=field(payload, "original_description"),
        quantity=field(payload, "quantity"),
        purchase_unit=field(payload, "purchase_unit"),
        unit_price=field(payload, "unit_price"),
        line_amount=field(payload, "line_amount"),
        currency=field(payload, "currency"),
        reason=payload.get("reason"),
        accept_amount_mismatch=_flag(payload, "accept_amount_mismatch"),
    )
    return jsonify(purchase_payload(view)), 200


@purchases_bp.delete("/purchases/<int:purchase_id>")
def void_purchase(purchase_id: int) -> tuple[Response, int]:
    """作废而不是删除。记录仍在，`include_voided=true` 能查到，发票可追溯。"""
    payload = request.get_json(silent=True) or {}
    if payload:
        reject_unknown_fields(payload, VOID_FIELDS)
    service().void_purchase(purchase_id, reason=payload.get("reason"))
    return (
        jsonify(
            {
                "status": "voided",
                "message": "采购记录已撤销，修改历史仍会保留。",
            }
        ),
        200,
    )


@purchases_bp.get("/purchases/<int:purchase_id>/revisions")
def list_revisions(purchase_id: int) -> tuple[Response, int]:
    views = service().list_revisions(purchase_id)
    return jsonify(purchase_revisions_payload(views)), 200
