"""提取相关接口的测试（步骤 08）。

用默认的 `OCR_PROVIDER=fake` 走通接口形状；真实识别的行为由
`tests/test_extraction_service.py`（桩件）与 `tools/e2e_extraction.py`（真调）覆盖。
"""

from __future__ import annotations

import io

import pytest
from flask.testing import FlaskClient
from PIL import Image
from sqlalchemy.exc import IntegrityError

from src.db import Database
from src.models import ExtractionJob
from src.repositories.invoices import InvoiceFileRepository


def _upload(client: FlaskClient) -> dict:
    response = client.post(
        "/api/invoices",
        data={"files": (io.BytesIO(b"fake-invoice-bytes"), "invoice.jpg")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def test_extraction_jobs_are_queryable_after_upload(migrated_client: FlaskClient) -> None:
    """⚠️ 08.5：提取状态持久化，关掉浏览器回来还查得到。"""
    invoice = _upload(migrated_client)

    response = migrated_client.get(f"/api/invoices/{invoice['id']}/extraction")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total"] >= 1
    job = payload["items"][0]
    assert job["invoice_id"] == invoice["id"]
    assert job["status"] in ("queued", "running", "succeeded", "failed")
    # 事后要能查出"这张单是用哪个被测配置提取的"。
    assert "prompt_sha12" in job
    assert "models" in job
    assert "warnings" in job


def test_extraction_can_be_restarted(migrated_client: FlaskClient) -> None:
    invoice = _upload(migrated_client)

    response = migrated_client.post(f"/api/invoices/{invoice['id']}/extraction")

    assert response.status_code == 202
    assert response.get_json()["total"] >= 1


def test_restarting_is_refused_once_a_line_is_confirmed(
    migrated_client: FlaskClient,
) -> None:
    """⚠️ 重新提取会替换草稿行，和已入账的行对不上——而那种错位不报错。"""
    invoice = _upload(migrated_client)
    created = migrated_client.post(
        "/api/food-items",
        json={"name": "Rice", "category_id": 1, "base_unit": "lb"},
    ).get_json()
    line_id = invoice["lines"][0]["id"]
    confirmed = migrated_client.post(
        f"/api/invoices/{invoice['id']}/confirm",
        json={"lines": [{"line_id": line_id, "food_item_id": created["id"]}]},
    )
    assert confirmed.status_code == 200

    response = migrated_client.post(f"/api/invoices/{invoice['id']}/extraction")

    assert response.status_code == 409
    assert "撤销确认" in response.get_json()["error"]["message"]


def test_extraction_of_an_unknown_invoice_is_404(migrated_client: FlaskClient) -> None:
    assert migrated_client.get("/api/invoices/9999/extraction").status_code == 404


def test_supplier_routes_can_be_listed_and_set(migrated_client: FlaskClient) -> None:
    """按供应商路由是分流的主依据；`date_order` 让日/月顺序每家说一次。"""
    assert migrated_client.get("/api/ocr-routes").get_json()["total"] == 0

    response = migrated_client.put(
        "/api/ocr-routes",
        json={
            "source_name": "Metro Prime",
            "doc_class": "printed_scan",
            "date_order": "mdy",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["doc_class"] == "printed_scan"
    assert payload["date_order"] == "mdy"
    assert payload["decided_from"] == "manual"
    assert payload["source_name_normalized"] == "metro prime"

    listing = migrated_client.get("/api/ocr-routes").get_json()
    assert listing["total"] == 1


def test_an_invalid_route_is_refused(migrated_client: FlaskClient) -> None:
    response = migrated_client.put(
        "/api/ocr-routes", json={"source_name": "X", "doc_class": "whatever"}
    )

    assert response.status_code == 400


def test_an_unknown_route_field_is_refused(migrated_client: FlaskClient) -> None:
    response = migrated_client.put(
        "/api/ocr-routes",
        json={"source_name": "X", "doc_class": "printed_scan", "nope": 1},
    )

    assert response.status_code == 400


def test_supplier_candidates_fall_back_to_the_invoice_source(
    migrated_client: FlaskClient,
) -> None:
    """没带 `?source=` 时取单据头上的来源；单据头也没有才报错。"""
    invoice = _upload(migrated_client)

    response = migrated_client.get(
        f"/api/invoices/{invoice['id']}/supplier-candidates"
    )

    # Fake 识别器会填一个带 FAKE 标记的供应商名，所以这里取得到来源；
    # 这家没有历史采购，候选自然是空的。
    assert response.status_code == 200
    assert response.get_json()["candidates"] == []


def test_supplier_candidates_list_what_this_supplier_bought(
    migrated_client: FlaskClient,
) -> None:
    """⚠️ 08.3i：商品名不走 OCR——裁图给老板看，让他从这张清单里选。

    05.6 那条「按原始描述精确命中」在手写件上用不了：中文商品名跨模型分歧率
    96.0%，模型给的名字几乎一定是错的。
    """
    food = migrated_client.post(
        "/api/food-items",
        json={"name": "Beef Tripe", "category_id": 1, "base_unit": "lb"},
    ).get_json()
    migrated_client.post(
        "/api/purchases",
        json={
            "food_item_id": food["id"],
            "source_name": "Tang Fa",
            "original_description": "牛柏叶",
            "quantity": "10",
            "purchase_unit": "lb",
            "unit_price": "2.00",
            "line_amount": "20.00",
            "purchase_date": "2026-11-01",
        },
    )
    invoice = _upload(migrated_client)

    response = migrated_client.get(
        f"/api/invoices/{invoice['id']}/supplier-candidates?source=Tang%20Fa"
    )

    assert response.status_code == 200
    candidates = response.get_json()["candidates"]
    assert [item["food_item"]["name"] for item in candidates] == ["Beef Tripe"]
    assert candidates[0]["same_source"] is True


# --- 删文件与提取任务的交界 -------------------------------------------------
#
# 这一段守着一条真实缺陷：`extraction_jobs.invoice_file_id` 上有外键，而步骤 05 的
# 删文件没管它——于是「删掉拍坏的那一张」在那张照片已经跑过提取之后直接 500
# （`sqlite3.IntegrityError: FOREIGN KEY constraint failed`）。原来的用例一直没抓到
# 它，因为它们删的都是 `POST /files` 补传上来的文件，而补传**不触发提取**，
# 那种文件身上根本没有任务。


def _upload_two(client: FlaskClient) -> dict:
    """一次传两张照片：两张都会各自生成一条提取任务。

    必须是两张——单据不许删到一个不剩，只传一张就没法删。
    """
    response = client.post(
        "/api/invoices",
        data={
            "files": [
                (io.BytesIO(b"fake-invoice-page-1"), "page1.jpg"),
                (io.BytesIO(b"fake-invoice-page-2"), "page2.jpg"),
            ]
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def _jobs(client: FlaskClient, invoice_id: int) -> list[dict]:
    response = client.get(f"/api/invoices/{invoice_id}/extraction")
    assert response.status_code == 200, response.get_json()
    return response.get_json()["items"]


def _set_job_status(database: Database, job_id: int, status: str) -> None:
    """把一条任务的状态改回「没跑完」，用来造出「正在识别」这个局面。

    Fake 识别器是当场跑完的（`_runs_inline`），所以接口测试里拿不到一条
    `queued` 任务——而那恰好是最要紧的那种局面：删掉正在识别的文件。
    """
    with database.unit_of_work() as session:
        job = session.get(ExtractionJob, job_id)
        assert job is not None
        job.status = status


def test_deleting_a_recognized_photo_keeps_its_extraction_history(
    migrated_client: FlaskClient,
) -> None:
    """⚠️ 这张照片跑过提取，删它不该 500，任务历史也不该跟着消失。

    历史任务是事后唯一能解释「这张单当初怎么没出行」的东西，所以删文件只解开
    关联、不删任务行；文件的编号与名字写进 `warnings`，否则界面上会多出一条
    既没有文件也没有理由的任务。
    """
    invoice = _upload_two(migrated_client)
    second = invoice["files"][1]
    before = _jobs(migrated_client, invoice["id"])
    assert sorted(job["invoice_file_id"] for job in before) == sorted(
        item["id"] for item in invoice["files"]
    )
    job_id = next(job["id"] for job in before if job["invoice_file_id"] == second["id"])

    deleted = migrated_client.delete(
        f"/api/invoices/{invoice['id']}/files/{second['id']}"
    )

    assert deleted.status_code == 200, deleted.get_json()
    assert [item["id"] for item in deleted.get_json()["files"]] == [
        invoice["files"][0]["id"]
    ]
    after = _jobs(migrated_client, invoice["id"])
    assert len(after) == len(before)
    released = next(job for job in after if job["id"] == job_id)
    assert released["invoice_file_id"] is None
    assert released["status"] == "succeeded"
    assert any("page2.jpg" in warning for warning in released["warnings"])
    # 别的任务不受影响：删一张照片只影响它自己那一条。
    other = next(job for job in after if job["id"] != job_id)
    assert other["invoice_file_id"] == invoice["files"][0]["id"]
    # 原件确实没了。
    assert (
        migrated_client.get(
            f"/api/invoices/{invoice['id']}/files/{second['id']}/content"
        ).status_code
        == 404
    )


def test_deleting_a_photo_is_refused_while_it_is_still_being_recognized(
    migrated_client: FlaskClient, migrated_database: Database
) -> None:
    """⚠️ 正在识别的文件不许删：worker 会按它的 storage_key 去读字节。

    删掉只会换来一条看不懂的失败，而那次模型调用的钱照样要花。所以这里回 409
    并说清楚等什么，而不是让它跑成一条 500 或一次白花的调用。
    """
    invoice = _upload_two(migrated_client)
    second = invoice["files"][1]
    job_id = next(
        job["id"]
        for job in _jobs(migrated_client, invoice["id"])
        if job["invoice_file_id"] == second["id"]
    )
    _set_job_status(migrated_database, job_id, "queued")

    response = migrated_client.delete(
        f"/api/invoices/{invoice['id']}/files/{second['id']}"
    )

    assert response.status_code == 409
    error = response.get_json()["error"]
    assert error["details"]["extraction_job_id"] == job_id
    assert error["details"]["status"] == "queued"
    assert "排队" in error["message"]
    # 文件与字节都还在：被拦住的删除不该留下半个结果。
    assert (
        migrated_client.get(
            f"/api/invoices/{invoice['id']}/files/{second['id']}/content"
        ).status_code
        == 200
    )
    assert len(migrated_client.get(f"/api/invoices/{invoice['id']}").get_json()["files"]) == 2


def test_deleting_a_photo_whose_extraction_failed_is_allowed(
    migrated_client: FlaskClient, migrated_database: Database
) -> None:
    """跑失败了的那条任务不拦着删——失败的照片正是最该重拍的那一张。"""
    invoice = _upload_two(migrated_client)
    second = invoice["files"][1]
    job_id = next(
        job["id"]
        for job in _jobs(migrated_client, invoice["id"])
        if job["invoice_file_id"] == second["id"]
    )
    _set_job_status(migrated_database, job_id, "failed")

    response = migrated_client.delete(
        f"/api/invoices/{invoice['id']}/files/{second['id']}"
    )

    assert response.status_code == 200, response.get_json()
    released = next(
        job for job in _jobs(migrated_client, invoice["id"]) if job["id"] == job_id
    )
    assert released["invoice_file_id"] is None
    assert released["status"] == "failed"


def test_the_job_foreign_key_stays_strict(
    migrated_client: FlaskClient, migrated_database: Database
) -> None:
    """⚠️ 这个外键是**刻意**保持严格的（没有 `ON DELETE SET NULL`）。

    数据库那一层分不清「排队中」和「已跑完」，一律置空等于把一次正在花钱的识别
    悄悄变成一条指向空气的记录。所以「能不能删」由 Service 判断，外键只当兜底——
    这一条就是在证明兜底还在：少了 Service 那一步，删文件会被数据库拦住。
    （删文件当初那条 500 就是这么来的。）
    """
    invoice = _upload_two(migrated_client)
    file_id = invoice["files"][1]["id"]

    with pytest.raises(IntegrityError):
        with migrated_database.unit_of_work() as session:
            record = InvoiceFileRepository(session).get(file_id)
            assert record is not None
            InvoiceFileRepository(session).delete(record)

    # 拦住之后什么都没变。
    assert (
        migrated_client.get(
            f"/api/invoices/{invoice['id']}/files/{file_id}/content"
        ).status_code
        == 200
    )
