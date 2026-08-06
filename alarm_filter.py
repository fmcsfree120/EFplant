"""Shared exclusion rules for operational alarm views and weekly reporting."""

from __future__ import annotations

import pandas as pd


ALARM_DESCRIPTION_COLUMN = "ALM_DESCR"
PLANT_COLUMN = "PLANT"

# Alarm points in the supplied S2A/T2A action-alarm lists must always remain
# visible in operational-risk calculations.  UL3 is the historical tag prefix
# for the LC3 dashboard plant.  The supplied UPW.UPW... action-alarm tags
# belong to S2; S2 is already covered by this plant-wide exemption.
ACTION_ALARM_PLANTS = frozenset({
    "S2", "S2A", "S3", "T2A", "HJ1", "HJ2", "PCB", "LC3",
})


def canonical_alarm_plant(plant: object) -> str:
    """Return the dashboard-compatible plant label for an alarm row."""
    value = "" if pd.isna(plant) else str(plant).strip().upper()
    return "KF1" if value == "KF" else value


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
        or "L" in text
        or "H" in text
    )


def filter_alarm_records(frame: pd.DataFrame) -> pd.DataFrame:
    """Apply SOP exclusions except for action-alarm plants.

    A frame without ``PLANT`` retains the conservative legacy behaviour so
    callers cannot accidentally bypass the policy merely by omitting context.
    """
    if frame.empty or ALARM_DESCRIPTION_COLUMN not in frame.columns:
        return frame.copy()
    excluded = frame[ALARM_DESCRIPTION_COLUMN].map(excluded_alarm_description)
    if PLANT_COLUMN not in frame.columns:
        return frame.loc[~excluded].copy()
    action_plant = frame[PLANT_COLUMN].map(canonical_alarm_plant).isin(ACTION_ALARM_PLANTS)
    return frame.loc[~excluded | action_plant].copy()
