#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate _weekly_analysis.json from local EFplant CSV backups.

This is the no-API weekly report writer. It turns measured data into a
structured report JSON that build_pdf_from_json.py can render as PDF.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd


from alarm_filter import filter_alarm_records

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


BASE_DIR = Path(__file__).parent
RUN_RATE_CSV = BASE_DIR / "latest_run_rate_backup.csv"
QUALITY_CSV = BASE_DIR / "latest_quality_backup.csv"
DATA_CSV = BASE_DIR / "latest_data_backup.csv"
ALARM_KF1_CSV = BASE_DIR / "latest_alarm_history_backup.csv"
ALARM_OTHER_CSV = BASE_DIR / "latest_alarm_history_other_backup.csv"
OUT_JSON = BASE_DIR / "_weekly_analysis.json"
MEMORY_JSON = BASE_DIR / "weekly_report_memory.json"
OPENAI_KEY_PATH = BASE_DIR / "openaiKEY.txt"
SPECIAL_CASE_TXT = BASE_DIR / "specialcase-weekly.txt"
LOCAL_CONTEXT_DIR = BASE_DIR / "localcontext"
LOCAL_CONTEXT_MD = LOCAL_CONTEXT_DIR / "weekly_report_api_context.md"
API_PROMPT_MD = LOCAL_CONTEXT_DIR / "weekly_report_api_prompt.md"
LOCAL_CONTEXT_RETENTION_DAYS = 31
OPENAI_MODEL = "gpt-4o-mini"

SEMICONDUCTOR_FACILITY_REFERENCE = """
半導體廠務技術參照摘要：
1. UPW/DI：半導體製程以超純水清洗晶圓、稀釋化學品及支援關鍵冷卻；水阻接近 18.18 MΩ·cm 代表離子污染低，水阻下降或導電度上升需優先聯想到離子突破、樹脂/RO/EDI 效能、取樣管路 CO2 滲入或支管污染。
2. UPW 品質風險：粒子、TOC、金屬離子、矽與微生物會造成晶圓缺陷或製程變異；若水質異常，建議行動需包含儀表校正、POD/POC 交叉採樣、耗材狀態與支管沖洗。
3. 冰機/冷卻水：KW/RT 偏高通常與冷凝器結垢、冷卻水溫差不足、冷卻塔散熱不佳、冷媒量、負載分配或旁通控制有關；建議行動優先檢查 Approach、ΔT、流量、冷卻水水質與主機負載率。
4. 空壓/CDA：CMM/kWh 偏低常見原因包含洩漏、壓力設定過高、乾燥機壓損、濾芯堵塞、進氣溫度偏高、卸載運轉或多機負載分配不佳；建議先查壓力、露點、洩漏與耗氣尖峰。
5. 廢水/pH：pH 偏離需聯想到酸鹼廢液負荷、加藥泵、pH 電極校正、攪拌與中和槽停留時間；表述需同時注意排放合規與對後段處理的衝擊。
6. FMCS/資料完整性：資料中斷或回傳不足會形成監控盲區；建議行動需包含 PLC/SCADA 通訊、網路節點、資料匯入排程與現場趨勢交叉確認。
資料來源參照：SEMI/ASTM UPW 指南摘要、DOE 壓縮空氣與冰水系統節能實務、半導體廠常見 UPW/CDA/HVAC/廢水廠務維運經驗。
"""

PLANT_ORDER = ["T2A", "S2A", "PCB", "S2", "S3", "HJ1", "HJ2", "LC2", "LC3", "TH", "KF1"]
LEVEL_WEIGHT = {"CRITICAL": 3, "MAJOR": 2, "WARNING": 1, "NORMAL": 0}

TARGETS = {
    "run_rate": 75.0,
    "run_rate_good": 95.0,
    "chiller_kwrt": 0.70,
    "compressor_cmmkwh": 7.0,
    "upw_resistance": 15.0,
    "conductivity": 5.0,
    "ph_low": 6.0,
    "ph_high": 9.0,
}
FLATLINE_MIN_POINTS = 24
FLATLINE_ITEM_PREFIX = "整週數值無變動"
FLATLINE_CHECK_TEXT = "應檢查儀表與通訊數據流是否故障(Keep Last)"
DATA_GAP_MIN_HOURS = 1.0
FLATLINE_MIN_HOURS = 48.0
ZERO_MIN_HOURS = 4.0
ALARM_CONNECTED_PLANTS = {"KF1", "HF", "HJ1", "HJ2", "LC2", "LC3", "PCB", "S2", "S2A", "S3", "T2A"}


@dataclass
class Issue:
    plant: str
    item: str
    phenomenon: str
    impact: str
    level: str
    action: str
    score: float
    date: str = "全週"
    metric: str = ""
    current: str = ""
    target: str = ""
    trend: str = "偏低"
    forecast: str = ""


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    for enc in ("utf-8-sig", "utf-8", "cp950"):
        try:
            df = pd.read_csv(path, encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        df = pd.read_csv(path)
    if "PLANT" in df.columns:
        df["PLANT"] = (
            df["PLANT"].astype(str).str.strip().str.upper()
            .replace({"KF": "KF1"})
        )
    return df


def clean_float(v: Any) -> float | None:
    try:
        x = float(v)
    except Exception:
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return x


def fmt_num(v: float | None, digits: int = 1) -> str:
    if v is None:
        return "N/A"
    text = f"{v:.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def read_openai_api_key() -> str | None:
    if not OPENAI_KEY_PATH.exists():
        return None
    key = OPENAI_KEY_PATH.read_text(encoding="utf-8-sig", errors="ignore").strip()
    return key or None


def load_special_case_text() -> str:
    """每週例外情況登記檔：由使用者手動維護，空白代表本週無特殊情況。"""
    if not SPECIAL_CASE_TXT.exists():
        return ""
    try:
        text = SPECIAL_CASE_TXT.read_text(encoding="utf-8-sig", errors="ignore")
    except OSError:
        return ""
    return text.strip()


def split_special_case_items(text: str) -> list[str]:
    items = re.split(r"\n(?=\s*\d+[\.\、])", text.strip())
    return [item.strip() for item in items if item.strip()]


def match_special_case_plants(item: str, known_plants: set[str]) -> list[str]:
    matched = []
    for plant in sorted(known_plants, key=len, reverse=True):
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(plant)}(?![A-Za-z0-9])", item):
            matched.append(plant)
    return matched


def assign_special_cases(text: str, known_plants: set[str]) -> dict[str, list[str]]:
    """Map each item in specialcase-weekly.txt to the plant(s) it mentions."""
    by_plant: dict[str, list[str]] = {}
    for item in split_special_case_items(text):
        clean = re.sub(r"^\s*\d+[\.\、]\s*", "", item).strip()
        if not clean:
            continue
        for plant in match_special_case_plants(item, known_plants):
            by_plant.setdefault(plant, []).append(clean)
    return by_plant


def special_case_action_rows(known_plants: set[str]) -> list[dict[str, Any]]:
    """Read specialcase-weekly.txt and turn each item into a P7 action row.

    Raw text is kept here so information is never lost even if the OpenAI
    polishing step is unavailable; polish_weekly_report_text_with_openai() then
    rewrites the "action" text (like any other action row) into report tone.
    Priority is left blank and assigned later when the full actions list is
    renumbered, so these rows are appended after the existing priority order.
    """
    text = load_special_case_text()
    if not text:
        return []
    rows = []
    for item in split_special_case_items(text):
        clean = re.sub(r"^\s*\d+[\.\、]\s*", "", item).strip()
        if not clean:
            continue
        matched = match_special_case_plants(item, known_plants)
        rows.append({
            "priority": "",
            "plant": "/".join(matched) if matched else "全廠",
            "item": "本週特殊情況",
            "action": clean,
            "level": "WARNING",
        })
    return rows


def prune_local_context(now: datetime | None = None) -> None:
    LOCAL_CONTEXT_DIR.mkdir(exist_ok=True)
    now = now or datetime.now()
    cutoff = now - timedelta(days=LOCAL_CONTEXT_RETENTION_DAYS)
    for path in LOCAL_CONTEXT_DIR.iterdir():
        if not path.is_file():
            continue
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            continue
        if mtime < cutoff:
            try:
                path.unlink()
            except OSError:
                pass


def compact_report_for_context(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "meta": report.get("meta", {}),
        "overview": report.get("overview", {}),
        "top3": report.get("top3", [])[:3],
        "trend_warnings": report.get("trend_warnings", [])[:5],
        "health_scores": report.get("health_scores", []),
        "actions": report.get("actions", [])[:5],
    }


def build_api_prompt_markdown() -> str:
    return f"""# EFplant 週報文字生成 API Prompt

## 角色
你是半導體廠務 FMCS 週報文字編修助手，負責把 Python 規則已判定的異常、等級、趨勢與數值，改寫成主管週報可直接閱讀的文字。

## 固定技術參照
{SEMICONDUCTOR_FACILITY_REFERENCE.strip()}

## 生成範圍
- 本週重點 TOP3：event、impact、action。
- 本週異常事件清單：phenomenon、impact。
- 未來風險預警：forecast。
- 各廠區健康度評分：advantage、weakness。
- 管理建議與追蹤事項：action。

## 管理建議與追蹤事項中的廠區例外情況
- 部分 actions 列的 item 為「本週特殊情況」，代表現場人員本週在 specialcase-weekly.txt 手動登記的例外情況，屬本週資料的一部分。
- 這類 action 文字必須修飾為主管週報語氣，只能改寫語氣與用詞，不得刪減原始事實，也不得新增登記內容沒有提到的臆測原因，不可搬移到其他 plant。

## 不可變更
- 不得改 rank、priority、date、plant、item、metric、current、target、trend、level、score、status。
- 不得新增或刪除列。
- 不得新增本週資料沒有支持的事實。
- 每個判斷必須能回推到輸入 JSON 的數值、等級或趨勢。
- 警報資料涵蓋不足本身屬於監控風險，不得寫成「不評分」或解讀為零警報。
- 不主動產生「運轉率不計分」等制度說明；運轉率低於 50% 時，只依 Python 既有列在管理建議中描述現象。

## 文字要求
- 繁體中文。
- 主管週報語氣，短、具體、可執行。
- 優先使用半導體廠務語彙：UPW、水阻、導電度、CDA、冰機 KW/RT、冷卻水、廢水中和、FMCS、SCADA。
- 不寫空泛句，例如「持續關注」、「加強管理」；需指出查核對象或下一步。
- 浮點數小數點後無意義的尾端 0 不顯示。
- 只輸出 JSON。
"""


def load_local_context_text(limit_chars: int = 12000) -> str:
    if not LOCAL_CONTEXT_MD.exists():
        return ""
    try:
        text = LOCAL_CONTEXT_MD.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    return text[-limit_chars:]


def format_report_markdown(report: dict[str, Any]) -> str:
    meta = report.get("meta", {})
    ov = report.get("overview", {})
    lines = [
        f"# EFplant FMCS W{meta.get('week_num')} 週報標準格式",
        "",
        f"- 產生時間: {meta.get('generated_at')}",
        f"- 資料期間: {meta.get('data_from')} ~ {meta.get('data_to')}",
        f"- 異常統計: Critical {ov.get('critical_count')} / Major {ov.get('major_count')} / Warning {ov.get('warning_count')}",
        f"- 表現最佳廠區: {ov.get('best_plants')}",
        f"- 待改善廠區: {ov.get('worst_plants')}",
        "",
        "## 本週重點 TOP 3",
    ]
    for item in report.get("top3", []):
        lines.extend([
            f"### {item.get('rank')}. {item.get('event')}",
            f"- 影響: {item.get('impact')}",
            f"- 等級: {item.get('level')}",
            f"- 建議行動: {item.get('action')}",
        ])
    lines.append("")
    lines.append("## 各廠區健康度評分")
    for item in report.get("health_scores", []):
        lines.extend([
            f"### {item.get('rank')}. {item.get('plant')} - {item.get('score')} ({item.get('status')})",
            f"- 主要優勢: {item.get('advantage')}",
            f"- 主要弱點: {item.get('weakness')}",
        ])
    lines.append("")
    lines.append("## 管理建議與追蹤事項")
    for item in report.get("actions", []):
        lines.append(f"- P{item.get('priority')} [{item.get('level')}] {item.get('plant')} {item.get('item')}: {item.get('action')}")
    lines.append("")
    return "\n".join(lines)


def update_local_context(report: dict[str, Any]) -> None:
    prune_local_context()
    write_api_prompt_file()
    meta = report.get("meta", {})
    week = meta.get("week_num", "NA")
    data_to = str(meta.get("data_to", datetime.now().strftime("%Y-%m-%d"))).replace("-", "")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = f"{data_to}_W{week}_weekly_report_standard"

    md_text = format_report_markdown(report)
    (LOCAL_CONTEXT_DIR / f"{base_name}.md").write_text(md_text, encoding="utf-8")
    (LOCAL_CONTEXT_DIR / f"{base_name}.json").write_text(
        json.dumps(compact_report_for_context(report), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    header = [
        "# EFplant 週報 API 上下文記憶",
        "",
        f"- 保留天數: {LOCAL_CONTEXT_RETENTION_DAYS} 天",
        "- 用途: 提供 OpenAI 生成週報指定文字欄位時參照。",
        "- 標準: 文字需短、具體、可回推數據，不新增報表沒有的事實。",
        "",
    ]
    existing = load_local_context_text()
    entry = [
        f"## {stamp} W{week} ({meta.get('data_from')} ~ {meta.get('data_to')})",
        "",
        md_text,
    ]
    combined = "\n".join(header + [existing, "\n".join(entry)])
    LOCAL_CONTEXT_MD.write_text(combined[-60000:], encoding="utf-8")


def openai_chat_json(api_key: str, messages: list[dict[str, str]], temperature: float = 0.2) -> dict[str, Any]:
    payload = {
        "model": OPENAI_MODEL,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        "messages": messages,
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        detail = body[:800]
        try:
            err = json.loads(body).get("error", {})
            detail = " | ".join(
                str(v) for v in [
                    f"status={exc.code}",
                    err.get("type"),
                    err.get("code"),
                    err.get("message"),
                ] if v
            )
        except json.JSONDecodeError:
            detail = f"status={exc.code} body={detail}"
        raise RuntimeError(detail) from exc
    content = data["choices"][0]["message"]["content"]
    return json.loads(content)


def report_text_payload(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "meta": report.get("meta", {}),
        "overview": report.get("overview", {}),
        "top3": [
            {
                "idx": idx,
                "rank": row.get("rank"),
                "event": row.get("event"),
                "impact": row.get("impact"),
                "level": row.get("level"),
                "action": row.get("action"),
            }
            for idx, row in enumerate(report.get("top3", []))
        ],
        "events": [
            {
                "idx": idx,
                "date": row.get("date"),
                "plant": row.get("plant"),
                "item": row.get("item"),
                "phenomenon": row.get("phenomenon"),
                "impact": row.get("impact"),
                "level": row.get("level"),
            }
            for idx, row in enumerate(report.get("events", []))
        ],
        "trend_warnings": [
            {
                "idx": idx,
                "plant": row.get("plant"),
                "metric": row.get("metric"),
                "current": row.get("current"),
                "target": row.get("target"),
                "trend": row.get("trend"),
                "forecast": row.get("forecast"),
                "level": row.get("level"),
            }
            for idx, row in enumerate(report.get("trend_warnings", []))
        ],
        "health_scores": [
            {
                "idx": idx,
                "rank": row.get("rank"),
                "plant": row.get("plant"),
                "score": row.get("score"),
                "advantage": row.get("advantage"),
                "weakness": row.get("weakness"),
                "status": row.get("status"),
            }
            for idx, row in enumerate(report.get("health_scores", []))
        ],
        "actions": [
            {
                "idx": idx,
                "priority": row.get("priority"),
                "plant": row.get("plant"),
                "item": row.get("item"),
                "action": row.get("action"),
                "level": row.get("level"),
            }
            for idx, row in enumerate(report.get("actions", []))
        ],
    }


def write_api_prompt_file() -> None:
    LOCAL_CONTEXT_DIR.mkdir(exist_ok=True)
    API_PROMPT_MD.write_text(build_api_prompt_markdown(), encoding="utf-8")


def set_text_if_present(row: dict[str, Any], update: dict[str, Any], fields: list[str]) -> bool:
    changed = False
    for field in fields:
        value = str(update.get(field, "")).strip()
        if not value:
            continue
        row[field] = value
        changed = True
    return changed


def polish_weekly_report_text_with_openai(report: dict[str, Any]) -> bool:
    """Use OpenAI only for report wording fields; Python keeps metrics and ranking."""
    api_key = read_openai_api_key()
    if not api_key:
        return False
    if not any(report.get(k) for k in ("top3", "events", "trend_warnings", "health_scores", "actions")):
        return False

    write_api_prompt_file()
    prompt_data = {
        "technical_reference": SEMICONDUCTOR_FACILITY_REFERENCE.strip(),
        "current_report": report_text_payload(report),
        "local_context_memory": load_local_context_text(limit_chars=6000),
    }
    messages = [
        {
            "role": "system",
            "content": (
                build_api_prompt_markdown()
                + "\n輸出格式："
                + "{\"top3\":[{\"idx\":0,\"event\":\"...\",\"impact\":\"...\",\"action\":\"...\"}],"
                + "\"events\":[{\"idx\":0,\"phenomenon\":\"...\",\"impact\":\"...\"}],"
                + "\"trend_warnings\":[{\"idx\":0,\"forecast\":\"...\"}],"
                + "\"health_scores\":[{\"idx\":0,\"advantage\":\"...\",\"weakness\":\"...\"}],"
                + "\"actions\":[{\"idx\":0,\"action\":\"...\"}]}"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(prompt_data, ensure_ascii=False),
        },
    ]
    try:
        result = openai_chat_json(api_key, messages)
    except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError, OSError, RuntimeError) as exc:
        report.setdefault("meta", {})["openai_text_error"] = str(exc)[:200]
        return False

    changed = False

    update_specs = [
        ("top3", ["event", "impact", "action"]),
        ("events", ["phenomenon", "impact"]),
        ("trend_warnings", ["forecast"]),
        ("health_scores", ["advantage", "weakness"]),
        ("actions", ["action"]),
    ]
    for section, fields in update_specs:
        rows = report.get(section, [])
        if not isinstance(rows, list):
            continue
        for update in result.get(section, []):
            try:
                idx = int(update.get("idx"))
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(rows):
                changed = set_text_if_present(rows[idx], update, fields) or changed
    return changed


def level_for(kind: str, value: float) -> str:
    if kind == "run_rate":
        if value < 65:
            return "CRITICAL"
        if value < TARGETS["run_rate"]:
            return "MAJOR"
        if value < 85:
            return "WARNING"
        return "NORMAL"
    if kind == "chiller":
        if value > 0.85:
            return "CRITICAL"
        if value > TARGETS["chiller_kwrt"]:
            return "MAJOR"
        if value > 0.65:
            return "WARNING"
        return "NORMAL"
    if kind == "compressor":
        if value < 5.5:
            return "CRITICAL"
        if value < TARGETS["compressor_cmmkwh"]:
            return "MAJOR"
        if value < 7.5:
            return "WARNING"
        return "NORMAL"
    if kind == "resistance":
        if value < 8:
            return "CRITICAL"
        if value < TARGETS["upw_resistance"]:
            return "MAJOR"
        if value < 16:
            return "WARNING"
        return "NORMAL"
    if kind == "conductivity":
        if value > 10:
            return "CRITICAL"
        if value > TARGETS["conductivity"]:
            return "MAJOR"
        if value > 3:
            return "WARNING"
        return "NORMAL"
    if kind == "ph":
        if value < 5.5 or value > 9.5:
            return "CRITICAL"
        if value < TARGETS["ph_low"] or value > TARGETS["ph_high"]:
            return "MAJOR"
        if value < 6.5 or value > 8.5:
            return "WARNING"
        return "NORMAL"
    return "NORMAL"


def rank_level(level: str, score: float) -> tuple[int, float]:
    return (LEVEL_WEIGHT.get(level, 0), score)


def date_label(ts: pd.Series | None, data_from: pd.Timestamp, data_to: pd.Timestamp) -> str:
    if ts is None or len(ts) == 0:
        return "全週"
    mn = pd.to_datetime(ts.min())
    mx = pd.to_datetime(ts.max())
    if mn.date() <= data_from.date() and mx.date() >= data_to.date():
        return "全週"
    if mn.date() == mx.date():
        return mn.strftime("%m/%d")
    return f"{mn:%m/%d}-{mx:%m/%d}"


def choose_period(frames: list[pd.DataFrame]) -> tuple[pd.Timestamp, pd.Timestamp]:
    latest: pd.Timestamp | None = None
    for df in frames:
        if df.empty or "TIMESTAMP" not in df.columns:
            continue
        ts = pd.to_datetime(df["TIMESTAMP"], errors="coerce").dropna()
        if ts.empty:
            continue
        mx = ts.max()
        latest = mx if latest is None or mx > latest else latest
    if latest is None:
        latest = pd.Timestamp(datetime.now())
    end = latest.normalize()
    start = end - pd.Timedelta(days=6)
    return start, end


def filter_period(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    if df.empty or "TIMESTAMP" not in df.columns:
        return df.copy()
    out = df.copy()
    out["TIMESTAMP"] = pd.to_datetime(out["TIMESTAMP"], errors="coerce")
    out = out.dropna(subset=["TIMESTAMP"])
    return out[(out["TIMESTAMP"] >= start) & (out["TIMESTAMP"] < end + pd.Timedelta(days=1))]


def summarize_group(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    tmp = df.copy()
    tmp["VALUE"] = pd.to_numeric(tmp["VALUE"], errors="coerce")
    return (
        tmp.groupby(["PLANT", "EQNAME", "DESCRIPTION", "TAGNAME"], dropna=False)
        .agg(mean=("VALUE", "mean"), min=("VALUE", "min"), max=("VALUE", "max"), count=("VALUE", "count"))
        .reset_index()
    )


def short_text(value: Any, limit: int = 24) -> str:
    text = str(value).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def strip_plant_prefix(text: Any, plant: str) -> str:
    desc = str(text).strip()
    if not desc:
        return desc
    prefixes = sorted(set(PLANT_ORDER + [plant]), key=len, reverse=True)
    pattern = "|".join(re.escape(p) for p in prefixes if p)
    if pattern:
        desc = re.sub(rf"^(?:{pattern})(?:[_\-\s:：]+)?", "", desc, flags=re.IGNORECASE)
    desc = re.sub(r"\s*\(F_CV\)\s*$", "", desc, flags=re.IGNORECASE)
    return desc.strip() or str(text).strip()


def is_chiller(eq: str, desc: str, tag: str) -> bool:
    text = f"{eq} {desc} {tag}".upper()
    return "冰機" in text or "CHU" in text or "KW/RT" in text


def is_compressor(eq: str, desc: str, tag: str) -> bool:
    text = f"{eq} {desc} {tag}".upper()
    return "空壓" in text or "CDA" in text or "CMM/KWH" in text


def is_resistance(eq: str, desc: str, tag: str) -> bool:
    text = f"{eq} {desc} {tag}".upper()
    return "電阻" in text or "水阻" in text or "RIT" in text


def is_conductivity(eq: str, desc: str, tag: str) -> bool:
    text = f"{eq} {desc} {tag}".upper()
    return "導電" in text or "COND" in text or "CIT" in text


def is_ph(eq: str, desc: str, tag: str) -> bool:
    text = f"{eq} {desc} {tag}".upper()
    return "PH" in text or "中和" in text


def is_pressure(eq: str, desc: str, tag: str) -> bool:
    text = f"{eq} {desc} {tag}".upper()
    return "靜壓" in text or "差壓" in text or "DPT" in text or "PIT" in text


def analyze_flatline_quality(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> list[Issue]:
    """Find non-run-rate points whose value stayed unchanged for the whole week."""
    if df.empty:
        return []

    tmp = df.copy()
    tmp["VALUE"] = pd.to_numeric(tmp["VALUE"], errors="coerce")
    tmp = tmp.dropna(subset=["TIMESTAMP", "VALUE"])
    if tmp.empty:
        return []

    grouped = (
        tmp.groupby(["PLANT", "EQNAME", "DESCRIPTION", "TAGNAME"], dropna=False)
        .agg(
            min=("VALUE", "min"),
            max=("VALUE", "max"),
            count=("VALUE", "count"),
            first_ts=("TIMESTAMP", "min"),
            last_ts=("TIMESTAMP", "max"),
        )
        .reset_index()
    )

    issues: list[Issue] = []
    for _, row in grouped.iterrows():
        count = int(row["count"])
        if count < FLATLINE_MIN_POINTS:
            continue
        first_ts = pd.to_datetime(row["first_ts"])
        last_ts = pd.to_datetime(row["last_ts"])
        if first_ts.date() > start.date() or last_ts.date() < end.date():
            continue

        mn = clean_float(row["min"])
        mx = clean_float(row["max"])
        if mn is None or mx is None or mn != mx:
            continue

        plant = str(row["PLANT"])
        desc = str(row["DESCRIPTION"])
        tag = str(row["TAGNAME"])
        desc_for_report = strip_plant_prefix(desc, plant)
        label = short_text(desc or tag, 26)
        issues.append(Issue(
            plant=plant,
            item=f"{FLATLINE_ITEM_PREFIX}：{label}",
            phenomenon=f"{desc} 本週 {count} 筆資料皆為 {fmt_num(mn, 3)}，變動幅度 0%。",
            impact=FLATLINE_CHECK_TEXT,
            level="WARNING",
            action=FLATLINE_CHECK_TEXT,
            score=float(count),
            metric="數值持平",
            current=f"{fmt_num(mn, 3)}（{count}筆，0%）",
            target="排除資料凍結",
            trend="持平",
            forecast=f"{desc_for_report}：{FLATLINE_CHECK_TEXT}",
        ))

    issues.sort(key=lambda i: i.score, reverse=True)
    return issues


def _signal_label(row: pd.Series) -> str:
    return str(row.get("DESCRIPTION") or row.get("EQNAME") or row.get("EQNO") or row.get("TAGNAME") or "未命名訊號")


def _is_analog_signal(group: pd.DataFrame) -> bool:
    text = " ".join(
        str(group.iloc[0].get(c, ""))
        for c in ("TAGNAME", "DESCRIPTION", "EQNAME", "EQNO")
    ).upper()
    digital_tokens = ("RUN", "STOP", "STATUS", "COMMAND", "CMD", "ONOFF", "ON/OFF", "ALARM")
    return not any(token in text for token in digital_tokens)


def analyze_data_quality(
    frames: list[pd.DataFrame], start: pd.Timestamp, end: pd.Timestamp
) -> tuple[list[dict[str, Any]], list[Issue]]:
    """Locate curve gaps, zero signals and rolling >=48 hour Keep Last segments."""
    rows: list[dict[str, Any]] = []
    issues: list[Issue] = []
    normalized: list[pd.DataFrame] = []
    for frame in frames:
        if frame.empty or not {"PLANT", "TIMESTAMP", "TAGNAME", "VALUE"}.issubset(frame.columns):
            continue
        part = filter_period(frame, start, end).copy()
        part["VALUE"] = pd.to_numeric(part["VALUE"], errors="coerce")
        part = part.dropna(subset=["TIMESTAMP", "VALUE"])
        if not part.empty:
            normalized.append(part)
    if not normalized:
        return rows, issues

    data = pd.concat(normalized, ignore_index=True, sort=False)
    group_cols = ["PLANT", "TAGNAME"]
    for (plant, tag), group in data.groupby(group_cols, dropna=False):
        group = group.sort_values("TIMESTAMP").drop_duplicates("TIMESTAMP").reset_index(drop=True)
        if len(group) < 3 or not _is_analog_signal(group):
            continue
        label = _signal_label(group.iloc[0])
        times = group["TIMESTAMP"].reset_index(drop=True)
        values = group["VALUE"].reset_index(drop=True)
        diffs = times.diff().dt.total_seconds().div(3600)
        positive = diffs[(diffs > 0) & (diffs <= 24)]
        cadence = float(positive.median()) if not positive.empty else 1.0
        gap_threshold = max(DATA_GAP_MIN_HOURS, cadence * 2.5)

        for idx in diffs[diffs > gap_threshold].index:
            gap_hours = float(diffs.iloc[idx])
            gap_start = times.iloc[idx - 1]
            gap_end = times.iloc[idx]
            level = "CRITICAL" if gap_hours >= 24 else "MAJOR" if gap_hours >= 4 else "WARNING"
            rows.append({
                "plant": str(plant), "type": "曲線空窗", "point": short_text(label, 34),
                "period": f"{gap_start:%m/%d %H:%M}～{gap_end:%m/%d %H:%M}",
                "duration": f"{fmt_num(gap_hours)} 小時", "detail": f"推估取樣週期 {fmt_num(cadence, 2)} 小時",
                "level": level,
            })
            issues.append(Issue(
                plant=str(plant), item="資料中斷／曲線空窗",
                phenomenon=f"{label} 於 {gap_start:%m/%d %H:%M}～{gap_end:%m/%d %H:%M} 無曲線，持續 {fmt_num(gap_hours)} 小時。",
                impact="形成 FMCS 監控盲區，期間無法確認設備或製程狀態。",
                level=level, action=f"檢查 {plant} 的儀表、PLC/SCADA 通訊、網路節點及資料匯入排程。",
                score=gap_hours, date=f"{gap_start:%m/%d}", metric="資料完整性",
                current=f"中斷 {fmt_num(gap_hours)} 小時", target="曲線連續無空窗", trend="下降",
                forecast="若持續中斷，將造成監控盲區與異常追溯困難。",
            ))

        zero_mask = values.abs() <= 1e-12
        if zero_mask.any():
            segment = (zero_mask != zero_mask.shift(fill_value=False)).cumsum()
            for _, idxs in group.groupby(segment).groups.items():
                idxs = list(idxs)
                local = group.loc[idxs].sort_values("TIMESTAMP")
                if local.empty or abs(float(local.iloc[0]["VALUE"])) > 1e-12:
                    continue
                z_start, z_end = local["TIMESTAMP"].iloc[0], local["TIMESTAMP"].iloc[-1]
                duration = max(0.0, (z_end - z_start).total_seconds() / 3600)
                all_zero = bool(zero_mask.all())
                if not all_zero and duration < ZERO_MIN_HOURS:
                    continue
                kind = "持續為 0" if all_zero else "降為 0"
                level = "MAJOR" if all_zero or duration >= 24 else "WARNING"
                rows.append({
                    "plant": str(plant), "type": kind, "point": short_text(label, 34),
                    "period": f"{z_start:%m/%d %H:%M}～{z_end:%m/%d %H:%M}",
                    "duration": f"{fmt_num(duration)} 小時", "detail": "類比值為 0，需確認是否符合操作狀態",
                    "level": level,
                })
                issues.append(Issue(
                    plant=str(plant), item=f"訊號{kind}",
                    phenomenon=f"{label} {kind}，自 {z_start:%m/%d %H:%M} 起持續 {fmt_num(duration)} 小時。",
                    impact="可能為儀表、電源、I/O 或訊號迴路異常。",
                    level=level, action=f"比對 {plant} 現場儀表及操作狀態，檢查電源、I/O 與訊號迴路。",
                    score=max(duration, 1.0), date=f"{z_start:%m/%d}", metric="零值訊號",
                    current=f"{kind} {fmt_num(duration)} 小時", target="依製程正常變動", trend="下降",
                    forecast="若非正常停機狀態，可能已失去有效量測。",
                ))
                break

        change_group = values.ne(values.shift()).cumsum()
        for _, idxs in group.groupby(change_group).groups.items():
            local = group.loc[list(idxs)].sort_values("TIMESTAMP")
            if len(local) < 2:
                continue
            f_start, f_end = local["TIMESTAMP"].iloc[0], local["TIMESTAMP"].iloc[-1]
            duration = (f_end - f_start).total_seconds() / 3600
            if duration < FLATLINE_MIN_HOURS:
                continue
            fixed = float(local["VALUE"].iloc[0])
            rows.append({
                "plant": str(plant), "type": "Keep Last", "point": short_text(label, 34),
                "period": f"{f_start:%m/%d %H:%M}～{f_end:%m/%d %H:%M}",
                "duration": f"{fmt_num(duration)} 小時", "detail": f"固定值 {fmt_num(fixed, 3)}；振幅 0%",
                "level": "MAJOR",
            })
            issues.append(Issue(
                plant=str(plant), item="超過48小時數值無變動",
                phenomenon=f"{label} 固定於 {fmt_num(fixed, 3)}，連續 {fmt_num(duration)} 小時振幅 0%。",
                impact=FLATLINE_CHECK_TEXT, level="MAJOR",
                action=FLATLINE_CHECK_TEXT, score=duration, date=f"{f_start:%m/%d}",
                metric="Keep Last", current=f"{fmt_num(fixed, 3)}（{fmt_num(duration)} 小時，0%）",
                target="數值應隨製程合理變動", trend="持平",
                forecast=f"{strip_plant_prefix(label, str(plant))}：{FLATLINE_CHECK_TEXT}",
            ))
            break

    type_order = {"曲線空窗": 0, "降為 0": 1, "持續為 0": 1, "Keep Last": 2}
    rows.sort(key=lambda r: (LEVEL_WEIGHT.get(r["level"], 0), -type_order.get(r["type"], 9)), reverse=True)
    return rows, issues


def analyze_alarm_risk(start: pd.Timestamp, end: pd.Timestamp) -> list[dict[str, Any]]:
    frames: list[pd.DataFrame] = []
    for path in (ALARM_KF1_CSV, ALARM_OTHER_CSV):
        frame = read_csv(path)
        if not frame.empty:
            frames.append(frame)
    if not frames:
        return []
    alarms = pd.concat(frames, ignore_index=True, sort=False)
    alarms["TIME"] = pd.to_datetime(alarms.get("ALM_NATIVETIMELAST"), errors="coerce")
    alarms = alarms.dropna(subset=["TIME"])
    alarms = filter_alarm_records(alarms)
    period_hours = max((end - start).total_seconds() / 3600, 1.0)
    expected_days = max((end.normalize() - start.normalize()).days + 1, 1)
    results: list[dict[str, Any]] = []
    for plant in sorted(ALARM_CONNECTED_PLANTS, key=lambda p: (PLANT_ORDER.index(p), p) if p in PLANT_ORDER else (99, p)):
        source = alarms[alarms["PLANT"].astype(str).str.strip().str.upper() == plant].copy()
        period = source[(source["TIME"] >= start) & (source["TIME"] <= end)].copy()
        if period.empty:
            results.append({
                "plant": plant, "risk_score": 100.0, "health_score": 0.0, "abnormal": 0,
                "critical": 0, "active_high": 0, "coverage": 0.0, "freshness": "無當週資料",
                "top_risk": "本週無警報資料，疑似資料中斷", "status": "CRITICAL",
            })
            continue
        for col in ("ALM_ALMSTATUS", "ALM_ALMPRIORITY", "ALM_TAGNAME", "ALM_DESCR"):
            period[col] = period.get(col, "").fillna("").astype(str).str.strip().str.upper()
        actual_days = period["TIME"].dt.normalize().nunique()
        coverage = min(100.0, actual_days / expected_days * 100)
        latest = period.sort_values("TIME").drop_duplicates("ALM_TAGNAME", keep="last")
        weights = {"OK": 0, "CFN": 30, "LO": 60, "HI": 60, "LOLO": 90, "HIHI": 90}
        tag_risk = latest["ALM_ALMSTATUS"].map(weights).fillna(30).astype(float)
        tag_risk += (latest["ALM_ALMPRIORITY"] == "CRITICAL").astype(int) * 10
        risk_score = float(tag_risk.clip(upper=100).mean()) if len(latest) else 0.0
        abnormal = int((period["ALM_ALMSTATUS"] != "OK").sum())
        critical = int((period["ALM_ALMPRIORITY"] == "CRITICAL").sum())
        active_high = int(latest["ALM_ALMSTATUS"].isin(["HIHI", "LOLO"]).sum())
        volume_penalty = min(25.0, abnormal / period_hours * 5.0)
        critical_penalty = min(25.0, critical * 1.5 + active_high * 5.0)
        coverage_penalty = max(0.0, 100.0 - coverage) * 0.6
        combined_risk = min(
            100.0,
            risk_score * 0.5 + volume_penalty + critical_penalty + coverage_penalty,
        )
        top = (
            period[period["ALM_ALMSTATUS"] != "OK"]
            .groupby(["ALM_TAGNAME", "ALM_DESCR"]).size().sort_values(ascending=False)
        )
        top_risk = "本週無異常警報" if top.empty else f"{top.index[0][1] or top.index[0][0]}（{int(top.iloc[0])}次）"
        health_score = 100.0 - combined_risk
        status = (
            "NORMAL" if combined_risk < 30
            else "WARNING" if combined_risk < 60
            else "MAJOR" if combined_risk < 80
            else "CRITICAL"
        )
        coverage_note = f"資料覆蓋 {fmt_num(coverage)}%"
        top_risk = f"{coverage_note}；{top_risk}"
        results.append({
            "plant": plant, "risk_score": round(combined_risk, 1),
            "health_score": round(health_score, 1), "abnormal": abnormal, "critical": critical,
            "active_high": active_high, "coverage": round(coverage, 1),
            "freshness": f"{period['TIME'].max():%m/%d %H:%M}",
            "top_risk": top_risk,
            "status": status,
        })
    return results


def analyze_run_rate(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> tuple[list[Issue], dict[str, dict[str, float]]]:
    issues: list[Issue] = []
    stats: dict[str, dict[str, float]] = {}
    if df.empty:
        return issues, stats

    df = df.copy()
    df["RUN_RATE"] = pd.to_numeric(df["RUN_RATE"], errors="coerce")
    expected = max(int(df["TIMESTAMP"].dt.floor("h").nunique()), 1)

    for plant, g in df.groupby("PLANT"):
        vals = g["RUN_RATE"].dropna()
        if vals.empty:
            continue
        mean = float(vals.mean())
        mn = float(vals.min())
        mx = float(vals.max())
        count = int(vals.count())
        first = float(g[g["TIMESTAMP"] < start + (end - start) / 2]["RUN_RATE"].mean())
        second = float(g[g["TIMESTAMP"] >= start + (end - start) / 2]["RUN_RATE"].mean())
        stats[plant] = {"mean": mean, "min": mn, "max": mx, "count": count, "expected": expected, "first": first, "second": second}

        level = level_for("run_rate", mean)
        if level != "NORMAL":
            trend = "惡化中" if not math.isnan(first) and not math.isnan(second) and second < first - 3 else "偏低"
            issues.append(Issue(
                plant=plant,
                item="運轉率偏低",
                phenomenon=f"週均 {fmt_num(mean)}%，最低 {fmt_num(mn)}%，低於目標 {TARGETS['run_rate']:.0f}%。",
                impact="設備可用率偏低，可能影響產能與排程穩定度。",
                level=level,
                action=f"追蹤 {plant} 運轉率低落時段，確認設備停機、保養或排程切換原因。",
                score=TARGETS["run_rate"] - mean,
                metric="運轉率",
                current=f"週均 {fmt_num(mean)}%，最低 {fmt_num(mn)}%",
                target=f">{TARGETS['run_rate']:.0f}%",
                trend=trend,
                forecast=f"若下週仍低於 {TARGETS['run_rate']:.0f}%，建議啟動設備可用率與排程合理性檢討。",
            ))

        if count < expected * 0.85:
            missing = expected - count
            level2 = "CRITICAL" if missing >= 12 else "MAJOR" if missing >= 4 else "WARNING"
            issues.append(Issue(
                plant=plant,
                item="資料回傳不完整",
                phenomenon=f"本週僅 {count}/{expected} 筆小時資料，推估缺 {missing} 筆。",
                impact="監控資料不完整，異常期間可能形成 FMCS 盲區。",
                level=level2,
                action=f"確認 {plant} PLC、SCADA、網路節點與資料匯入排程。",
                score=float(missing),
                metric="資料完整性",
                current=f"{count}/{expected} 筆",
                target="小時資料完整",
                trend="惡化中",
                forecast="若資料缺口持續，將降低異常判斷可信度與即時處置能力。",
            ))

        drops = g[g["RUN_RATE"] <= max(30, mean - 30)]
        if not drops.empty:
            min_row = drops.sort_values("RUN_RATE").iloc[0]
            issues.append(Issue(
                plant=plant,
                item="運轉率突降",
                phenomenon=f"{pd.to_datetime(min_row['TIMESTAMP']):%m/%d %H:%M} 運轉率降至 {fmt_num(clean_float(min_row['RUN_RATE']))}%。",
                impact="短時間產能損失或排程/設備突發事件，需確認是否為一次性事件。",
                level="MAJOR" if clean_float(min_row["RUN_RATE"]) and clean_float(min_row["RUN_RATE"]) < 40 else "WARNING",
                action=f"回查 {plant} 該時段停機、保養、告警與生產排程紀錄。",
                score=mean - float(min_row["RUN_RATE"]),
                date=pd.to_datetime(min_row["TIMESTAMP"]).strftime("%m/%d"),
                metric="運轉率",
                current=f"最低 {fmt_num(float(min_row['RUN_RATE']))}%",
                target="避免短時大幅跌落",
                trend="惡化中",
                forecast="若相同時段再次發生，建議列入下週重點追蹤。",
            ))

    return issues, stats


def best_worst_quality(summary: pd.DataFrame, predicate, lower_is_better: bool) -> tuple[pd.Series | None, pd.Series | None]:
    rows = summary[summary.apply(lambda r: predicate(str(r["EQNAME"]), str(r["DESCRIPTION"]), str(r["TAGNAME"])), axis=1)]
    rows = rows.dropna(subset=["mean"])
    if rows.empty:
        return None, None
    best = rows.sort_values("mean", ascending=lower_is_better).iloc[0]
    worst = rows.sort_values("mean", ascending=not lower_is_better).iloc[0]
    return best, worst


def analyze_quality(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> tuple[list[Issue], dict[str, dict[str, list[pd.Series]]], list[dict[str, str]], list[dict[str, str]]]:
    issues: list[Issue] = []
    plant_metrics: dict[str, dict[str, list[pd.Series]]] = {}
    best: list[dict[str, str]] = []
    worst: list[dict[str, str]] = []
    if df.empty:
        return issues, plant_metrics, best, worst

    summary = summarize_group(df)

    metric_defs = [
        ("冰機效率（KW/RT，越低越好，管制值<0.70）", "chiller", is_chiller, True, TARGETS["chiller_kwrt"], "KW/RT"),
        ("空壓效率（CMM/kWh，越高越好，管制值>7.0）", "compressor", is_compressor, False, TARGETS["compressor_cmmkwh"], "CMM/kWh"),
        ("超純水水阻（MΩ，越高越好）", "resistance", is_resistance, False, TARGETS["upw_resistance"], "MΩ"),
        ("出水導電度（越低越好）", "conductivity", is_conductivity, True, TARGETS["conductivity"], ""),
        ("廢水 pH（接近 7.0 最佳）", "ph", is_ph, True, 7.0, ""),
    ]

    for metric_name, kind, pred, lower, target, unit in metric_defs:
        rows = summary[summary.apply(lambda r: pred(str(r["EQNAME"]), str(r["DESCRIPTION"]), str(r["TAGNAME"])), axis=1)].copy()
        rows = rows.dropna(subset=["mean"])
        if rows.empty:
            continue

        if kind == "ph":
            rows["distance"] = (rows["mean"] - 7.0).abs()
            best_row = rows.sort_values("distance").iloc[0]
            worst_row = rows.sort_values("distance", ascending=False).iloc[0]
        else:
            best_row = rows.sort_values("mean", ascending=lower).iloc[0]
            worst_row = rows.sort_values("mean", ascending=not lower).iloc[0]

        best.append({
            "metric": metric_name,
            "plant": str(best_row["PLANT"]),
            "data_point": str(best_row["DESCRIPTION"]),
            "value": f"週均 {fmt_num(float(best_row['mean']), 2)} {unit}".strip(),
        })
        worst.append({
            "metric": metric_name,
            "plant": str(worst_row["PLANT"]),
            "data_point": str(worst_row["DESCRIPTION"]),
            "value": f"週均 {fmt_num(float(worst_row['mean']), 2)} {unit}".strip(),
        })

        for _, row in rows.iterrows():
            plant = str(row["PLANT"])
            plant_metrics.setdefault(plant, {}).setdefault(kind, []).append(row)
            mean = float(row["mean"])
            mn = float(row["min"])
            mx = float(row["max"])
            desc = str(row["DESCRIPTION"])

            level = level_for(kind, mean if kind != "ph" else (mx if mx > TARGETS["ph_high"] else mn if mn < TARGETS["ph_low"] else mean))
            if level == "NORMAL":
                continue

            if kind == "chiller":
                item = "冰機效率超標"
                phenomenon = f"{desc} 週均 {fmt_num(mean, 3)} KW/RT，超過管制值 {target:.2f}。"
                impact = "冰機能耗偏高，可能增加用電成本並反映熱交換效率下降。"
                action = f"安排 {plant} 冰機效率檢查，確認冷凝器、冷卻水溫差與負載配置。"
                current = f"週均 {fmt_num(mean, 3)} KW/RT"
                target_txt = f"<{target:.2f} KW/RT"
                trend = "偏高"
            elif kind == "compressor":
                item = "空壓效率偏低"
                phenomenon = f"{desc} 週均 {fmt_num(mean, 2)} CMM/kWh，低於管制值 {fmt_num(target)}。"
                impact = "空壓系統效率不足，可能與洩漏、負載分配或壓縮機效率有關。"
                action = f"檢查 {plant} 空壓機負載分配、管路洩漏與進氣條件。"
                current = f"週均 {fmt_num(mean, 2)} CMM/kWh"
                target_txt = f">{fmt_num(target)} CMM/kWh"
                trend = "偏低"
            elif kind == "resistance":
                item = "超純水水阻偏低"
                phenomenon = f"{desc} 週均 {fmt_num(mean, 2)} MΩ，最低 {fmt_num(mn, 2)} MΩ，低於目標 {target:.0f} MΩ。"
                impact = "水質未達目標，可能影響製程清洗品質。"
                action = f"排查 {plant} UPW 系統、離子交換樹脂、儀表校正與支管狀態。"
                current = f"週均 {fmt_num(mean, 2)} MΩ"
                target_txt = f">{target:.0f} MΩ"
                trend = "偏低"
            elif kind == "conductivity":
                item = "出水導電度偏高"
                phenomenon = f"{desc} 週均 {fmt_num(mean, 2)}，高於管制值 {fmt_num(target)}。"
                impact = "導電度偏高代表水質惡化，需確認純水或回收水處理狀態。"
                action = f"確認 {plant} 導電度儀表、RO/DI 單元與水處理耗材狀態。"
                current = f"週均 {fmt_num(mean, 2)}"
                target_txt = f"<{fmt_num(target)}"
                trend = "偏高"
            else:
                item = "廢水 pH 偏離"
                phenomenon = f"{desc} 範圍 {fmt_num(mn, 2)}～{fmt_num(mx, 2)}，超出 {fmt_num(TARGETS['ph_low'])}～{fmt_num(TARGETS['ph_high'])} 管制區間或接近邊界。"
                impact = "廢水 pH 偏離可能造成排放風險與加藥控制異常。"
                action = f"複核 {plant} 中和槽加藥量、pH 控制邏輯與告警設定。"
                current = f"{fmt_num(mn, 2)}～{fmt_num(mx, 2)}"
                target_txt = f"{fmt_num(TARGETS['ph_low'])}～{fmt_num(TARGETS['ph_high'])}"
                trend = "偏離"

            issues.append(Issue(
                plant=plant,
                item=item,
                phenomenon=phenomenon,
                impact=impact,
                level=level,
                action=action,
                score=abs(mean - target) if kind != "ph" else max(abs(mx - 7.0), abs(mn - 7.0)),
                metric=item.replace("偏低", "").replace("偏高", "").replace("超標", ""),
                current=current,
                target=target_txt,
                trend=trend,
                forecast="若下週仍未回到管制範圍，建議列為跨部門追蹤事項。",
            ))

    pressure = summary[summary.apply(lambda r: is_pressure(str(r["EQNAME"]), str(r["DESCRIPTION"]), str(r["TAGNAME"])), axis=1)].copy()
    pressure = pressure.dropna(subset=["mean"])
    low_pressure = pressure[pressure["mean"].abs() < 10]
    for _, row in low_pressure.head(5).iterrows():
        issues.append(Issue(
            plant=str(row["PLANT"]),
            item="排氣/集塵靜壓偏低",
            phenomenon=f"{row['DESCRIPTION']} 週均 {fmt_num(float(row['mean']), 1)}，接近零或低於合理運轉壓差。",
            impact="靜壓偏低可能代表設備未運轉、感測器異常或風量不足。",
            level="WARNING",
            action=f"現場確認 {row['PLANT']} 對應排氣/集塵設備運轉與差壓計狀態。",
            score=10 - abs(float(row["mean"])),
            metric="靜壓",
            current=f"週均 {fmt_num(float(row['mean']), 1)}",
            target="維持有效壓差",
            trend="偏低",
            forecast="若持續接近零，建議納入設備巡檢與儀表校驗。",
        ))

    return issues, plant_metrics, best[:5], worst[:5]


def health_scores(run_stats: dict[str, dict[str, float]], plant_metrics: dict[str, dict[str, list[pd.Series]]],
                  issues: list[Issue], source_plants: set[str] | None = None) -> list[dict[str, Any]]:
    by_plant: dict[str, list[Issue]] = {}
    for issue in issues:
        by_plant.setdefault(issue.plant, []).append(issue)

    rows = []
    plants = sorted(
        set(PLANT_ORDER) | set(run_stats) | set(plant_metrics) | set(source_plants or set()),
        key=lambda p: (PLANT_ORDER.index(p), p) if p in PLANT_ORDER else (len(PLANT_ORDER), p),
    )
    for plant in plants:
        score = 100.0
        rs = run_stats.get(plant)
        adv: list[str] = []
        weak: list[str] = []

        if rs:
            rr = rs["mean"]
            if rr >= TARGETS["run_rate_good"]:
                adv.append(f"運轉率優良（週均 {fmt_num(rr)}%）")
            elif rr >= TARGETS["run_rate"]:
                adv.append(f"運轉率達基本目標（週均 {fmt_num(rr)}%）")
            else:
                weak.append(f"運轉率偏低（週均 {fmt_num(rr)}%）")
                score -= min(5, TARGETS["run_rate"] - rr)

            expected = rs.get("expected", rs.get("count", 0))
            if rs["count"] < expected * 0.85:
                weak.append(f"資料完整性不足（{int(rs['count'])}/{int(expected)} 筆）")
                score -= 15
            else:
                adv.append("小時資料完整性良好")
        else:
            weak.append("缺少稼動率資料")
            score -= 5

        pm = plant_metrics.get(plant, {})
        if "chiller" in pm:
            best = min(float(r["mean"]) for r in pm["chiller"])
            if best <= TARGETS["chiller_kwrt"]:
                adv.append(f"冰機效率達標（最佳週均 {fmt_num(best, 3)} KW/RT）")
            else:
                weak.append(f"冰機效率超標（最佳週均仍 {fmt_num(best, 3)} KW/RT）")
                score -= 12
        if "compressor" in pm:
            best = max(float(r["mean"]) for r in pm["compressor"])
            if best >= TARGETS["compressor_cmmkwh"]:
                adv.append(f"空壓效率達標（最佳週均 {fmt_num(best, 2)} CMM/kWh）")
            else:
                weak.append(f"空壓效率偏低（最佳週均 {fmt_num(best, 2)} CMM/kWh）")
                score -= 10
        if "resistance" in pm:
            worst = min(float(r["mean"]) for r in pm["resistance"])
            if worst >= TARGETS["upw_resistance"]:
                adv.append(f"UPW 水阻達標（最低週均 {fmt_num(worst, 2)} MΩ）")
            else:
                weak.append(f"UPW 水阻偏低（最低週均 {fmt_num(worst, 2)} MΩ）")
                score -= 18
        if "ph" in pm:
            ph_vals = [float(r["mean"]) for r in pm["ph"]]
            worst_dist = max(abs(v - 7.0) for v in ph_vals)
            if worst_dist <= 1.5:
                adv.append("廢水 pH 大致穩定")
            else:
                weak.append("廢水 pH 偏離中性或接近管制邊界")
                score -= 8

        for issue in by_plant.get(plant, []):
            score -= {"CRITICAL": 12, "MAJOR": 7, "WARNING": 3}.get(issue.level, 0)

        score = round(max(0, min(100, score)), 1)
        status = "NORMAL" if score >= 85 else "WARNING" if score >= 70 else "MAJOR" if score >= 55 else "CRITICAL"
        rows.append({
            "rank": 0,
            "plant": plant,
            "score": score,
            "advantage": "、".join(adv[:3]) if adv else "本週未見明顯優勢指標",
            "weakness": "、".join(weak[:3]) if weak else "未見重大弱點",
            "status": status,
        })

    rows.sort(key=lambda r: r["score"], reverse=True)
    for idx, row in enumerate(rows, 1):
        row["rank"] = idx
    return rows


def health_scores_v2(
    run_stats: dict[str, dict[str, float]],
    plant_metrics: dict[str, dict[str, list[pd.Series]]],
    issues: list[Issue],
    source_plants: set[str],
    alarm_risk: list[dict[str, Any]],
    data_quality: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Approved weighted score: energy/water/data/alarm/trend; run rate excluded."""
    by_plant: dict[str, list[Issue]] = {}
    for issue in issues:
        by_plant.setdefault(issue.plant, []).append(issue)
    alarm_by_plant = {row["plant"]: row for row in alarm_risk}
    dq_by_plant: dict[str, list[dict[str, Any]]] = {}
    for row in data_quality:
        dq_by_plant.setdefault(row["plant"], []).append(row)
    plants = sorted(
        set(PLANT_ORDER) | set(source_plants) | set(alarm_by_plant),
        key=lambda p: (PLANT_ORDER.index(p), p) if p in PLANT_ORDER else (len(PLANT_ORDER), p),
    )
    rows: list[dict[str, Any]] = []
    for plant in plants:
        adv: list[str] = []
        weak: list[str] = []
        pm = plant_metrics.get(plant, {})
        energy_parts: list[float] = []
        if pm.get("chiller"):
            value = min(float(r["mean"]) for r in pm["chiller"])
            energy_parts.append(min(100.0, TARGETS["chiller_kwrt"] / max(value, 0.01) * 100.0))
        if pm.get("compressor"):
            value = max(float(r["mean"]) for r in pm["compressor"])
            energy_parts.append(min(100.0, value / TARGETS["compressor_cmmkwh"] * 100.0))
        energy = sum(energy_parts) / len(energy_parts) if energy_parts else 50.0

        water_parts: list[float] = []
        if pm.get("resistance"):
            value = min(float(r["mean"]) for r in pm["resistance"])
            water_parts.append(min(100.0, value / TARGETS["upw_resistance"] * 100.0))
        if pm.get("ph"):
            distance = max(abs(float(r["mean"]) - 7.0) for r in pm["ph"])
            water_parts.append(max(0.0, 100.0 - max(0.0, distance - 1.5) * 20.0))
        if pm.get("conductivity"):
            value = max(float(r["mean"]) for r in pm["conductivity"])
            water_parts.append(min(100.0, TARGETS["conductivity"] / max(value, 0.01) * 100.0))
        water = sum(water_parts) / len(water_parts) if water_parts else 50.0

        dq_rows = dq_by_plant.get(plant, [])
        dq_penalty = sum({"CRITICAL": 25, "MAJOR": 12, "WARNING": 5}.get(r["level"], 0) for r in dq_rows)
        data_health = max(0.0, 100.0 - min(100.0, dq_penalty))
        if dq_rows:
            weak.append(f"資料品質異常 {len(dq_rows)} 項")
        else:
            adv.append("未發現曲線空窗、歸零或48小時 Keep Last")

        alarm = alarm_by_plant.get(plant)
        alarm_health = float(alarm["health_score"]) if alarm else 50.0
        if alarm and alarm.get("risk_score") is not None:
            (adv if alarm["risk_score"] < 30 else weak).append(f"警報風險 {fmt_num(alarm['risk_score'])}")
        elif plant in ALARM_CONNECTED_PLANTS:
            weak.append("警報資料涵蓋不足，不列入正式比較")

        trend_issues = [
            issue for issue in by_plant.get(plant, [])
            if not any(token in issue.item for token in ("資料", "訊號", "48小時"))
        ]
        trend_penalty = sum({"CRITICAL": 35, "MAJOR": 20, "WARNING": 8}.get(i.level, 0) for i in trend_issues)
        trend = max(0.0, 100.0 - min(100.0, trend_penalty))
        breakdown = {
            "energy": round(energy * 0.30, 1),
            "water": round(water * 0.25, 1),
            "data_health": round(data_health * 0.20, 1),
            "alarm": round(alarm_health * 0.15, 1),
            "trend": round(trend * 0.10, 1),
        }
        score = round(sum(breakdown.values()), 1)
        status = "NORMAL" if score >= 85 else "WARNING" if score >= 70 else "MAJOR" if score >= 55 else "CRITICAL"
        rows.append({
            "rank": 0, "plant": plant, "score": score,
            "advantage": "；".join(adv[:3]) if adv else "本週未發現明顯優勢",
            "weakness": "；".join(weak[:3]) if weak else "本週無重大待改善項目",
            "status": status, "score_breakdown": breakdown,
        })
    rows.sort(key=lambda row: row["score"], reverse=True)
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
    return rows


def dedupe_issues(issues: list[Issue]) -> list[Issue]:
    """Keep the strongest issue per plant/item pair to avoid noisy repeats."""
    chosen: dict[tuple[str, str], Issue] = {}
    for issue in issues:
        key = (issue.plant, issue.item)
        old = chosen.get(key)
        if old is None or rank_level(issue.level, issue.score) > rank_level(old.level, old.score):
            chosen[key] = issue
    return list(chosen.values())


def select_diverse(issues: list[Issue], limit: int) -> list[Issue]:
    """Prefer distinct plants in executive TOP items, then fill by severity."""
    selected: list[Issue] = []
    used_plants: set[str] = set()
    for issue in issues:
        if issue.plant in used_plants:
            continue
        selected.append(issue)
        used_plants.add(issue.plant)
        if len(selected) >= limit:
            return selected
    for issue in issues:
        if issue in selected:
            continue
        selected.append(issue)
        if len(selected) >= limit:
            break
    return selected


def issue_to_trend_row(issue: Issue) -> dict[str, str]:
    return {
        "plant": issue.plant,
        "metric": issue.metric or issue.item,
        "current": issue.current or issue.phenomenon,
        "target": issue.target or "維持管制範圍",
        "trend": issue.trend,
        "forecast": issue.forecast or "下週持續追蹤是否回到正常區間。",
        "level": issue.level,
    }


def build_report(start: pd.Timestamp, end: pd.Timestamp) -> dict[str, Any]:
    run_df = filter_period(read_csv(RUN_RATE_CSV), start, end)
    quality_df = filter_period(read_csv(QUALITY_CSV), start, end)
    equipment_df = filter_period(read_csv(DATA_CSV), start, end)
    source_plants = {
        str(plant)
        for frame in (run_df, quality_df, equipment_df)
        if not frame.empty and "PLANT" in frame.columns
        for plant in frame["PLANT"].dropna().unique()
    }

    _run_issues, run_stats = analyze_run_rate(run_df, start, end)
    quality_issues, plant_metrics, best, worst = analyze_quality(quality_df, start, end)
    data_quality, data_quality_issues = analyze_data_quality(
        [quality_df, equipment_df], start, end
    )
    alarm_risk = analyze_alarm_risk(start, end)
    alarm_issues: list[Issue] = []
    for alarm in alarm_risk:
        risk = alarm.get("risk_score")
        if risk is None or risk < 30:
            continue
        level = "CRITICAL" if alarm.get("active_high", 0) > 0 or risk >= 75 else "MAJOR" if risk >= 60 else "WARNING"
        alarm_issues.append(Issue(
            plant=alarm["plant"], item="警報風險",
            phenomenon=(
                f"本週異常警報 {alarm['abnormal']} 次、Critical {alarm['critical']} 次、"
                f"HIHI/LOLO 未復歸 {alarm['active_high']} 點，風險 {fmt_num(risk)}。"
            ),
            impact="高頻或未復歸警報可能代表設備、製程或環安風險。",
            level=level,
            action=f"優先處理 {alarm['plant']} 警報 TOP 項目：{alarm['top_risk']}，確認原因與復歸紀錄。",
            score=float(risk), metric="警報風險", current=fmt_num(risk),
            target="<30", trend="上升",
            forecast="若高風險警報持續，可能造成設備異常擴大或監控負荷增加。",
        ))
    # Run rate is intentionally excluded from ranking, TOP events and trend pages.
    all_issues = dedupe_issues(quality_issues + data_quality_issues + alarm_issues)
    all_issues.sort(key=lambda x: rank_level(x.level, x.score), reverse=True)

    health = health_scores_v2(
        run_stats, plant_metrics, all_issues, source_plants, alarm_risk, data_quality
    )

    top3 = []
    for idx, issue in enumerate(select_diverse(all_issues, 3), 1):
        top3.append({
            "rank": str(idx),
            "event": f"{issue.plant} {issue.item}：{issue.phenomenon}",
            "impact": issue.impact,
            "level": issue.level,
            "action": issue.action,
        })

    events = [{
        "date": issue.date,
        "plant": issue.plant,
        "item": issue.item,
        "phenomenon": issue.phenomenon,
        "impact": issue.impact,
        "level": issue.level,
    } for issue in all_issues[:8]]

    trend_issues = [issue for issue in all_issues if issue.level in ("CRITICAL", "MAJOR", "WARNING")]
    trends = [issue_to_trend_row(issue) for issue in trend_issues[:5]]
    existing_trend_keys = {
        (row["plant"], row["metric"], row["current"], row["forecast"])
        for row in trends
    }
    for issue in trend_issues:
        if not issue.item.startswith(FLATLINE_ITEM_PREFIX):
            continue
        row = issue_to_trend_row(issue)
        key = (row["plant"], row["metric"], row["current"], row["forecast"])
        if key not in existing_trend_keys:
            trends.append(row)
            existing_trend_keys.add(key)

    # The page-1 KPI cards summarize what the report visibly lists:
    # P2 event rows for Critical/Major, and P3 trend rows for Warning.
    critical_count = sum(1 for row in events if row.get("level") == "CRITICAL")
    major_count = sum(1 for row in events if row.get("level") == "MAJOR")
    warning_count = sum(1 for row in trends if row.get("level") == "WARNING")

    actions = [{
        "priority": str(idx),
        "plant": issue.plant,
        "item": issue.item,
        "action": issue.action,
        "level": issue.level,
    } for idx, issue in enumerate(all_issues[:5], 1)]
    low_run_rate_reminders = [
        {
            "priority": "",
            "plant": plant,
            "item": "運轉率低於50%提醒",
            "action": f"本週平均運轉率 {fmt_num(stats['mean'])}%，提醒主管注意此現象。",
            "level": "WARNING",
        }
        for plant, stats in sorted(run_stats.items())
        if float(stats.get("mean", 100.0)) < 50.0
    ]
    actions.extend(low_run_rate_reminders)
    actions.extend(special_case_action_rows(set(PLANT_ORDER) | source_plants))
    for idx, action in enumerate(actions, 1):
        action["priority"] = str(idx)

    best_plants = " / ".join(r["plant"] for r in health[:3])
    worst_plants = " / ".join(r["plant"] for r in health[-3:][::-1])

    return {
        "meta": {
            "week_num": int(end.isocalendar().week),
            "year": int(end.year),
            "data_from": start.strftime("%Y-%m-%d"),
            "data_to": end.strftime("%Y-%m-%d"),
            "generator": "local_rules_v1",
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
        "overview": {
            "critical_count": critical_count,
            "major_count": major_count,
            "warning_count": warning_count,
            "best_plants": best_plants,
            "worst_plants": worst_plants,
        },
        "top3": top3,
        "events": events,
        "trend_warnings": trends,
        "health_scores": health,
        "best_performers": best,
        "worst_areas": worst,
        "actions": actions,
        "alarm_risk": alarm_risk,
        "data_quality": data_quality,
    }


def load_memory() -> dict[str, Any]:
    if MEMORY_JSON.exists():
        try:
            return json.loads(MEMORY_JSON.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "version": 1,
        "purpose": "Local context store for EFplant weekly reports. Future OpenAI API polishing can use this file as memory.",
        "style_rules": [
            "主管摘要要短、具體、可執行。",
            "避免誇大；每個判斷都要能回推到數據。",
            "Critical 優先寫生產/水質/資料中斷風險。",
            "建議行動需指出廠區、設備類型與下一步確認事項。",
        ],
        "thresholds": TARGETS,
        "history": [],
    }


def update_memory(report: dict[str, Any]) -> None:
    memory = load_memory()
    history = memory.setdefault("history", [])
    summary = {
        "generated_at": report["meta"]["generated_at"],
        "week_num": report["meta"]["week_num"],
        "data_from": report["meta"]["data_from"],
        "data_to": report["meta"]["data_to"],
        "overview": report["overview"],
        "top3": report["top3"],
        "actions": report["actions"],
    }
    history = [h for h in history if not (h.get("week_num") == summary["week_num"] and h.get("data_to") == summary["data_to"])]
    history.append(summary)
    cutoff = datetime.now() - timedelta(days=LOCAL_CONTEXT_RETENTION_DAYS)
    kept = []
    for item in history:
        try:
            generated_at = datetime.strptime(str(item.get("generated_at", "")), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            kept.append(item)
            continue
        if generated_at >= cutoff:
            kept.append(item)
    memory["history"] = kept[-8:]
    memory["last_report"] = summary
    MEMORY_JSON.write_text(json.dumps(memory, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate EFplant weekly report analysis JSON without external AI.")
    parser.add_argument("--output", default=str(OUT_JSON), help="Output JSON path")
    parser.add_argument("--memory", action="store_true", default=True, help="Update weekly_report_memory.json")
    args = parser.parse_args()

    raw_frames = [read_csv(RUN_RATE_CSV), read_csv(QUALITY_CSV), read_csv(DATA_CSV)]
    for df in raw_frames:
        if not df.empty and "TIMESTAMP" in df.columns:
            df["TIMESTAMP"] = pd.to_datetime(df["TIMESTAMP"], errors="coerce")
    start, end = choose_period(raw_frames)
    report = build_report(start, end)
    if polish_weekly_report_text_with_openai(report):
        report["meta"]["generator"] = "local_rules_v1_openai_weekly_text"
        report["meta"]["openai_text"] = "enabled"
    else:
        report["meta"]["openai_text"] = "fallback_local_rules"

    out = Path(args.output)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.memory:
        update_memory(report)
        update_local_context(report)

    print(f"[OK] Weekly analysis generated: {out}")
    print(f"     Period: {report['meta']['data_from']} ~ {report['meta']['data_to']} W{report['meta']['week_num']}")
    print(
        f"     Issues: Critical={report['overview']['critical_count']} "
        f"Major={report['overview']['major_count']} Warning={report['overview']['warning_count']}"
    )


if __name__ == "__main__":
    main()
