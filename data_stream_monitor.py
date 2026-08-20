"""Detect per-plant EFplant telemetry interruptions and notify Synology Chat."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd


STALE_HOURS = 6
SCRIPT_DIR = Path(__file__).resolve().parent
STATE_PATH = SCRIPT_DIR / "data_stream_alert_state.json"
PY_PROJECT_DIR = Path(r"C:\Users\U01572\Documents\YPput\py")
CHAT_SENDER = PY_PROJECT_DIR / "synology_chat_notify.py"
CHAT_PYTHON = PY_PROJECT_DIR / "myenv" / "Scripts" / "python.exe"


def _normalize_plant(value: object) -> str:
    plant = str(value).strip().upper()
    return "KF1" if plant == "KF" else plant


def equipment_streams(df: pd.DataFrame, classifier: Callable[[str], str]) -> dict[str, pd.Timestamp]:
    required = {"PLANT", "TIMESTAMP", "EQNO"}
    if df.empty or not required.issubset(df.columns):
        return {}
    work = df.copy()
    work["PLANT"] = work["PLANT"].map(_normalize_plant)
    work["TIMESTAMP"] = pd.to_datetime(work["TIMESTAMP"], errors="coerce")
    work["STREAM"] = work["EQNO"].map(lambda value: classifier(str(value)))
    work = work.dropna(subset=["TIMESTAMP"])
    return {
        f"{plant}|設備運轉|{stream}": timestamp
        for (plant, stream), timestamp in work.groupby(["PLANT", "STREAM"])["TIMESTAMP"].max().items()
    }


def quality_streams(df: pd.DataFrame, classifier: Callable[..., tuple[object, object]]) -> dict[str, pd.Timestamp]:
    required = {"PLANT", "TIMESTAMP", "TAGNAME", "EQNAME"}
    if df.empty or not required.issubset(df.columns):
        return {}
    latest: dict[str, pd.Timestamp] = {}
    for row in df.itertuples(index=False):
        plant = _normalize_plant(getattr(row, "PLANT"))
        timestamp = pd.to_datetime(getattr(row, "TIMESTAMP"), errors="coerce")
        if pd.isna(timestamp):
            continue
        category, _ = classifier(
            getattr(row, "TAGNAME"),
            getattr(row, "EQNAME"),
            plant,
            getattr(row, "DESCRIPTION", ""),
        )
        if not category:
            continue
        key = f"{plant}|品質趨勢|{category}"
        if key not in latest or timestamp > latest[key]:
            latest[key] = timestamp
    return latest


def load_state(path: Path = STATE_PATH) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"streams": {}}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"streams": {}}


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, path)


def send_chat_message(message: str) -> bool:
    python_exe = CHAT_PYTHON if CHAT_PYTHON.is_file() else Path(sys.executable)
    if not CHAT_SENDER.is_file():
        print(f"[STREAM][ERROR] Synology Chat sender not found: {CHAT_SENDER}")
        return False
    result = subprocess.run(
        [str(python_exe), str(CHAT_SENDER), "--message", message],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        print(f"[STREAM][ERROR] Synology Chat notification failed: {detail}")
        return False
    return True


def _describe(grouped: dict[str, list[str]]) -> str:
    parts = []
    for board in ("品質趨勢", "設備運轉"):
        names = sorted(set(grouped.get(board, [])))
        if names:
            parts.append(f"{board}({ '、'.join(names) })")
    return "及".join(parts)


def evaluate_streams(
    observations: dict[str, pd.Timestamp],
    *,
    now: datetime | pd.Timestamp | None = None,
    state_path: Path = STATE_PATH,
    sender: Callable[[str], bool] = send_chat_message,
) -> list[str]:
    now_ts = pd.Timestamp(now or datetime.now())
    cutoff = now_ts - pd.Timedelta(hours=STALE_HOURS)
    state = load_state(state_path)
    stream_state = state.setdefault("streams", {})
    transitions: dict[tuple[str, str], dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))

    effective_observations = dict(observations)
    for key, entry in stream_state.items():
        if key in effective_observations or not isinstance(entry, dict):
            continue
        cached_last_seen = pd.to_datetime(entry.get("last_seen"), errors="coerce")
        if pd.notna(cached_last_seen):
            effective_observations[key] = cached_last_seen

    for key, last_seen in effective_observations.items():
        plant, board, detail = key.split("|", 2)
        is_stale = pd.Timestamp(last_seen) < cutoff
        previous_active = bool(stream_state.get(key, {}).get("alert_active", False))
        if is_stale and not previous_active:
            transitions[(plant, "interruption")][board].append(detail)
        elif not is_stale and previous_active:
            transitions[(plant, "recovery")][board].append(detail)

    sent_messages: list[str] = []
    for (plant, transition), grouped in sorted(transitions.items()):
        description = _describe(grouped)
        if transition == "interruption":
            message = f"廠區:{plant}，{description}資料中斷，請檢查資料庫串流是否正常"
            target_active = True
        else:
            message = f"廠區:{plant}，{description}資料已回補或恢復抓到新資料"
            target_active = False
        if not sender(message):
            continue
        sent_messages.append(message)
        for board, details in grouped.items():
            for detail in details:
                key = f"{plant}|{board}|{detail}"
                stream_state.setdefault(key, {})["alert_active"] = target_active

    for key, last_seen in effective_observations.items():
        entry = stream_state.setdefault(key, {})
        entry["last_seen"] = pd.Timestamp(last_seen).isoformat()
        entry["last_checked"] = now_ts.isoformat()
        entry.setdefault("alert_active", False)

    state["updated_at"] = now_ts.isoformat()
    save_state(state, state_path)
    return sent_messages


def monitor_data_streams(
    equipment_df: pd.DataFrame,
    quality_df: pd.DataFrame,
    equipment_classifier: Callable[[str], str],
    quality_classifier: Callable[..., tuple[object, object]],
    *,
    now: datetime | pd.Timestamp | None = None,
) -> list[str]:
    observations = equipment_streams(equipment_df, equipment_classifier)
    observations.update(quality_streams(quality_df, quality_classifier))
    return evaluate_streams(observations, now=now)
