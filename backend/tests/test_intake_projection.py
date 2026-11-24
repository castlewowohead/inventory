"""System groups use evidence without silently writing human decisions."""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

from src.services.intake_projection import project_items


def candidate(field: str, value: str, *, raw: str | None = None,
              reading: str | None = None):
    return SimpleNamespace(
        field=field, value=value, value_normalized=None, basis="paper",
        evidence=json.dumps({"raw_text": raw, "reading": reading}),
    )


def item(file_id: int, raw: str, *dates: tuple[str, str], source: str = "Moy's Market",
         decided_date: date | None = None):
    return SimpleNamespace(
        batch_file_id=file_id, decided_purchase_date=decided_date,
        decided_source_name=None, decided_at=None,
        candidates=[candidate("source_name", source)] + [
            candidate("purchase_date", value, raw=raw, reading=reading)
            for value, reading in dates
        ],
    )


def test_same_file_unambiguous_date_resolves_ambiguous_pages_without_mutating_decisions():
    ambiguous = item(1, "6/8/2026", ("2026-06-08", "mdy"), ("2026-08-06", "dmy"))
    anchor = item(1, "6/18/2026", ("2026-06-18", "strict"))
    projected = project_items([ambiguous, anchor])

    assert [row.purchase_date for row in projected] == [date(2026, 6, 8), date(2026, 6, 18)]
    assert [row.source_name for row in projected] == ["Moy's Market", "Moy's Market"]
    assert all(row.suggested for row in projected)
    assert ambiguous.decided_purchase_date is None
    assert ambiguous.decided_source_name is None


def test_a_same_file_day_month_order_beats_the_project_default():
    """同一个商家、同一份文件里有 `18/6/2026` → 这家写日/月/年，`6/8` 就读 8 月 6 日。

    第 15 节的默认读法是**最后一条**依据：纸面上有更硬的证据时让路。
    """
    ambiguous = item(1, "6/8/2026", ("2026-06-08", "mdy"), ("2026-08-06", "dmy"))
    anchor = item(1, "18/6/2026", ("2026-06-18", "strict"))
    assert project_items([ambiguous, anchor])[0].purchase_date == date(2026, 8, 6)


def test_different_sellers_in_one_pdf_do_not_lend_a_date_order_to_each_other():
    """另一家写日/月/年，不代表这一家也写日/月/年 → 落回默认的月/日/年。

    ⚠️ 这里要分清两件事：**没借用**别人的顺序（否则会是 8 月 6 日），和
    **落到项目默认**（第 15 节，6 月 8 日）。以前这一条的结果是「日期留空」，
    现在是「按默认读」，但被守住的规则没变。
    """
    ambiguous = item(1, "6/8/2026", ("2026-06-08", "mdy"), ("2026-08-06", "dmy"))
    other_seller = item(1, "18/6/2026", ("2026-06-18", "strict"), source="Other Market")
    assert project_items([ambiguous, other_seller])[0].purchase_date == date(2026, 6, 8)


def test_a_human_date_overrides_the_system_projection():
    corrected = item(1, "6/8/2026", ("2026-06-08", "mdy"), ("2026-08-06", "dmy"),
                     decided_date=date(2026, 8, 6))
    anchor = item(1, "6/18/2026", ("2026-06-18", "strict"))
    assert project_items([corrected, anchor])[0].purchase_date == date(2026, 8, 6)


def test_explicit_clear_keeps_a_paper_suggestion_in_pending():
    cleared = item(1, "6/8/2026", ("2026-06-08", "mdy"))
    cleared.decided_at = object()
    projected = project_items([cleared])[0]
    assert (projected.purchase_date, projected.source_name, projected.suggested) == (None, None, False)
