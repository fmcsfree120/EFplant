"""Shared exclusion rules for operational alarm views and weekly reporting."""

from __future__ import annotations

import csv
import re
from difflib import SequenceMatcher, get_close_matches
from functools import lru_cache
from pathlib import Path

import pandas as pd


ALARM_DESCRIPTION_COLUMN = "ALM_DESCR"
ALARM_TAG_COLUMN = "ALM_TAGNAME"

PLANT_COLUMN = "PLANT"
ACTION_ALARM_LIST_GLOBS = ("S2A*.csv", "T2A*.csv")
ACTION_ALARM_SIMILARITY = 80.0
ACTION_ALARM_PLANTS = frozenset({"S2", "S2A", "S3", "T2A", "HJ1", "HJ2", "PCB", "LC3"})
PCB_STATIC_PRESSURE_EXCLUDE_TAGS = frozenset({
    "4F_A004_DC_DPT_PV",
    "1F_A005_DC_DPT_PV",
    "1F_A003_DC_DPT_PV",
    "1F_A002_DC_DPT_PV",
    "1F_A001_DC_DPT_PV",
})


def _normalise_alarm_text(value: object, remove_system_prefix: bool = False) -> str:
    """Normalise tags/descriptions before the approved fuzzy comparison."""
    text = "" if pd.isna(value) else str(value).upper().strip()
    if remove_system_prefix and "." in text:
        text = text.split(".", 1)[1]
    text = re.sub(r"\.-?NOPRI$", "", text)
    return "".join(char for char in text if char.isalnum())


@lru_cache(maxsize=1)
def _action_alarm_reference() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Load enabled action-alarm tags and descriptions from the supplied CSVs."""
    base_dir = Path(__file__).resolve().parent
    tags: set[str] = set()
    descriptions: set[str] = set()
    for pattern in ACTION_ALARM_LIST_GLOBS:
        for path in base_dir.glob(pattern):
            with path.open(encoding="cp950", errors="replace", newline="") as handle:
                for row in csv.DictReader(handle):
                    if str(row.get("enable", "")).strip().lower() != "true":
                        continue
                    raw_tag = row.get("almtag", "")
                    tags.add(_normalise_alarm_text(raw_tag))
                    tags.add(_normalise_alarm_text(raw_tag, remove_system_prefix=True))
                    description = _normalise_alarm_text(row.get("description", ""))
                    if description:
                        descriptions.add(description)
    return tuple(sorted(tags - {""})), tuple(sorted(descriptions))


def _similarity_over_threshold(value: str, references: tuple[str, ...]) -> bool:
    """Return true only for a strict similarity score greater than 80%."""
    if not value or not references:
        return False
    matches = get_close_matches(value, references, n=1, cutoff=ACTION_ALARM_SIMILARITY / 100)
    if not matches:
        return False
    score = SequenceMatcher(None, value, matches[0], autojunk=False).ratio() * 100
    return score > ACTION_ALARM_SIMILARITY


@lru_cache(maxsize=8192)
def is_action_alarm_point(tag: object, description: object) -> bool:
    """Identify a supplied action-alarm point by tag or description similarity."""
    tags, descriptions = _action_alarm_reference()
    raw_tag = "" if pd.isna(tag) else str(tag)
    tag_match = any(
        _similarity_over_threshold(_normalise_alarm_text(raw_tag, strip_prefix), tags)
        for strip_prefix in (False, True)
    )
    return tag_match or _similarity_over_threshold(_normalise_alarm_text(description), descriptions)


def action_alarm_mask(frame: pd.DataFrame) -> pd.Series:
    """Return the supplied-action-alarm membership mask for a dataframe."""
    if ALARM_TAG_COLUMN not in frame.columns or PLANT_COLUMN not in frame.columns:
        return pd.Series(False, index=frame.index)
    tags = frame[ALARM_TAG_COLUMN].fillna("").astype(str)
    descriptions = frame.get(ALARM_DESCRIPTION_COLUMN, pd.Series("", index=frame.index)).fillna("").astype(str)
    candidate = frame[PLANT_COLUMN].fillna("").astype(str).str.strip().str.upper().isin(ACTION_ALARM_PLANTS)
    result = pd.Series(False, index=frame.index)
    if not candidate.any():
        return result
    result.loc[candidate] = [
        is_action_alarm_point(tag, description)
        for tag, description in zip(tags.loc[candidate], descriptions.loc[candidate])
    ]
    return result


def pcb_static_pressure_exclusion_mask(frame: pd.DataFrame) -> pd.Series:
    """Exclude five PCB dust-collector pressure points pending suppression setup."""
    if PLANT_COLUMN not in frame.columns or ALARM_TAG_COLUMN not in frame.columns:
        return pd.Series(False, index=frame.index)
    plants = frame[PLANT_COLUMN].fillna("").astype(str).str.strip().str.upper()
    tags = frame[ALARM_TAG_COLUMN].fillna("").astype(str).map(_normalise_alarm_text)
    return pd.Series(
        [is_pcb_static_pressure_excluded(plant, tag) for plant, tag in zip(plants, tags)],
        index=frame.index,
    )


def is_pcb_static_pressure_excluded(plant: object, tag: object) -> bool:
    """Shared all-surface exclusion for five PCB static-pressure points."""
    if str(plant).strip().upper() != "PCB":
        return False
    normalized_tag = _normalise_alarm_text(tag)
    return any(
        _normalise_alarm_text(target) in normalized_tag
        for target in PCB_STATIC_PRESSURE_EXCLUDE_TAGS
    )


def excluded_alarm_description(description: object) -> bool:
    """Return whether an alarm description matches an approved exclusion rule.

    ``L`` and ``H`` are intentionally literal uppercase substring rules, as
    requested by the alarm-filter SOP.  They are not alarm-status filters.
    """
    text = "" if pd.isna(description) else str(description)
    upper = text.upper()
    return (
        ("MAU" in upper and "液位" in text)
        or any(term in text for term in ("KW", "供電", "用電", "功率", "S2A電力"))
        or (
            any(term in text for term in ("溫度", "濕度", "溼度", "室壓"))
            and any(term in text for term in ("無塵室", "CR"))
        )
        or any(term in text for term in ("再生", "樹脂"))
        or "變頻回授" in text
        or ("區" in text and "溫度" in text)
        or ("區" in text and any(term in text for term in ("溼度", "濕度")))
        or any(term in text for term in ("陽塔", "陰塔", "陽離子塔", "陰離子塔"))
        or "頻率" in text
        or "L" in text
        or "H" in text
    )


def filter_alarm_records(frame: pd.DataFrame) -> pd.DataFrame:
    """Apply the action-list whitelist, then legacy exclusions elsewhere."""
    if frame.empty or ALARM_DESCRIPTION_COLUMN not in frame.columns:
        return frame.copy()
    excluded = frame[ALARM_DESCRIPTION_COLUMN].map(excluded_alarm_description)
    # "頻率" alarms are globally excluded even when a plant/action-list
    # whitelist would otherwise retain the record.
    mandatory_excluded = frame[ALARM_DESCRIPTION_COLUMN].fillna("").astype(str).str.contains(
        "頻率", regex=False
    )
    mandatory_excluded |= pcb_static_pressure_exclusion_mask(frame)
    action_point = action_alarm_mask(frame)
    if PLANT_COLUMN not in frame.columns:
        return frame.loc[~excluded].copy()
    plants = frame[PLANT_COLUMN].fillna("").astype(str).str.strip().str.upper()
    action_plant = plants.isin(ACTION_ALARM_PLANTS)
    # S2過濾條款：排除所有低側／高側 tag（``_L``、``_H``）。
    s2_filter_clause = plants.eq("S2") & frame[ALARM_TAG_COLUMN].fillna("").astype(str).str.upper().str.contains(r"_(?:L|H)", regex=True)
    keep = (
        ((~action_plant & ~excluded) | (action_plant & action_point))
        & ~s2_filter_clause
        & ~mandatory_excluded
    )
    return frame.loc[keep].copy()
