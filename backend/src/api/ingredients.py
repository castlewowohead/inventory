"""食材与分类接口（步骤 03）。

本层只做三件事：解析请求、调用 Service、把结果或异常转成响应。没有业务判断，
也不碰 Session——事务边界在 Service（见 README 第 10 节）。

接口一览（前缀 `/api`）：

| 方法   | 路径                     | 说明                                    |
| ------ | ------------------------ | --------------------------------------- |
| GET    | /api/categories          | 列出分类                                |
| POST   | /api/categories          | 新增分类                                |
| PATCH  | /api/categories/<id>     | 修改分类名称 / 中文名                   |
| DELETE | /api/categories/<id>     | 删除分类（被食材引用时 409）            |
| GET    | /api/food-items          | 搜索 + 分类筛选 + 分页（步骤 10 加 `never_purchased`） |
| GET    | /api/food-items/suggest  | 候选建议：模糊匹配 / 最近 / 常用（步骤 10） |
| POST   | /api/food-items          | 快速新增（必填 name/category_id/base_unit） |
| GET    | /api/food-items/<id>     | 单条详情                                |
| PATCH  | /api/food-items/<id>     | 局部修改                                |
| DELETE | /api/food-items/<id>     | 删除（被采购/发票引用时 409）           |
| POST   | /api/food-items/<id>/merge-preview | 合并预览：会搬动什么、什么拦着（只读，步骤 10.3） |
| POST   | /api/food-items/<id>/merge | 执行合并（一次事务；**不可逆**）      |

`<id>` 用 Flask 的 `<int:...>` 转换器：非整数的路径由路由层直接给 404，不会带着
脏数据进入 Service。`/suggest` 因此不会被 `<int:food_item_id>` 抢走。

⚠️ **`/api/food-items` 与 `/api/food-items/suggest` 刻意是两个接口**，不是一个
接口加开关。前者是食材库那一页（分页、稳定排序、找确定的某一条然后改它或删它），
后者是选食材那一屏（容忍缩写、带命中理由、不分页）。理由写在
`src/services/suggestions.py` 的模块文档里。
"""

from __future__ import annotations

from flask import Blueprint, Response, current_app, jsonify, request

from src.api.schemas import (
    category_payload,
    field,
    food_item_page_payload,
    food_item_payload,
    food_item_suggestions_payload,
    json_object,
    merge_plan_payload,
    query_bool,
    query_int,
    query_text,
    reject_unknown_fields,
    require_non_empty,
    require_present,
)
from src.factory import get_database
from src.services.ingredients import DEFAULT_LIMIT, IngredientService
from src.services.merging import MergeService
from src.services.suggestions import DEFAULT_SUGGEST_LIMIT, SuggestionService

ingredients_bp = Blueprint("ingredients", __name__, url_prefix="/api")

CATEGORY_FIELDS = ("name", "name_zh")
FOOD_ITEM_FIELDS = ("name", "name_zh", "specification", "base_unit", "category_id")
FOOD_ITEM_REQUIRED_FIELDS = ("name", "category_id", "base_unit")
MERGE_FIELDS = ("source_ids",)


def service() -> IngredientService:
    """按请求创建 Service。依赖来自应用工厂注入的 `Database`，不读模块级全局量。"""
    return IngredientService(get_database(current_app))


def merges() -> MergeService:
    """步骤 10.3 的合并服务。同样与 `service()` 分开：合并要判断两个基础单位是不是
    同一类计量，那是 `services/units.py` 的事，而它 import 词表模块。"""
    return MergeService(get_database(current_app))


def suggestions() -> SuggestionService:
    """步骤 10 的建议服务。与 `service()` 分开是因为依赖方向：建议依赖词表，
    词表不依赖建议（见 `src/services/suggestions.py`）。"""
    return SuggestionService(get_database(current_app))


# --- 分类 -------------------------------------------------------------------


@ingredients_bp.get("/categories")
def list_categories() -> tuple[Response, int]:
    views = service().list_categories()
    return jsonify({"items": [category_payload(view) for view in views]}), 200


@ingredients_bp.post("/categories")
def create_category() -> tuple[Response, int]:
    payload = json_object(request)
    reject_unknown_fields(payload, CATEGORY_FIELDS)
    require_present(payload, ("name",))
    view = service().create_category(
        name=payload.get("name"), name_zh=payload.get("name_zh")
    )
    return jsonify(category_payload(view)), 201


@ingredients_bp.patch("/categories/<int:category_id>")
def update_category(category_id: int) -> tuple[Response, int]:
    payload = json_object(request)
    reject_unknown_fields(payload, CATEGORY_FIELDS)
    require_non_empty(payload)
    view = service().update_category(
        category_id,
        name=field(payload, "name"),
        name_zh=field(payload, "name_zh"),
    )
    return jsonify(category_payload(view)), 200


@ingredients_bp.delete("/categories/<int:category_id>")
def delete_category(category_id: int) -> tuple[str, int]:
    service().delete_category(category_id)
    return "", 204


# --- 食材 -------------------------------------------------------------------


@ingredients_bp.get("/food-items")
def search_food_items() -> tuple[Response, int]:
    page = service().search_food_items(
        query=query_text(request.args, "q"),
        category_id=query_int(request.args, "category_id"),
        never_purchased=query_bool(request.args, "never_purchased"),
        limit=query_int(request.args, "limit", DEFAULT_LIMIT),
        offset=query_int(request.args, "offset", 0),
    )
    return jsonify(food_item_page_payload(page)), 200


@ingredients_bp.get("/food-items/suggest")
def suggest_food_items() -> tuple[Response, int]:
    """候选建议（步骤 10.1 / 10.2）。带 `q` 做四档名称匹配，不带就给最近 + 常用。"""
    result = suggestions().suggest_food_items(
        query=query_text(request.args, "q"),
        category_id=query_int(request.args, "category_id"),
        limit=query_int(request.args, "limit", DEFAULT_SUGGEST_LIMIT),
    )
    return jsonify(food_item_suggestions_payload(result)), 200


@ingredients_bp.post("/food-items")
def create_food_item() -> tuple[Response, int]:
    payload = json_object(request)
    reject_unknown_fields(payload, FOOD_ITEM_FIELDS)
    require_present(payload, FOOD_ITEM_REQUIRED_FIELDS)
    view = service().create_food_item(
        name=payload.get("name"),
        category_id=payload.get("category_id"),
        base_unit=payload.get("base_unit"),
        name_zh=payload.get("name_zh"),
        specification=payload.get("specification"),
    )
    return jsonify(food_item_payload(view)), 201


@ingredients_bp.get("/food-items/<int:food_item_id>")
def get_food_item(food_item_id: int) -> tuple[Response, int]:
    return jsonify(food_item_payload(service().get_food_item(food_item_id))), 200


@ingredients_bp.patch("/food-items/<int:food_item_id>")
def update_food_item(food_item_id: int) -> tuple[Response, int]:
    payload = json_object(request)
    reject_unknown_fields(payload, FOOD_ITEM_FIELDS)
    require_non_empty(payload)
    view = service().update_food_item(
        food_item_id,
        name=field(payload, "name"),
        name_zh=field(payload, "name_zh"),
        specification=field(payload, "specification"),
        base_unit=field(payload, "base_unit"),
        category_id=field(payload, "category_id"),
    )
    return jsonify(food_item_payload(view)), 200


@ingredients_bp.delete("/food-items/<int:food_item_id>")
def delete_food_item(food_item_id: int) -> tuple[str, int]:
    service().delete_food_item(food_item_id)
    return "", 204


# --- 重复食材合并（步骤 10.3）----------------------------------------------
#
# 两个接口刻意都用 POST：预览虽然只读，但它的输入是一个**数组**，塞进查询字符串
# 既难读也容易被截断。路径里的 `<id>` 是**保留下来的**那一条，body 里的
# `source_ids` 是被并掉的那些——方向写进 URL，不靠 body 里两个长得一样的字段
# 来区分谁吞并谁。


@ingredients_bp.post("/food-items/<int:food_item_id>/merge-preview")
def preview_merge(food_item_id: int) -> tuple[Response, int]:
    payload = json_object(request)
    reject_unknown_fields(payload, MERGE_FIELDS)
    require_present(payload, MERGE_FIELDS)
    plan = merges().preview(food_item_id, payload.get("source_ids"))
    return jsonify(merge_plan_payload(plan)), 200


@ingredients_bp.post("/food-items/<int:food_item_id>/merge")
def merge_food_items(food_item_id: int) -> tuple[Response, int]:
    payload = json_object(request)
    reject_unknown_fields(payload, MERGE_FIELDS)
    require_present(payload, MERGE_FIELDS)
    plan = merges().merge(food_item_id, payload.get("source_ids"))
    return jsonify(merge_plan_payload(plan)), 200
