"""Read-only grouping of recognized contents before a person edits them.

The recognized values stay separate from the manually decided columns. A
date order is inferred only when a single seller appears in one file and an
unambiguous numeric date in that file proves the order (6/18 -> month/day).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Sequence

from src.models import IntakeItem
from src.normalization import DATE_ORDER_ASSUMED
from src.services.ingredients import normalize_name

_NUMERIC_DATE = re.compile(r"^\s*(\d{1,2})[/-](\d{1,2})[/-](\d{4})\s*$")


@dataclass(frozen=True)
class ProjectedItem:
    item: IntakeItem
    purchase_date: date | None
    source_name: str | None
    source_key: str | None
    suggested: bool


def _evidence(candidate) -> dict:
    try:
        result = json.loads(candidate.evidence or "{}")
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}


def _paper_sources(item: IntakeItem) -> dict[str, str]:
    names: dict[str, str] = {}
    for candidate in item.candidates:
        if candidate.field == "source_name" and candidate.basis == "paper":
            key = candidate.value_normalized or normalize_name(candidate.value)
            if key:
                names.setdefault(key, candidate.value)
    return names


def _file_context(items: Sequence[IntakeItem]) -> tuple[dict[int, str], dict[int, str]]:
    sources: dict[int, dict[str, str]] = {}
    orders: dict[int, set[str]] = {}
    for item in items:
        file_id = item.batch_file_id
        sources.setdefault(file_id, {}).update(_paper_sources(item))
        if item.decided_source_name:
            sources[file_id].setdefault(normalize_name(item.decided_source_name), item.decided_source_name)
        for candidate in item.candidates:
            if candidate.field != "purchase_date" or candidate.basis != "paper":
                continue
            raw = _evidence(candidate).get("raw_text")
            match = _NUMERIC_DATE.fullmatch(raw) if isinstance(raw, str) else None
            if not match:
                continue
            first, second = int(match[1]), int(match[2])
            if first <= 12 < second <= 31:
                orders.setdefault(file_id, set()).add("mdy")
            elif second <= 12 < first <= 31:
                orders.setdefault(file_id, set()).add("dmy")
    sole_sources = {file_id: next(iter(names.values())) for file_id, names in sources.items()
                    if len(names) == 1}
    sole_orders = {file_id: next(iter(values)) for file_id, values in orders.items()
                   if file_id in sole_sources and len(values) == 1}
    return sole_sources, sole_orders


def project_items(items: Sequence[IntakeItem]) -> list[ProjectedItem]:
    file_sources, file_orders = _file_context(items)
    projected: list[ProjectedItem] = []
    for item in items:
        # An explicit move back to 待归类 is a human decision. Both values are
        # empty, but decided_at distinguishes it from an untouched suggestion.
        if item.decided_at is not None and item.decided_purchase_date is None and item.decided_source_name is None:
            projected.append(ProjectedItem(item, None, None, None, False))
            continue
        date_value = item.decided_purchase_date
        source = item.decided_source_name
        suggested = False
        if date_value is None:
            values: dict[date, str | None] = {}
            for candidate in item.candidates:
                if candidate.field != "purchase_date" or candidate.basis != "paper":
                    continue
                try:
                    parsed = date.fromisoformat(candidate.value)
                except ValueError:
                    continue
                values[parsed] = _evidence(candidate).get("reading")
            if len(values) == 1:
                date_value = next(iter(values))
            elif len(values) > 1:
                # Two readings only happen for cards recognized before the project
                # settled on a default day/month order (plan section 15). Same-file
                # evidence still wins; otherwise read the US way, exactly as a fresh
                # recognition would. Nothing is written: this is still a suggestion.
                order = file_orders.get(item.batch_file_id) or DATE_ORDER_ASSUMED
                matches = [value for value, reading in values.items() if reading == order]
                if len(matches) == 1:
                    date_value = matches[0]
            suggested = date_value is not None
        if source is None:
            own = _paper_sources(item)
            if len(own) == 1:
                source = next(iter(own.values()))
            elif not own:
                source = file_sources.get(item.batch_file_id)
            suggested = suggested or source is not None
        key = normalize_name(source) if source else None
        projected.append(ProjectedItem(item, date_value, source, key, suggested))
    return projected
