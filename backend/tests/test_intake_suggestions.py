"""商家记忆与相似堆（多文件计划 M07）：Service 层。

M07 的四条验收条件各自有用例：

1. **一堆六张里排除一张例外之后，一次给剩余五张赋来源**
   （`test_pile_of_six_excludes_one_exception_then_names_the_rest_in_one_move`）。
2. **未来相似单据出现候选且能纠正**
   （`test_future_similar_document_gets_a_candidate_and_can_be_corrected`）。
3. **不同商家共用版式不会被静默合并**
   （`test_two_suppliers_sharing_a_template_are_never_merged`）。
4. **纸面明确冲突的来源不被同文件或外观建议覆盖**
   （`test_paper_reading_beats_same_file_suggestion`）。

样本说明：这里的表单图片由 Pillow 当场画出来（横线/竖线/墨迹块）、PDF 由 pypdf
造空白页、商家名一律是 `Costco` / `Hua Feng Market` 这类合成或公开字符串。
**全是合成样本**，用途是把代码路径与边界验到底；`samples/real/` 的客户原件不进
测试（那个目录被 Git 忽略）。真实原件上**各层线索的实测覆盖范围**另见
`docs/multifiles.md` M07 的验证记录——错归率在那里标为「未测」，因为仓库里没有
跨商家的逐项真值。

⚠️ 本模块一次模型调用都不会发生，而且是**结构上**的：建议服务连识别器这个依赖都
没有（有一条用例专门断言这件事）。
"""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw
from PIL.PngImagePlugin import PngInfo

from src.errors import NotFoundError, ValidationError
from src.models import (
    INTAKE_ALIAS_BASIS_CONFIRMED_READING,
    INTAKE_ALIAS_BASIS_PAPER_BILINGUAL,
    INTAKE_ALIAS_BASIS_USER,
    INTAKE_CANDIDATE_BASIS_HISTORY,
    INTAKE_CANDIDATE_BASIS_PAPER,
    INTAKE_CANDIDATE_BASIS_SAME_FILE,
    INTAKE_CANDIDATE_BASIS_SIMILAR_LAYOUT,
    INTAKE_CANDIDATE_FIELD_SOURCE_NAME,
)
from src.services.intake import CandidateInput, IntakeService
from src.services.intake_ingest import IntakeIngestService
from src.services.intake_moves import IntakeMoveService
from src.services.intake_suggestions import (
    SCORE_HISTORY_ALIAS,
    SCORE_HISTORY_EXACT,
    SCORE_SAME_FILE,
    SCORE_SIMILAR_LAYOUT,
    IntakeSuggestionService,
)
from src.services.invoices import UploadedFile

# --- 合成样本 ---------------------------------------------------------------

TEMPLATE_A_ROWS = 12
"""「模板 A」：12 条格线行。实测这份合成表单稳定量出 `rows=12`。"""

TEMPLATE_B_ROWS = 8
"""「模板 B」：8 条格线行，于是指纹与模板 A 不同——两家不同商家的单据不会落进
同一堆。"""


def form_png(*, rows: int = TEMPLATE_A_ROWS, tag: str = "a") -> bytes:
    """画一张**有格线的表单**：横线 + 竖线 + 墨迹块。

    `tag` 只写进 PNG 的文本块，**一个像素都不改**：同一个批次里同一份字节会撞
    `(batch_id, intake_file_id)` 唯一约束，而「六张版式相同的扫描件」必须是六份
    不同的字节。改像素去凑唯一性会把版式指纹也改掉，那就验不了堆了。
    """
    width, height = 840, 1100
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    top, bottom = int(height * 0.18), int(height * 0.92)
    step = (bottom - top) / rows
    for index in range(rows + 1):
        y = int(top + index * step)
        draw.line(
            [(int(width * 0.06), y), (int(width * 0.94), y)], fill=(20, 20, 20), width=3
        )
    for index in range(5):
        x = int(width * 0.06 + index * (width * 0.88 / 4))
        draw.line([(x, top), (x, bottom)], fill=(20, 20, 20), width=3)
    for row in range(rows):
        y = int(top + row * step + step * 0.3)
        for column in range(6):
            left = int(width * 0.1) + column * 22
            draw.rectangle([left, y, left + 9, y + 14], fill=(30, 30, 30))
    info = PngInfo()
    info.add_text("intake-test-tag", tag)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", pnginfo=info)
    return buffer.getvalue()


def photo_jpeg(*, tint: int = 230) -> bytes:
    """一张**没有格线**的照片：量不出模板，所以只够形成「值得一起看」的一堆。"""
    buffer = io.BytesIO()
    Image.new("RGB", (400, 520), (250, 248, tint)).save(buffer, format="JPEG")
    return buffer.getvalue()


def scan_pdf(pages: int = 1, *, tag: bytes = b"") -> bytes:
    """没有文字层也没有整页图的 PDF（空白页）：量不出版式，用来隔离第 2/3 层。"""
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    if tag:
        writer.add_metadata({"/Keywords": tag.decode("ascii")})
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def upload(ingest: IntakeIngestService, batch_id: int, name: str, content: bytes):
    """上传一份原件并返回它切出来的内容（页）。"""
    outcome = ingest.upload(
        batch_id, UploadedFile(content=content, original_name=name, content_type=None)
    )
    assert outcome.ingested is not None, outcome.ref.error_message
    return list(outcome.ingested.items)


def paper_source(
    intake: IntakeService, item_id: int, value: str, **evidence: object
) -> None:
    """写一条**纸面读出**的商家候选（M05 的产出，这里直接摆进去当前提）。"""
    intake.replace_candidates(
        item_id,
        field=INTAKE_CANDIDATE_FIELD_SOURCE_NAME,
        basis=INTAKE_CANDIDATE_BASIS_PAPER,
        candidates=[
            CandidateInput(
                value=value,
                evidence={"basis_detail": "paper_reading", **evidence},
            )
        ],
    )


def suggestions_of(run, item_id: int, basis: str | None = None):
    result = next(item for item in run.items if item.item_id == item_id)
    if basis is None:
        return result
    return [item for item in result.suggestions if item.basis == basis]


def candidate_bases(intake: IntakeService, item_id: int) -> list[str]:
    return sorted(
        candidate.basis
        for candidate in intake.get_item(item_id).candidates
        if candidate.field == INTAKE_CANDIDATE_FIELD_SOURCE_NAME
    )


# --- 第二层：同文件参考 -----------------------------------------------------


def test_same_file_confirmation_suggests_the_other_pages(
    intake_service, intake_ingest_service, intake_suggestion_service
):
    batch = intake_service.create_batch()
    pages = upload(intake_ingest_service, batch.id, "mixed.pdf", scan_pdf(3))
    intake_service.classify_item(pages[0].id, source_name="Costco")

    run = intake_suggestion_service.suggest_batch(batch.id)

    assert run.model_calls == 0
    same_file = suggestions_of(run, pages[1].id, INTAKE_CANDIDATE_BASIS_SAME_FILE)
    assert [(item.value, item.score) for item in same_file] == [
        ("Costco", SCORE_SAME_FILE)
    ]
    assert same_file[0].evidence["confirmed_pages"] == [1]
    # 建议只是建议：这一页仍然在「商家待定」里。
    assert intake_service.get_item(pages[1].id).decided_source_name is None
    assert candidate_bases(intake_service, pages[1].id) == [
        INTAKE_CANDIDATE_BASIS_SAME_FILE
    ]
    # 已经确认过的那一页不再接收建议，纸面读法（这里没有）也不会被动。
    assert suggestions_of(run, pages[0].id).resolved is True


def test_paper_reading_beats_same_file_suggestion(
    intake_service, intake_ingest_service, intake_suggestion_service
):
    """第 3.2 节第 2 层的原话：「若某页明确写着不同来源，单页证据优先」。"""
    batch = intake_service.create_batch()
    pages = upload(intake_ingest_service, batch.id, "mixed.pdf", scan_pdf(2))
    intake_service.classify_item(pages[0].id, source_name="Costco")
    paper_source(intake_service, pages[1].id, "Restaurant Depot", page=2)

    run = intake_suggestion_service.suggest_batch(batch.id)

    conflicted = suggestions_of(run, pages[1].id)
    assert [item.basis for item in conflicted.suggestions] == []
    assert conflicted.conflicts[0]["suppressed_value"] == "Costco"
    assert conflicted.conflicts[0]["basis"] == INTAKE_CANDIDATE_BASIS_SAME_FILE
    # 纸面那条候选一个字都没动，而同文件的建议**没有**被写进库。
    assert candidate_bases(intake_service, pages[1].id) == [
        INTAKE_CANDIDATE_BASIS_PAPER
    ]


def test_resolved_card_clears_stale_suggestions_but_keeps_the_paper_reading(
    intake_service, intake_ingest_service, intake_suggestion_service
):
    batch = intake_service.create_batch()
    pages = upload(intake_ingest_service, batch.id, "mixed.pdf", scan_pdf(2))
    intake_service.classify_item(pages[0].id, source_name="Costco")
    paper_source(intake_service, pages[1].id, "Costco", page=2)
    intake_suggestion_service.suggest_batch(batch.id)
    assert INTAKE_CANDIDATE_BASIS_SAME_FILE in candidate_bases(
        intake_service, pages[1].id
    )

    intake_service.classify_item(pages[1].id, source_name="Costco")
    run = intake_suggestion_service.suggest_batch(batch.id)

    assert suggestions_of(run, pages[1].id).resolved is True
    assert candidate_bases(intake_service, pages[1].id) == [
        INTAKE_CANDIDATE_BASIS_PAPER
    ]


# --- 第三层：历史来源与别名 -------------------------------------------------


def test_history_converges_two_writings_of_one_supplier(
    intake_service, intake_ingest_service, intake_suggestion_service
):
    """`COSTCO` 与 `Costco` 不该在整理页上长成两组（第 3.2 节第 3 层）。"""
    first = intake_service.create_batch()
    page = upload(intake_ingest_service, first.id, "one.pdf", scan_pdf(1))[0]
    intake_service.classify_item(page.id, source_name="Costco")

    second = intake_service.create_batch()
    later = upload(intake_ingest_service, second.id, "two.pdf", scan_pdf(1, tag=b"b"))[0]
    paper_source(intake_service, later.id, "COSTCO", page=1)
    run = intake_suggestion_service.suggest_batch(second.id)

    history = suggestions_of(run, later.id, INTAKE_CANDIDATE_BASIS_HISTORY)
    assert [(item.value, item.value_normalized, item.score) for item in history] == [
        ("Costco", "costco", SCORE_HISTORY_EXACT)
    ]
    assert history[0].evidence["basis_detail"] == "history_exact"
    assert history[0].evidence["confirmed_items"] == 1
    assert intake_service.get_item(later.id).decided_source_name is None


def test_confirming_a_different_writing_records_a_correctable_alias(
    intake_service, intake_ingest_service, intake_suggestion_service
):
    first = intake_service.create_batch()
    page = upload(intake_ingest_service, first.id, "one.pdf", scan_pdf(1))[0]
    paper_source(intake_service, page.id, "COSTCO WHOLESALE #1162", page=1)
    intake_service.classify_item(page.id, source_name="Costco")

    recorded = intake_suggestion_service.suggest_batch(first.id).aliases_recorded
    assert [(item.alias_key, item.source_key, item.basis) for item in recorded] == [
        ("costco wholesale #1162", "costco", INTAKE_ALIAS_BASIS_CONFIRMED_READING)
    ]

    second = intake_service.create_batch()
    later = upload(intake_ingest_service, second.id, "two.pdf", scan_pdf(1, tag=b"b"))[0]
    paper_source(intake_service, later.id, "COSTCO WHOLESALE #1162", page=1)
    run = intake_suggestion_service.suggest_batch(second.id)
    history = suggestions_of(run, later.id, INTAKE_CANDIDATE_BASIS_HISTORY)
    assert [(item.value, item.score) for item in history] == [
        ("Costco", SCORE_HISTORY_ALIAS)
    ]
    assert history[0].evidence["basis_detail"] == "history_alias"

    # 「能纠正」的那一半：删掉别名之后这条建议立刻不再出现。
    intake_suggestion_service.delete_alias(recorded[0].id)
    after = intake_suggestion_service.suggest_batch(second.id)
    assert [
        item.evidence["basis_detail"]
        for item in suggestions_of(after, later.id, INTAKE_CANDIDATE_BASIS_HISTORY)
    ] != ["history_alias"]
    # 已确认的分类一个字都没动。
    assert intake_service.get_item(page.id).decided_source_name == "Costco"


def test_user_alias_is_not_overwritten_by_an_observed_one(
    intake_service, intake_ingest_service, intake_suggestion_service
):
    """老板纠正过的别名，下一批不该被系统改回去。"""
    intake_suggestion_service.set_alias(
        alias_text="HF Market", source_name="Hua Feng Market"
    )
    batch = intake_service.create_batch()
    page = upload(intake_ingest_service, batch.id, "one.pdf", scan_pdf(1))[0]
    paper_source(intake_service, page.id, "HF Market", page=1)
    intake_service.classify_item(page.id, source_name="Costco")

    run = intake_suggestion_service.suggest_batch(batch.id)

    assert run.aliases_recorded == ()
    aliases = intake_suggestion_service.list_aliases()
    assert [(item.alias_key, item.source_key, item.basis) for item in aliases] == [
        ("hf market", "hua feng market", INTAKE_ALIAS_BASIS_USER)
    ]


def test_bilingual_paper_writings_become_aliases_without_translating(
    intake_service, intake_ingest_service, intake_suggestion_service
):
    """同一张纸上印了两个写法，人确认了一个 → 另一个是别名，**不是翻译**。"""
    batch = intake_service.create_batch()
    page = upload(intake_ingest_service, batch.id, "one.pdf", scan_pdf(1))[0]
    paper_source(
        intake_service,
        page.id,
        "Feng Yuan Meat",
        supplier_english="Feng Yuan Meat",
        supplier_chinese="丰源肉业",
        page=1,
    )
    intake_service.classify_item(page.id, source_name="Feng Yuan Meat")

    recorded = intake_suggestion_service.suggest_batch(batch.id).aliases_recorded

    assert [(item.alias_text, item.source_name, item.basis) for item in recorded] == [
        ("丰源肉业", "Feng Yuan Meat", INTAKE_ALIAS_BASIS_PAPER_BILINGUAL)
    ]
    memory = intake_suggestion_service.source_memory()
    assert [(item.source_key, item.source_name) for item in memory] == [
        ("feng yuan meat", "Feng Yuan Meat")
    ]


def test_similar_writing_only_ranks_and_never_replaces_the_paper_text(
    intake_service, intake_ingest_service, intake_suggestion_service
):
    first = intake_service.create_batch()
    page = upload(intake_ingest_service, first.id, "one.pdf", scan_pdf(1))[0]
    intake_service.classify_item(page.id, source_name="Costco Wholesale")

    second = intake_service.create_batch()
    later = upload(intake_ingest_service, second.id, "two.pdf", scan_pdf(1, tag=b"b"))[0]
    paper_source(intake_service, later.id, "Costco Wholsale", page=1)
    run = intake_suggestion_service.suggest_batch(second.id)

    history = suggestions_of(run, later.id, INTAKE_CANDIDATE_BASIS_HISTORY)
    assert [item.evidence["basis_detail"] for item in history] == ["history_similar"]
    assert history[0].value == "Costco Wholesale"
    # 分数低于别名那一档：相似写法只排序，不冒充同一个名字。
    assert history[0].score < SCORE_HISTORY_ALIAS
    assert 0 < history[0].evidence["similarity"] <= 1
    assert intake_service.get_item(later.id).decided_source_name is None


def test_no_paper_reading_means_no_invented_source(
    intake_service, intake_ingest_service, intake_suggestion_service
):
    """没有纸面写法时一条历史建议都不给——**也绝不看文件名**。"""
    first = intake_service.create_batch()
    page = upload(intake_ingest_service, first.id, "one.pdf", scan_pdf(1))[0]
    intake_service.classify_item(page.id, source_name="Costco")

    second = intake_service.create_batch()
    later = upload(
        intake_ingest_service, second.id, "Costco invoice.pdf", scan_pdf(1, tag=b"b")
    )[0]
    run = intake_suggestion_service.suggest_batch(second.id)

    assert suggestions_of(run, later.id).suggestions == ()
    assert candidate_bases(intake_service, later.id) == []
    assert intake_service.get_item(later.id).decided_source_name is None


def test_alias_input_is_validated(intake_suggestion_service):
    with pytest.raises(ValidationError):
        intake_suggestion_service.set_alias(alias_text="COSTCO", source_name="costco")
    with pytest.raises(ValidationError):
        intake_suggestion_service.set_alias(alias_text="  ", source_name="Costco")
    with pytest.raises(ValidationError):
        intake_suggestion_service.set_alias(
            alias_text="HF", source_name="Hua Feng", basis="guessed"
        )
    with pytest.raises(NotFoundError):
        intake_suggestion_service.delete_alias(9999)


# --- 第四层：相似外观的待办堆 -----------------------------------------------


def test_pile_of_six_excludes_one_exception_then_names_the_rest_in_one_move(
    intake_service,
    intake_ingest_service,
    intake_suggestion_service,
    migrated_database,
):
    """M07 的验收条件一：六张一堆，排除一张例外，一次给剩下五张赋来源。"""
    batch = intake_service.create_batch()
    pages = [
        upload(
            intake_ingest_service, batch.id, f"scan{index}.png", form_png(tag=str(index))
        )[0]
        for index in range(6)
    ]

    run = intake_suggestion_service.suggest_batch(batch.id)

    assert run.layout_profiled == 6
    assert run.layout_pending == 0
    assert len(run.piles) == 1
    pile = run.piles[0]
    assert pile.item_count == 6
    assert pile.template_evidence is True
    assert pile.basis_detail == "layout_template"
    assert pile.features["ruled_rows"] == TEMPLATE_A_ROWS
    # 还没人确认过这个版式，所以堆里**没有**可建议的商家名——它只是一堆待办。
    assert pile.confirmed_sources == ()
    assert all(
        suggestions_of(run, page.id, INTAKE_CANDIDATE_BASIS_SIMILAR_LAYOUT) == []
        for page in pages
    )

    moves = IntakeMoveService(migrated_database)
    exception = pages[5]
    moves.move(
        batch.id,
        request_key="exclude-one",
        action="exclude",
        items=[{"id": exception.id, "version": exception.version}],
    )
    after_exclusion = intake_suggestion_service.piles(batch.id)
    assert after_exclusion.piles[0].item_count == 5
    assert after_exclusion.piles[0].excluded_item_ids == (exception.id,)
    assert exception.id not in after_exclusion.piles[0].item_ids

    named = moves.move(
        batch.id,
        request_key="name-the-pile",
        action="source",
        items=[
            {"id": page.id, "version": intake_service.get_item(page.id).version}
            for page in pages[:5]
        ],
        source_name="Kunkee Trading",
    )
    assert [item.decided_source_name for item in named.items] == ["Kunkee Trading"] * 5
    # 只写来源：日期仍然待定（堆里的几张卡片常常各属不同的一天）。
    assert all(item.decided_purchase_date is None for item in named.items)
    assert intake_service.get_item(exception.id).decided_source_name is None
    # 那张例外还是被排除着，所以这一堆空了——它仍然在「待归类」里（M06 的组），
    # 只是不再跟着这一堆走。
    assert intake_suggestion_service.piles(batch.id).piles == ()
    assert intake_service.get_item(exception.id).pile_excluded is True


def test_future_similar_document_gets_a_candidate_and_can_be_corrected(
    intake_service, intake_ingest_service, intake_suggestion_service
):
    """M07 的验收条件二：以后的相似单据出现候选，而且能纠正。"""
    first = intake_service.create_batch()
    known = upload(intake_ingest_service, first.id, "known.png", form_png(tag="k"))[0]
    # 刻意**先确认、后重算**：纸面能读出商家的页往往一上来就被确认，所以「已确认
    # 的页也要量版式」必须成立，否则最常见的那条路径上根本攒不出版式记忆
    # （真实实跑第一版就栽在这里，见 docs/multifiles.md M07 的验证记录）。
    intake_service.classify_item(known.id, source_name="Kunkee Trading")
    reference_run = intake_suggestion_service.suggest_batch(first.id)
    assert reference_run.layout_profiled == 1
    assert reference_run.piles == ()

    second = intake_service.create_batch()
    fresh = upload(intake_ingest_service, second.id, "fresh.png", form_png(tag="f"))[0]
    run = intake_suggestion_service.suggest_batch(second.id)

    layout = suggestions_of(run, fresh.id, INTAKE_CANDIDATE_BASIS_SIMILAR_LAYOUT)
    assert [(item.value, item.score) for item in layout] == [
        ("Kunkee Trading", SCORE_SIMILAR_LAYOUT)
    ]
    assert layout[0].evidence["pages_with_this_layout"] == 1
    assert "版式相同不证明商家相同" in layout[0].evidence["note"]
    # 候选永远不会自己变成决定。
    assert intake_service.get_item(fresh.id).decided_source_name is None

    # 纠正：这一张其实是另一家。
    intake_service.classify_item(fresh.id, source_name="New Moys Meat")
    corrected = intake_suggestion_service.suggest_batch(second.id)
    assert suggestions_of(corrected, fresh.id).resolved is True
    assert candidate_bases(intake_service, fresh.id) == []


def test_two_suppliers_sharing_a_template_are_never_merged(
    intake_service, intake_ingest_service, intake_suggestion_service
):
    """M07 的验收条件三：不同商家共用版式不会被静默合并。"""
    history = intake_service.create_batch()
    first = upload(intake_ingest_service, history.id, "a.png", form_png(tag="a"))[0]
    second = upload(intake_ingest_service, history.id, "b.png", form_png(tag="b"))[0]
    intake_suggestion_service.suggest_batch(history.id)
    intake_service.classify_item(first.id, source_name="Kunkee Trading")
    intake_service.classify_item(second.id, source_name="New Moys Meat")

    fresh_batch = intake_service.create_batch()
    fresh = upload(intake_ingest_service, fresh_batch.id, "c.png", form_png(tag="c"))[0]
    run = intake_suggestion_service.suggest_batch(fresh_batch.id)

    layout = suggestions_of(run, fresh.id, INTAKE_CANDIDATE_BASIS_SIMILAR_LAYOUT)
    assert sorted(item.value for item in layout) == [
        "Kunkee Trading",
        "New Moys Meat",
    ]
    assert all(item.score == SCORE_SIMILAR_LAYOUT for item in layout)
    assert any("家被人确认过" in note for note in suggestions_of(run, fresh.id).notes)
    assert intake_service.get_item(fresh.id).decided_source_name is None
    assert any(
        "同一个版式下已经有 2 家" in note
        for pile in run.piles
        for note in pile.notes
    )


def test_pile_without_template_evidence_gets_no_source_candidate(
    intake_service, intake_ingest_service, intake_suggestion_service
):
    """量不出格线的照片只够「一起看一眼」，不够当成某一家的证据。"""
    history = intake_service.create_batch()
    known = upload(intake_ingest_service, history.id, "p0.jpg", photo_jpeg(tint=230))[0]
    intake_suggestion_service.suggest_batch(history.id)
    intake_service.classify_item(known.id, source_name="Costco")

    batch = intake_service.create_batch()
    photos = [
        upload(intake_ingest_service, batch.id, f"p{index}.jpg", photo_jpeg(tint=tint))[
            0
        ]
        for index, tint in enumerate((231, 232), start=1)
    ]
    run = intake_suggestion_service.suggest_batch(batch.id)

    pile = run.piles[0]
    assert pile.template_evidence is False
    assert pile.basis_detail == "layout_unknown"
    assert pile.item_count == 2
    assert all(
        suggestions_of(run, photo.id, INTAKE_CANDIDATE_BASIS_SIMILAR_LAYOUT) == []
        for photo in photos
    )
    assert any("没有模板信息" in note for note in pile.notes)


def test_two_templates_do_not_share_a_pile(
    intake_service, intake_ingest_service, intake_suggestion_service
):
    batch = intake_service.create_batch()
    upload(intake_ingest_service, batch.id, "a1.png", form_png(tag="a1"))
    upload(intake_ingest_service, batch.id, "a2.png", form_png(tag="a2"))
    upload(
        intake_ingest_service,
        batch.id,
        "b1.png",
        form_png(rows=TEMPLATE_B_ROWS, tag="b1"),
    )

    board = intake_suggestion_service.suggest_batch(batch.id)

    assert [pile.item_count for pile in board.piles] == [2, 1]
    assert [pile.features["ruled_rows"] for pile in board.piles] == [
        TEMPLATE_A_ROWS,
        TEMPLATE_B_ROWS,
    ]


def test_layout_is_measured_once_and_the_queue_resumes(
    intake_service, intake_ingest_service, intake_suggestion_service, monkeypatch
):
    """指纹本身就是队列：一次量不完下次接着量，量过的不再量。"""
    import src.services.intake_suggestions as module

    calls: list[int] = []
    original = module.profile_page_layout

    def counted(content, **kwargs):
        calls.append(1)
        return original(content, **kwargs)

    monkeypatch.setattr(module, "profile_page_layout", counted)
    monkeypatch.setattr(module, "MAX_LAYOUT_PROFILES_PER_RUN", 2)

    batch = intake_service.create_batch()
    for index in range(3):
        upload(intake_ingest_service, batch.id, f"s{index}.png", form_png(tag=str(index)))

    first = intake_suggestion_service.suggest_batch(batch.id)
    assert (first.layout_profiled, first.layout_pending) == (2, 1)
    assert len(calls) == 2

    second = intake_suggestion_service.suggest_batch(batch.id)
    assert (second.layout_profiled, second.layout_pending) == (1, 0)
    assert len(calls) == 3
    assert second.piles[0].item_count == 3

    third = intake_suggestion_service.suggest_batch(batch.id)
    assert (third.layout_profiled, third.layout_pending) == (0, 0)
    assert len(calls) == 3


def test_unmeasurable_page_is_reported_and_not_retried_forever(
    intake_service, intake_ingest_service, intake_suggestion_service
):
    """空白 PDF 页既没有文字层也没有整页图：量不出版式，如实记下来。"""
    batch = intake_service.create_batch()
    page = upload(intake_ingest_service, batch.id, "blank.pdf", scan_pdf(1))[0]

    first = intake_suggestion_service.suggest_batch(batch.id)
    assert first.layout_profiled == 1
    assert first.piles == ()
    board = intake_suggestion_service.piles(batch.id)
    assert board.unprofiled_item_ids == (page.id,)
    assert any("不在任何堆里" in note for note in board.notes)

    second = intake_suggestion_service.suggest_batch(batch.id)
    assert (second.layout_profiled, second.layout_pending) == (0, 0)


def test_rows_and_decided_pages_never_enter_a_pile(
    intake_service, intake_ingest_service, intake_suggestion_service
):
    batch = intake_service.create_batch()
    pages = [
        upload(intake_ingest_service, batch.id, f"s{index}.png", form_png(tag=str(index)))[
            0
        ]
        for index in range(2)
    ]
    ref = intake_service.register_file(
        batch.id,
        storage_key="synthetic/m07.csv",
        sha256="c" * 64,
        size=40,
        original_name="rows.csv",
        content_type="text/csv",
    ).ref
    rows = intake_service.record_items(ref.id, rows=[1, 2])
    intake_service.classify_item(pages[0].id, source_name="Costco")

    board = intake_suggestion_service.suggest_batch(batch.id)

    assert [pile.item_count for pile in board.piles] == [1]
    assert board.piles[0].item_ids == (pages[1].id,)
    assert all(row.id not in board.piles[0].item_ids for row in rows)


# --- 结构性保证 -------------------------------------------------------------


def test_suggestions_cost_nothing_by_construction(intake_suggestion_service):
    """「建议不花钱」是结构事实，不是承诺：服务里没有任何可调用的识别器。"""
    assert not hasattr(intake_suggestion_service, "_recognizer")
    assert not any(
        "recogniz" in name for name in vars(intake_suggestion_service)
    )


def test_unknown_batch_is_not_found(intake_suggestion_service):
    with pytest.raises(NotFoundError):
        intake_suggestion_service.suggest_batch(9999)
    with pytest.raises(NotFoundError):
        intake_suggestion_service.piles(9999)


def test_memory_counts_confirmed_purchases_too(
    intake_service, intake_ingest_service, intake_suggestion_service, purchase_service, food_item
):
    """已入账采购记录里的来源也是**人确认过**的来源（步骤 04），一并算进记忆。"""
    purchase_service.create_purchase(
        food_item_id=food_item.id,
        purchase_date="2026-11-08",
        source_name="Hua Feng Market",
        quantity="2",
        purchase_unit="lb",
        unit_price="3.50",
        line_amount="7.00",
    )
    batch = intake_service.create_batch()
    page = upload(intake_ingest_service, batch.id, "one.pdf", scan_pdf(1))[0]
    paper_source(intake_service, page.id, "HUA FENG MARKET", page=1)

    run = intake_suggestion_service.suggest_batch(batch.id)

    history = suggestions_of(run, page.id, INTAKE_CANDIDATE_BASIS_HISTORY)
    assert [item.value_normalized for item in history] == ["hua feng market"]
    assert history[0].evidence["purchase_records"] == 1
    memory = {item.source_key: item for item in intake_suggestion_service.source_memory()}
    assert memory["hua feng market"].purchase_records == 1
