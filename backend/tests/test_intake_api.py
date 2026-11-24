"""整理批次的 HTTP 边界（多文件计划 M03）。

验证的是**请求与响应**：一次一个文件、处理状态能不能轮询、页能不能回看、路径里的
两个 id 对不上会不会漏出别的批次的原件。业务规则本身在 `test_intake_ingest.py`。

样本同样是合成的（当场造的 PDF 与图片）；真实原件在被 Git 忽略的 `samples/real/`，
测试不依赖它存在。
"""

from __future__ import annotations

import io

from flask.testing import FlaskClient
from PIL import Image
from pypdf import PdfWriter

INTAKE = "/api/intake-batches"


def pdf_bytes(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (80, 60), (250, 250, 240)).save(buffer, format="PNG")
    return buffer.getvalue()


def new_batch(client: FlaskClient) -> int:
    response = client.post(INTAKE)
    assert response.status_code == 201
    return response.get_json()["id"]


def upload(client: FlaskClient, batch_id: int, name: str, content: bytes):
    return client.post(
        f"{INTAKE}/{batch_id}/files",
        data={"file": (io.BytesIO(content), name)},
        content_type="multipart/form-data",
    )


# --- 建批次与恢复 -----------------------------------------------------------


def test_creating_a_batch_returns_an_open_batch_with_no_files(
    migrated_client: FlaskClient,
) -> None:
    response = migrated_client.post(INTAKE)

    assert response.status_code == 201
    body = response.get_json()
    assert body["status"] == "open"
    assert body["files"] == []
    assert body["closed_at"] is None


def test_no_open_batch_is_a_404_not_an_empty_object(
    migrated_client: FlaskClient,
) -> None:
    """「没有在整理的批次」与「有一个空批次」不能长得一样。

    前端据此决定是"接着整理"还是"新建一批"；返回 `{}` 会让这两件事分不开。
    """
    missing = migrated_client.get(f"{INTAKE}/current")
    assert missing.status_code == 404

    batch_id = new_batch(migrated_client)
    assert migrated_client.get(f"{INTAKE}/current").status_code == 404
    upload(migrated_client, batch_id, "resume.pdf", pdf_bytes(1))
    found = migrated_client.get(f"{INTAKE}/current")
    assert found.status_code == 200
    assert found.get_json()["id"] == batch_id


def test_an_unknown_batch_is_a_404(migrated_client: FlaskClient) -> None:
    assert migrated_client.get(f"{INTAKE}/4242").status_code == 404


# --- 逐文件上传 -------------------------------------------------------------


def test_a_five_page_pdf_shows_up_as_five_viewable_page_refs(
    migrated_client: FlaskClient,
) -> None:
    """M03 验收第一条，走完整的 HTTP 通路。"""
    batch_id = new_batch(migrated_client)

    created = upload(migrated_client, batch_id, "five.pdf", pdf_bytes(5))

    assert created.status_code == 201
    body = created.get_json()
    assert body["queued"] is False  # 测试里没装后台调度器：当场切完
    assert body["reused_bytes"] is False
    assert body["ingest"]["outcome"] == "ingested"
    assert body["ingest"]["page_count"] == 5
    assert body["ingest"]["document_format"] == "pdf"
    assert len(body["ingest"]["item_ids"]) == 5
    assert body["file"]["status"] == "ready"
    assert body["file"]["item_count"] == 5
    # 不透出存储布局的内部引用。
    assert "storage_key" not in body["file"]

    items = migrated_client.get(f"{INTAKE}/{batch_id}/items").get_json()
    assert items["total"] == 5
    assert [item["page_no"] for item in items["items"]] == [1, 2, 3, 4, 5]
    assert {item["kind"] for item in items["items"]} == {"page"}
    assert {item["decided_purchase_date"] for item in items["items"]} == {None}
    assert {item["decided_source_name"] for item in items["items"]} == {None}

    # 五个页引用都能回看原件，页号在响应头里。
    for item in items["items"]:
        page = migrated_client.get(
            f"{INTAKE}/{batch_id}/items/{item['id']}/original"
        )
        assert page.status_code == 200
        assert page.headers["X-Intake-Page"] == str(item["page_no"])
        assert page.headers["X-Intake-Item-Kind"] == "page"
        assert page.data == pdf_bytes(5)


def test_the_status_has_no_fake_percentage(migrated_client: FlaskClient) -> None:
    """对外只有离散状态与"切出了几个内容"，没有进度百分比。"""
    batch_id = new_batch(migrated_client)
    upload(migrated_client, batch_id, "a.pdf", pdf_bytes(2))

    batch = migrated_client.get(f"{INTAKE}/{batch_id}").get_json()

    assert [ref["status"] for ref in batch["files"]] == ["ready"]
    assert batch["files"][0]["item_count"] == 2
    rendered = repr(batch)
    for forbidden in ("percent", "progress", "eta"):
        assert forbidden not in rendered


def test_delete_one_unreviewed_page_and_reupload_after_last_page(migrated_client: FlaskClient) -> None:
    batch_id = new_batch(migrated_client)
    content = pdf_bytes(2)
    assert upload(migrated_client, batch_id, "two.pdf", content).status_code == 201
    items = migrated_client.get(f"{INTAKE}/{batch_id}/items").get_json()["items"]
    first, second = items
    url = f"{INTAKE}/{batch_id}/items/{first['id']}"

    assert migrated_client.delete(url, json={"version": first["version"] + 1}).status_code == 409
    assert migrated_client.delete(url, json={"version": first["version"]}).status_code == 200
    remaining = migrated_client.get(f"{INTAKE}/{batch_id}/items").get_json()
    assert [item["id"] for item in remaining["items"]] == [second["id"]]
    assert migrated_client.get(f"{INTAKE}/{batch_id}").get_json()["files"][0]["item_count"] == 1
    assert upload(migrated_client, batch_id, "same.pdf", content).status_code == 409

    assert migrated_client.delete(
        f"{INTAKE}/{batch_id}/items/{second['id']}", json={"version": second["version"]}
    ).status_code == 200
    assert migrated_client.get(f"{INTAKE}/{batch_id}/items").get_json()["total"] == 0
    assert upload(migrated_client, batch_id, "again.pdf", content).status_code == 201


def test_one_file_per_request(migrated_client: FlaskClient) -> None:
    """字段名是 `file`，一次一个。与 `POST /api/invoices` 的 `files` 刻意不同：
    这里的十几份文件属于不同的单据。"""
    batch_id = new_batch(migrated_client)

    nothing = migrated_client.post(
        f"{INTAKE}/{batch_id}/files", data={}, content_type="multipart/form-data"
    )
    assert nothing.status_code == 400
    assert nothing.get_json()["error"]["details"]["field"] == "file"

    two = migrated_client.post(
        f"{INTAKE}/{batch_id}/files",
        data={"file": [(io.BytesIO(png_bytes()), "a.png"), (io.BytesIO(pdf_bytes(1)), "b.pdf")]},
        content_type="multipart/form-data",
    )
    assert two.status_code == 400

    wrong_field = migrated_client.post(
        f"{INTAKE}/{batch_id}/files",
        data={"files": (io.BytesIO(png_bytes()), "a.png")},
        content_type="multipart/form-data",
    )
    assert wrong_field.status_code == 400


def test_an_unsupported_type_is_refused_with_the_allowed_list(
    migrated_client: FlaskClient,
) -> None:
    batch_id = new_batch(migrated_client)

    response = upload(migrated_client, batch_id, "rows.csv", b"a,b\n1,2\n")

    assert response.status_code == 400
    details = response.get_json()["error"]["details"]
    assert details["allowed_extensions"] == ["jpg", "jpeg", "png", "pdf", "heic"]


def test_the_same_file_twice_in_one_batch_is_a_409_pointing_at_the_existing_one(
    migrated_client: FlaskClient,
) -> None:
    batch_id = new_batch(migrated_client)
    first = upload(migrated_client, batch_id, "a.png", png_bytes())
    assert first.status_code == 201

    again = upload(migrated_client, batch_id, "a-copy.png", png_bytes())

    assert again.status_code == 409
    details = again.get_json()["error"]["details"]
    assert details["batch_file_id"] == first.get_json()["file"]["id"]
    # 批次里仍然只有一个文件、一张卡片。
    batch = migrated_client.get(f"{INTAKE}/{batch_id}").get_json()
    assert len(batch["files"]) == 1
    assert migrated_client.get(f"{INTAKE}/{batch_id}/items").get_json()["total"] == 1


def test_the_second_file_failing_leaves_the_first_one_and_the_batch_usable(
    migrated_client: FlaskClient,
) -> None:
    """M03 验收第二条，走完整的 HTTP 通路。"""
    batch_id = new_batch(migrated_client)
    first = upload(migrated_client, batch_id, "one.pdf", pdf_bytes(3))
    assert first.status_code == 201

    oversized = upload(migrated_client, batch_id, "two.pdf", b"%PDF-1.4" + b"x" * 200_000)
    assert oversized.status_code == 400

    batch = migrated_client.get(f"{INTAKE}/{batch_id}").get_json()
    assert batch["status"] == "open"
    assert [ref["original_name"] for ref in batch["files"]] == ["one.pdf"]
    assert batch["files"][0]["status"] == "ready"
    assert migrated_client.get(f"{INTAKE}/{batch_id}/items").get_json()["total"] == 3
    # 接着传第三个照样可以。
    assert upload(migrated_client, batch_id, "three.png", png_bytes()).status_code == 201


def test_a_broken_file_is_a_201_with_a_failed_status_not_a_500(
    migrated_client: FlaskClient,
) -> None:
    """字节收下了就是 201；解析失败是**文件上的状态**，带可读的原因，可重试。"""
    batch_id = new_batch(migrated_client)

    response = upload(migrated_client, batch_id, "bad.pdf", b"%PDF-1.4\nnope")

    assert response.status_code == 201
    body = response.get_json()
    assert body["ingest"]["outcome"] == "failed"
    assert body["file"]["status"] == "failed"
    assert body["file"]["error"]["code"] == "pdf_unreadable"
    assert body["file"]["error"]["message"]
    assert body["ingest"]["item_ids"] == []
    # 坏文件照样能回看：人要看见自己到底传了什么上来。
    original = migrated_client.get(
        f"{INTAKE}/{batch_id}/files/{body['file']['id']}/original"
    )
    assert original.status_code == 200
    assert original.data == b"%PDF-1.4\nnope"


# --- 驱动队列 ---------------------------------------------------------------


def test_processing_reports_每个文件各自的结果(migrated_client: FlaskClient) -> None:
    """「五份切好三份、两份坏了」是正常结果，所以 200 + 逐项结果。"""
    batch_id = new_batch(migrated_client)
    upload(migrated_client, batch_id, "ok.pdf", pdf_bytes(2))
    bad = upload(migrated_client, batch_id, "bad.pdf", b"%PDF-1.4\nnope").get_json()

    response = migrated_client.post(f"{INTAKE}/{batch_id}/process")

    assert response.status_code == 200
    body = response.get_json()
    # 两个文件都已经处理过了（上传时当场切完），所以队列是空的。
    assert body["results"] == []
    assert body["batch"]["id"] == batch_id

    retry = migrated_client.post(
        f"{INTAKE}/{batch_id}/process", json={"retry_failed": True}
    )
    assert retry.status_code == 200
    results = retry.get_json()["results"]
    assert [item["file"]["original_name"] for item in results] == ["bad.pdf"]
    assert results[0]["outcome"] == "failed"
    assert results[0]["file"]["id"] == bad["file"]["id"]


def test_process_rejects_unknown_fields(migrated_client: FlaskClient) -> None:
    batch_id = new_batch(migrated_client)

    assert migrated_client.post(
        f"{INTAKE}/{batch_id}/process", json={"force": True}
    ).status_code == 400
    assert migrated_client.post(
        f"{INTAKE}/{batch_id}/process", json={"retry_failed": "yes"}
    ).status_code == 400


# --- 移除文件 ---------------------------------------------------------------


def test_removing_a_file_says_whether_the_bytes_are_still_referenced(
    migrated_client: FlaskClient,
) -> None:
    """移除文件不等于能删盘上的字节——别的批次可能还在用同一份。

    第 16 节：这个批次里的记录是**真删**（引用、页、分类、撤销日志一起走），
    但 `intake_files` 那行字节与磁盘上的原件不动，`bytes_still_referenced`
    回答「现在还有没有人用它」。
    """
    content = pdf_bytes(2)
    first_batch = new_batch(migrated_client)
    second_batch = new_batch(migrated_client)
    shared = upload(migrated_client, first_batch, "x.pdf", content).get_json()
    upload(migrated_client, second_batch, "x.pdf", content)

    response = migrated_client.delete(
        f"{INTAKE}/{first_batch}/files/{shared['file']['id']}"
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["file"]["original_name"] == "x.pdf"  # 删掉之前那条引用的样子
    assert body["removed_item_count"] == 2
    assert body["bytes_still_referenced"] is True
    # 移除之后这个批次的卡片里不再有它的页，别的批次一页不少。
    assert migrated_client.get(f"{INTAKE}/{first_batch}/items").get_json()["total"] == 0
    assert migrated_client.get(f"{INTAKE}/{second_batch}/items").get_json()["total"] == 2
    batch = migrated_client.get(f"{INTAKE}/{first_batch}").get_json()
    assert batch["files"] == []
    # 同一份文件再传回来：全新的一份，照常切出两页。
    again = upload(migrated_client, first_batch, "x.pdf", content)
    assert again.status_code == 201, again.get_json()
    assert migrated_client.get(f"{INTAKE}/{first_batch}/items").get_json()["total"] == 2


# --- 路径里的归属 -----------------------------------------------------------


def test_a_file_from_another_batch_is_a_404_not_someone_elses_original(
    migrated_client: FlaskClient,
) -> None:
    """换个批次号试试，不该变成一条能读到别人原件的路径。"""
    mine = new_batch(migrated_client)
    other = new_batch(migrated_client)
    uploaded = upload(migrated_client, mine, "mine.pdf", pdf_bytes(2)).get_json()
    file_id = uploaded["file"]["id"]
    item_id = migrated_client.get(f"{INTAKE}/{mine}/items").get_json()["items"][0]["id"]

    assert migrated_client.get(
        f"{INTAKE}/{other}/files/{file_id}/original"
    ).status_code == 404
    assert migrated_client.get(
        f"{INTAKE}/{other}/items/{item_id}/original"
    ).status_code == 404
    assert migrated_client.delete(f"{INTAKE}/{other}/files/{file_id}").status_code == 404
    # 自己的批次照样能读。
    assert migrated_client.get(
        f"{INTAKE}/{mine}/files/{file_id}/original"
    ).status_code == 200


def test_an_unknown_file_or_item_is_a_404(migrated_client: FlaskClient) -> None:
    batch_id = new_batch(migrated_client)

    assert migrated_client.get(f"{INTAKE}/{batch_id}/files/777/original").status_code == 404
    assert migrated_client.get(f"{INTAKE}/{batch_id}/items/777/original").status_code == 404


# --- 与旧单张上传路径并存 ---------------------------------------------------


def test_the_legacy_single_upload_api_still_works_alongside(
    migrated_client: FlaskClient,
) -> None:
    """M03 验收第三条：旧单张上传 API 不受影响，而且跨路径重复只提示不阻断。"""
    content = png_bytes()
    legacy = migrated_client.post(
        "/api/invoices",
        data={"files": (io.BytesIO(content), "legacy.png"), "recognize": "false"},
        content_type="multipart/form-data",
    )
    assert legacy.status_code == 201
    invoice_id = legacy.get_json()["id"]

    batch_id = new_batch(migrated_client)
    response = upload(migrated_client, batch_id, "same.png", content)

    assert response.status_code == 201
    assert response.get_json()["same_bytes_invoice_ids"] == [invoice_id]
    assert response.get_json()["ingest"]["outcome"] == "ingested"
    # 旧路径自己的全局重复拦截仍然有效。
    duplicate = migrated_client.post(
        "/api/invoices",
        data={"files": (io.BytesIO(content), "again.png"), "recognize": "false"},
        content_type="multipart/form-data",
    )
    assert duplicate.status_code == 409
