"""标准化价格、周度与月度统计、来源比价和涨价提醒的接口。

本层只做三件事：解析请求、调用 Service、把结果或异常转成响应。没有业务判断，
也不碰 Session——事务边界在 Service（见 README 第 10 节）。

接口一览（前缀 `/api/prices`）：

| 方法   | 路径                                   | 说明                                        |
| ------ | -------------------------------------- | ------------------------------------------- |
| GET    | /api/prices/units                      | 支持的单位、量纲与两条拒绝规则（静态）      |
| GET    | /api/prices/food-items/<id>/history    | 采购历史 + 每行的标准化价格或算不出的原因   |
| GET    | /api/prices/food-items/<id>/monthly    | 月度加权均价序列 + 本期/基期比较            |
| GET    | /api/prices/food-items/<id>/weekly     | 周度加权均价序列 + 本周/上周同期比较        |
| GET    | /api/prices/food-items/<id>/sources    | 同一食材的来源比价                          |
| GET    | /api/prices/alerts                     | 涨得超过阈值的食材                          |
| GET    | /api/prices/alert-settings             | 提醒阈值与开关                              |
| PUT    | /api/prices/alert-settings             | 改阈值 / 开关                               |
| GET    | /api/prices/pack-specs                 | 箱规列表                                    |
| POST   | /api/prices/pack-specs                 | 新增或更新一条箱规（同一个键是更新）        |
| DELETE | /api/prices/pack-specs/<id>            | 删一条箱规                                  |
| GET    | /api/prices/pack-specs/pending         | 待补箱规清单（按涉及金额排序）              |

几处与别的模块不同的约定：

1. **数值一律字符串**（沿用步骤 04）。标准化单价最多 4 位小数，百分比 2 位。
   算不出来的值是 `null`，**不是 0**。
2. **统计类接口在多币种时报 400** 而不是挑一种：本轮不做汇率换算，把 USD 与 CNY
   相加得到的数没有意义却长得像均价。响应与错误 `details` 都带
   `available_currencies`，前端据此让人选一个。
3. **`POST /api/prices/pack-specs` 是 upsert**。键是（食材, 来源, 包装单位）：
   同一个键再提交一次表达的是"这个箱规填错了/变了"。返回 201（新建）或 200（更新），
   并带上 `affected_purchase_count`——⚠️ 改箱规会让**历史**均价跟着变，这个数字
   是让那件事可见的最低成本做法。
4. **本模块只读采购记录，不写**。标准化价格是派生值；采购记录的数量、单位与金额
   要改请走 `PATCH /api/purchases/<id>`，那条路有修改日志。
"""

from __future__ import annotations

from flask import Blueprint, Response, current_app, jsonify, request

from src.api.schemas import (
    alert_settings_payload,
    alerts_payload,
    json_object,
    monthly_prices_payload,
    weekly_prices_payload,
    pack_spec_page_payload,
    pack_spec_payload,
    pending_pack_spec_payload,
    price_history_payload,
    query_int,
    query_text,
    reject_unknown_fields,
    require_present,
    source_comparison_payload,
)
from src.factory import get_database
from src.services.ingredients import DEFAULT_LIMIT, MAX_LIMIT
from src.services.prices import DEFAULT_MONTHS, DEFAULT_WEEKS, PriceService

prices_bp = Blueprint("prices", __name__, url_prefix="/api/prices")

PACK_SPEC_FIELDS = (
    "food_item_id",
    "source_name",
    "purchase_unit",
    "pack_size",
    "pack_unit",
    "spec_text",
    "note",
)
# `source_name` 不必填：留空表示「不限来源」，手工录入时常常没有来源名。
PACK_SPEC_REQUIRED_FIELDS = (
    "food_item_id",
    "purchase_unit",
    "pack_size",
    "pack_unit",
)
ALERT_SETTINGS_FIELDS = ("enabled", "threshold_percent")


def service() -> PriceService:
    """按请求创建 Service。依赖来自应用工厂注入的 `Database`，不读模块级全局量。"""
    return PriceService(get_database(current_app))


@prices_bp.get("/units")
def list_units() -> tuple[Response, int]:
    """支持的单位与规则。静态数据，不查库——前端据此列出可选的基础单位与箱规单位。"""
    return jsonify(PriceService.units_catalog()), 200


@prices_bp.get("/food-items/<int:food_item_id>/history")
def price_history(food_item_id: int) -> tuple[Response, int]:
    page = service().price_history(
        food_item_id,
        currency=query_text(request.args, "currency"),
        limit=query_int(request.args, "limit", DEFAULT_LIMIT),
        offset=query_int(request.args, "offset", 0),
    )
    return jsonify(price_history_payload(page)), 200


@prices_bp.get("/food-items/<int:food_item_id>/monthly")
def monthly_prices(food_item_id: int) -> tuple[Response, int]:
    report = service().monthly_prices(
        food_item_id,
        currency=query_text(request.args, "currency"),
        months=query_int(request.args, "months", DEFAULT_MONTHS),
    )
    return jsonify(monthly_prices_payload(report)), 200


@prices_bp.get("/food-items/<int:food_item_id>/weekly")
def weekly_prices(food_item_id: int) -> tuple[Response, int]:
    report = service().weekly_prices(
        food_item_id,
        currency=query_text(request.args, "currency"),
        weeks=query_int(request.args, "weeks", DEFAULT_WEEKS),
    )
    return jsonify(weekly_prices_payload(report)), 200


@prices_bp.get("/food-items/<int:food_item_id>/sources")
def source_comparison(food_item_id: int) -> tuple[Response, int]:
    report = service().source_comparison(
        food_item_id,
        currency=query_text(request.args, "currency"),
        date_from=query_text(request.args, "date_from"),
        date_to=query_text(request.args, "date_to"),
        period=query_text(request.args, "period") or "month",
    )
    return jsonify(source_comparison_payload(report)), 200


@prices_bp.get("/alerts")
def alerts() -> tuple[Response, int]:
    """涨价提醒。`threshold` 可临时覆盖设置里的阈值，**不写库**。"""
    report = service().alerts(
        threshold_percent=query_text(request.args, "threshold"),
        limit=query_int(request.args, "limit", MAX_LIMIT),
        period=query_text(request.args, "period") or "month",
    )
    return jsonify(alerts_payload(report)), 200


@prices_bp.get("/alert-settings")
def get_alert_settings() -> tuple[Response, int]:
    return jsonify(alert_settings_payload(service().get_alert_settings())), 200


@prices_bp.put("/alert-settings")
def update_alert_settings() -> tuple[Response, int]:
    payload = json_object(request)
    reject_unknown_fields(payload, ALERT_SETTINGS_FIELDS)
    view = service().update_alert_settings(
        enabled=payload.get("enabled"),
        threshold_percent=payload.get("threshold_percent"),
    )
    return jsonify(alert_settings_payload(view)), 200


@prices_bp.get("/pack-specs")
def list_pack_specs() -> tuple[Response, int]:
    page = service().list_pack_specs(
        food_item_id=query_int(request.args, "food_item_id"),
        limit=query_int(request.args, "limit", DEFAULT_LIMIT),
        offset=query_int(request.args, "offset", 0),
    )
    return jsonify(pack_spec_page_payload(page)), 200


@prices_bp.get("/pack-specs/pending")
def pending_pack_specs() -> tuple[Response, int]:
    """待补箱规清单：按包装单位计价、但还算不出标准化价格的那些采购记录。

    按「涉及金额」从大到小排——先补金额大的那组，老板的时间花在影响最大的地方。
    每组可能带一个 `suggestion`（从原始商品描述里读出来的候选），**必须人确认**。
    """
    rows, total = service().pending_pack_specs(
        limit=query_int(request.args, "limit", DEFAULT_LIMIT)
    )
    return (
        jsonify(
            {
                "items": [pending_pack_spec_payload(row) for row in rows],
                "total": total,
            }
        ),
        200,
    )


@prices_bp.post("/pack-specs")
def save_pack_spec() -> tuple[Response, int]:
    """新增或更新一条箱规。

    同一个（食材, 来源, 包装单位）再提交一次是**更新**而不是 409：那表达的是
    "这个箱规我填错了/变了"。⚠️ 更新会让**历史**标准化价格跟着变（口径与步骤 04
    的「统计按当前值算」一致），所以响应里带 `affected_purchase_count`。
    """
    payload = json_object(request)
    reject_unknown_fields(payload, PACK_SPEC_FIELDS)
    require_present(payload, PACK_SPEC_REQUIRED_FIELDS)
    view, created, affected = service().save_pack_spec(
        food_item_id=payload.get("food_item_id"),
        source_name=payload.get("source_name"),
        purchase_unit=payload.get("purchase_unit"),
        pack_size=payload.get("pack_size"),
        pack_unit=payload.get("pack_unit"),
        spec_text=payload.get("spec_text"),
        note=payload.get("note"),
    )
    body = pack_spec_payload(view)
    body["created"] = created
    body["affected_purchase_count"] = affected
    body["notice"] = (
        f"这条箱规影响 {affected} 条采购记录的标准化价格（含历史记录）。"
        "原始数量、单位与金额没有被改动——标准化价格是查询时算出来的派生值。"
    )
    return jsonify(body), (201 if created else 200)


@prices_bp.delete("/pack-specs/<int:pack_spec_id>")
def delete_pack_spec(pack_spec_id: int) -> tuple[Response, int]:
    """删一条箱规。

    硬删：箱规是配置而不是业务事实，删掉只是让这家这种货重新变成"算不出标准化
    价格"，采购记录一条都不动。
    """
    affected = service().delete_pack_spec(pack_spec_id)
    return (
        jsonify(
            {
                "status": "deleted",
                "affected_purchase_count": affected,
                "message": (
                    f"箱规已删除；{affected} 条采购记录因此重新变成「待补箱规」，"
                    "采购记录本身没有改动。"
                ),
            }
        ),
        200,
    )
