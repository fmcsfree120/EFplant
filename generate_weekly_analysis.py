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
METRIC_LABELS = {
    "resistance": "超純水水阻",
    "conductivity": "純水出水導電度",
    "ph": "廢水 pH",
    "chiller": "冰機能耗效率",
    "compressor": "空壓能耗效率",
    "pressure": "排氣／集塵靜壓",
    "general": "其他廠務趨勢",
}
LOCAL_CONTEXT_DIR = BASE_DIR / "localcontext"
LOCAL_CONTEXT_MD = LOCAL_CONTEXT_DIR / "weekly_report_api_context.md"
API_PROMPT_MD = LOCAL_CONTEXT_DIR / "weekly_report_api_prompt.md"
LOCAL_CONTEXT_RETENTION_DAYS = 31
OPENAI_MODEL = "gpt-4o-mini"
# 管理週報的對外論述由本地規則完整產生；API 僅保留為未啟用的人工審稿備援。
API_REPORT_REWRITE_ENABLED = True

SEMICONDUCTOR_FACILITY_REFERENCE = """
半導體廠務技術參照摘要：
1. UPW/DI：半導體製程以超純水清洗晶圓及支援關鍵製程；水阻下降或導電度上升需優先聯想到離子突破、樹脂/RO/EDI 效能、取樣管路 CO2 滲入或支管污染。
2. UPW 品質風險：粒子、TOC、金屬離子、矽與微生物會造成晶圓缺陷或製程變異；若水質異常，建議行動需包含儀表校正、POD/POC 交叉採樣、耗材狀態與支管沖洗。
3. 廢水/pH：pH 偏離自身歷史水準需聯想到酸鹼廢液負荷、加藥泵、pH 電極校正、攪拌與中和槽停留時間。
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
EXCLUDED_REPORT_TERMS = ("冰機", "冰水主機", "空壓", "CDA", "供藥", "化學", "H2SO4", "H2O2", "HNO3", "HCL", "NaOH", "Na2CO3", "MGCB")


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
            "item": "本週異常事件清單",
            "action": clean,
            "level": "WARNING",
        })
    # Use a concise, language-neutral label when a special case applies to
    # every plant rather than a named subset.
    for row in rows:
        if row["plant"] == "\u5168\u5ee0":
            row["plant"] = "ALL"
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
        "trend_rankings": report.get("trend_rankings", []),
        "actions": report.get("actions", [])[:5],
    }


def build_api_prompt_markdown() -> str:
    return f"""# EFplant 週報文字生成 API Prompt

## 角色
你是半導體廠務 FMCS 週報評判描述生成器。Python 提供資料、排序與等級骨架；本次測試要求所有對外評判描述由 API 生成。管理週報只聚焦純水、廢水及其他保留的製造供應品質趨勢。

## 固定技術參照
{SEMICONDUCTOR_FACILITY_REFERENCE.strip()}

## API 權限範圍
- 本次測試 API 負責 TOP3、事件、趨勢預警、廠區排序描述、最佳／待改善描述、警報摘要與管理行動的評判文字。
- 不得改 rank、plant、item、metric、current、target、trend、level、status，也不得新增或刪除列。
- value、abnormal、critical、active_high、detail 中的數字與事實不可改動，只能將文字改寫為主管報告語氣。
- 只可根據輸入數值與前段／近期比較生成文字，不得引入資料中斷、覆蓋率、冰機、空壓、化學品供應或運轉率內容。

## 報告主軸與取捨
- 只比較近期三日與同一張趨勢圖前段四日的歷史水準，說明偏離方向、幅度及其對製造供應穩定度的意義。
- 不得引用管制界線、法規界線或固定目標作為優劣排序依據。
- TOP3、異常事件、趨勢預警與管理建議只處理純水、廢水及保留的製造供應品質指標。
- 資料品質問題只放在資料品質附表作為維護資訊；除非已能證明造成製造供應中斷，否則不得上升為主管摘要主軸。
- 不得把「資料筆數多寡」誤寫為「製造供應品質好壞」。

## 管理建議與追蹤事項中的廠區例外情況
- 部分 actions 列的 item 為「本週特殊情況」，代表現場人員本週在 specialcase-weekly.txt 手動登記的例外情況，屬本週資料的一部分。
- 這類 action 文字必須修飾為主管週報語氣，只能改寫語氣與用詞，不得刪減原始事實，也不得新增登記內容沒有提到的臆測原因，不可搬移到其他 plant。

## 不可變更
- 不得改 rank、priority、date、plant、item、metric、current、target、trend、level、status。
- 不得新增或刪除列。
- 不得新增本週資料沒有支持的事實。
- 每個判斷必須能回推到輸入 JSON 的數值、等級或趨勢。
- 全報告不使用任何分數、加權或分數區間；只呈現由上至下的相對優劣順位。
- 不得提及資料中斷、資料覆蓋率、冰機、空壓、化學品供應或運轉率。

## 文字要求
- 繁體中文。
- 主管週報語氣，短、具體、可執行。
- 優先使用半導體廠務語彙：UPW、水阻、導電度、廢水中和、FMCS、SCADA。
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
    lines.append("## 各廠區趨勢表現排序")
    for item in report.get("trend_rankings", []):
        lines.extend([
            f"### {item.get('rank')}. {item.get('plant')} ({item.get('status')})",
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
        "trend_rankings": [
            {
                "idx": idx,
                "rank": row.get("rank"),
                "plant": row.get("plant"),
                "advantage": row.get("advantage"),
                "weakness": row.get("weakness"),
                "status": row.get("status"),
            }
            for idx, row in enumerate(report.get("trend_rankings", []))
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
        "best_performers": [
            {"idx": idx, "metric": row.get("metric"), "plant": row.get("plant"), "data_point": row.get("data_point"), "value": row.get("value")}
            for idx, row in enumerate(report.get("best_performers", []))
        ],
        "worst_areas": [
            {"idx": idx, "metric": row.get("metric"), "plant": row.get("plant"), "data_point": row.get("data_point"), "value": row.get("value")}
            for idx, row in enumerate(report.get("worst_areas", []))
        ],
        "alarm_risk": [
            {"idx": idx, "plant": row.get("plant"), "abnormal": row.get("abnormal"), "critical": row.get("critical"), "active_high": row.get("active_high"), "top_risk": row.get("top_risk"), "status": row.get("status")}
            for idx, row in enumerate(report.get("alarm_risk", []))
        ],
        "data_quality": [
            {"idx": idx, "plant": row.get("plant"), "type": row.get("type"), "point": row.get("point"), "detail": row.get("detail"), "level": row.get("level")}
            for idx, row in enumerate(report.get("data_quality", []))
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
    if not any(report.get(k) for k in ("top3", "events", "trend_warnings", "trend_rankings", "actions")):
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
                + "\n硬性規則：禁止使用『未見明顯』『沒有明顯』等空泛句型；每個判定必須帶數字、百分比、項目數或資料筆數。所有 metric、item、status 與表格欄位必須使用繁體中文（pH、UPW、CDA、Tag 代碼可保留）。"
                + "\n輸出格式："
                + "{\"top3\":[{\"idx\":0,\"event\":\"...\",\"impact\":\"...\",\"action\":\"...\"}],"
                + "\"events\":[{\"idx\":0,\"phenomenon\":\"...\",\"impact\":\"...\"}],"
                + "\"trend_warnings\":[{\"idx\":0,\"forecast\":\"...\"}],"
                + "\"trend_rankings\":[{\"idx\":0,\"advantage\":\"...\",\"weakness\":\"...\"}],"
                + "\"actions\":[{\"idx\":0,\"action\":\"...\"}],"
                + "\"best_performers\":[{\"idx\":0,\"value\":\"...\"}],"
                + "\"worst_areas\":[{\"idx\":0,\"value\":\"...\"}],"
                + "\"alarm_risk\":[{\"idx\":0,\"top_risk\":\"...\"}],"
                + "\"data_quality\":[{\"idx\":0,\"detail\":\"...\"}]}"
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
        ("trend_rankings", ["advantage", "weakness"]),
        ("actions", ["action"]),
        ("best_performers", ["value"]),
        ("worst_areas", ["value"]),
        ("alarm_risk", ["top_risk"]),
        ("data_quality", ["detail"]),
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


def narrative_ownership_metrics(report: dict[str, Any]) -> dict[str, Any]:
    """Quantify ownership by externally visible narrative fields stored in JSON."""
    field_map = {
        "top3": ("event", "impact", "action", "explanation"),
        "events": ("phenomenon", "impact", "explanation"),
        "trend_warnings": ("forecast", "explanation"),
        "trend_rankings": ("advantage", "weakness"),
        "best_performers": ("value",),
        "worst_areas": ("value",),
        "actions": ("action", "explanation"),
        "alarm_risk": ("top_risk",),
        "data_quality": ("detail",),
    }
    total = sum(
        1
        for section, fields in field_map.items()
        for row in report.get(section, [])
        for field in fields
        if str(row.get(field, "")).strip()
    )
    api_owned = 0 if not API_REPORT_REWRITE_ENABLED else total
    share = round(api_owned / total * 100.0, 1) if total else 0.0
    return {
        "measurement": "API可改寫的對外論述欄位數 ÷ 全部對外論述欄位數",
        "total_narrative_fields": total,
        "api_editable_fields": api_owned,
        "local_rule_fields": total - api_owned,
        "api_narrative_share_pct": share,
    }


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


def is_general_facility_trend(eq: str, desc: str, tag: str) -> bool:
    """Keep other numeric trend-chart signals, excluding run-rate/energy and
    signals already assigned to a named quality or pressure family."""
    text = f"{eq} {desc} {tag}"
    excluded = ("運轉率", "能耗", "效率", "KW/RT", "CMM/KW", "CDA_CMM/KWH", "CHU_KW/RT")
    named = (is_resistance, is_conductivity, is_ph, is_pressure)
    return not any(word.lower() in text.lower() for word in excluded) and not any(fn(eq, desc, tag) for fn in named)


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
    frames: list[pd.DataFrame], start: pd.Timestamp, end: pd.Timestamp,
    expected_plants: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[Issue]]:
    """Locate missing plants, curve gaps, zero signals and >=48 hour Keep Last segments."""
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
    observed_plants = {
        str(plant).strip()
        for part in normalized
        for plant in part["PLANT"].dropna().unique()
    }
    missing_plants = sorted(set(expected_plants or set()) - observed_plants)
    period_hours = max(
        1.0,
        ((end.normalize() + pd.Timedelta(days=1)) - start.normalize()).total_seconds() / 3600,
    )
    for plant in missing_plants:
        rows.append({
            "plant": plant, "type": "整廠資料缺失", "point": "品質／設備監控資料",
            "period": f"{start:%m/%d}～{end:%m/%d}",
            "duration": f"{fmt_num(period_hours)} 小時", "detail": "分析期間內無任何有效資料",
            "level": "CRITICAL",
        })
        issues.append(Issue(
            plant=plant, item="整廠監控資料缺失",
            phenomenon=f"{plant} 於 {start:%m/%d}～{end:%m/%d} 無品質或設備監控資料。",
            impact="形成整廠 FMCS 監控盲區，無法確認設備、能源與水質狀態。",
            level="CRITICAL",
            action=f"檢查 {plant} 的資料匯入排程、PLC/SCADA 通訊及網路節點。",
            score=period_hours, date=f"{start:%m/%d}", metric="資料完整性",
            current="整週無有效資料", target="分析期間應持續回傳資料", trend="下降",
            forecast="若資料仍未恢復，設備、能源與水質異常將無法被週報辨識。",
        ))

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
                "duration": f"{fmt_num(gap_hours)} 小時", "detail": f"正常資料大約每 {fmt_num(cadence, 2)} 小時會有一筆；目前資料中斷超過這個間隔",
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

    type_order = {"整廠資料缺失": -1, "曲線空窗": 0, "降為 0": 1, "持續為 0": 1, "Keep Last": 2}
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
                "plant": plant, "abnormal": 0,
                "critical": 0, "active_high": 0, "coverage": 0.0, "freshness": "無當週資料",
                "top_risk": "本週無警報資料", "status": "WARNING",
            })
            continue
        for col in ("ALM_ALMSTATUS", "ALM_ALMPRIORITY", "ALM_TAGNAME", "ALM_DESCR"):
            period[col] = period.get(col, "").fillna("").astype(str).str.strip().str.upper()
        actual_days = period["TIME"].dt.normalize().nunique()
        coverage = min(100.0, actual_days / expected_days * 100)
        latest = period.sort_values("TIME").drop_duplicates("ALM_TAGNAME", keep="last")
        abnormal = int((period["ALM_ALMSTATUS"] != "OK").sum())
        critical = int((period["ALM_ALMPRIORITY"] == "CRITICAL").sum())
        active_high = int(latest["ALM_ALMSTATUS"].isin(["HIHI", "LOLO"]).sum())
        top = (
            period[period["ALM_ALMSTATUS"] != "OK"]
            .groupby(["ALM_TAGNAME", "ALM_DESCR"]).size().sort_values(ascending=False)
        )
        top_risk = "本週無異常警報" if top.empty else f"{top.index[0][1] or top.index[0][0]}（{int(top.iloc[0])}次）"
        status = ("CRITICAL" if active_high > 0 else "MAJOR" if critical > 0
                  else "WARNING" if abnormal > 0 else "NORMAL")
        results.append({
            "plant": plant, "abnormal": abnormal, "critical": critical,
            "active_high": active_high, "coverage": round(coverage, 1),
            "freshness": f"{period['TIME'].max():%m/%d %H:%M}",
            "top_risk": top_risk,
            "status": status,
        })
    # Alarm assessment is a volume/risk ranking: show the largest counts first.
    results.sort(key=lambda row: (row.get("active_high", 0), row.get("critical", 0), row.get("abnormal", 0)), reverse=True)
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
    """Approved score: pure water/wastewater/alarm/trend, each weighted 25%."""
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
        pure_water_parts: list[float] = []
        if pm.get("resistance"):
            value = min(float(r["mean"]) for r in pm["resistance"])
            pure_water_parts.append(min(100.0, value / TARGETS["upw_resistance"] * 100.0))
        if pm.get("conductivity"):
            value = max(float(r["mean"]) for r in pm["conductivity"])
            pure_water_parts.append(min(100.0, TARGETS["conductivity"] / max(value, 0.01) * 100.0))
        pure_water = sum(pure_water_parts) / len(pure_water_parts) if pure_water_parts else 50.0

        wastewater_parts: list[float] = []
        if pm.get("ph"):
            distance = max(abs(float(r["mean"]) - 7.0) for r in pm["ph"])
            wastewater_parts.append(max(0.0, 100.0 - max(0.0, distance - 1.5) * 20.0))
        wastewater = sum(wastewater_parts) / len(wastewater_parts) if wastewater_parts else 50.0

        dq_rows = dq_by_plant.get(plant, [])
        whole_plant_missing = any(r.get("type") == "整廠資料缺失" for r in dq_rows)
        if dq_rows:
            if whole_plant_missing:
                weak.append("整週無品質或設備監控資料")
            else:
                weak.append(f"資料品質異常 {len(dq_rows)} 項")
        else:
            adv.append("未發現曲線空窗、歸零或48小時 Keep Last")

        alarm = alarm_by_plant.get(plant)
        alarm_health = float(alarm["health_score"]) if alarm else 50.0
        if alarm and alarm.get("risk_score") is not None:
            (adv if alarm["risk_score"] < 30 else weak).append("警報狀態穩定" if alarm["risk_score"] < 30 else "警報風險偏高")
        elif plant in ALARM_CONNECTED_PLANTS:
            weak.append("警報資料涵蓋不足，不列入正式比較")

        trend_issues = [
            issue for issue in by_plant.get(plant, [])
            if not any(token in issue.item for token in ("資料", "訊號", "48小時"))
        ]
        trend_penalty = sum({"CRITICAL": 35, "MAJOR": 20, "WARNING": 8}.get(i.level, 0) for i in trend_issues)
        trend = max(0.0, 100.0 - min(100.0, trend_penalty))
        breakdown = {
            "pure_water": round(pure_water * 0.25, 1),
            "wastewater": round(wastewater * 0.25, 1),
            "alarm": round(alarm_health * 0.25, 1),
            "trend": round(trend * 0.25, 1),
        }
        score = round(sum(breakdown.values()), 1)
        score_formula = (
            f"純水{fmt_num(breakdown['pure_water'])}/25＋廢水{fmt_num(breakdown['wastewater'])}/25＋"
            f"警報{fmt_num(breakdown['alarm'])}/25＋異常趨勢{fmt_num(breakdown['trend'])}/25"
        )
        status = "NORMAL" if score >= 85 else "WARNING" if score >= 70 else "MAJOR" if score >= 55 else "CRITICAL"
        rows.append({
            "rank": 0, "plant": plant, "score": score,
            "advantage": "；".join(adv[:3]) if adv else "本週未發現明顯優勢",
            "weakness": "；".join(weak[:3]) if weak else "本週無重大待改善項目",
            "status": status, "score_breakdown": breakdown, "score_formula": score_formula,
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
        "score": float(issue.score),
    }


def analyze_quality_by_history(
    df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp,
) -> tuple[list[Issue], dict[str, dict[str, list[dict[str, Any]]]], list[dict[str, str]], list[dict[str, str]]]:
    """Rank visible trends by recent 3-day deviation from the preceding 4-day baseline."""
    issues: list[Issue] = []
    plant_metrics: dict[str, dict[str, list[dict[str, Any]]]] = {}
    best: list[dict[str, str]] = []
    worst: list[dict[str, str]] = []
    if df.empty:
        return issues, plant_metrics, best, worst
    work = df.copy()
    work["TIMESTAMP"] = pd.to_datetime(work["TIMESTAMP"], errors="coerce")
    work["VALUE"] = pd.to_numeric(work["VALUE"], errors="coerce")
    work = work.dropna(subset=["TIMESTAMP", "VALUE"])
    split = end.normalize() - pd.Timedelta(days=2)
    metric_defs = [
        ("超純水水阻近期走勢", "resistance", is_resistance, "higher", "MΩ"),
        ("出水導電度近期走勢", "conductivity", is_conductivity, "lower", ""),
        ("廢水 pH 穩定度", "ph", is_ph, "stable", "pH"),
        ("排氣／集塵靜壓穩定度", "pressure", is_pressure, "stable", "Pa"),
    ]
    keys = ["PLANT", "EQNAME", "DESCRIPTION", "TAGNAME"]
    for metric_name, kind, predicate, direction, unit in metric_defs:
        metric_df = work[work.apply(
            lambda row: predicate(str(row.get("EQNAME", "")), str(row.get("DESCRIPTION", "")), str(row.get("TAGNAME", ""))),
            axis=1,
        )].copy()
        comparisons: list[dict[str, Any]] = []
        for key, group in metric_df.groupby(keys, dropna=False):
            baseline = group[group["TIMESTAMP"] < split]["VALUE"]
            recent = group[group["TIMESTAMP"] >= split]["VALUE"]
            if baseline.empty or recent.empty:
                continue
            base = float(baseline.mean())
            current = float(recent.mean())
            raw_change = (current - base) / max(abs(base), 0.01) * 100.0
            if direction == "higher":
                performance = raw_change
            elif direction == "lower":
                performance = -raw_change
            else:
                performance = -abs(raw_change)
            row = {
                "PLANT": str(key[0]), "EQNAME": str(key[1]), "DESCRIPTION": str(key[2]),
                "TAGNAME": str(key[3]), "baseline": base, "recent": current,
                "change_pct": raw_change, "performance": performance,
                "_kind": kind,
            }
            comparisons.append(row)
            plant_metrics.setdefault(str(key[0]), {}).setdefault(kind, []).append(row)
        if not comparisons:
            continue
        comparisons.sort(key=lambda row: row["performance"], reverse=True)
        best_row, worst_row = comparisons[0], comparisons[-1]
        def trend_value(row: dict[str, Any]) -> str:
            suffix = f" {unit}" if unit else ""
            return (
                f"近期 {fmt_num(row['recent'], 2)}{suffix}；前段 {fmt_num(row['baseline'], 2)}{suffix}；"
                f"偏離 {fmt_num(row['change_pct'], 1)}%"
            )
        best.append({"metric": metric_name, "plant": best_row["PLANT"],
                     "data_point": best_row["DESCRIPTION"], "value": trend_value(best_row)})
        worst.append({"metric": metric_name, "plant": worst_row["PLANT"],
                      "data_point": worst_row["DESCRIPTION"], "value": trend_value(worst_row)})
        for row in comparisons:
            deterioration = max(0.0, -float(row["performance"]))
            if deterioration <= 0.0:
                continue
            level = ("CRITICAL" if deterioration >= 20 else "MAJOR" if deterioration >= 10
                     else "WARNING" if deterioration >= 5 else "NORMAL")
            direction_text = "上升" if row["change_pct"] > 0 else "下降"
            phenomenon = (
                f"{row['DESCRIPTION']} 近期水準 {fmt_num(row['recent'], 2)} {unit}，"
                f"相較前段水準 {fmt_num(row['baseline'], 2)} {unit}{direction_text} {fmt_num(abs(row['change_pct']), 1)}%。"
            ).replace("  ，", "，")
            impact_map = {
                "chiller": "冰機效率近期轉差，可能增加公用系統負荷。",
                "compressor": "空壓效率近期轉差，可能降低 CDA 供應效率。",
                "resistance": "UPW 水阻較以往下降，代表近期供水品質走弱。",
                "conductivity": "出水導電度較以往上升，代表近期水質走弱。",
                "ph": "廢水 pH 較自身以往水準偏離，代表中和控制穩定度下降。",
                "pressure": "排氣／集塵靜壓偏離前段水準，可能代表風量、濾網或設備運轉狀態變化。",
                "general": "廠務趨勢偏離前段水準，需確認對應設備與製程影響。",
            }
            action_map = {
                "chiller": "比對近期負載、冷卻水溫差與主機運轉配置。",
                "compressor": "比對近期用氣量、壓力、洩漏與機組負載配置。",
                "resistance": "比對 UPW 產水、樹脂／膜組狀態及儀表校正紀錄。",
                "conductivity": "比對純水處理單元、耗材狀態及儀表校正紀錄。",
                "ph": "比對中和槽負荷、加藥量、攪拌及 pH 儀表紀錄。",
                "pressure": "確認排氣／集塵設備運轉、濾網壓損、風量與差壓計校正狀態。",
                "general": "確認該監測點設備狀態、資料品質與現場操作紀錄。",
            }
            issues.append(Issue(
                plant=row["PLANT"], item=metric_name, phenomenon=phenomenon,
                impact=impact_map[kind], level=level, action=action_map[kind],
                score=deterioration, metric=metric_name,
                current=f"近期 {fmt_num(row['recent'], 2)}；前段 {fmt_num(row['baseline'], 2)}",
                target="前段歷史水準", trend="偏離擴大",
                forecast="若近期偏離持續，製造供應品質或系統效率可能進一步走弱。",
            ))
    return issues, plant_metrics, best[:5], worst[:5]


def health_rankings_by_trend(
    plant_metrics: dict[str, dict[str, list[dict[str, Any]]]], source_plants: set[str],
    alarm_risk: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Create ordinal rankings from visible trend direction; no weighted or total score."""
    alarm_by_plant = {row["plant"]: row for row in alarm_risk}
    plants = sorted(set(PLANT_ORDER) | source_plants | set(alarm_by_plant),
                    key=lambda p: (PLANT_ORDER.index(p), p) if p in PLANT_ORDER else (99, p))
    rows: list[dict[str, Any]] = []
    for plant in plants:
        metrics = [item for group in plant_metrics.get(plant, {}).values() for item in group]
        performances = [float(item["performance"]) for item in metrics]
        worsening = sum(value < -5 for value in performances)
        improving = sum(value > 5 for value in performances)
        worst_deviation = min(performances, default=0.0)
        improving_points = [str(item.get("DESCRIPTION", "")) for item in metrics if float(item.get("performance", 0)) > 5]
        worsening_points = [str(item.get("DESCRIPTION", "")) for item in metrics if float(item.get("performance", 0)) < -5]
        best_point = max(metrics, key=lambda item: float(item.get("performance", 0)), default=None)
        worst_point = min(metrics, key=lambda item: float(item.get("performance", 0)), default=None)
        alarm = alarm_by_plant.get(plant, {})
        alarm_tuple = (int(alarm.get("active_high", 0)), int(alarm.get("critical", 0)), int(alarm.get("abnormal", 0)))
        if worsening == 0 and alarm_tuple[0] == 0:
            status = "NORMAL"
        elif worsening <= 1 and alarm_tuple[0] == 0:
            status = "WARNING"
        elif worsening <= 2:
            status = "MAJOR"
        else:
            status = "CRITICAL"
        rows.append({
            "rank": 0, "plant": plant,
            "advantage": (f"最佳改善：{best_point['DESCRIPTION']}，改善幅度 {fmt_num(abs(float(best_point['performance'])), 1)}%") if best_point and float(best_point["performance"]) > 0 else "最佳改善：無正向變化",
            "weakness": (f"最嚴重惡化：{worst_point['DESCRIPTION']}，惡化幅度 {fmt_num(abs(float(worst_point['performance'])), 1)}%") if worst_point and float(worst_point["performance"]) < 0 else "最嚴重惡化：0%；無負向變化",
            "status": status,
            # The displayed table has no status column; rank only by the
            # visible primary advantage, from best to worst.
            "_sort": (-max(float(item.get("performance", 0)) for item in metrics) if metrics else 0,
                       plant),
        })
    rows.sort(key=lambda row: row["_sort"])
    for idx, row in enumerate(rows, 1):
        row["rank"] = idx
        row.pop("_sort", None)
    return rows


def build_report(start: pd.Timestamp, end: pd.Timestamp) -> dict[str, Any]:
    run_df = filter_period(read_csv(RUN_RATE_CSV), start, end)
    raw_quality_df = read_csv(QUALITY_CSV)
    raw_equipment_df = read_csv(DATA_CSV)
    expected_data_plants = {
        str(plant).strip()
        for frame in (raw_quality_df, raw_equipment_df)
        if not frame.empty and "PLANT" in frame.columns
        for plant in frame["PLANT"].dropna().unique()
    }
    quality_df = filter_period(raw_quality_df, start, end)
    equipment_df = filter_period(raw_equipment_df, start, end)
    source_plants = {
        str(plant)
        for frame in (run_df, quality_df, equipment_df)
        if not frame.empty and "PLANT" in frame.columns
        for plant in frame["PLANT"].dropna().unique()
    }

    _run_issues, run_stats = analyze_run_rate(run_df, start, end)
    # All visible trend sources share the same history comparison logic.  The
    # former implementation only passed the water-quality CSV, which silently
    # excluded exhaust static pressure and other facility trends.
    trend_frames = [frame for frame in (quality_df, equipment_df) if not frame.empty]
    trend_df = pd.concat(trend_frames, ignore_index=True) if trend_frames else pd.DataFrame()
    quality_issues, plant_metrics, best, worst = analyze_quality_by_history(trend_df, start, end)
    data_quality, data_quality_issues = analyze_data_quality(
        [quality_df, equipment_df], start, end, expected_data_plants
    )
    data_quality = [
        row for row in data_quality
        if not any(term.lower() in str(row.get("point", "")).lower() for term in EXCLUDED_REPORT_TERMS)
    ]
    alarm_risk = analyze_alarm_risk(start, end)
    # 主題排序只採趨勢圖可見的品質/效率走勢；資料中斷與警報彙總另頁呈現。
    # 主管週報主軸聚焦製造供應品質；資料中斷僅保留於 P9 維護附表。
    all_issues = dedupe_issues(quality_issues)
    # 供應品質趨勢優先於警報彙總；同類內再按等級與規則判定值排序。
    all_issues.sort(
        key=lambda x: (x.item != "警報風險", *rank_level(x.level, x.score)),
        reverse=True,
    )

    health = health_rankings_by_trend(plant_metrics, source_plants, alarm_risk)

    # P5/P6 are plant-comparison topics: retain one representative best and
    # worst metric for every plant, rather than only one row per metric type.
    all_plant_rows = []
    for plant, groups in plant_metrics.items():
        all_plant_rows.extend([dict(row, _kind=kind) for kind, rows in groups.items() for row in rows])
    def report_metric_row(row: dict[str, Any]) -> dict[str, Any]:
        unit = {"resistance": "MΩ", "conductivity": "", "ph": "pH", "chiller": "", "compressor": ""}.get(row.get("_kind", ""), "")
        suffix = f" {unit}" if unit else ""
        return {"metric": METRIC_LABELS.get(row["_kind"], row["_kind"]), "plant": row["PLANT"], "data_point": row["DESCRIPTION"],
                "value": f"近期水準 {fmt_num(row['recent'], 2)}{suffix}；前段水準 {fmt_num(row['baseline'], 2)}{suffix}（變化 {fmt_num(row['change_pct'], 1)}%）"}
    best = [report_metric_row(max(rows, key=lambda r: r["performance"])) for plant in sorted(set(PLANT_ORDER) | source_plants)
            if (rows := [r for r in all_plant_rows if r["PLANT"] == plant])]
    worst = [report_metric_row(min(rows, key=lambda r: r["performance"])) for plant in sorted(set(PLANT_ORDER) | source_plants)
             if (rows := [r for r in all_plant_rows if r["PLANT"] == plant])]

    top3 = []
    for idx, issue in enumerate(select_diverse(all_issues, 3), 1):
        top3.append({
            "rank": str(idx),
            "event": f"{issue.plant} {issue.item}：{issue.phenomenon}",
            "impact": issue.impact,
            "level": issue.level,
            "action": issue.action,
        })

    reportable_issues = [issue for issue in all_issues if issue.level != "NORMAL"]
    events = []
    # specialcase-weekly.txt is an event annotation, not a management action.
    # Keep it in the event topic so it is visible with the week's abnormal events.
    special_events = []
    for row in special_case_action_rows(set(PLANT_ORDER) | source_plants):
        special_events.append({
            "date": end.strftime("%Y-%m-%d"),
            "plant": row["plant"], "item": row["item"],
            "phenomenon": row["action"],
            "impact": "需由現場確認並追蹤",
            "level": row["level"],
        })
    events.extend(special_events)

    trend_issues = [issue for issue in all_issues if issue.level in ("CRITICAL", "MAJOR", "WARNING")]
    trends = []
    for issue in trend_issues[:5]:
        row = issue_to_trend_row(issue)
        trends.append(row)
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

    # Future-risk page is a plant-wide assessment, not an exception-only list.
    # Add a quantified normal row for every plant without a warning row.
    trend_plants = {row["plant"] for row in trends}
    for plant in health:
        if plant["plant"] in trend_plants:
            continue
        plant_rows = [r for group in plant_metrics.get(plant["plant"], {}).values() for r in group]
        if plant_rows:
            representative = min(plant_rows, key=lambda r: abs(float(r["performance"])))
            label = METRIC_LABELS.get(representative.get("_kind", ""), representative.get("_kind", "指標"))
            trends.append({
                "plant": plant["plant"], "metric": label,
                "current": f"近期水準 {fmt_num(representative['recent'], 2)}；前段水準 {fmt_num(representative['baseline'], 2)}",
                "target": f"偏離 {fmt_num(abs(representative['change_pct']), 1)}%",
                "trend": "穩定" if abs(float(representative["performance"])) <= 5 else "改善",
                "forecast": f"{label}（{representative['DESCRIPTION']}）近期相較前段水準變化 {fmt_num(representative['change_pct'], 1)}%，後續持續監測。",
                "level": "NORMAL",
            })
        else:
            trends.append({
                "plant": plant["plant"], "metric": "本週可評估指標",
                "current": "有效資料 0 筆", "target": "無法計算偏離百分比",
                "trend": "資料不足",
                "forecast": "本週沒有可用數值指標，無法進行量化趨勢評估；請確認資料來源與通訊狀態。",
                "level": "WARNING",
            })
    # Risk rank is severity-first, then quantified deviation.  This prevents
    # a NORMAL plant from appearing above a CRITICAL plant merely because of
    # the fixed plant order.
    trends.sort(key=lambda row: (-LEVEL_WEIGHT.get(str(row.get("level", "NORMAL")), 0),
                                 -float(row.get("score", 0) or 0), row["plant"]))

    # The page-1 KPI cards summarize what the report visibly lists:
    # P2 event rows for Critical/Major, and P3 trend rows for Warning.
    critical_count = sum(1 for row in events if row.get("level") == "CRITICAL")
    major_count = sum(1 for row in events if row.get("level") == "MAJOR")
    warning_count = sum(1 for row in trends if row.get("level") == "WARNING")

    # Management actions are one row per plant.  This keeps the page
    # comparable with the other all-plant topics while preserving the common
    # severity-first ordering.
    issues_by_plant: dict[str, list[Issue]] = {}
    for issue in reportable_issues:
        issues_by_plant.setdefault(issue.plant, []).append(issue)
    actions = []
    all_report_plants = {row["plant"] for row in health} | set(PLANT_ORDER) | source_plants
    for plant in sorted(all_report_plants,
                        key=lambda p: (PLANT_ORDER.index(p), p) if p in PLANT_ORDER else (99, p)):
        plant_issues = sorted(issues_by_plant.get(plant, []),
                              key=lambda x: rank_level(x.level, x.score), reverse=True)
        if plant_issues:
            issue = plant_issues[0]
            actions.append({"priority": "", "plant": plant, "item": issue.item,
                            "action": issue.action, "level": issue.level,
                            "_sort": (-LEVEL_WEIGHT.get(issue.level, 0), -float(issue.score), plant)})
        else:
            actions.append({"priority": "", "plant": plant, "item": "本週例行追蹤",
                            "action": "本週無需新增改善行動，維持監測並於下週確認趨勢。",
                            "level": "NORMAL", "_sort": (0, 0, plant)})
    actions.sort(key=lambda row: row["_sort"])
    for idx, action in enumerate(actions, 1):
        action["priority"] = str(idx)
        action.pop("_sort", None)

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
        "trend_rankings": health,
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
    if API_REPORT_REWRITE_ENABLED and polish_weekly_report_text_with_openai(report):
        report["meta"]["generator"] = "local_rules_v1_openai_weekly_text"
        report["meta"]["openai_text"] = "enabled"
    else:
        report["meta"]["openai_text"] = "disabled_local_rules"
    report["meta"]["narrative_ownership"] = narrative_ownership_metrics(report)

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
