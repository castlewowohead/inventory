"""第 0 层墨迹闸门：页内逐像素配准（步骤 08.3a，批次 2）。

⚠️ 这里用**程序生成的图像**，不用真实样本——真实样本含客户数据，已 gitignore，
不进测试夹具（步骤 07 定下的规矩）。真实样本上的标定数字记在
`docs/impl_record.md` 步骤 08，由 `tools/e2e_extraction.py` 实跑守着。
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from src.documents.grid import detect_grid
from src.documents.ink import INK_THRESHOLD, InkGate, gate_column, gate_rows


def _form(rows: int = 14, columns: int = 5, written: tuple[int, ...] = ()) -> Image.Image:
    """画一张预印表格，并在指定的几行里"写字"。

    `written` 里的序号是**数据行序号**（1 开始），与 `detect_grid` 切出来的行带
    序号未必相同——所以用例一律先锚定、再按行带序号断言。
    """
    width, height = 900, 1300
    image = Image.new("RGB", (width, height), (250, 248, 230))
    draw = ImageDraw.Draw(image)
    left, right, top = 80, width - 80, 150
    step = (height - 300) // rows
    for index in range(rows + 1):
        draw.line([(left, top + index * step), (right, top + index * step)],
                  fill=(40, 90, 60), width=3)
    column_step = (right - left) // columns
    for index in range(columns + 1):
        x = left + index * column_step
        draw.line([(x, top), (x, top + rows * step)], fill=(40, 90, 60), width=3)

    for row in written:
        y = top + (row - 1) * step + step // 2
        # 笔画粗细不均匀：像手写，也确保墨量明显超过格线残影。
        for n in range(14):
            x = left + column_step + 12 + n * 17
            draw.line([(x, y - 12), (x + 11, y + 13)], fill=(60, 60, 120), width=3 + n % 4)
    return image


def _bands(grid, first: int, count: int):
    return tuple(band for band in grid.rows if first <= band.index < first + count)


def test_the_gate_finds_exactly_the_rows_that_were_written_on() -> None:
    image = _form(rows=14, written=(2, 5))
    grid = detect_grid(image)
    assert grid.determined

    gate = gate_rows(image, grid, grid.rows)

    assert gate.determined
    written_bands = {grid.rows[1].index, grid.rows[4].index}
    assert set(gate.inked) == written_bands


def test_a_blank_form_has_no_inked_rows_at_all() -> None:
    """⚠️ 这是整条链路上唯一不依赖模型诚实度的那道闸门——空表必须判成全空。"""
    image = _form(rows=14, written=())
    grid = detect_grid(image)

    gate = gate_rows(image, grid, grid.rows)

    assert gate.determined
    assert gate.inked == ()


def test_blank_cells_score_far_below_the_threshold() -> None:
    """留白要看得见，不能刚好卡在阈值上。"""
    image = _form(rows=14, written=(3,))
    grid = detect_grid(image)

    gate = gate_rows(image, grid, grid.rows)
    scores = {item.index: item.score for item in gate.verdicts}
    inked = gate.inked[0]

    assert scores[inked] > INK_THRESHOLD
    assert max(score for index, score in scores.items() if index != inked) < INK_THRESHOLD


def test_too_few_rows_is_honestly_undetermined() -> None:
    """中位数模板要「多数是空的」才站得住，格子太少时一两行手写就能把它带偏。"""
    image = _form(rows=14, written=())
    grid = detect_grid(image)

    gate = gate_rows(image, grid, grid.rows[:3])

    assert not gate.determined
    assert gate.reason
    assert gate.inked == ()


def test_a_mostly_written_page_is_undetermined_rather_than_inverted() -> None:
    """过半格子有墨时模板本身就带上了墨，结论会反过来——那种页诚实不给结论。"""
    image = _form(rows=14, written=tuple(range(1, 13)))
    grid = detect_grid(image)

    gate = gate_rows(image, grid, grid.rows)

    assert not gate.determined
    assert "过半" in gate.reason


def test_the_column_gate_finds_the_cell_that_was_written_in() -> None:
    image = _form(rows=14, written=(4,))
    grid = detect_grid(image)
    assert len(grid.columns) >= 2

    # 字写在第 2 条列带那一片；第 1 条列带应当全空。
    written_band = grid.rows[3].index
    second = gate_column(image, grid, grid.rows, grid.columns[1].index)
    first = gate_column(image, grid, grid.rows, grid.columns[0].index)

    assert second.determined and written_band in second.inked
    assert first.determined and first.inked == ()


def test_an_undetermined_gate_reports_no_verdicts() -> None:
    gate = InkGate(determined=False, reason="测试")

    assert gate.inked == ()
    assert gate.blank == ()
    assert gate.verdict(1) is None
