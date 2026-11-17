"""发票上传、草稿、行匹配与逐行确认的业务测试（步骤 05）。

这些用例对着步骤 05 的验收条件写：

- 三行只确认两行时只有两条采购记录，第三行可以稍后确认；
- 重复确认不重复记价；
- 写入失败不留下半条正式记录；
- 能根据采购记录查到原始单据。

以及决定里那几条容易被悄悄做错的：认不出来的数值不补 0、原始识别值永不改动、
匹配只给候选不自动确认、对账只提示不阻断、漏行在收口确认那一下才有人看。
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from src.errors import ConflictError, NotFoundError, ValidationError
from src.integrations.file_storage import LocalFileStorage
from src.services.ingredients import FoodItemView, IngredientService
from src.services.invoices import (
    INVOICE_STATUS_COMPLETED,
    INVOICE_STATUS_DRAFT,
    INVOICE_STATUS_IN_REVIEW,
    INVOICE_STATUS_READY,
    INVOICE_STATUS_VOIDED,
    MAX_FILES_PER_INVOICE,
    RECONCILIATION_BALANCED,
    RECONCILIATION_MISMATCH,
    RECONCILIATION_NOT_CHECKED,
    RECONCILIATION_NO_TOTAL,
    ConfirmOption,
    InvoiceService,
    InvoiceView,
    UploadedFile,
)
from src.services.purchases import PurchaseService

PHOTO = b"\x89PNG\r\n\x1a\n fake photo bytes for tests"
OTHER_PHOTO = b"\x89PNG\r\n\x1a\n a different photo"


def upload(
    content: bytes = PHOTO,
    name: str = "invoice.png",
    content_type: str | None = "image/png",
) -> UploadedFile:
    return UploadedFile(content=content, original_name=name, content_type=content_type)


def draft(service: InvoiceService, **kwargs) -> InvoiceView:
    """建一张来自 Fake 识别的草稿：两行，第二行的单价认不出来。"""
    return service.create_invoice([upload()], **kwargs)


def line_by_no(invoice: InvoiceView, line_no: int):
    return next(line for line in invoice.lines if line.line_no == line_no)


# --- 上传与原始文件 ---------------------------------------------------------


def test_upload_creates_a_draft_and_keeps_the_original_bytes(
    invoice_service: InvoiceService, storage: LocalFileStorage
) -> None:
    """原始文件只写不改：存进去的字节与读回来的完全一致，它是核对用的证据。"""
    invoice = draft(invoice_service)

    assert len(invoice.files) == 1
    stored = invoice.files[0]
    assert stored.original_name == "invoice.png"
    assert stored.page_order == 1
    _, stream = invoice_service.open_file(invoice.id, stored.id)
    with stream:
        assert stream.read() == PHOTO


def test_invoice_needs_at_least_one_file(invoice_service: InvoiceService) -> None:
    """没有单据的零散采购走步骤 04 的单条录入，不提供「无附件的空发票」。"""
    with pytest.raises(ValidationError) as error:
        invoice_service.create_invoice([])

    assert "至少需要一个文件" in error.value.message
    assert "POST /api/purchases" in error.value.message


@pytest.mark.parametrize("name", ["invoice.txt", "invoice.docx", "invoice", "invoice.exe"])
def test_unsupported_file_types_are_rejected(
    invoice_service: InvoiceService, name: str
) -> None:
    with pytest.raises(ValidationError) as error:
        invoice_service.create_invoice([upload(name=name, content_type=None)])

    assert "不支持的文件类型" in error.value.message
    assert "pdf" in error.value.details["allowed_extensions"]


def test_heic_is_stored_but_not_recognized(invoice_service: InvoiceService) -> None:
    """iPhone 原图是 HEIC：先把原件保住，解码留给步骤 08。"""
    invoice = invoice_service.create_invoice(
        [upload(name="IMG_0421.HEIC", content_type="image/heic")]
    )

    assert len(invoice.files) == 1
    assert invoice.lines == ()
    assert invoice.recognition_provider is None
    assert invoice.status == INVOICE_STATUS_DRAFT


def test_file_larger_than_the_limit_is_rejected(
    invoice_service: InvoiceService,
) -> None:
    with pytest.raises(ValidationError) as error:
        invoice_service.create_invoice([upload(content=b"x" * (64 * 1024 + 1))])

    assert "超过了单个文件" in error.value.message
    assert error.value.details["max_bytes"] == 64 * 1024


def test_empty_file_is_rejected(invoice_service: InvoiceService) -> None:
    with pytest.raises(ValidationError) as error:
        invoice_service.create_invoice([upload(content=b"")])

    assert "是空的" in error.value.message


def test_too_many_files_are_rejected(invoice_service: InvoiceService) -> None:
    uploads = [
        upload(content=f"page {index}".encode(), name=f"page{index}.png")
        for index in range(MAX_FILES_PER_INVOICE + 1)
    ]

    with pytest.raises(ValidationError) as error:
        invoice_service.create_invoice(uploads)

    assert error.value.details["max_files"] == MAX_FILES_PER_INVOICE


def test_the_same_file_twice_points_back_at_the_existing_invoice(
    invoice_service: InvoiceService,
) -> None:
    """完全相同的文件硬拦，并告诉老板去哪张单据接着审，而不是再建一张一样的。"""
    first = draft(invoice_service)

    with pytest.raises(ConflictError) as error:
        invoice_service.create_invoice([upload(name="again.png")])

    assert error.value.details["invoice_id"] == first.id
    assert "已经上传过了" in error.value.message


def test_two_identical_files_in_one_request_are_rejected(
    invoice_service: InvoiceService,
) -> None:
    with pytest.raises(ValidationError) as error:
        invoice_service.create_invoice([upload(name="a.png"), upload(name="b.png")])

    assert "两个完全相同的文件" in error.value.message


def test_a_retaken_photo_can_still_be_uploaded(
    invoice_service: InvoiceService,
) -> None:
    """补拍的照片字节不同，摘要判重抓不到它——这是正常操作，不该被拦。"""
    invoice = draft(invoice_service)

    updated = invoice_service.add_files(
        invoice.id, [upload(content=OTHER_PHOTO, name="page2.png")]
    )

    assert [item.page_order for item in updated.files] == [1, 2]


def test_the_last_file_cannot_be_deleted(invoice_service: InvoiceService) -> None:
    """单据必须保留至少一份原始文件，否则它就只是一堆没有凭据的数字。"""
    invoice = draft(invoice_service)

    with pytest.raises(ConflictError) as error:
        invoice_service.delete_file(invoice.id, invoice.files[0].id)

    assert "最后一个文件" in error.value.message


def test_deleting_a_bad_photo_also_removes_the_bytes(
    invoice_service: InvoiceService, storage: LocalFileStorage
) -> None:
    invoice = draft(invoice_service)
    invoice = invoice_service.add_files(
        invoice.id, [upload(content=OTHER_PHOTO, name="blurry.png")]
    )
    blurry = invoice.files[1]

    updated = invoice_service.delete_file(invoice.id, blurry.id)

    assert [item.id for item in updated.files] == [invoice.files[0].id]
    with pytest.raises(NotFoundError):
        invoice_service.open_file(invoice.id, blurry.id)


def test_deleted_file_can_be_uploaded_again(invoice_service: InvoiceService) -> None:
    """删掉之后摘要也没了，所以同一张照片可以重新传——否则拍坏重传会被自己拦住。"""
    invoice = draft(invoice_service)
    invoice = invoice_service.add_files(
        invoice.id, [upload(content=OTHER_PHOTO, name="second.png")]
    )
    invoice_service.delete_file(invoice.id, invoice.files[1].id)

    again = invoice_service.add_files(
        invoice.id, [upload(content=OTHER_PHOTO, name="second-again.png")]
    )

    assert len(again.files) == 2


def test_possible_duplicate_is_only_a_hint(invoice_service: InvoiceService) -> None:
    """来源 + 单号 + 总额都一样：提示可能重复，但**不阻止**——可能是补拍。"""
    first = draft(invoice_service)
    invoice_service.update_invoice(
        first.id, source_name="Costco", invoice_number="INV-77", invoice_total="286.00"
    )
    second = invoice_service.create_invoice([upload(content=OTHER_PHOTO, name="b.png")])
    invoice_service.update_invoice(
        second.id, source_name="costco ", invoice_number="inv-77", invoice_total="286.00"
    )

    assert invoice_service.get_invoice(second.id).possible_duplicate_invoice_ids == (
        first.id,
    )
    assert invoice_service.get_invoice(first.id).possible_duplicate_invoice_ids == (
        second.id,
    )


def test_no_duplicate_hint_without_an_invoice_number(
    invoice_service: InvoiceService,
) -> None:
    """手写单常常没有单号：三个字段缺一个就不判重，免得天天误报。"""
    first = draft(invoice_service)
    invoice_service.update_invoice(first.id, source_name="Costco", invoice_total="286.00")
    second = invoice_service.create_invoice([upload(content=OTHER_PHOTO, name="b.png")])
    invoice_service.update_invoice(
        second.id, source_name="Costco", invoice_total="286.00"
    )
    # Fake 识别器给每张草稿都填了同一个假单号，这里把它清掉，才是"手写单没有单号"
    # 那个场景。
    invoice_service.update_invoice(first.id, invoice_number=None)
    invoice_service.update_invoice(second.id, invoice_number=None)

    assert invoice_service.get_invoice(second.id).possible_duplicate_invoice_ids == ()


# --- 草稿与原始识别证据 -----------------------------------------------------


def test_fake_draft_marks_itself_as_fake(invoice_service: InvoiceService) -> None:
    """假数据必须自报：演示时不能让它看起来像真识别。"""
    invoice = draft(invoice_service)

    assert invoice.recognition_provider == "fake"
    assert invoice.recognition_is_fake is True
    assert invoice.recognized_at is not None
    assert invoice.raw_document_fields["is_fake"] is True


def test_unreadable_price_is_not_defaulted_to_zero(
    invoice_service: InvoiceService,
) -> None:
    """0 是「赠品」这种具体含义，「没认出来」不是。"""
    invoice = draft(invoice_service)
    second = line_by_no(invoice, 2)

    assert second.unit_price is None
    assert second.raw_fields["fields"]["unit_price"]["status"] == "unreadable"
    assert second.raw_fields["fields"]["unit_price"]["raw_text"] is None


def test_draft_keeps_the_raw_text_and_the_region(invoice_service: InvoiceService) -> None:
    """页码与区域留着，审核界面点一个字段才能跳到原图对应位置。"""
    first = line_by_no(draft(invoice_service), 1)
    evidence = first.raw_fields["fields"]["line_amount"]

    assert evidence["raw_text"] == "60.00"
    assert evidence["page"] == 1
    assert evidence["box"]["width"] > 0
    assert first.page == 1


def test_unparsable_document_fields_stay_empty_but_keep_the_raw_text(
    invoice_service: InvoiceService,
) -> None:
    """`FAKE 2026-11-09` / `FAKE 286.00` 解析不成日期与金额，但原文不能跟着丢。"""
    invoice = draft(invoice_service)

    assert invoice.invoice_date is None
    assert invoice.invoice_total is None
    fields = invoice.raw_document_fields["fields"]
    assert "2026-11-09" in fields["invoice_date"]["raw_text"]
    assert "286.00" in fields["invoice_total"]["raw_text"]


def test_currency_defaults_to_usd_when_missing(
    invoice_service: InvoiceService,
) -> None:
    """没有识别出币种时单据直接使用美元默认值，原件证据仍保留缺失状态。"""
    invoice = draft(invoice_service)

    assert invoice.currency == "USD"
    assert invoice.raw_document_fields["fields"]["currency"]["status"] == "missing"


def test_a_draft_can_be_created_without_recognition(
    invoice_service: InvoiceService,
) -> None:
    """真实 OCR 没接通之前最实在的一条路：存下照片，老板对着它手工加行。"""
    invoice = draft(invoice_service, recognize=False)

    assert invoice.lines == ()
    assert invoice.recognition_provider is None
    assert len(invoice.files) == 1


def test_manual_lines_are_numbered_after_the_recognized_ones(
    invoice_service: InvoiceService, food_item: FoodItemView
) -> None:
    invoice = draft(invoice_service)

    added = invoice_service.add_line(
        invoice.id,
        description="手写补的一行",
        quantity="3",
        purchase_unit="lb",
        unit_price="4.00",
        line_amount="12.00",
        food_item_id=food_item.id,
    )

    assert added.line_no == 3
    assert added.raw_fields is None
    assert added.missing_fields == ()


def test_recognized_lines_cannot_be_deleted(invoice_service: InvoiceService) -> None:
    """识别出来的行删掉就和原件对不上了；不该入账的行标成 skipped。"""
    invoice = draft(invoice_service)

    with pytest.raises(ConflictError) as error:
        invoice_service.delete_line(invoice.id, invoice.lines[0].id)

    assert "skipped" in error.value.message


def test_manual_lines_can_be_deleted(invoice_service: InvoiceService) -> None:
    invoice = draft(invoice_service)
    added = invoice_service.add_line(invoice.id, description="加错了")

    invoice_service.delete_line(invoice.id, added.id)

    assert [line.line_no for line in invoice_service.get_invoice(invoice.id).lines] == [
        1,
        2,
    ]


# --- 行状态 -----------------------------------------------------------------


def test_a_line_can_be_skipped_with_a_reason(invoice_service: InvoiceService) -> None:
    """签名、手写备注、认不出的内容：标成跳过并写一句原因。"""
    invoice = draft(invoice_service)

    skipped = invoice_service.update_line(
        invoice.id, invoice.lines[1].id, status="skipped", skip_reason="手写备注，不是食材"
    )

    assert skipped.status == "skipped"
    assert skipped.skip_reason == "手写备注，不是食材"


def test_unskipping_clears_the_reason(invoice_service: InvoiceService) -> None:
    """改回待确认还留着跳过原因，界面上就会显示一行自相矛盾的话。"""
    invoice = draft(invoice_service)
    line_id = invoice.lines[1].id
    invoice_service.update_line(invoice.id, line_id, status="skipped", skip_reason="备注")

    reopened = invoice_service.update_line(invoice.id, line_id, status="pending")

    assert reopened.status == "pending"
    assert reopened.skip_reason is None


def test_status_cannot_jump_to_confirmed_through_an_update(
    invoice_service: InvoiceService,
) -> None:
    """确认只有一个入口：确认接口。否则「确认」会有两套校验。"""
    invoice = draft(invoice_service)

    with pytest.raises(ValidationError) as error:
        invoice_service.update_line(invoice.id, invoice.lines[0].id, status="confirmed")

    assert error.value.details["allowed"] == ["pending", "skipped"]


def test_a_confirmed_line_cannot_be_edited_in_place(
    invoice_service: InvoiceService, food_item: FoodItemView
) -> None:
    """已入账的行原地改会让两处数字对不上：要么改采购记录，要么先撤销。"""
    invoice = draft(invoice_service)
    line_id = invoice.lines[0].id
    invoice_service.confirm_lines(
        invoice.id,
        options=[ConfirmOption(line_id=line_id, food_item_id=food_item.id)],
    )

    with pytest.raises(ConflictError) as error:
        invoice_service.update_line(invoice.id, line_id, quantity="99")

    assert "已经确认入账" in error.value.message
    assert error.value.details["purchase_id"] is not None


def test_version_mismatch_is_refused(invoice_service: InvoiceService) -> None:
    """两个标签页同时审同一行，后一个被拦住，而不是悄悄覆盖前一个的改动。"""
    invoice = draft(invoice_service)
    line = invoice.lines[0]
    invoice_service.update_line(invoice.id, line.id, quantity="5")

    with pytest.raises(ConflictError) as error:
        invoice_service.update_line(
            invoice.id, line.id, version=line.version, quantity="6"
        )

    assert error.value.details["expected_version"] == line.version
    assert error.value.details["current_version"] == line.version + 1


# --- 逐行确认（步骤 05 的验收核心）------------------------------------------


def test_confirming_two_of_three_lines_creates_exactly_two_purchases(
    invoice_service: InvoiceService,
    purchase_service: PurchaseService,
    food_item: FoodItemView,
    other_food_item: FoodItemView,
) -> None:
    """验收第一条：三行只确认两行时只有两条采购记录。"""
    invoice = draft(invoice_service)
    third = invoice_service.add_line(
        invoice.id,
        description="第三行",
        quantity="1",
        purchase_unit="case",
        unit_price="20.00",
        line_amount="20.00",
        food_item_id=other_food_item.id,
    )

    outcome = invoice_service.confirm_lines(
        invoice.id,
        options=[
            ConfirmOption(line_id=invoice.lines[0].id, food_item_id=food_item.id),
            ConfirmOption(line_id=third.id),
        ],
    )

    assert outcome.confirmed_count == 2
    assert purchase_service.search_purchases(limit=50).total == 2
    assert line_by_no(invoice_service.get_invoice(invoice.id), 2).status == "pending"


def test_the_pending_line_can_be_confirmed_later(
    invoice_service: InvoiceService,
    purchase_service: PurchaseService,
    food_item: FoodItemView,
    other_food_item: FoodItemView,
) -> None:
    """验收第二条：剩下那行可以明天再确认。"""
    invoice = draft(invoice_service)
    invoice_service.confirm_lines(
        invoice.id,
        options=[ConfirmOption(line_id=invoice.lines[0].id, food_item_id=food_item.id)],
    )

    invoice_service.update_line(invoice.id, invoice.lines[1].id, unit_price="113.00")
    outcome = invoice_service.confirm_lines(
        invoice.id,
        options=[
            ConfirmOption(line_id=invoice.lines[1].id, food_item_id=other_food_item.id)
        ],
    )

    assert outcome.confirmed_count == 1
    assert purchase_service.search_purchases(limit=50).total == 2


def test_confirming_twice_does_not_record_the_price_twice(
    invoice_service: InvoiceService,
    purchase_service: PurchaseService,
    food_item: FoodItemView,
) -> None:
    """验收第三条：重复确认不重复记价。"""
    invoice = draft(invoice_service)
    option = ConfirmOption(line_id=invoice.lines[0].id, food_item_id=food_item.id)
    first = invoice_service.confirm_lines(invoice.id, options=[option])

    second = invoice_service.confirm_lines(invoice.id, options=[option])

    assert first.results[0].ok is True
    assert second.results[0].ok is False
    assert second.results[0].error_code == "conflict"
    assert second.results[0].error_details["purchase_id"] == first.results[0].purchase_id
    assert purchase_service.search_purchases(limit=50).total == 1


def test_missing_values_block_confirmation_and_name_the_fields(
    invoice_service: InvoiceService, food_item: FoodItemView
) -> None:
    """认不出来的数字不会自动补 0，所以必须说清缺哪个字段。"""
    invoice = draft(invoice_service)

    outcome = invoice_service.confirm_lines(
        invoice.id,
        options=[ConfirmOption(line_id=invoice.lines[1].id, food_item_id=food_item.id)],
    )

    assert outcome.results[0].ok is False
    assert outcome.results[0].error_details["missing_fields"] == ["unit_price"]


def test_a_purchase_line_needs_a_food_item(invoice_service: InvoiceService) -> None:
    invoice = draft(invoice_service)

    outcome = invoice_service.confirm_lines(
        invoice.id, options=[ConfirmOption(line_id=invoice.lines[0].id)]
    )

    assert outcome.results[0].error_details["missing_fields"] == ["food_item_id"]


def test_an_adjustment_line_needs_no_food_item(
    invoice_service: InvoiceService, purchase_service: PurchaseService
) -> None:
    """整单折让在单据上不指向任何一种食材（步骤 04 的调整行）。"""
    invoice = draft(invoice_service, recognize=False)
    discount = invoice_service.add_line(
        invoice.id,
        description="本单让 $20",
        line_kind="adjustment",
        quantity="1",
        purchase_unit="each",
        unit_price="-20.00",
        line_amount="-20.00",
    )

    outcome = invoice_service.confirm_lines(
        invoice.id, options=[ConfirmOption(line_id=discount.id)]
    )

    assert outcome.results[0].ok is True
    assert purchase_service.search_purchases(limit=50, kind="adjustment").total == 1
    assert purchase_service.search_purchases(limit=50).total == 0


def test_amount_mismatch_needs_an_explicit_confirmation(
    invoice_service: InvoiceService, food_item: FoodItemView
) -> None:
    """口径与步骤 04 完全一致：差额超 $0.05 要老板核对单据后显式接受。"""
    invoice = draft(invoice_service)
    line_id = invoice.lines[0].id
    invoice_service.update_line(invoice.id, line_id, line_amount="75.00")

    refused = invoice_service.confirm_lines(
        invoice.id, options=[ConfirmOption(line_id=line_id, food_item_id=food_item.id)]
    )
    accepted = invoice_service.confirm_lines(
        invoice.id,
        options=[
            ConfirmOption(
                line_id=line_id,
                food_item_id=food_item.id,
                accept_amount_mismatch=True,
            )
        ],
    )

    assert refused.results[0].error_code == "amount_mismatch"
    assert refused.results[0].error_details["expected"] == "60.00"
    assert accepted.results[0].ok is True


def test_supplier_rounding_passes_without_asking(
    invoice_service: InvoiceService, food_item: FoodItemView
) -> None:
    """11.3 × $6.35 = $71.755，单据上印 $71.76：几分钱的舍入不该去烦老板。"""
    invoice = draft(invoice_service, recognize=False)
    line = invoice_service.add_line(
        invoice.id,
        quantity="11.3",
        purchase_unit="lb",
        unit_price="6.35",
        line_amount="71.76",
        food_item_id=food_item.id,
    )

    outcome = invoice_service.confirm_lines(
        invoice.id, options=[ConfirmOption(line_id=line.id)]
    )

    assert outcome.results[0].ok is True


def test_one_failing_line_does_not_roll_back_the_others(
    invoice_service: InvoiceService,
    purchase_service: PurchaseService,
    food_item: FoodItemView,
) -> None:
    """`imp_notes.md` 4.7：不能因为一两行模糊就卡住其它正确的行。"""
    invoice = draft(invoice_service)

    outcome = invoice_service.confirm_lines(
        invoice.id,
        options=[
            ConfirmOption(line_id=invoice.lines[0].id, food_item_id=food_item.id),
            ConfirmOption(line_id=invoice.lines[1].id, food_item_id=food_item.id),
        ],
    )

    assert (outcome.confirmed_count, outcome.failed_count) == (1, 1)
    assert purchase_service.search_purchases(limit=50).total == 1


def test_confirm_all_takes_every_pending_line(
    invoice_service: InvoiceService,
    purchase_service: PurchaseService,
    food_item: FoodItemView,
) -> None:
    """「全部确认」：前端把预填好的食材逐行带上来，老板点一下。"""
    invoice = draft(invoice_service)
    for line in invoice.lines:
        invoice_service.update_line(invoice.id, line.id, food_item_id=food_item.id)
    # 第二行的单价认不出来，补上（2 case × $113 = $226，与行金额自洽）。
    invoice_service.update_line(invoice.id, invoice.lines[1].id, unit_price="113.00")

    outcome = invoice_service.confirm_lines(invoice.id)

    assert outcome.confirmed_count == 2
    assert purchase_service.search_purchases(limit=50).total == 2


def test_confirm_all_on_a_finished_invoice_is_refused(
    invoice_service: InvoiceService, food_item: FoodItemView
) -> None:
    invoice = draft(invoice_service, recognize=False)
    line = invoice_service.add_line(
        invoice.id,
        quantity="1",
        purchase_unit="lb",
        unit_price="1.00",
        line_amount="1.00",
        food_item_id=food_item.id,
    )
    invoice_service.confirm_lines(invoice.id, options=[ConfirmOption(line_id=line.id)])

    with pytest.raises(ValidationError) as error:
        invoice_service.confirm_lines(invoice.id)

    assert "没有待确认的行" in error.value.message


def test_a_skipped_line_must_be_reopened_before_confirming(
    invoice_service: InvoiceService, food_item: FoodItemView
) -> None:
    invoice = draft(invoice_service)
    line_id = invoice.lines[0].id
    invoice_service.update_line(invoice.id, line_id, status="skipped", skip_reason="备注")

    outcome = invoice_service.confirm_lines(
        invoice.id, options=[ConfirmOption(line_id=line_id, food_item_id=food_item.id)]
    )

    assert outcome.results[0].error_code == "validation_error"
    assert "改回待确认" in outcome.results[0].error_message


# --- 当场新建食材 -----------------------------------------------------------


def test_a_food_item_can_be_created_while_confirming(
    invoice_service: InvoiceService, ingredient_service: IngredientService
) -> None:
    """老板遇到数据库里没有的货，不用跳到食材页面来回切。"""
    invoice = draft(invoice_service)

    outcome = invoice_service.confirm_lines(
        invoice.id,
        options=[
            ConfirmOption(
                line_id=invoice.lines[0].id,
                new_food_item={
                    "name": "Jasmine Rice",
                    "name_zh": "香米",
                    "category_id": 5,
                    "base_unit": "lb",
                },
            )
        ],
    )

    assert outcome.results[0].ok is True
    found = ingredient_service.search_food_items(query="Jasmine Rice")
    assert found.total == 1
    line = line_by_no(invoice_service.get_invoice(invoice.id), 1)
    assert line.food_item.name == "Jasmine Rice"


@pytest.mark.parametrize("unit, expected", [("lbs", "lb"), ("case", "case")])
def test_first_purchase_uses_its_unit_when_creating_food_item(
    invoice_service: InvoiceService, ingredient_service: IngredientService,
    unit: str, expected: str,
) -> None:
    invoice = draft(invoice_service)
    line_id = invoice.lines[0].id
    invoice_service.update_line(invoice.id, line_id, purchase_unit=unit)

    outcome = invoice_service.confirm_lines(invoice.id, options=[ConfirmOption(
        line_id=line_id,
        new_food_item={"name": f"First {unit}", "category_id": 5,
                       "use_purchase_unit_as_base": True},
    )])

    assert outcome.results[0].ok is True
    assert ingredient_service.search_food_items(query=f"First {unit}").items[0].base_unit == expected


def test_first_purchase_reuses_exact_existing_food_without_category(
    invoice_service: InvoiceService, food_item: FoodItemView,
) -> None:
    invoice = draft(invoice_service)
    outcome = invoice_service.confirm_lines(invoice.id, options=[ConfirmOption(
        line_id=invoice.lines[0].id,
        new_food_item={"name": food_item.name, "use_purchase_unit_as_base": True},
    )])
    assert outcome.results[0].ok is True
    assert invoice_service.get_invoice(invoice.id).lines[0].food_item.id == food_item.id


def test_unrecognized_first_purchase_unit_does_not_leave_food_item(
    invoice_service: InvoiceService, ingredient_service: IngredientService,
) -> None:
    invoice = draft(invoice_service)
    line_id = invoice.lines[0].id
    invoice_service.update_line(invoice.id, line_id, purchase_unit="mystery")
    outcome = invoice_service.confirm_lines(invoice.id, options=[ConfirmOption(
        line_id=line_id,
        new_food_item={"name": "Unknown Pack", "category_id": 5,
                       "use_purchase_unit_as_base": True},
    )])
    assert outcome.results[0].ok is False
    assert ingredient_service.search_food_items(query="Unknown Pack").total == 0


def test_a_failed_confirmation_leaves_no_food_item_behind(
    invoice_service: InvoiceService,
    ingredient_service: IngredientService,
    purchase_service: PurchaseService,
) -> None:
    """验收第四条：写入失败不留下半条正式记录——新建的食材也要跟着回滚。"""
    invoice = draft(invoice_service)
    line_id = invoice.lines[0].id
    invoice_service.update_line(invoice.id, line_id, line_amount="999.00")

    outcome = invoice_service.confirm_lines(
        invoice.id,
        options=[
            ConfirmOption(
                line_id=line_id,
                new_food_item={
                    "name": "Never Saved Rice",
                    "category_id": 5,
                    "base_unit": "lb",
                },
            )
        ],
    )

    assert outcome.results[0].error_code == "amount_mismatch"
    assert ingredient_service.search_food_items(query="Never Saved Rice").total == 0
    assert purchase_service.search_purchases(limit=50).total == 0


def test_new_food_item_needs_name_category_and_unit(
    invoice_service: InvoiceService,
) -> None:
    invoice = draft(invoice_service)

    outcome = invoice_service.confirm_lines(
        invoice.id,
        options=[
            ConfirmOption(line_id=invoice.lines[0].id, new_food_item={"name": "Rice"})
        ],
    )

    assert outcome.results[0].error_details["missing_fields"] == [
        "base_unit",
        "category_id",
    ]


def test_new_food_item_and_food_item_id_cannot_both_be_given(
    invoice_service: InvoiceService, food_item: FoodItemView
) -> None:
    invoice = draft(invoice_service)

    outcome = invoice_service.confirm_lines(
        invoice.id,
        options=[
            ConfirmOption(
                line_id=invoice.lines[0].id,
                food_item_id=food_item.id,
                new_food_item={"name": "X", "category_id": 1, "base_unit": "lb"},
            )
        ],
    )

    assert "只能给一个" in outcome.results[0].error_message


def test_a_duplicate_name_points_at_the_existing_food_item(
    invoice_service: InvoiceService, food_item: FoodItemView
) -> None:
    """重名按步骤 03 的口径返回 409 并指向已存在那条，由前端改用它。"""
    invoice = draft(invoice_service)

    outcome = invoice_service.confirm_lines(
        invoice.id,
        options=[
            ConfirmOption(
                line_id=invoice.lines[0].id,
                new_food_item={
                    "name": food_item.name,
                    "category_id": 1,
                    "base_unit": "lb",
                },
            )
        ],
    )

    assert outcome.results[0].error_code == "conflict"
    assert outcome.results[0].error_details["existing"]["id"] == food_item.id


# --- 继承、入账口径与追溯 ---------------------------------------------------


def test_confirmed_line_inherits_the_invoice_header(
    invoice_service: InvoiceService,
    purchase_service: PurchaseService,
    food_item: FoodItemView,
) -> None:
    """日期、来源与币种在确认那一刻从单据头继承下来。"""
    invoice = draft(invoice_service)
    invoice_service.update_invoice(
        invoice.id, source_name="Restaurant Depot", invoice_date="2026-11-08", currency="USD"
    )

    outcome = invoice_service.confirm_lines(
        invoice.id,
        options=[ConfirmOption(line_id=invoice.lines[0].id, food_item_id=food_item.id)],
    )

    purchase = purchase_service.get_purchase(outcome.results[0].purchase_id)
    assert purchase.source_name == "Restaurant Depot"
    assert purchase.purchase_date.isoformat() == "2026-11-08"
    assert purchase.currency == "USD"
    assert purchase.entry_source == "invoice"
    assert purchase.original_description == invoice.lines[0].description


def test_a_line_override_beats_the_invoice_header(
    invoice_service: InvoiceService,
    purchase_service: PurchaseService,
    food_item: FoodItemView,
) -> None:
    """一张单上偶尔有一行是别的日期或别的来源。"""
    invoice = draft(invoice_service)
    invoice_service.update_invoice(
        invoice.id, source_name="Costco", invoice_date="2026-11-08"
    )
    invoice_service.update_line(
        invoice.id,
        invoice.lines[0].id,
        source_name="本地肉铺",
        purchase_date="2026-11-07",
        food_item_id=food_item.id,
    )

    outcome = invoice_service.confirm_lines(
        invoice.id, options=[ConfirmOption(line_id=invoice.lines[0].id)]
    )

    purchase = purchase_service.get_purchase(outcome.results[0].purchase_id)
    assert purchase.source_name == "本地肉铺"
    assert purchase.purchase_date.isoformat() == "2026-11-07"


def test_currency_falls_back_to_usd(
    invoice_service: InvoiceService,
    purchase_service: PurchaseService,
    food_item: FoodItemView,
) -> None:
    """单据默认 USD，确认后采购记录继承这个币种。"""
    invoice = draft(invoice_service)
    assert invoice.currency == "USD"

    outcome = invoice_service.confirm_lines(
        invoice.id,
        options=[ConfirmOption(line_id=invoice.lines[0].id, food_item_id=food_item.id)],
    )

    assert purchase_service.get_purchase(outcome.results[0].purchase_id).currency == "USD"


def test_invoice_currency_can_be_changed_to_cny_before_confirmation(
    invoice_service: InvoiceService,
    purchase_service: PurchaseService,
    food_item: FoodItemView,
) -> None:
    invoice = draft(invoice_service)
    changed = invoice_service.update_invoice(invoice.id, currency="CNY")
    assert changed.currency == "CNY"

    outcome = invoice_service.confirm_lines(
        invoice.id,
        options=[ConfirmOption(line_id=invoice.lines[0].id, food_item_id=food_item.id)],
    )
    assert purchase_service.get_purchase(outcome.results[0].purchase_id).currency == "CNY"


def test_an_undated_invoice_lands_on_the_undated_worklist(
    invoice_service: InvoiceService,
    purchase_service: PurchaseService,
    food_item: FoodItemView,
) -> None:
    """识别不出日期就留空，但必须能被单独筛出来（步骤 04 的「待补日期」清单）。"""
    invoice = draft(invoice_service)

    invoice_service.confirm_lines(
        invoice.id,
        options=[ConfirmOption(line_id=invoice.lines[0].id, food_item_id=food_item.id)],
    )

    assert purchase_service.search_purchases(undated=True, limit=50).total == 1


def test_changing_the_header_later_does_not_touch_confirmed_purchases(
    invoice_service: InvoiceService,
    purchase_service: PurchaseService,
    food_item: FoodItemView,
) -> None:
    """继承只发生在确认那一刻；之后改单据头走 PATCH /api/purchases（有修改日志）。"""
    invoice = draft(invoice_service)
    invoice_service.update_invoice(invoice.id, source_name="Costco")
    outcome = invoice_service.confirm_lines(
        invoice.id,
        options=[ConfirmOption(line_id=invoice.lines[0].id, food_item_id=food_item.id)],
    )

    invoice_service.update_invoice(invoice.id, source_name="Restaurant Depot")

    purchase = purchase_service.get_purchase(outcome.results[0].purchase_id)
    assert purchase.source_name == "Costco"
    assert purchase.revision_count == 0


def test_a_purchase_points_back_at_the_original_document(
    invoice_service: InvoiceService,
    purchase_service: PurchaseService,
    food_item: FoodItemView,
) -> None:
    """验收第五条：能根据采购记录查到原始单据。"""
    invoice = draft(invoice_service)
    outcome = invoice_service.confirm_lines(
        invoice.id,
        options=[ConfirmOption(line_id=invoice.lines[0].id, food_item_id=food_item.id)],
    )

    purchase = purchase_service.get_purchase(outcome.results[0].purchase_id)

    assert purchase.source_invoice.invoice_id == invoice.id
    assert purchase.source_invoice.invoice_line_id == invoice.lines[0].id
    assert purchase.source_invoice.line_no == 1
    # 原始文件还能打开核对
    _, stream = invoice_service.open_file(
        purchase.source_invoice.invoice_id, invoice.files[0].id
    )
    with stream:
        assert stream.read() == PHOTO


def test_a_manual_purchase_has_no_source_invoice(
    purchase_service: PurchaseService, food_item: FoodItemView
) -> None:
    """手工录入与表格导入没有单据，`source_invoice` 是 null 而不是编一个。"""
    purchase = purchase_service.create_purchase(
        food_item_id=food_item.id,
        quantity="1",
        purchase_unit="lb",
        unit_price="5.00",
        line_amount="5.00",
    )

    assert purchase.source_invoice is None


def test_raw_fields_never_change_after_edits_and_confirmation(
    invoice_service: InvoiceService, food_item: FoodItemView
) -> None:
    """原始识别值是单据事实：老板把数字改对了，它也不动。"""
    invoice = draft(invoice_service)
    line = invoice.lines[0]
    original = json.dumps(line.raw_fields, ensure_ascii=False, sort_keys=True)

    invoice_service.update_line(
        invoice.id, line.id, quantity="12", unit_price="7.00", line_amount="84.00"
    )
    invoice_service.confirm_lines(
        invoice.id, options=[ConfirmOption(line_id=line.id, food_item_id=food_item.id)]
    )

    after = line_by_no(invoice_service.get_invoice(invoice.id), 1)
    assert json.dumps(after.raw_fields, ensure_ascii=False, sort_keys=True) == original


def test_a_food_item_used_by_an_invoice_line_cannot_be_deleted(
    invoice_service: InvoiceService,
    ingredient_service: IngredientService,
    food_item: FoodItemView,
) -> None:
    """发票草稿也算引用：删掉食材会让草稿指向不存在的记录。"""
    invoice = draft(invoice_service)
    invoice_service.update_line(invoice.id, invoice.lines[0].id, food_item_id=food_item.id)

    with pytest.raises(ConflictError) as error:
        ingredient_service.delete_food_item(food_item.id)

    assert "发票" in error.value.message


# --- 撤销确认 ---------------------------------------------------------------


def test_unconfirm_voids_the_purchase_and_reopens_the_line(
    invoice_service: InvoiceService,
    purchase_service: PurchaseService,
    food_item: FoodItemView,
) -> None:
    """撤销 = 作废采购记录 + 行回到待确认；记录仍可追溯，不是硬删。"""
    invoice = draft(invoice_service)
    outcome = invoice_service.confirm_lines(
        invoice.id,
        options=[ConfirmOption(line_id=invoice.lines[0].id, food_item_id=food_item.id)],
    )
    purchase_id = outcome.results[0].purchase_id

    reopened = invoice_service.unconfirm_line(invoice.id, invoice.lines[0].id)

    assert reopened.status == "pending"
    assert reopened.purchase_id is None
    assert purchase_service.search_purchases(limit=50).total == 0
    assert purchase_service.search_purchases(limit=50, include_voided=True).total == 1
    voided = purchase_service.get_purchase(purchase_id)
    assert voided.voided_at is not None
    assert voided.revision_count == 1


def test_unconfirm_then_confirm_again_is_allowed(
    invoice_service: InvoiceService,
    purchase_service: PurchaseService,
    food_item: FoodItemView,
    other_food_item: FoodItemView,
) -> None:
    """局部唯一索引只约束未作废的记录，所以撤销之后能重新确认（换对食材）。"""
    invoice = draft(invoice_service)
    line_id = invoice.lines[0].id
    invoice_service.confirm_lines(
        invoice.id, options=[ConfirmOption(line_id=line_id, food_item_id=food_item.id)]
    )
    invoice_service.unconfirm_line(invoice.id, line_id)

    again = invoice_service.confirm_lines(
        invoice.id,
        options=[ConfirmOption(line_id=line_id, food_item_id=other_food_item.id)],
    )

    assert again.results[0].ok is True
    active = purchase_service.search_purchases(limit=50)
    assert active.total == 1
    assert active.items[0].food_item.id == other_food_item.id


def test_unconfirming_a_pending_line_is_a_conflict(
    invoice_service: InvoiceService,
) -> None:
    invoice = draft(invoice_service)

    with pytest.raises(ConflictError) as error:
        invoice_service.unconfirm_line(invoice.id, invoice.lines[0].id)

    assert error.value.details["status"] == "pending"


# --- 行匹配 -----------------------------------------------------------------


def test_history_gives_a_candidate_but_does_not_confirm_anything(
    invoice_service: InvoiceService, food_item: FoodItemView
) -> None:
    """命中历史只预填候选：行上的食材仍然是空的，确认必须带上选中的那个。"""
    first = draft(invoice_service)
    invoice_service.confirm_lines(
        first.id,
        options=[ConfirmOption(line_id=first.lines[0].id, food_item_id=food_item.id)],
    )

    second = invoice_service.create_invoice([upload(content=OTHER_PHOTO, name="b.png")])
    line = line_by_no(second, 1)

    assert line.food_item is None
    assert [c.food_item.id for c in line.match_candidates] == [food_item.id]
    assert line.match_candidates[0].purchase_count == 1
    outcome = invoice_service.confirm_lines(
        second.id, options=[ConfirmOption(line_id=line.id)]
    )
    assert outcome.results[0].error_details["missing_fields"] == ["food_item_id"]


def test_the_same_source_candidate_ranks_first(
    invoice_service: InvoiceService,
    purchase_service: PurchaseService,
    food_item: FoodItemView,
    other_food_item: FoodItemView,
) -> None:
    """同一家供应商的同一句描述几乎一定是同一样东西，所以它排前面。"""
    description = "CHIX BRST BNL"
    purchase_service.create_purchase(
        food_item_id=other_food_item.id,
        quantity="1",
        purchase_unit="lb",
        unit_price="1.00",
        line_amount="1.00",
        original_description=description,
        source_name="Other Supplier",
    )
    purchase_service.create_purchase(
        food_item_id=food_item.id,
        quantity="1",
        purchase_unit="lb",
        unit_price="1.00",
        line_amount="1.00",
        original_description=description,
        source_name="Costco",
    )

    candidates = invoice_service.match_candidates_for(description, source_name="costco")

    assert [c.food_item.id for c in candidates] == [food_item.id, other_food_item.id]
    assert candidates[0].same_source is True
    assert candidates[1].same_source is False


def test_matching_ignores_case_and_spacing(
    invoice_service: InvoiceService,
    purchase_service: PurchaseService,
    food_item: FoodItemView,
) -> None:
    purchase_service.create_purchase(
        food_item_id=food_item.id,
        quantity="1",
        purchase_unit="lb",
        unit_price="1.00",
        line_amount="1.00",
        original_description="CHIX BRST BNL",
    )

    candidates = invoice_service.match_candidates_for("  chix   brst   bnl ")

    assert [c.food_item.id for c in candidates] == [food_item.id]


def test_matching_ignores_voided_and_adjustment_history(
    invoice_service: InvoiceService,
    purchase_service: PurchaseService,
    food_item: FoodItemView,
) -> None:
    """作废的记录是老板明确否掉的，调整行不代表「这句描述是这个食材」。"""
    description = "RETURN CREDIT"
    voided = purchase_service.create_purchase(
        food_item_id=food_item.id,
        quantity="1",
        purchase_unit="lb",
        unit_price="1.00",
        line_amount="1.00",
        original_description=description,
    )
    purchase_service.void_purchase(voided.id)
    purchase_service.create_purchase(
        line_kind="adjustment",
        food_item_id=food_item.id,
        quantity="1",
        purchase_unit="lb",
        unit_price="-1.00",
        line_amount="-1.00",
        original_description=description,
    )

    assert invoice_service.match_candidates_for(description) == ()


def test_an_unknown_description_has_no_candidates(
    invoice_service: InvoiceService,
) -> None:
    assert invoice_service.match_candidates_for("从没见过的描述") == ()


# --- 行匹配的模糊层（步骤 10.4）--------------------------------------------


def _remember(
    purchase_service: PurchaseService,
    food_item_id: int,
    description: str,
    *,
    source_name: str | None = None,
) -> None:
    """让系统"记住"一次纠正：这句描述被确认成了这条食材。"""
    purchase_service.create_purchase(
        food_item_id=food_item_id,
        quantity="1",
        purchase_unit="lb",
        unit_price="1.00",
        line_amount="1.00",
        original_description=description,
        source_name=source_name,
    )


def test_the_same_item_written_with_and_without_its_pack_spec_matches(
    invoice_service: InvoiceService,
    purchase_service: PurchaseService,
    food_item: FoodItemView,
) -> None:
    """真实实例：NewMoys 的同一个商品，一次写了箱规一次没写。

    `samples/real/ground_truth/04_NewMoys_Meat_5p__p04.json` 的描述是
    `Beef Stomach - 牛肚6X44.10`，而同一家另一页写成不带箱规的形式。精确命中
    对不上，模糊层要能对上，并且**说清楚命中的是历史上哪一句**。
    """
    _remember(purchase_service, food_item.id, "Beef Stomach - 牛肚6X44.10")

    candidates = invoice_service.match_candidates_for("Beef Stomach - 牛肚")

    assert [c.food_item.id for c in candidates] == [food_item.id]
    assert candidates[0].match_reason == "description_fuzzy"
    assert candidates[0].matched_description == "beef stomach - 牛肚6x44.10"
    # ⚠️ 分数是 **1.0**，而理由仍然是 `description_fuzzy`——两列说的不是同一件事：
    # 理由说的是**哪一层找到的**（原文不同，所以走索引的精确层对不上），
    # 分数说的是**内容有多像**（剥掉包装规格之后，剩下的词完全相同）。
    # 这两列都要给界面：只给分数会让这条看起来和精确命中一样确定。
    assert candidates[0].score == 1.0


def test_a_punctuation_only_difference_is_still_an_exact_memory(
    invoice_service: InvoiceService,
    purchase_service: PurchaseService,
    food_item: FoodItemView,
) -> None:
    """⚠️ `CHIX-BRST` 与 `CHIX BRST` 规范化后**不相同**（连字符留着）。

    05.6 那层精确命中因此对不上，这正是步骤 10 要补的那一类。
    """
    _remember(purchase_service, food_item.id, "CHIX BRST")

    candidates = invoice_service.match_candidates_for("CHIX-BRST")

    assert [c.food_item.id for c in candidates] == [food_item.id]
    assert candidates[0].match_reason == "description_fuzzy"


def test_an_exact_memory_suppresses_the_fuzzy_layer_entirely(
    invoice_service: InvoiceService,
    purchase_service: PurchaseService,
    food_item: FoodItemView,
    other_food_item: FoodItemView,
) -> None:
    """⚠️ 精确命中过的描述是确定答案，旁边再掺几条"像的"只是噪声。"""
    _remember(purchase_service, food_item.id, "Beef Stomach - 牛肚")
    _remember(purchase_service, other_food_item.id, "Beef Stomach - 牛肚6X44.10")

    candidates = invoice_service.match_candidates_for("Beef Stomach - 牛肚")

    assert [c.food_item.id for c in candidates] == [food_item.id]
    assert candidates[0].match_reason == "description_exact"
    assert candidates[0].score == 1.0
    assert candidates[0].matched_description is None


def test_a_shorter_description_does_not_match_a_longer_one_containing_it(
    invoice_service: InvoiceService,
    purchase_service: PurchaseService,
    food_item: FoodItemView,
) -> None:
    """⚠️ `Pork - CT` 与 `Pork Spare Rib(Cube)` 在 NewMoys 的 5 页上都出现过，
    **各有自己的价格**。把它们当成一条食材会让两个价格进同一个加权均价。"""
    _remember(purchase_service, food_item.id, "Pork Spare Rib(Cube) - 靓排粒")

    assert invoice_service.match_candidates_for("Pork - CT") == ()


def test_a_description_with_no_content_words_gets_no_candidate(
    invoice_service: InvoiceService,
    purchase_service: PurchaseService,
    food_item: FoodItemView,
) -> None:
    """全是数字与单位的描述比什么都不像；给一条"分数最高的"凑数，等于让老板把
    一行采购挂到一个随机食材上。"""
    _remember(purchase_service, food_item.id, "Beef Stomach - 牛肚")

    assert invoice_service.match_candidates_for("1000g x 10") == ()


def test_the_fuzzy_layer_prefers_the_closest_description_then_the_same_source(
    invoice_service: InvoiceService,
    purchase_service: PurchaseService,
    food_item: FoodItemView,
    other_food_item: FoodItemView,
) -> None:
    """模糊层按"有多像"排在前，与精确层（所有候选都是 1.0）不同。"""
    _remember(
        purchase_service,
        other_food_item.id,
        "Beef Stomach Trim - 牛肚边",
        source_name="Other Supplier",
    )
    _remember(
        purchase_service,
        food_item.id,
        "Beef Stomach - 牛肚6X44.10",
        source_name="NewMoys",
    )

    candidates = invoice_service.match_candidates_for(
        "Beef Stomach - 牛肚", source_name="newmoys"
    )

    assert candidates[0].food_item.id == food_item.id
    assert candidates[0].same_source is True
    assert candidates[0].score is not None
    assert all(c.match_reason == "description_fuzzy" for c in candidates)


def test_the_fuzzy_layer_ignores_voided_and_adjustment_history(
    invoice_service: InvoiceService,
    purchase_service: PurchaseService,
    food_item: FoodItemView,
) -> None:
    """与精确层同一组过滤条件：换一套条件就等于让同一件事在两处有两个答案。"""
    voided = purchase_service.create_purchase(
        food_item_id=food_item.id,
        quantity="1",
        purchase_unit="lb",
        unit_price="1.00",
        line_amount="1.00",
        original_description="Beef Stomach - 牛肚6X44.10",
    )
    purchase_service.void_purchase(voided.id)

    assert invoice_service.match_candidates_for("Beef Stomach - 牛肚") == ()


# --- 对账与收口确认 ---------------------------------------------------------


def test_reconciliation_waits_until_every_line_is_handled(
    invoice_service: InvoiceService, food_item: FoodItemView
) -> None:
    """还有待确认的行时不对账——算了只会报一堆假警。"""
    invoice = draft(invoice_service)
    invoice_service.update_invoice(invoice.id, invoice_total="286.00")
    invoice_service.confirm_lines(
        invoice.id,
        options=[ConfirmOption(line_id=invoice.lines[0].id, food_item_id=food_item.id)],
    )

    reconciliation = invoice_service.get_invoice(invoice.id).reconciliation

    assert reconciliation.status == RECONCILIATION_NOT_CHECKED
    assert reconciliation.difference is None
    assert reconciliation.pending_count == 1


def test_reconciliation_says_no_total_instead_of_zero_difference(
    invoice_service: InvoiceService, food_item: FoodItemView
) -> None:
    """手写单常常没有总额：那是「没法对」，不是「差 0 元」。"""
    invoice = draft(invoice_service)
    invoice_service.update_line(
        invoice.id, invoice.lines[1].id, status="skipped", skip_reason="认不出"
    )
    invoice_service.confirm_lines(
        invoice.id,
        options=[ConfirmOption(line_id=invoice.lines[0].id, food_item_id=food_item.id)],
    )

    reconciliation = invoice_service.get_invoice(invoice.id).reconciliation

    assert reconciliation.status == RECONCILIATION_NO_TOTAL
    assert reconciliation.difference is None
    assert reconciliation.confirmed_amount == Decimal("60.00")


def test_reconciliation_balances_when_the_lines_add_up(
    invoice_service: InvoiceService, food_item: FoodItemView, other_food_item: FoodItemView
) -> None:
    invoice = draft(invoice_service)
    invoice_service.update_invoice(invoice.id, invoice_total="286.00")
    invoice_service.update_line(invoice.id, invoice.lines[1].id, unit_price="113.00")
    invoice_service.confirm_lines(
        invoice.id,
        options=[
            ConfirmOption(line_id=invoice.lines[0].id, food_item_id=food_item.id),
            ConfirmOption(line_id=invoice.lines[1].id, food_item_id=other_food_item.id),
        ],
    )

    reconciliation = invoice_service.get_invoice(invoice.id).reconciliation

    assert reconciliation.status == RECONCILIATION_BALANCED
    assert reconciliation.confirmed_amount == Decimal("286.00")
    assert reconciliation.difference == Decimal("0.00")


def test_a_missing_line_shows_up_as_a_mismatch_but_blocks_nothing(
    invoice_service: InvoiceService, food_item: FoodItemView
) -> None:
    """漏行的主要兜底：各行之和对不上单据总额就提示，但不阻止完成。"""
    invoice = draft(invoice_service)
    invoice_service.update_invoice(invoice.id, invoice_total="286.00")
    invoice_service.update_line(
        invoice.id, invoice.lines[1].id, status="skipped", skip_reason="认不出"
    )
    invoice_service.confirm_lines(
        invoice.id,
        options=[ConfirmOption(line_id=invoice.lines[0].id, food_item_id=food_item.id)],
    )

    completed = invoice_service.complete_invoice(invoice.id)

    assert completed.status == INVOICE_STATUS_COMPLETED
    assert completed.reconciliation.status == RECONCILIATION_MISMATCH
    assert completed.reconciliation.difference == Decimal("226.00")


def test_adjustment_lines_count_towards_the_invoice_total(
    invoice_service: InvoiceService, food_item: FoodItemView
) -> None:
    """退货 / 折让行留在库里就是为了对账：$60 − $20 = $40。"""
    invoice = draft(invoice_service, recognize=False)
    invoice_service.update_invoice(invoice.id, invoice_total="40.00")
    goods = invoice_service.add_line(
        invoice.id,
        quantity="10",
        purchase_unit="lb",
        unit_price="6.00",
        line_amount="60.00",
        food_item_id=food_item.id,
    )
    discount = invoice_service.add_line(
        invoice.id,
        description="本单让 $20",
        line_kind="adjustment",
        quantity="1",
        purchase_unit="each",
        unit_price="-20.00",
        line_amount="-20.00",
    )

    invoice_service.confirm_lines(
        invoice.id,
        options=[ConfirmOption(line_id=goods.id), ConfirmOption(line_id=discount.id)],
    )

    reconciliation = invoice_service.get_invoice(invoice.id).reconciliation
    assert reconciliation.confirmed_amount == Decimal("40.00")
    assert reconciliation.status == RECONCILIATION_BALANCED


def test_completing_needs_every_line_handled(
    invoice_service: InvoiceService, food_item: FoodItemView
) -> None:
    """还有行没处理就不是"做完了"；报错里要指出是哪几行。"""
    invoice = draft(invoice_service)
    invoice_service.confirm_lines(
        invoice.id,
        options=[ConfirmOption(line_id=invoice.lines[0].id, food_item_id=food_item.id)],
    )

    with pytest.raises(ConflictError) as error:
        invoice_service.complete_invoice(invoice.id)

    assert error.value.details["pending_line_nos"] == [2]


def test_an_empty_invoice_cannot_be_completed(invoice_service: InvoiceService) -> None:
    invoice = draft(invoice_service, recognize=False)

    with pytest.raises(ConflictError) as error:
        invoice_service.complete_invoice(invoice.id)

    assert "还没有任何行" in error.value.message


def test_status_goes_draft_in_review_ready_completed(
    invoice_service: InvoiceService, food_item: FoodItemView, other_food_item: FoodItemView
) -> None:
    """单据状态由行状态派生，`ready_to_complete` 与 `completed` 分开。"""
    invoice = draft(invoice_service)
    assert invoice.status == INVOICE_STATUS_DRAFT

    invoice_service.confirm_lines(
        invoice.id,
        options=[ConfirmOption(line_id=invoice.lines[0].id, food_item_id=food_item.id)],
    )
    assert invoice_service.get_invoice(invoice.id).status == INVOICE_STATUS_IN_REVIEW

    invoice_service.update_line(
        invoice.id, invoice.lines[1].id, status="skipped", skip_reason="备注"
    )
    assert invoice_service.get_invoice(invoice.id).status == INVOICE_STATUS_READY

    assert invoice_service.complete_invoice(invoice.id).status == INVOICE_STATUS_COMPLETED


def test_unconfirming_after_completion_reopens_the_invoice(
    invoice_service: InvoiceService, food_item: FoodItemView
) -> None:
    """收口确认是对某个状态的确认：行变了就得重新确认一次，不能停留在过期判断上。"""
    invoice = draft(invoice_service)
    invoice_service.update_line(
        invoice.id, invoice.lines[1].id, status="skipped", skip_reason="备注"
    )
    invoice_service.confirm_lines(
        invoice.id,
        options=[ConfirmOption(line_id=invoice.lines[0].id, food_item_id=food_item.id)],
    )
    invoice_service.complete_invoice(invoice.id)

    invoice_service.unconfirm_line(invoice.id, invoice.lines[0].id)

    assert invoice_service.get_invoice(invoice.id).completed_at is None


# --- 作废与列表 -------------------------------------------------------------


def test_an_invoice_with_confirmed_lines_cannot_be_voided(
    invoice_service: InvoiceService, food_item: FoodItemView
) -> None:
    """已入账的采购记录会指回这张单据，直接作废会留下指向作废单据的记录。"""
    invoice = draft(invoice_service)
    invoice_service.confirm_lines(
        invoice.id,
        options=[ConfirmOption(line_id=invoice.lines[0].id, food_item_id=food_item.id)],
    )

    with pytest.raises(ConflictError) as error:
        invoice_service.void_invoice(invoice.id)

    assert error.value.details["confirmed_line_nos"] == [1]


def test_confirmed_invoice_can_be_removed_with_explicit_current_line_ids(
    invoice_service: InvoiceService, purchase_service: PurchaseService,
    food_item: FoodItemView,
) -> None:
    invoice = draft(invoice_service)
    line_id = invoice.lines[0].id
    result = invoice_service.confirm_lines(
        invoice.id, options=[ConfirmOption(line_id=line_id, food_item_id=food_item.id)]
    )
    purchase_id = result.results[0].purchase_id

    with pytest.raises(ConflictError):
        invoice_service.void_invoice(invoice.id, confirmed_line_ids=[])
    assert purchase_service.get_purchase(purchase_id).voided_at is None

    voided = invoice_service.void_invoice(invoice.id, confirmed_line_ids=[line_id])

    assert voided.status == INVOICE_STATUS_VOIDED
    assert voided.files == ()
    assert purchase_service.search_purchases(limit=50).total == 0
    purchase = purchase_service.get_purchase(purchase_id)
    assert purchase.voided_at is not None
    assert purchase.revision_count == 1
    assert invoice_service.search_invoices(limit=50).total == 0
    assert len(invoice_service.create_invoice([upload()]).files) == 1


def test_a_voided_invoice_cannot_be_modified(invoice_service: InvoiceService) -> None:
    invoice = draft(invoice_service)
    voided = invoice_service.void_invoice(invoice.id)

    assert voided.status == INVOICE_STATUS_VOIDED
    with pytest.raises(ConflictError):
        invoice_service.update_invoice(invoice.id, source_name="Costco")
    with pytest.raises(ConflictError):
        invoice_service.add_line(invoice.id, description="x")


def test_voiding_twice_is_a_conflict(invoice_service: InvoiceService) -> None:
    invoice = draft(invoice_service)
    invoice_service.void_invoice(invoice.id)

    with pytest.raises(ConflictError) as error:
        invoice_service.void_invoice(invoice.id)

    assert "已经作废过了" in error.value.message


def test_voiding_deletes_the_originals_so_the_same_file_can_be_uploaded_again(
    invoice_service: InvoiceService,
) -> None:
    """⚠️ 作废会把原件一起删掉，而这正是为了让同一张照片还能再传一次。

    实跑踩到的死胡同：作废一张单据之后重传同一张图，撞上 `invoice_files.sha256`
    的唯一索引，接口回 409 并指回那张**界面上已经看不见**的单据——图传不进来，
    单据也打不开。作废的前提是一行都没入账，所以这些文件不是任何采购记录的凭据。
    """
    invoice = draft(invoice_service)
    file_id = invoice.files[0].id

    voided = invoice_service.void_invoice(invoice.id)

    assert voided.files == ()
    # 文件记录没了，读它会 404 而不是回字节。
    with pytest.raises(NotFoundError):
        invoice_service.open_file(invoice.id, file_id)
    # 同一份字节重新建一张单据：不再被判成重复。
    again = invoice_service.create_invoice([upload()])
    assert again.id != invoice.id
    assert len(again.files) == 1


def test_voiding_without_confirmation_keeps_originals_of_booked_invoice(
    invoice_service: InvoiceService, food_item: FoodItemView
) -> None:
    """删不掉的单据，它的原件一个都不能少——拒绝必须发生在删任何东西之前。"""
    invoice = draft(invoice_service)
    file_id = invoice.files[0].id
    invoice_service.confirm_lines(
        invoice.id,
        options=[ConfirmOption(line_id=invoice.lines[0].id, food_item_id=food_item.id)],
    )

    with pytest.raises(ConflictError):
        invoice_service.void_invoice(invoice.id)

    assert len(invoice_service.get_invoice(invoice.id).files) == 1
    _, stream = invoice_service.open_file(invoice.id, file_id)
    with stream:
        assert stream.read() == PHOTO


def test_voided_invoices_are_hidden_unless_asked(
    invoice_service: InvoiceService,
) -> None:
    invoice = draft(invoice_service)
    invoice_service.void_invoice(invoice.id)

    assert invoice_service.search_invoices(limit=50).total == 0
    assert invoice_service.search_invoices(limit=50, include_voided=True).total == 1


def test_the_worklist_lists_unfinished_invoices(
    invoice_service: InvoiceService, food_item: FoodItemView
) -> None:
    """`completed=False` 就是老板的待办清单。"""
    done = draft(invoice_service)
    invoice_service.update_line(
        done.id, done.lines[1].id, status="skipped", skip_reason="备注"
    )
    invoice_service.confirm_lines(
        done.id,
        options=[ConfirmOption(line_id=done.lines[0].id, food_item_id=food_item.id)],
    )
    invoice_service.complete_invoice(done.id)
    todo = invoice_service.create_invoice([upload(content=OTHER_PHOTO, name="b.png")])

    unfinished = invoice_service.search_invoices(completed=False, limit=50)
    finished = invoice_service.search_invoices(completed=True, limit=50)

    assert [item.id for item in unfinished.items] == [todo.id]
    assert [item.id for item in finished.items] == [done.id]


def test_the_list_can_filter_by_source(invoice_service: InvoiceService) -> None:
    first = draft(invoice_service)
    invoice_service.update_invoice(first.id, source_name="Costco")
    second = invoice_service.create_invoice([upload(content=OTHER_PHOTO, name="b.png")])
    invoice_service.update_invoice(second.id, source_name="Restaurant Depot")

    page = invoice_service.search_invoices(source="costco ", limit=50)

    assert [item.id for item in page.items] == [first.id]


def test_pagination_reports_the_full_total(invoice_service: InvoiceService) -> None:
    for index in range(3):
        invoice_service.create_invoice(
            [upload(content=f"photo {index}".encode(), name=f"p{index}.png")]
        )

    page = invoice_service.search_invoices(limit=2, offset=0)

    assert (len(page.items), page.total) == (2, 3)


def test_unknown_invoice_and_foreign_line_are_not_found(
    invoice_service: InvoiceService,
) -> None:
    invoice = draft(invoice_service)
    other = invoice_service.create_invoice([upload(content=OTHER_PHOTO, name="b.png")])

    with pytest.raises(NotFoundError):
        invoice_service.get_invoice(99999)
    with pytest.raises(NotFoundError) as error:
        invoice_service.update_line(invoice.id, other.lines[0].id, quantity="1")
    assert error.value.details["invoice_id"] == invoice.id


# --- 收口面板按类别摆出本单分布（步骤 10.A）--------------------------------


def test_the_completion_panel_rolls_the_lines_up_by_category(
    invoice_service: InvoiceService,
    ingredient_service: IngredientService,
    food_item: FoodItemView,
) -> None:
    """⚠️ 这是「让老板理解分类在干什么」的主力。

    解释一个字段最有效的方式不是加说明文字，是把它的产物摆在旁边：他审完最后
    一行点「完成单据」，就看见自己那几次点击变成了一张看得懂的账。

    金额口径沿用 `_reconciliation()` 现成的那一条——**以采购记录上的金额为准**。
    """
    vegetable = ingredient_service.create_food_item(
        name="Bok Choy", name_zh="上海青", category_id=3, base_unit="lb"
    )
    invoice = draft(invoice_service)
    invoice_service.update_line(invoice.id, invoice.lines[1].id, unit_price="113.00")
    invoice_service.confirm_lines(
        invoice.id,
        options=[
            ConfirmOption(line_id=invoice.lines[0].id, food_item_id=food_item.id),
            ConfirmOption(line_id=invoice.lines[1].id, food_item_id=vegetable.id),
        ],
    )

    reconciliation = invoice_service.get_invoice(invoice.id).reconciliation

    assert [(row.name, row.line_count) for row in reconciliation.by_category] == [
        ("Vegetables", 1),
        ("Meat & Poultry", 1),
    ]
    # 金额从大到小：面板第一眼就该是"钱花在哪儿"。
    assert reconciliation.by_category[0].amount > reconciliation.by_category[1].amount
    # 分类的各格之和 = 已确认总额（没有漏桶、也没有重复计）。
    assert sum(row.amount for row in reconciliation.by_category) == (
        reconciliation.confirmed_amount
    )
    assert reconciliation.adjustment_total is None


def test_adjustment_lines_get_their_own_bucket_and_never_join_a_category(
    invoice_service: InvoiceService, food_item: FoodItemView
) -> None:
    """⚠️ 与步骤 09 的价格统计同一条口径：运费 / 折让不是某一类食材的成本。

    摆成独立字段而不是 `by_category` 里的一行，是为了让界面**不可能**把它和分类
    加在一起。
    """
    invoice = draft(invoice_service)
    invoice_service.update_line(invoice.id, invoice.lines[1].id, status="skipped",
                                skip_reason="认不出")
    freight = invoice_service.add_line(
        invoice.id,
        line_kind="adjustment",
        description="运费",
        quantity="1",
        purchase_unit="each",
        unit_price="25.00",
        line_amount="25.00",
    )
    invoice_service.confirm_lines(
        invoice.id,
        options=[
            ConfirmOption(line_id=invoice.lines[0].id, food_item_id=food_item.id),
            ConfirmOption(line_id=freight.id),
        ],
    )

    reconciliation = invoice_service.get_invoice(invoice.id).reconciliation

    assert [row.name for row in reconciliation.by_category] == ["Meat & Poultry"]
    assert reconciliation.adjustment_total is not None
    assert reconciliation.adjustment_total.category_id is None
    assert reconciliation.adjustment_total.amount == Decimal("25.00")
    # 它没有混进任何分类——分类那几格加起来不含这 25 元。
    assert sum(row.amount for row in reconciliation.by_category) == Decimal("60.00")
    assert any("单列一桶" in note for note in reconciliation.notes)


def test_the_panel_is_withheld_when_the_invoice_mixes_currencies(
    invoice_service: InvoiceService,
    ingredient_service: IngredientService,
    food_item: FoodItemView,
) -> None:
    """⚠️ 币种不混算（步骤 09 的硬规则）。

    几个币种的金额相加得到的数字没有任何含义，而它看起来仍然像个总额。
    """
    other = ingredient_service.create_food_item(
        name="Bok Choy", category_id=3, base_unit="lb"
    )
    invoice = draft(invoice_service)
    invoice_service.update_line(invoice.id, invoice.lines[0].id, currency="USD")
    invoice_service.update_line(
        invoice.id, invoice.lines[1].id, unit_price="113.00", currency="CNY"
    )
    invoice_service.confirm_lines(
        invoice.id,
        options=[
            ConfirmOption(line_id=invoice.lines[0].id, food_item_id=food_item.id),
            ConfirmOption(line_id=invoice.lines[1].id, food_item_id=other.id),
        ],
    )

    reconciliation = invoice_service.get_invoice(invoice.id).reconciliation

    assert reconciliation.by_category == ()
    assert reconciliation.adjustment_total is None
    assert any("多个币种" in note for note in reconciliation.notes)


def test_pending_lines_are_not_counted_in_the_panel(
    invoice_service: InvoiceService, food_item: FoodItemView
) -> None:
    """面板只摆已确认的钱：待确认的行还没入账，算进去就是在报一个还没发生的数。"""
    invoice = draft(invoice_service)
    invoice_service.confirm_lines(
        invoice.id,
        options=[ConfirmOption(line_id=invoice.lines[0].id, food_item_id=food_item.id)],
    )

    reconciliation = invoice_service.get_invoice(invoice.id).reconciliation

    assert reconciliation.pending_count == 1
    assert [row.line_count for row in reconciliation.by_category] == [1]


def test_confirming_a_line_clears_the_needs_review_flag(
    invoice_service: InvoiceService, ingredient_service: IngredientService
) -> None:
    """步骤 10.13：老板**用这条食材确认过一行**，「待确认」就结束了。

    他是在看着裁剪图、数量与单价的情况下按下确认的——这正是这个标记要等的那一下。
    清不掉的话，一条已经被他认可的食材会一直挂着「待确认」，那个标记很快就会被
    当成噪声忽略，于是真正需要看一眼的那些也跟着被忽略。
    """
    auto = ingredient_service.create_food_item(
        name="Beef Stomach", category_id=1, base_unit="lb", needs_review=True
    )
    invoice = draft(invoice_service)

    invoice_service.confirm_lines(
        invoice.id,
        options=[ConfirmOption(line_id=invoice.lines[0].id, food_item_id=auto.id)],
    )

    assert ingredient_service.get_food_item(auto.id).needs_review is False


def test_a_failed_confirm_leaves_the_needs_review_flag_alone(
    invoice_service: InvoiceService, ingredient_service: IngredientService
) -> None:
    """确认失败就回滚：不能出现「行没入账，食材却算确认过了」。"""
    auto = ingredient_service.create_food_item(
        name="Beef Stomach", category_id=1, base_unit="lb", needs_review=True
    )
    invoice = draft(invoice_service)

    # 第 2 行的单价认不出来，确认必然失败。
    outcome = invoice_service.confirm_lines(
        invoice.id,
        options=[ConfirmOption(line_id=invoice.lines[1].id, food_item_id=auto.id)],
    )

    assert outcome.results[0].ok is False
    assert ingredient_service.get_food_item(auto.id).needs_review is True
