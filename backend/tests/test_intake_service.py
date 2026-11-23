"""上传整理批次的数据与存储边界（多文件计划 M02）。

本文件对着 M02 的三条验收条件写：

1. **同一个文件的第 1 页和第 3 页可以是两个独立的分类内容，各自回指原页。**
   这是现有 `invoice_files`（一份文件只属一张单据）表达不了的那件事，所以它是
   本步骤最关键的一条。
2. **旧发票 / 旧导入批次迁移后照常回读**，而且旧的「同一个文件不许重复上传」
   仍然有效——新表不是靠放宽旧约束换来的。
3. **失败事务不留下半套组关系**：切页切到一半出错，文件退回原状，不会留下
   「已就绪但只有三页」。

所有测试都跑在 `migrated_database` 上（真实 `alembic upgrade head` 出来的库），
不用 `create_all`：否则"迁移到底能不能跑"就失去了验证。

本轮**没有真实 OCR、没有文件上传、没有浏览器操作**：M02 的服务刻意不碰磁盘与外部
服务，字节只是"已经存好了"这个事实。文件名与商家名一律是合成字符串。
"""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from src.db import Database
from src.errors import ConflictError, NotFoundError, ValidationError
from src.models import (
    INTAKE_CANDIDATE_BASIS_HISTORY,
    INTAKE_CANDIDATE_BASIS_PAPER,
    INTAKE_CANDIDATE_FIELD_PURCHASE_DATE,
    INTAKE_CANDIDATE_FIELD_SOURCE_NAME,
    INTAKE_FILE_STATUS_DISCARDED,
    INTAKE_FILE_STATUS_FAILED,
    INTAKE_FILE_STATUS_PENDING,
    INTAKE_FILE_STATUS_READY,
    INTAKE_ITEM_KIND_PAGE,
    INTAKE_ITEM_KIND_ROW,
    IntakeFile,
    IntakeItem,
)
from src.services.intake import (
    BATCH_STATUS_CLOSED,
    BATCH_STATUS_DISCARDED,
    BATCH_STATUS_OPEN,
    CandidateInput,
    IntakeService,
    RegisteredFile,
)
from src.services.invoices import InvoiceService, UploadedFile
from src.services.purchases import PurchaseService
from src.services.spreadsheets import SpreadsheetService


def digest_of(label: str) -> str:
    """合成一个稳定的内容摘要。这里不造真实文件，摘要只是一个标识。"""
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def register(
    service: IntakeService,
    batch_id: int,
    label: str,
    *,
    original_name: str | None = None,
    content_type: str | None = "application/pdf",
) -> RegisteredFile:
    return service.register_file(
        batch_id,
        storage_key=f"synthetic/{label}",
        sha256=digest_of(label),
        size=1024,
        original_name=original_name or f"{label}.pdf",
        content_type=content_type,
    )


# --- 验收 1：一份文件的不同页进入不同的组 -------------------------------------


def test_two_pages_of_one_file_are_independent_items(
    intake_service: IntakeService,
) -> None:
    """五页 PDF 的第 1 页与第 3 页各是一个内容，各自回指自己那一页。"""
    batch = intake_service.create_batch()
    registered = register(intake_service, batch.id, "five-page-invoices")

    items = intake_service.record_items(registered.ref.id, pages=[1, 2, 3, 4, 5])

    assert len(items) == 5
    assert [item.page_no for item in items] == [1, 2, 3, 4, 5]
    assert {item.kind for item in items} == {INTAKE_ITEM_KIND_PAGE}
    # 回看原件要的三样东西都在内容上：文件名、存储引用、页号。
    assert {item.original_name for item in items} == {"five-page-invoices.pdf"}
    assert {item.storage_key for item in items} == {
        "synthetic/five-page-invoices"
    }
    assert all(item.row_no is None for item in items)
    # 五个内容只对应一份字节。
    assert len({item.sha256 for item in items}) == 1


def test_pages_of_one_file_can_land_in_different_groups(
    intake_service: IntakeService,
) -> None:
    """同一份 PDF 的第 1 页与第 3 页可以属于两个不同的日期/商家组。

    这正是 `invoice_files` 表达不了、M02 要解决的那件事。
    """
    batch = intake_service.create_batch()
    registered = register(intake_service, batch.id, "mixed-dates")
    items = intake_service.record_items(registered.ref.id, pages=[1, 2, 3])

    intake_service.classify_item(
        items[0].id, purchase_date=date(2026, 11, 8), source_name="Costco"
    )
    intake_service.classify_item(
        items[2].id, purchase_date=date(2026, 11, 9), source_name="Restaurant Depot"
    )

    groups = intake_service.list_groups(batch.id)

    assert [
        (group.purchase_date, group.source_name_normalized, group.item_count)
        for group in groups
    ] == [
        (date(2026, 11, 8), "costco", 1),
        (date(2026, 11, 9), "restaurant depot", 1),
        # 第 2 页还没归组：它落在「待归类」这一组里，而不是凭空消失。
        (None, None, 1),
    ]


def test_groups_are_derived_so_moving_a_card_needs_no_sync(
    intake_service: IntakeService,
) -> None:
    """组是算出来的：改一张卡片的决定值，组立刻跟着变，不需要任何同步动作。"""
    batch = intake_service.create_batch()
    registered = register(intake_service, batch.id, "derived-groups")
    items = intake_service.record_items(registered.ref.id, pages=[1, 2])
    for item in items:
        intake_service.classify_item(
            item.id, purchase_date=date(2026, 11, 8), source_name="Costco"
        )
    assert [group.item_count for group in intake_service.list_groups(batch.id)] == [2]

    intake_service.classify_item(items[1].id, purchase_date=date(2026, 11, 9))

    groups = intake_service.list_groups(batch.id)
    assert [
        (group.purchase_date, group.source_name_normalized, group.item_count)
        for group in groups
    ] == [
        (date(2026, 11, 8), "costco", 1),
        (date(2026, 11, 9), "costco", 1),
    ]


def test_dropping_on_a_date_header_leaves_the_supplier_unknown(
    intake_service: IntakeService,
) -> None:
    """拖到日期标题只写日期：商家仍然未知，落在那一天的「商家待定」里。

    ⚠️ 这里顺带守住一条产品规则：来源不明**不会**被任何建议悄悄填成一个名字。
    """
    batch = intake_service.create_batch()
    registered = register(intake_service, batch.id, "date-only")
    (item,) = intake_service.record_items(registered.ref.id, pages=[1])

    updated = intake_service.classify_item(item.id, purchase_date="2026-11-08")

    assert updated.decided_purchase_date == date(2026, 11, 8)
    assert updated.decided_source_name is None
    assert updated.decided_source_name_normalized is None
    (group,) = intake_service.list_groups(batch.id)
    assert (group.purchase_date, group.source_name) == (date(2026, 11, 8), None)


def test_classifying_only_the_date_does_not_erase_a_confirmed_supplier(
    intake_service: IntakeService,
) -> None:
    """「没传这个字段」与「传了 null」必须分开：只改日期不该抹掉已确认的商家。"""
    batch = intake_service.create_batch()
    registered = register(intake_service, batch.id, "keep-supplier")
    (item,) = intake_service.record_items(registered.ref.id, pages=[1])
    intake_service.classify_item(
        item.id, purchase_date=date(2026, 11, 8), source_name="Costco"
    )

    moved = intake_service.classify_item(item.id, purchase_date=date(2026, 11, 9))
    assert moved.decided_source_name == "Costco"

    cleared = intake_service.classify_item(item.id, source_name=None)
    assert cleared.decided_source_name is None
    assert cleared.decided_source_name_normalized is None


def test_case_differences_do_not_split_one_supplier_into_two_groups(
    intake_service: IntakeService,
) -> None:
    """`Costco` 与 `COSTCO` 是同一家，不该在整理页上长出两个组。"""
    batch = intake_service.create_batch()
    registered = register(intake_service, batch.id, "costco-case")
    items = intake_service.record_items(registered.ref.id, pages=[1, 2])
    intake_service.classify_item(
        items[0].id, purchase_date=date(2026, 11, 8), source_name="Costco"
    )
    intake_service.classify_item(
        items[1].id, purchase_date=date(2026, 11, 8), source_name="COSTCO"
    )

    (group,) = intake_service.list_groups(batch.id)

    assert group.source_name_normalized == "costco"
    assert group.item_count == 2
    # 展示写法只能挑一个，但它必须是老板确认过的那两种之一，不是编出来的第三种。
    assert group.source_name in {"Costco", "COSTCO"}


def test_spreadsheet_rows_keep_their_original_row_numbers(
    intake_service: IntakeService,
) -> None:
    """表格按行形成内容，原行号与工作表名可回查（M04 要靠这个定位单元格）。"""
    batch = intake_service.create_batch()
    registered = register(
        intake_service,
        batch.id,
        "two-day-orders",
        original_name="orders.xlsx",
        content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )

    items = intake_service.record_items(
        registered.ref.id, rows=[2, 3, 7], sheet_name="November"
    )

    assert [item.row_no for item in items] == [2, 3, 7]
    assert {item.kind for item in items} == {INTAKE_ITEM_KIND_ROW}
    assert all(item.page_no is None for item in items)
    assert {item.sheet_name for item in items} == {"November"}


def test_one_file_cannot_be_both_pages_and_rows(
    intake_service: IntakeService,
) -> None:
    batch = intake_service.create_batch()
    registered = register(intake_service, batch.id, "either-or")

    with pytest.raises(ValidationError):
        intake_service.record_items(registered.ref.id, pages=[1], rows=[1])
    with pytest.raises(ValidationError):
        intake_service.record_items(registered.ref.id)


# --- 原件字节只存一次 ---------------------------------------------------------


def test_the_same_bytes_in_two_batches_are_stored_once(
    intake_service: IntakeService, migrated_database: Database
) -> None:
    """同一份 PDF 进两个批次：一行字节、两条引用，磁盘上只有一份。"""
    first = intake_service.create_batch()
    second = intake_service.create_batch()

    one = register(intake_service, first.id, "shared-pdf")
    two = register(intake_service, second.id, "shared-pdf")

    assert one.reused_bytes is False
    assert two.reused_bytes is True
    assert one.ref.intake_file_id == two.ref.intake_file_id
    assert one.ref.id != two.ref.id
    # 第二次登记要能说出「这份字节还在哪个批次里」——M04 的重复提示靠它。
    assert two.shared_with_batch_ids == (first.id,)

    with migrated_database.unit_of_work() as session:
        rows = session.scalars(
            select(IntakeFile).where(IntakeFile.sha256 == digest_of("shared-pdf"))
        ).all()
    assert len(rows) == 1


def test_the_same_file_twice_in_one_batch_is_rejected(
    intake_service: IntakeService,
) -> None:
    """同一批次里同一份字节只能进来一次，否则整理页会出现两张一样的卡片。"""
    batch = intake_service.create_batch()
    register(intake_service, batch.id, "duplicate-in-batch")

    with pytest.raises(ConflictError) as error:
        register(intake_service, batch.id, "duplicate-in-batch")

    assert error.value.details["batch_id"] == batch.id
    assert "batch_file_id" in error.value.details


def test_discarded_file_comes_back_with_its_items_when_re_registered(
    intake_service: IntakeService,
) -> None:
    """撤掉文件是可以反悔的：再传一次同一份文件，页与已有分类原样回来。"""
    batch = intake_service.create_batch()
    registered = register(intake_service, batch.id, "oops-removed")
    (item,) = intake_service.record_items(registered.ref.id, pages=[1])
    intake_service.classify_item(item.id, purchase_date=date(2026, 11, 8))

    discarded = intake_service.discard_file(registered.ref.id)
    assert discarded.ref.status == INTAKE_FILE_STATUS_DISCARDED
    assert discarded.bytes_still_referenced is False
    assert intake_service.list_items(batch.id).total == 0

    again = register(intake_service, batch.id, "oops-removed")

    assert again.ref.id == registered.ref.id
    assert again.ref.status == INTAKE_FILE_STATUS_READY
    page = intake_service.get_item(item.id)
    assert page.decided_purchase_date == date(2026, 11, 8)


def test_discarding_one_batch_does_not_free_bytes_another_batch_uses(
    intake_service: IntakeService,
) -> None:
    """回收要能说清「这份字节还有人用吗」，否则会删掉别的批次的原件。"""
    first = intake_service.create_batch()
    second = intake_service.create_batch()
    one = register(intake_service, first.id, "still-in-use")
    register(intake_service, second.id, "still-in-use")

    result = intake_service.discard_file(one.ref.id)

    assert result.bytes_still_referenced is True


def test_cross_path_duplicate_with_the_old_upload_is_reported_not_blocked(
    intake_service: IntakeService, invoice_service: InvoiceService
) -> None:
    """旧单张上传路径传过的同一份字节：只提示，不阻断。

    两条路径的判重表是分开的（`invoice_files.sha256` 与 `intake_files.sha256`），
    所以这里既不能静默放过，也不该把批次上传卡死。
    """
    content = b"%PDF-1.4 synthetic single upload"
    invoice = invoice_service.create_invoice(
        [UploadedFile(original_name="old.pdf", content=content)], recognize=False
    )
    batch = intake_service.create_batch()

    registered = intake_service.register_file(
        batch.id,
        storage_key="synthetic/cross-path",
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
        original_name="old.pdf",
        content_type="application/pdf",
    )

    assert registered.ref.status == INTAKE_FILE_STATUS_PENDING
    assert registered.same_bytes_invoice_ids == (invoice.id,)


# --- 验收 2：旧数据与旧约束都没被动过 -----------------------------------------


def test_old_invoices_still_read_back_after_the_new_tables_exist(
    intake_service: IntakeService, invoice_service: InvoiceService
) -> None:
    """同一个库里既有整理批次又有旧发票，旧发票照常回读。"""
    invoice = invoice_service.create_invoice(
        [UploadedFile(original_name="legacy.pdf", content=b"%PDF old invoice")],
        recognize=False,
    )
    invoice_service.update_invoice(
        invoice.id, source_name="Costco", invoice_date="2026-11-08"
    )
    batch = intake_service.create_batch()
    register(intake_service, batch.id, "new-world")

    reread = invoice_service.get_invoice(invoice.id)

    assert reread.source_name == "Costco"
    assert len(reread.files) == 1
    assert reread.files[0].original_name == "legacy.pdf"


def test_old_import_batches_still_read_back_after_the_new_tables_exist(
    intake_service: IntakeService,
    invoice_service: InvoiceService,
    purchase_service: PurchaseService,
) -> None:
    """旧的表格导入批次（`entry_source='import'`）照常回读。

    导入批次复用的是步骤 05 的草稿表，所以这里验的是「新加五张表没碰到那条路」。
    CSV 内容是明确标为合成的两行，不是真实客户表格。
    """
    spreadsheets = SpreadsheetService(
        invoice_service, purchase_service, max_bytes=1024 * 1024
    )
    rows = [
        "商品描述,数量,单位,单价,金额,采购日期,采购来源,币种,行类型",
        "鸡胸肉,2,lb,6.35,12.70,2026-10-01,Example Seller,USD,purchase",
        "",
    ]
    # utf-8-sig：Excel 存出来的 CSV 带 BOM，现有导入按那个口径读。
    content = "\r\n".join(rows).encode("utf-8-sig")
    imported = spreadsheets.create(
        UploadedFile(content=content, original_name="synthetic-import.csv"), {}
    )

    batch = intake_service.create_batch()
    register(intake_service, batch.id, "after-import")

    reread = invoice_service.get_invoice(imported.id)

    assert reread.entry_source == "import"
    assert len(reread.lines) == 1
    assert reread.lines[0].description == "鸡胸肉"


def test_the_old_global_duplicate_upload_rule_still_holds(
    invoice_service: InvoiceService, intake_service: IntakeService
) -> None:
    """⚠️ 新表不是靠放宽 `invoice_files.sha256` 换来的。

    步骤 05 定下的「完全相同的文件不许重复上传」仍然在旧路径上生效；M02 的批次表
    与它并存，谁也没改谁。
    """
    content = b"%PDF-1.4 synthetic duplicate"
    first = invoice_service.create_invoice(
        [UploadedFile(original_name="a.pdf", content=content)], recognize=False
    )
    intake_service.create_batch()

    with pytest.raises(ConflictError) as error:
        invoice_service.create_invoice(
            [UploadedFile(original_name="b.pdf", content=content)], recognize=False
        )

    assert error.value.details["invoice_id"] == first.id


# --- 验收 3：失败事务不留下半套关系 -------------------------------------------


def test_a_failed_page_split_leaves_the_file_untouched(
    intake_service: IntakeService, migrated_database: Database
) -> None:
    """切页切到一半出错：一个内容都不留，文件退回 pending。

    这里用重复页号触发失败——它和真实世界里"第 4 页解析炸了"走的是同一条回滚路径。
    """
    batch = intake_service.create_batch()
    registered = register(intake_service, batch.id, "half-split")

    with pytest.raises(ValidationError):
        intake_service.record_items(registered.ref.id, pages=[1, 2, 2, 4])

    with migrated_database.unit_of_work() as session:
        leftovers = session.scalars(
            select(IntakeItem).where(
                IntakeItem.batch_file_id == registered.ref.id
            )
        ).all()
    assert leftovers == []
    reread = intake_service.get_batch(batch.id)
    assert reread.files[0].status == INTAKE_FILE_STATUS_PENDING
    assert reread.files[0].item_count is None


def test_the_database_also_refuses_two_items_for_the_same_page(
    migrated_database: Database, intake_service: IntakeService
) -> None:
    """服务层拦了一道，数据库上还有一道局部唯一索引兜底。

    局部索引是必须的：行内容的 `page_no` 是 NULL，而 SQLite 把多个 NULL 视为互不
    相同，一条普通唯一约束根本拦不住。
    """
    batch = intake_service.create_batch()
    registered = register(intake_service, batch.id, "db-guard")
    intake_service.record_items(registered.ref.id, pages=[1])

    with pytest.raises(Exception) as error:
        with migrated_database.unit_of_work() as session:
            session.add(
                IntakeItem(
                    batch_file_id=registered.ref.id,
                    kind=INTAKE_ITEM_KIND_PAGE,
                    page_no=1,
                )
            )
    assert "unique" in str(error.value).lower()


def test_the_database_refuses_a_page_item_without_a_page_number(
    migrated_database: Database, intake_service: IntakeService
) -> None:
    """坐标不许混填：`page` 必须有页号、没行号，否则「回看原件」没有落点。"""
    batch = intake_service.create_batch()
    registered = register(intake_service, batch.id, "coordinate-guard")

    with pytest.raises(Exception) as error:
        with migrated_database.unit_of_work() as session:
            session.add(
                IntakeItem(
                    batch_file_id=registered.ref.id,
                    kind=INTAKE_ITEM_KIND_PAGE,
                    row_no=3,
                )
            )
    assert "constraint" in str(error.value).lower()


def test_the_database_refuses_a_normalized_supplier_without_a_display_name(
    migrated_database: Database, intake_service: IntakeService
) -> None:
    """展示名与规范化名要么都有、要么都没有，不许只存一个。"""
    batch = intake_service.create_batch()
    registered = register(intake_service, batch.id, "supplier-pair-guard")

    with pytest.raises(Exception) as error:
        with migrated_database.unit_of_work() as session:
            session.add(
                IntakeItem(
                    batch_file_id=registered.ref.id,
                    kind=INTAKE_ITEM_KIND_PAGE,
                    page_no=1,
                    decided_source_name_normalized="costco",
                )
            )
    assert "constraint" in str(error.value).lower()


# --- 文件状态：等处理 / 失败 / 回收 -------------------------------------------


def test_a_failed_file_does_not_affect_the_rest_of_the_batch(
    intake_service: IntakeService,
) -> None:
    """一个坏文件不该让整批重传：它自己标失败，别的文件照常。"""
    batch = intake_service.create_batch()
    broken = register(intake_service, batch.id, "broken")
    good = register(intake_service, batch.id, "good")
    intake_service.record_items(good.ref.id, pages=[1, 2])

    failed = intake_service.fail_file(
        broken.ref.id, error_code="pdf_unreadable", error_message="这份 PDF 打不开。"
    )

    assert failed.status == INTAKE_FILE_STATUS_FAILED
    assert failed.error_code == "pdf_unreadable"
    reread = intake_service.get_batch(batch.id)
    statuses = {ref.original_name: ref.status for ref in reread.files}
    assert statuses == {
        "broken.pdf": INTAKE_FILE_STATUS_FAILED,
        "good.pdf": INTAKE_FILE_STATUS_READY,
    }
    assert intake_service.list_items(batch.id).total == 2


def test_a_failed_file_can_be_retried(intake_service: IntakeService) -> None:
    """重试一个失败的文件：成功之后错误信息跟着清掉，不留一个过期的红字。"""
    batch = intake_service.create_batch()
    registered = register(intake_service, batch.id, "retry-me")
    intake_service.fail_file(
        registered.ref.id, error_code="timeout", error_message="处理超时。"
    )

    intake_service.record_items(registered.ref.id, pages=[1, 2])

    reread = intake_service.get_batch(batch.id)
    assert reread.files[0].status == INTAKE_FILE_STATUS_READY
    assert reread.files[0].error_code is None
    assert reread.files[0].item_count == 2


def test_items_of_a_discarded_file_leave_the_working_queue(
    intake_service: IntakeService,
) -> None:
    """回收的文件不再出现在整理页上，但行留着（可查、可复活）。"""
    batch = intake_service.create_batch()
    registered = register(intake_service, batch.id, "recycled")
    items = intake_service.record_items(registered.ref.id, pages=[1, 2])
    intake_service.classify_item(items[0].id, purchase_date=date(2026, 11, 8))

    intake_service.discard_file(registered.ref.id)

    assert intake_service.list_items(batch.id).total == 0
    assert intake_service.list_groups(batch.id) == ()
    assert intake_service.list_items(batch.id, include_discarded=True).total == 2


def test_a_discarded_file_cannot_be_reclassified(
    intake_service: IntakeService,
) -> None:
    batch = intake_service.create_batch()
    registered = register(intake_service, batch.id, "no-more-moves")
    (item,) = intake_service.record_items(registered.ref.id, pages=[1])
    intake_service.discard_file(registered.ref.id)

    with pytest.raises(ConflictError):
        intake_service.classify_item(item.id, purchase_date=date(2026, 11, 8))


# --- 版本号：两个会话同时整理 -------------------------------------------------


def test_a_stale_version_is_refused_instead_of_overwriting(
    intake_service: IntakeService,
) -> None:
    """手机和电脑同时移动同一张卡片：后一个被拦住，不是悄悄覆盖。"""
    batch = intake_service.create_batch()
    registered = register(intake_service, batch.id, "two-sessions")
    (item,) = intake_service.record_items(registered.ref.id, pages=[1])
    assert item.version == 1

    moved = intake_service.classify_item(
        item.id, purchase_date=date(2026, 11, 8), version=1
    )
    assert moved.version == 2

    with pytest.raises(ConflictError) as error:
        intake_service.classify_item(
            item.id, purchase_date=date(2026, 11, 9), version=1
        )

    assert error.value.details == {
        "item_id": item.id,
        "expected_version": 1,
        "current_version": 2,
    }
    # 被拦住的那次一个字都没写进去。
    assert intake_service.get_item(item.id).decided_purchase_date == date(2026, 11, 8)


def test_repeating_the_same_command_does_not_bump_the_version(
    intake_service: IntakeService,
) -> None:
    """手抖点两下同一个目标组：值没变，版本就不该涨，免得另一个窗口凭空 409。"""
    batch = intake_service.create_batch()
    registered = register(intake_service, batch.id, "idempotent-move")
    (item,) = intake_service.record_items(registered.ref.id, pages=[1])

    first = intake_service.classify_item(item.id, purchase_date=date(2026, 11, 8))
    second = intake_service.classify_item(item.id, purchase_date=date(2026, 11, 8))

    assert first.version == second.version == 2


def test_ambiguous_date_text_is_refused_rather_than_guessed(
    intake_service: IntakeService,
) -> None:
    """`11/08/26` 的日/月顺序文本本身定不死，本模块不接收它——宁可报错也不猜。"""
    batch = intake_service.create_batch()
    registered = register(intake_service, batch.id, "no-guessing")
    (item,) = intake_service.record_items(registered.ref.id, pages=[1])

    with pytest.raises(ValidationError):
        intake_service.classify_item(item.id, purchase_date="11/08/26")


# --- 候选：系统建议与人工确认是两件事 -----------------------------------------


def test_candidates_never_become_decisions(intake_service: IntakeService) -> None:
    """写了一堆高可信候选，决定值仍然是空的。

    这是 docs/multifiles.md 第 4 节「系统建议／人工确认必须可区分」在代码上的落点。
    """
    batch = intake_service.create_batch()
    registered = register(intake_service, batch.id, "suggestions-only")
    (item,) = intake_service.record_items(registered.ref.id, pages=[1])

    updated = intake_service.replace_candidates(
        item.id,
        field=INTAKE_CANDIDATE_FIELD_SOURCE_NAME,
        basis=INTAKE_CANDIDATE_BASIS_PAPER,
        candidates=[
            CandidateInput(
                value="Example Seller",
                confidence=0.97,
                evidence={"text": "EXAMPLE SELLER INC", "page": 1},
            )
        ],
    )

    assert updated.decided_source_name is None
    assert updated.decided_source_name_normalized is None
    (candidate,) = updated.candidates
    assert candidate.value == "Example Seller"
    assert candidate.value_normalized == "example seller"
    assert candidate.basis == INTAKE_CANDIDATE_BASIS_PAPER
    assert candidate.evidence == {"text": "EXAMPLE SELLER INC", "page": 1}
    # 候选不进分组：这个内容仍然在「待归类」那一组里。
    (group,) = intake_service.list_groups(batch.id)
    assert (group.purchase_date, group.source_name, group.item_count) == (None, None, 1)


def test_two_bases_can_disagree_and_both_stay_visible(
    intake_service: IntakeService,
) -> None:
    """纸面读出一个、历史写法建议另一个——分歧恰恰是老板要看见的。"""
    batch = intake_service.create_batch()
    registered = register(intake_service, batch.id, "disagreement")
    (item,) = intake_service.record_items(registered.ref.id, pages=[1])

    intake_service.replace_candidates(
        item.id,
        field=INTAKE_CANDIDATE_FIELD_SOURCE_NAME,
        basis=INTAKE_CANDIDATE_BASIS_PAPER,
        candidates=[CandidateInput(value="Example Seller")],
    )
    updated = intake_service.replace_candidates(
        item.id,
        field=INTAKE_CANDIDATE_FIELD_SOURCE_NAME,
        basis=INTAKE_CANDIDATE_BASIS_HISTORY,
        candidates=[CandidateInput(value="Example Seller Inc")],
    )

    assert {candidate.basis for candidate in updated.candidates} == {
        INTAKE_CANDIDATE_BASIS_PAPER,
        INTAKE_CANDIDATE_BASIS_HISTORY,
    }


def test_recomputing_one_basis_replaces_only_that_basis(
    intake_service: IntakeService,
) -> None:
    """重算一层依据是整组替换，不叠加；别的依据不受影响。"""
    batch = intake_service.create_batch()
    registered = register(intake_service, batch.id, "replace-one-basis")
    (item,) = intake_service.record_items(registered.ref.id, pages=[1])
    intake_service.replace_candidates(
        item.id,
        field=INTAKE_CANDIDATE_FIELD_SOURCE_NAME,
        basis=INTAKE_CANDIDATE_BASIS_HISTORY,
        candidates=[CandidateInput(value="Old Guess")],
    )
    intake_service.replace_candidates(
        item.id,
        field=INTAKE_CANDIDATE_FIELD_PURCHASE_DATE,
        basis=INTAKE_CANDIDATE_BASIS_PAPER,
        candidates=[CandidateInput(value="2026-11-08")],
    )

    updated = intake_service.replace_candidates(
        item.id,
        field=INTAKE_CANDIDATE_FIELD_SOURCE_NAME,
        basis=INTAKE_CANDIDATE_BASIS_HISTORY,
        candidates=[CandidateInput(value="New Guess")],
    )

    by_field = {
        (candidate.field, candidate.basis): candidate.value
        for candidate in updated.candidates
    }
    assert by_field == {
        (INTAKE_CANDIDATE_FIELD_SOURCE_NAME, INTAKE_CANDIDATE_BASIS_HISTORY): "New Guess",
        (INTAKE_CANDIDATE_FIELD_PURCHASE_DATE, INTAKE_CANDIDATE_BASIS_PAPER): "2026-11-08",
    }


def test_a_date_candidate_is_stored_as_iso_text(
    intake_service: IntakeService,
) -> None:
    batch = intake_service.create_batch()
    registered = register(intake_service, batch.id, "iso-candidate")
    (item,) = intake_service.record_items(registered.ref.id, pages=[1])

    updated = intake_service.replace_candidates(
        item.id,
        field=INTAKE_CANDIDATE_FIELD_PURCHASE_DATE,
        basis=INTAKE_CANDIDATE_BASIS_PAPER,
        candidates=[CandidateInput(value=date(2026, 11, 8), confidence=0.5)],
    )

    (candidate,) = updated.candidates
    assert candidate.value == "2026-11-08"
    assert candidate.value_normalized is None
    # 置信度按整数最小单位存，读回来是精确的 Decimal，不经 float。
    assert candidate.confidence == Decimal("0.5000")


def test_an_unknown_basis_is_refused(intake_service: IntakeService) -> None:
    """依据词表没有数据库 CHECK，所以服务层这一道必须真的拦住。"""
    batch = intake_service.create_batch()
    registered = register(intake_service, batch.id, "unknown-basis")
    (item,) = intake_service.record_items(registered.ref.id, pages=[1])

    with pytest.raises(ValidationError):
        intake_service.replace_candidates(
            item.id,
            field=INTAKE_CANDIDATE_FIELD_SOURCE_NAME,
            basis="vibes",
            candidates=[CandidateInput(value="Example Seller")],
        )


# --- 批次生命周期 -------------------------------------------------------------


def test_batch_status_is_derived_from_two_timestamps(
    intake_service: IntakeService, migrated_database: Database
) -> None:
    """状态不落列：数据库里只有两个时刻，status 是算出来的。"""
    batch = intake_service.create_batch()
    assert batch.status == BATCH_STATUS_OPEN

    assert intake_service.close_batch(batch.id).status == BATCH_STATUS_CLOSED
    assert intake_service.reopen_batch(batch.id).status == BATCH_STATUS_OPEN
    assert intake_service.discard_batch(batch.id).status == BATCH_STATUS_DISCARDED

    with migrated_database.unit_of_work() as session:
        columns = {
            row[1]
            for row in session.execute(text("pragma table_info('intake_batches')"))
        }
    assert "status" not in columns


def test_latest_open_batch_restores_the_one_in_progress(
    intake_service: IntakeService,
) -> None:
    """浏览器关掉再打开应该接着整理上一批，而不是又建一个空批次。"""
    assert intake_service.latest_open_batch() is None

    first = intake_service.create_batch()
    assert intake_service.latest_open_batch().id == first.id

    second = intake_service.create_batch()
    assert intake_service.latest_open_batch().id == second.id

    intake_service.discard_batch(second.id)
    assert intake_service.latest_open_batch().id == first.id


def test_a_discarded_batch_refuses_further_changes(
    intake_service: IntakeService,
) -> None:
    batch = intake_service.create_batch()
    registered = register(intake_service, batch.id, "frozen")
    (item,) = intake_service.record_items(registered.ref.id, pages=[1])
    intake_service.discard_batch(batch.id)

    with pytest.raises(ConflictError):
        intake_service.classify_item(item.id, purchase_date=date(2026, 11, 8))
    with pytest.raises(ConflictError):
        register(intake_service, batch.id, "frozen-too")
    with pytest.raises(ConflictError):
        intake_service.reopen_batch(batch.id)


def test_closing_a_batch_does_not_require_everything_to_be_grouped(
    intake_service: IntakeService,
) -> None:
    """待定内容留着以后再办是允许的；强制清零只会逼人乱填一个日期。"""
    batch = intake_service.create_batch()
    registered = register(intake_service, batch.id, "leftovers-ok")
    intake_service.record_items(registered.ref.id, pages=[1, 2])

    closed = intake_service.close_batch(batch.id)

    assert closed.status == BATCH_STATUS_CLOSED


def test_missing_things_report_not_found(intake_service: IntakeService) -> None:
    with pytest.raises(NotFoundError):
        intake_service.get_batch(9999)
    with pytest.raises(NotFoundError):
        intake_service.get_item(9999)
    with pytest.raises(NotFoundError):
        intake_service.fail_file(9999, error_code="x", error_message="y")


# --- 分页 ---------------------------------------------------------------------


def test_items_are_paged_so_a_big_spreadsheet_is_not_five_thousand_cards(
    intake_service: IntakeService,
) -> None:
    """大表在读取时分页（M04 的「不为 5000 行一次返回 5000 张卡片」）。"""
    batch = intake_service.create_batch()
    registered = register(intake_service, batch.id, "big-sheet")
    intake_service.record_items(registered.ref.id, rows=list(range(2, 12)))

    page = intake_service.list_items(batch.id, limit=4, offset=4)

    assert page.total == 10
    assert [item.row_no for item in page.items] == [6, 7, 8, 9]

    with pytest.raises(ValidationError):
        intake_service.list_items(batch.id, limit=0)
