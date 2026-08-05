"""Shared exclusion rules for operational alarm views and weekly reporting."""

from __future__ import annotations

import pandas as pd


ALARM_DESCRIPTION_COLUMN = "ALM_DESCR"


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
        or any(term in text for term in ("陽塔", "陰塔"))
        or "L" in text
        or "H" in text
    )


def filter_alarm_records(frame: pd.DataFrame) -> pd.DataFrame:
    """Exclude SOP-defined alarms by description without mutating ``frame``."""
    if frame.empty or ALARM_DESCRIPTION_COLUMN not in frame.columns:
        return frame.copy()
    mask = frame[ALARM_DESCRIPTION_COLUMN].map(excluded_alarm_description)
    return frame.loc[~mask].copy()
