"""Guard the M00 baseline against counting an unasked field as an OCR miss."""

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from eval_multifiles_m00 import private_path, supplier_verdict  # noqa: E402
from eval_multifiles_m01 import private_path as m01_private_path  # noqa: E402


def test_old_prompt_without_supplier_is_not_scored_as_a_miss():
    label = {
        "review_state": "reviewed",
        "source_printed": "present",
        "seller_en_raw": "Example Seller",
    }
    assert supplier_verdict('{"invoice_date": "..."}', '{"invoice_date": "2026-01-01"}', label) == "not_asked"


def test_asked_supplier_separates_absent_unread_and_wrong():
    prompt = '{"supplier": "..."}'
    base = {"review_state": "reviewed", "seller_en_raw": "Example Seller"}
    assert supplier_verdict(prompt, '{"supplier": ""}', {**base, "source_printed": "absent"}) == "correct_absent"
    assert supplier_verdict(prompt, '{"supplier": ""}', {**base, "source_printed": "present"}) == "paper_present_unread"
    assert supplier_verdict(prompt, '{"supplier": "Buyer"}', {**base, "source_printed": "present"}) == "wrong_candidate"


def test_new_prompt_prefers_english_and_keeps_missing_keys_distinct():
    prompt = '{"supplier_english": ..., "supplier_chinese": ...}'
    label = {"review_state": "reviewed", "source_printed": "present", "seller_en_raw": "Paper Seller"}
    assert supplier_verdict(prompt, '{"supplier_english": "Paper Seller", "supplier_chinese": "纸面商家"}', label) == "correct_candidate"
    assert supplier_verdict(prompt, '{"supplier_english": null}', label) == "missing_response_field"


def test_reports_cannot_be_written_outside_private_sample_directory(tmp_path):
    with pytest.raises(ValueError, match="samples/real"):
        private_path(tmp_path / "public_report.json")
    with pytest.raises(ValueError, match="samples/real"):
        m01_private_path(str(tmp_path / "public_report.json"))
