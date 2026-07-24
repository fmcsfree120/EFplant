import pandas as pd
from datetime import datetime, timedelta
import os
import sys
import json
import base64
import time
import socket
import re
import html
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Hash import SHA256
from Crypto.Util.Padding import pad

# ── 強制 stdout/stderr 以 UTF-8 輸出 ────────────────────────────────
# Windows 主控台預設為 cp950(Big5)，print 中文或 emoji 時可能拋出
# UnicodeEncodeError 導致排程程序中斷。統一改為 UTF-8 並容錯。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def normalize_plant_label(value):
    """Canonical plant label used before frontend grouping or charting.

    SOP: every upstream KF row belongs to KF1.
    """
    plant = str(value).strip().upper()
    return "KF1" if plant == "KF" else plant


def normalize_plant_column(df):
    """Return data with canonical PLANT labels before it is published."""
    if not df.empty and "PLANT" in df.columns:
        df = df.copy()
        df["PLANT"] = df["PLANT"].map(normalize_plant_label)
    return df


def get_local_ip():
    try:
        # Create a dummy socket to resolve active subnet IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

# 定義設備類別與其對應字串、圖示、英文敘述
CATEGORIES = [
    {"name": "冰機",     "pattern": "CHU/CH", "icon": "❄️", "desc": "Chiller"},
    {"name": "空壓",     "pattern": "CDA",   "icon": "💨", "desc": "Compressed Air"},
    {"name": "外氣空調箱","pattern": "MAU",   "icon": "🌀", "desc": "MAU"},
    {"name": "製程專用空調箱風車","pattern": "PAH",   "icon": "🌀", "desc": "Process Air Motor"},
    {"name": "酸排氣",   "pattern": "ASCR",  "icon": "🧪", "desc": "Acid Scrubber"},
    {"name": "鹼排氣",   "pattern": "BSCR",  "icon": "⚗️", "desc": "Base Scrubber"},
    {"name": "有機排氣", "pattern": "VSCR",  "icon": "🍃", "desc": "VOC Scrubber"},
    {"name": "熱排氣",   "pattern": "HSCR",  "icon": "🔥", "desc": "Hot Scrubber"},
    {"name": "乾式集塵", "pattern": "DUST",  "exclude": "WDUST", "icon": "🧹", "desc": "Dry Dust"},
    {"name": "溼式集塵", "pattern": "WDUST", "icon": "💧", "desc": "Wet Dust"},
    {"name": "製程冷卻水","pattern": "PCW",   "icon": "🌊", "desc": "PCW"},
    {"name": "其他設備", "pattern": None,    "icon": "⚙️", "desc": "Other"},
]


def select_recovery_top_records(recovered: pd.DataFrame, limit: int = 10) -> pd.DataFrame:
    """Return each tag's longest single recovery event, ranked by that duration."""
    valid = recovered[recovered["RECOVERY_SECONDS"] >= 0].copy()
    if valid.empty or not (valid["RECOVERY_SECONDS"] > 30 * 60).any():
        return valid.iloc[0:0]
    counts = valid.groupby("ALM_TAGNAME").size().rename("SEGMENTS")
    longest_indexes = valid.groupby("ALM_TAGNAME")["RECOVERY_SECONDS"].idxmax()
    ranked = valid.loc[longest_indexes].copy()
    ranked["SEGMENTS"] = ranked["ALM_TAGNAME"].map(counts)
    return ranked.sort_values(
        ["RECOVERY_SECONDS", "RECOVERY_TIME"], ascending=[False, False]
    ).head(limit).reset_index(drop=True)

def classify_equipment(eqno: str) -> str:
    if not isinstance(eqno, str):
        return "其他設備"
    u = eqno.strip().upper()
    if "CHU" in u or "CH" in u: return "冰機"
    if "CDA"   in u: return "空壓"
    if "MAU"   in u: return "外氣空調箱"
    if "PAH"   in u: return "製程專用空調箱風車"
    if "ASCR"  in u: return "酸排氣"
    if "BSCR"  in u: return "鹼排氣"
    if "VSCR"  in u: return "有機排氣"
    if "HSCR"  in u: return "熱排氣"
    if "WDUST" in u: return "溼式集塵"
    if "DUST"  in u: return "乾式集塵"
    if "PCW"   in u: return "製程冷卻水"
    return "其他設備"


def update_known_equipment(df_actual: pd.DataFrame, script_dir: str):
    """
    全自動新設備偵測：
    - 首次執行：建立 known_equipment.json 做為基準，不標記 any 設備為 NEW
    - 後續執行：比對新出現的 EQNO，回傳 set of (plant, eqno)
    - 設備消失（除役）不影響運作，不誤報
    """
    known_path = os.path.join(script_dir, "known_equipment.json")

    try:
        with open(known_path, 'r', encoding='utf-8') as f:
            known = json.load(f)
        is_first_run = False
    except (FileNotFoundError, json.JSONDecodeError):
        known = {}
        is_first_run = True

    legacy_kf = set(known.pop("KF", []))
    if legacy_kf:
        known["KF1"] = sorted(set(known.get("KF1", [])) | legacy_kf)

    new_eqnos = set()

    for plant in sorted(df_actual['PLANT'].unique()):
        df_p    = df_actual[df_actual['PLANT'] == plant]
        current = set(df_p['EQNO'].astype(str))
        prev    = set(known.get(str(plant), []))

        if not is_first_run:
            for eq in sorted(current - prev):
                new_eqnos.add((str(plant), str(eq)))
                print(f"  [NEW EQ] {plant}: {eq}")

        known[str(plant)] = sorted(list(prev | current))

    with open(known_path, 'w', encoding='utf-8') as f:
        json.dump(known, f, indent=2, ensure_ascii=False)

    if new_eqnos:
        print(f"[NEW] 偵測到 {len(new_eqnos)} 個新設備，已自動標記於儀表板。")
    elif is_first_run:
        print("[OK] 首次建立設備清單，已記錄現有設備為基準。")
    else:
        print("[OK] 設備清單無新增。")

    return new_eqnos


def build_kf1_alarm_dashboard(script_dir: str) -> str:
    """Build the KF1 alarm-risk section (without the surrounding plant container)."""
    alarm_path = os.path.join(script_dir, "latest_alarm_history_backup.csv")
    if not os.path.exists(alarm_path):
        return """
  <div class="alarm-empty">
    <div class="alarm-empty-title">KF1 警報風險資料尚未建立</div>
    <div>請先執行 fetch_alarm_history.py 匯出 ALM_DB.dbo.ALM_KF 最近 7 天資料。</div>
  </div>"""

    try:
        alarms = pd.read_csv(alarm_path, encoding="utf-8-sig")
        required = {
            "ALM_NATIVETIMELAST", "ALM_TAGNAME", "ALM_DESCR",
            "ALM_ALMSTATUS", "ALM_ALMPRIORITY"
        }
        missing = sorted(required - set(alarms.columns))
        if missing:
            raise ValueError("缺少欄位：" + ", ".join(missing))

        alarms["TIME"] = pd.to_datetime(alarms["ALM_NATIVETIMELAST"], errors="coerce")
        for col in ("ALM_TAGNAME", "ALM_DESCR", "ALM_ALMSTATUS", "ALM_ALMPRIORITY"):
            alarms[col] = alarms[col].fillna("").astype(str).str.strip()
        alarms = alarms.dropna(subset=["TIME"]).sort_values("TIME")
        if alarms.empty:
            raise ValueError("CSV 沒有有效時間資料")

        abnormal = alarms[alarms["ALM_ALMSTATUS"] != "OK"]
        critical = abnormal[abnormal["ALM_ALMPRIORITY"] == "CRITICAL"]
        latest_by_tag = alarms.drop_duplicates("ALM_TAGNAME", keep="last")
        tag_count = int(alarms["ALM_TAGNAME"].nunique())
        total_count = int(len(abnormal))
        max_time = alarms["TIME"].max()
        min_time = alarms["TIME"].min()
        top10_cutoff = max_time - pd.Timedelta(hours=24)
        top10_alarms = alarms[alarms["TIME"] >= top10_cutoff].copy()

        # 整體風險＝各 Tag 最新狀態風險分數的平均值。
        # OK=0、CFN=30、LO/HI=60、LOLO/HIHI=90；CRITICAL 再加 10，單 Tag 上限 100。
        current_risk_weights = {"OK": 0, "CFN": 30, "LO": 60, "HI": 60, "LOLO": 90, "HIHI": 90}
        tag_risk = latest_by_tag["ALM_ALMSTATUS"].map(current_risk_weights).fillna(30).astype(float)
        tag_risk += (latest_by_tag["ALM_ALMPRIORITY"] == "CRITICAL").astype(int) * 10
        overall_risk = float(tag_risk.clip(upper=100).mean()) if len(tag_risk) else 0.0
        risk_class = "high" if overall_risk >= 60 else ("medium" if overall_risk >= 30 else "low")

        expected_days = pd.date_range(min_time.normalize(), max_time.normalize(), freq="D")
        actual_days = set(alarms["TIME"].dt.normalize().unique())
        missing_days = [d.strftime("%m/%d") for d in expected_days if d.to_datetime64() not in actual_days]
        coverage = ((len(expected_days) - len(missing_days)) / len(expected_days) * 100) if len(expected_days) else 0

        status_weights = {"HIHI": 5, "LOLO": 5, "HI": 3, "LO": 3, "CFN": 2, "OK": 0}
        # 兩個 TOP 10 僅統計資料最新時間往前 24 小時；其他總覽維持完整保留範圍。
        work = top10_alarms.copy()
        work["RISK_POINTS"] = work["ALM_ALMSTATUS"].map(status_weights).fillna(1)
        work.loc[work["ALM_ALMPRIORITY"] == "CRITICAL", "RISK_POINTS"] += 4
        risk = (work.groupby(["ALM_TAGNAME", "ALM_DESCR"], as_index=False)
                    .agg(EVENTS=("TIME", "size"),
                         CRITICAL=("ALM_ALMPRIORITY", lambda s: int((s == "CRITICAL").sum())),
                         RISK=("RISK_POINTS", "sum"),
                         LAST=("TIME", "max"))
                    .sort_values(["RISK", "EVENTS"], ascending=False)
                    .head(10))

        # 每日警報趨勢維持七日直條顯示，並納入每 30 分鐘同步的當日最新資料。
        daily = (alarms.assign(DAY=alarms["TIME"].dt.strftime("%m/%d"))
                       .groupby("DAY", sort=False)
                       .agg(TOTAL=("TIME", "size"),
                            ABNORMAL=("ALM_ALMSTATUS", lambda s: int((s != "OK").sum())),
                            CRITICAL=("ALM_ALMPRIORITY", lambda s: int((s == "CRITICAL").sum())))
                       .reset_index())
        hourly = (alarms.assign(HOUR=alarms["TIME"].dt.hour)
                        .groupby("HOUR").size().reindex(range(24), fill_value=0))

        risk_max = max(int(risk["RISK"].max()) if not risk.empty else 1, 1)
        risk_rows = []
        for _, row in risk.iterrows():
            width = max(3, int(row["RISK"] / risk_max * 100))
            risk_rows.append(f"""
        <div class="alarm-risk-row">
          <div class="alarm-risk-head"><span>{html.escape(row['ALM_DESCR'] or row['ALM_TAGNAME'])}</span><b>{int(row['RISK'])} pts</b></div>
          <div class="alarm-risk-tag">{html.escape(row['ALM_TAGNAME'])} · {int(row['EVENTS'])} 筆 · Critical {int(row['CRITICAL'])}</div>
          <div class="alarm-bar"><i style="width:{width}%"></i></div>
        </div>""")

        daily_max = max(int(daily["TOTAL"].max()) if not daily.empty else 1, 1)
        daily_bars = []
        for _, row in daily.iterrows():
            height = max(3, int(row["TOTAL"] / daily_max * 100))
            daily_bars.append(f"""
          <div class="alarm-day-col" tabindex="0" role="button" data-alarm-tip="{row['DAY']}：總計 {int(row['TOTAL'])}／異常 {int(row['ABNORMAL'])}／Critical {int(row['CRITICAL'])}">
            <div class="alarm-day-value">{int(row['TOTAL'])}</div>
            <div class="alarm-day-bar"><i style="height:{height}%"></i></div>
            <div class="alarm-day-label">{row['DAY']}</div>
          </div>""")

        hour_max = max(int(hourly.max()), 1)
        hour_cells = []
        for hour, count in hourly.items():
            alpha = 0.08 + (float(count) / hour_max * 0.82)
            hour_cells.append(
                f'<div class="alarm-hour" tabindex="0" role="button" style="background:rgba(244,63,94,{alpha:.2f})" '
                f'data-alarm-tip="{hour:02d}:00–{hour:02d}:59，共 {int(count)} 筆"><b>{hour:02d}</b><span>{int(count)}</span></div>'
            )

        # OK 紀錄同時帶有警報進入與最後復歸時間，可直接計算實際復歸耗時。
        recovered = top10_alarms[top10_alarms["ALM_ALMSTATUS"] == "OK"].copy()
        recovered["START_TIME"] = pd.to_datetime(
            recovered["ALM_DATEIN"].astype(str).str.strip() + " " + recovered["ALM_TIMEIN"].astype(str).str.strip(),
            format="mixed", errors="coerce")
        recovered["RECOVERY_TIME"] = pd.to_datetime(
            recovered["ALM_DATELAST"].astype(str).str.strip() + " " + recovered["ALM_TIMELAST"].astype(str).str.strip(),
            format="mixed", errors="coerce")
        recovered["RECOVERY_SECONDS"] = (recovered["RECOVERY_TIME"] - recovered["START_TIME"]).dt.total_seconds()

        # 每個 Tag 只取最近 24 小時內最長的一次「進入→OK」，依該次實際耗時排名。
        recovered = recovered[
            recovered["START_TIME"].notna()
            & recovered["RECOVERY_TIME"].notna()
            & (recovered["RECOVERY_SECONDS"] >= 0)
        ]
        # 若整批資料沒有任何一筆超過 30 分鐘，主管看板不顯示短時間雜訊。
        recovered = select_recovery_top_records(recovered, limit=10)

        def format_recovery_duration(seconds):
            total_seconds = max(0, int(round(float(seconds))))
            days, remain = divmod(total_seconds, 86400)
            hours, remain = divmod(remain, 3600)
            minutes, secs = divmod(remain, 60)
            if days:
                return f"{days}天 {hours}時 {minutes}分"
            if hours:
                return f"{hours}時 {minutes}分"
            return f"{minutes}分 {secs}秒"

        recovery_rows = []
        for rank, (_, row) in enumerate(recovered.iterrows(), start=1):
            recovery_rows.append(f"""
          <tr><td><span class="alarm-rank">{rank}</span></td>
          <td><b>{html.escape(row['ALM_DESCR'] or row['ALM_TAGNAME'])}</b><small>{html.escape(row['ALM_TAGNAME'])} · 24小時復歸 {int(row['SEGMENTS'])} 次</small></td>
          <td class="alarm-time"><span>{row['START_TIME'].strftime('%m/%d')}</span><span>{row['START_TIME'].strftime('%H:%M:%S')}</span></td>
          <td class="alarm-time"><span>{row['RECOVERY_TIME'].strftime('%m/%d')}</span><span>{row['RECOVERY_TIME'].strftime('%H:%M:%S')}</span></td>
          <td class="alarm-duration">{format_recovery_duration(row['RECOVERY_SECONDS'])}</td></tr>""")
        if not recovery_rows:
            recovery_rows.append('<tr><td colspan="5" class="alarm-all-clear">最近24小時無超過30分鐘未復歸記錄</td></tr>')

        missing_text = "、".join(missing_days) if missing_days else "無"
        freshness_hours = max(0.0, (datetime.now() - max_time.to_pydatetime()).total_seconds() / 3600)
        freshness_class = "danger" if freshness_hours > 24 else ("warn" if freshness_hours > 2 else "ok")
        critical_note = "Priority 與 HIHI/LOLO 並非完全一致，風險分數已同時納入狀態與 Priority。"

        return f"""
  <section class="alarm-hero">
    <div><div class="alarm-eyebrow">KF1 · iFIX ALARM RISK</div><h2>廠區運行風險總覽</h2>
      <p>{min_time.strftime('%Y/%m/%d %H:%M')} – {max_time.strftime('%Y/%m/%d %H:%M')} · 最近 7 日警報事件</p></div>
    <div class="alarm-freshness {freshness_class}"><span>資料最後更新</span><b>{freshness_hours:.1f} 小時前</b></div>
  </section>
  <div class="alarm-kpis">
    <div class="alarm-kpi red"><span>告警總計</span><b>{total_count:,}</b></div>
    <div class="alarm-kpi amber"><span>CRITICAL 紀錄</span><b>{len(critical):,}</b><small>{len(critical) / total_count * 100 if total_count else 0:.1f}% of total</small></div>
    <div class="alarm-kpi blue"><span>受監控 Tag</span><b>{tag_count:,}</b></div>
    <div class="alarm-kpi risk {risk_class}"><span>整體風險</span><b>{overall_risk:.1f}%</b><small>數字越高，代表目前未復歸警報越嚴重</small></div>
  </div>
  <div class="alarm-grid alarm-grid-primary">
    <section class="alarm-panel"><div class="alarm-panel-title"><span>每日警報趨勢</span><small>柱高＝全部事件；滑鼠移入或手機點擊可看明細</small></div>
      <div class="alarm-daily">{''.join(daily_bars)}</div></section>
    <section class="alarm-panel"><div class="alarm-panel-title"><span>24 小時風險熱區</span><small>顏色越深；滑鼠移入或手機點擊可看明細</small></div>
      <div class="alarm-hours">{''.join(hour_cells)}</div></section>
  </div>
  <div class="alarm-grid">
    <section class="alarm-panel"><div class="alarm-panel-title"><span>TOP 10 高風險設備</span><small>最近24小時；警報越嚴重、同一設備發生次數越多，排名越前面</small></div>{''.join(risk_rows)}</section>
    <section class="alarm-panel"><div class="alarm-panel-title"><span>TOP 10 警報復歸耗時</span><small>最近24小時；從警報發生到恢復正常所花的時間，時間越長排名越前面</small></div>
      <div class="alarm-table-wrap"><table class="alarm-table"><thead><tr><th>#</th><th>設備／訊息</th><th>警報發生</th><th>復歸時間</th><th>最長單次耗時</th></tr></thead><tbody>{''.join(recovery_rows)}</tbody></table></div></section>
  </div>
  <section class="alarm-data-health">
    <div><span>資料涵蓋率</span><b>{coverage:.0f}%</b></div><div><span>缺資料日期</span><b>{missing_text}</b></div>
    <p>注意：缺資料不可解讀為零警報。{critical_note}</p>
  </section>"""
    except Exception as exc:
        print(f"[WARN] KF1 警報儀表板建立失敗: {exc}")
        return f"""
  <div class="alarm-empty"><div class="alarm-empty-title">KF1 警報資料讀取失敗</div><div>{html.escape(str(exc))}</div></div>"""


def build_alarm_pending_section(plant: str) -> str:
    """Placeholder shown below equipment status until a plant's alarm feed is connected."""
    return f"""
  <section class="alarm-pending" aria-label="{html.escape(plant)} 廠區警報資料待接入">
    <div class="wip-icon">&#x1F6A7;</div>
    <div class="wip-title">施工中 / Under Construction</div>
    <div class="wip-msg">廠區警報資料待接入</div>
  </section>"""


# ─────────────────────────────────────────────────────────────────────────────
# 靜態 index.html 外殼（資料/外殼分離架構的「外殼」部分）
# ─────────────────────────────────────────────────────────────────────────────
STATIC_INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>FMCS - Facility Monitor Control System</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/crypto-js/4.1.1/crypto-js.min.js"></script>
<style>
:root{
  --bg:#0f172a;
  --sf:#1e293b;
  --cyan:#60a5fa; /* Brighter sky blue for logo/title */
  --red:#ef4444;
  --bd:#334155;
  --tx:#f1f5f9;
  --dim:#a2b9d2; /* Brighter slate blue for dark blue/gray text */
}
*{box-sizing:border-box;margin:0;padding:0;}
body{
  margin:0;background:var(--bg);color:var(--tx);
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  height:100vh;display:flex;align-items:center;justify-content:center;
}

/* ─────────────────────────────────────────────────────────────────────────────
   CYBER TECH LOGIN OVERLAY STYLES
   ───────────────────────────────────────────────────────────────────────────── */
@import url('https://fonts.googleapis.com/css2?family=Share+Tech+Mono&display=swap');

#lock-screen {
  position: fixed;
  top: 0;
  left: 0;
  width: 100vw;
  height: 100vh;
  background: radial-gradient(circle at center, #0c142b 0%, #030712 100%);
  display: none;
  align-items: center;
  justify-content: center;
  z-index: 99999;
  font-family: 'Share Tech Mono', Consolas, Monaco, monospace;
  overflow: hidden;
  box-sizing: border-box;
}

.cyber-grid {
  position: absolute;
  top: 0; left: 0; right: 0; bottom: 0;
  background-image: 
    linear-gradient(rgba(0, 240, 255, 0.03) 1px, transparent 1px),
    linear-gradient(90deg, rgba(0, 240, 255, 0.03) 1px, transparent 1px);
  background-size: 30px 30px;
  background-position: center;
  pointer-events: none;
}

.cyber-scanline {
  position: absolute;
  top: 0; left: 0; right: 0; bottom: 0;
  background: linear-gradient(
    rgba(18, 16, 16, 0) 50%, 
    rgba(0, 0, 0, 0.25) 50%
  ), linear-gradient(
    90deg, 
    rgba(255, 0, 0, 0.04), 
    rgba(0, 255, 0, 0.02), 
    rgba(0, 0, 255, 0.04)
  );
  background-size: 100% 4px, 6px 100%;
  pointer-events: none;
}

.cyber-scanline::after {
  content: "";
  position: absolute;
  top: 0; left: 0; right: 0; height: 20px;
  background: linear-gradient(to bottom, transparent, #00f0ff 40%, #ffffff 50%, #00f0ff 60%, transparent);
  box-shadow: 
    0 0 10px #00f0ff,
    0 0 25px #00f0ff,
    0 0 50px #00f0ff,
    0 0 80px rgba(0, 240, 255, 0.6);
  animation: lock-scan 2s linear infinite;
  opacity: 1.0;
}

@keyframes lock-scan {
  0% { top: -5%; }
  100% { top: 105%; }
}

#lock-screen .panel {
  border: 1px solid rgba(0, 240, 255, 0.25);
  border-top: 4px solid #00f0ff;
  padding: 30px 24px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
  background: rgba(10, 17, 34, 0.88);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border-radius: 8px;
  width: 90%;
  max-width: 380px;
  box-shadow: 
    0 15px 35px rgba(0,0,0,0.6),
    0 0 15px rgba(0, 240, 255, 0.1);
  position: relative;
  z-index: 10;
}

#lock-screen .logo {
  font-size: 2.2rem;
  font-weight: 800;
  letter-spacing: 5px;
  color: #fff;
  text-shadow: 0 0 10px rgba(0, 240, 255, 0.4);
  white-space: nowrap;
  text-align: center;
}

#lock-screen .sub {
  color: #a2b9d2;
  font-size: 0.68rem;
  letter-spacing: 1.2px;
  margin-top: -6px;
  text-transform: uppercase;
  white-space: nowrap;
  text-align: center;
  opacity: 0.8;
}

.terminal-header {
  font-size: 0.95rem; /* Enlarge but smaller than EFplant's 2.2rem */
  color: #00f0ff;     /* Brighter sky-cyan */
  letter-spacing: 2px;
  text-transform: uppercase;
  margin-top: 4px;
  font-weight: bold;
  text-shadow: 0 0 12px rgba(0, 240, 255, 0.8), 0 0 4px rgba(0, 240, 255, 0.4);
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
}

.fplus-inline {
  font-weight: 800;
  color: #00f0ff;
  text-shadow: 0 0 12px rgba(0, 240, 255, 0.9);
  letter-spacing: 1px;
}

.cyber-hr {
  width: 100%;
  border: none;
  border-top: 1px dashed rgba(0, 240, 255, 0.2);
}

.telemetry-panel {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 8px 16px;
  width: 100%;
  padding: 8px 10px;
  background: rgba(0, 0, 0, 0.35);
  border-radius: 4px;
  border: 1px solid rgba(0, 240, 255, 0.1);
  box-sizing: border-box;
}
.tel-row {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.tel-lbl {
  font-size: 0.52rem;
  color: rgba(162, 185, 210, 0.6);
  letter-spacing: 0.5px;
  text-align: left;
}
.tel-val {
  font-size: 0.65rem;
  color: #f1f5f9;
  font-weight: bold;
  text-align: left;
}
.glow-cyan { color: #00f0ff; text-shadow: 0 0 4px rgba(0, 240, 255, 0.4); }
.glow-green { color: #10b981; text-shadow: 0 0 4px rgba(16, 185, 129, 0.4); }
.glow-amber { color: #f59e0b; text-shadow: 0 0 4px rgba(245, 158, 11, 0.4); }

#lock-screen .lbl {
  color: rgba(162, 185, 210, 0.8);
  font-size: 0.68rem;
  letter-spacing: 1.5px;
  align-self: flex-start;
  font-weight: 600;
}

.input-wrapper {
  width: 100%;
  position: relative;
  display: flex;
  align-items: center;
}
.input-arrow {
  position: absolute;
  left: 12px;
  color: #00f0ff;
  font-size: 1rem;
  pointer-events: none;
  font-weight: bold;
}
#lock-screen #pwd {
  width: 100%;
  background: rgba(0, 0, 0, 0.5);
  border: 1px solid rgba(0, 240, 255, 0.3);
  border-left: 3px solid #00f0ff;
  color: #fff;
  font-family: inherit;
  font-size: 1.1rem;
  padding: 10px 12px 10px 30px;
  outline: none;
  letter-spacing: 6px;
  border-radius: 4px;
  transition: all 0.25s ease;
  box-sizing: border-box;
}
#lock-screen #pwd:focus {
  border-color: #00f0ff;
  box-shadow: 
    0 0 10px rgba(0, 240, 255, 0.15),
    inset 0 0 5px rgba(0, 240, 255, 0.05);
}
#lock-screen #pwd.err {
  border-color: #ef4444;
  border-left-color: #ef4444;
  box-shadow: 0 0 10px rgba(239, 68, 68, 0.2);
}

#lock-screen #btn {
  width: 100%;
  padding: 12px;
  background: transparent;
  border: 1px solid #00f0ff;
  color: #00f0ff;
  font-family: inherit;
  font-size: 0.85rem;
  font-weight: 700;
  letter-spacing: 2px;
  cursor: pointer;
  border-radius: 4px;
  white-space: nowrap;
  transition: all 0.2s ease;
  box-shadow: inset 0 0 4px rgba(0, 240, 255, 0.1);
  position: relative;
  overflow: hidden;
}
#lock-screen #btn:hover:not(:disabled) {
  background: rgba(0, 240, 255, 0.1);
  color: #fff;
  box-shadow: 
    0 0 15px rgba(0, 240, 255, 0.3),
    inset 0 0 8px rgba(0, 240, 255, 0.2);
  text-shadow: 0 0 4px #fff;
}
#lock-screen #btn:disabled {
  border-color: rgba(0, 240, 255, 0.2);
  color: rgba(0, 240, 255, 0.2);
  cursor: not-allowed;
}

.terminal-logs {
  width: 100%;
  min-height: 72px;
  max-height: 100px;
  overflow-y: auto;
  background: rgba(0, 0, 0, 0.6);
  border: 1px solid rgba(0, 240, 255, 0.15);
  border-radius: 4px;
  padding: 8px 10px;
  box-sizing: border-box;
  display: none;
  flex-direction: column;
  gap: 4px;
}
.log-line {
  font-size: 0.58rem;
  line-height: 1.3;
  letter-spacing: 0.5px;
  text-align: left;
  white-space: normal;
  word-break: break-all;
}
.text-cyan { color: #00f0ff; }
.text-green { color: #10b981; }
.text-red { color: #ef4444; }

.cyber-err {
  display: none;
  color: #ef4444;
  font-size: 0.68rem;
  letter-spacing: 1px;
  white-space: normal;
  text-align: center;
  font-weight: bold;
  animation: glitch-vibrate 0.15s infinite alternate;
  background: rgba(239, 68, 68, 0.1);
  border: 1px solid rgba(239, 68, 68, 0.3);
  padding: 8px 12px;
  width: 100%;
  border-radius: 4px;
  box-sizing: border-box;
}

@keyframes glitch-vibrate {
  0% { transform: translate(1px, 1px) rotate(0deg); }
  100% { transform: translate(-1px, -1px) rotate(0.5deg); }
}

.reload-btn {
  background: transparent;
  border: none;
  color: rgba(162, 185, 210, 0.5);
  font-family: inherit;
  font-size: 0.6rem;
  cursor: pointer;
  letter-spacing: 1px;
  font-weight: bold;
  transition: color 0.15s ease;
}
.reload-btn:hover {
  color: #00f0ff;
  text-decoration: underline;
}
</style>
</head>
<body>
<div id="lock-screen">
  <div class="cyber-grid"></div>
  <div class="cyber-scanline"></div>
  <div class="panel">
    <div class="logo">EFplant</div>
    <div class="sub">Facility Monitor Control System</div>
    <div class="terminal-header">POWERED BY FMCS DECK <span class="fplus-inline">[F+]</span></div>
    <hr class="cyber-hr">
    
    <div class="telemetry-panel">
      <div class="tel-row"><span class="tel-lbl">CORE SPIRIT</span><span class="tel-val glow-cyan">F+ ACTIVE</span></div>
      <div class="tel-row"><span class="tel-lbl">NODE COMPILER</span><span class="tel-val glow-green">ONLINE</span></div>
      <div class="tel-row"><span class="tel-lbl">SECURE SUITE</span><span class="tel-val">AES-256-CBC</span></div>
      <div class="tel-row"><span class="tel-lbl">SYSTEM BASE</span><span class="tel-val glow-amber">STABLE</span></div>
    </div>
    
    <hr class="cyber-hr">
    <div class="lbl">ACCESS CODE</div>
    
    <div class="input-wrapper">
      <span class="input-arrow">&gt;</span>
      <input type="password" id="pwd"
             placeholder="••••••••"
             autocomplete="new-password" onkeydown="if(event.key==='Enter')go()">
    </div>
    
    <button id="btn" onclick="go()">AUTHORIZE ACCESS</button>
    
    <div id="loading" class="terminal-logs"></div>
    <div id="err" class="cyber-err"></div>
    
    <hr class="cyber-hr" style="margin-top: 8px;">
    <button onclick="clearAndReload()" class="reload-btn">
      🔄 RESET SYSTEM CACHE & RELOAD
    </button>
  </div>
</div>
<script>
// 立即判斷：有 Session 則保持隱藏，無 Session 則立即顯示登入面板（防止閃屏關鍵）
(function(){
  try {
    var d = JSON.parse(localStorage.getItem('ef_sess_v2'));
    if (!d || d.exp <= Date.now() || !d.dk) {
      document.getElementById('lock-screen').style.display = 'flex';
    }
  } catch(e) {
    document.getElementById('lock-screen').style.display = 'flex';
  }
})();
</script>
<script>
var API_BASE = null;
var SK = 'ef_sess_v2';
var SEXP = 8 * 60 * 60 * 1000; // 8小時 Session 有效期
var _efpDk = null;
var _efpLastUpdated = null;
var _efpPollStarted = false;
var LOGIN_AUDIT_ENABLED = false;
var CACHE_EPOCH = 'alarm-daily-live-7d-20260724-24';

(function resetOldFrontendCache() {
  try {
    if (localStorage.getItem('ef_cache_epoch') === CACHE_EPOCH) return;
    localStorage.setItem('ef_cache_epoch', CACHE_EPOCH);
    var done = Promise.resolve();
    if ('serviceWorker' in navigator) {
      done = done.then(function() {
        return navigator.serviceWorker.getRegistrations().then(function(regs) {
          return Promise.all(regs.map(function(reg) { return reg.unregister(); }));
        });
      });
    }
    if ('caches' in window) {
      done = done.then(function() {
        return caches.keys().then(function(keys) {
          return Promise.all(keys.map(function(k) { return caches.delete(k); }));
        });
      });
    }
    done.then(function() {
      var base = location.protocol + '//' + location.host + location.pathname;
      location.replace(base + '?_cache=' + CACHE_EPOCH + '&_=' + Date.now());
    });
  } catch(e) {}
})();

// 跨頁面且持久的一致性 Session ID
(function() {
  try {
    var sId = localStorage.getItem('ef_sess_id');
    if (!sId) {
      sId = (Math.random().toString(36).substring(2) + Date.now().toString(36)).substring(0, 16);
      localStorage.setItem('ef_sess_id', sId);
    }
    if (API_BASE) {
      localStorage.setItem('ef_api_base', API_BASE);
    }
  } catch(e) {}
})();

function getDeviceId() {
  try {
    var id = localStorage.getItem('ef_device_id');
    if (!id) {
      id = 'EF-' + (Math.random().toString(36).substring(2) + Date.now().toString(36)).substring(0, 18).toUpperCase();
      localStorage.setItem('ef_device_id', id);
    }
    return id;
  } catch(e) {
    return 'EF-UNKNOWN';
  }
}

function sendLog(action, success, indexId) {
  if (!LOGIN_AUDIT_ENABLED) return;
  if (!API_BASE) return;
  var sess = null;
  try { sess = JSON.parse(localStorage.getItem(SK)) || {}; } catch(e) {}
  var sessId = localStorage.getItem('ef_sess_id') || '';
  var payload = {
    action: action,
    success: success !== undefined ? success : true,
    index_id: indexId || '',
    device_id: getDeviceId(),
    session_id: sessId,
    timestamp: Date.now()
  };
  if (action === 'LOGOUT' && sess && sess.login_time) {
    var durSec = Math.floor((Date.now() - sess.login_time) / 1000);
    var h = Math.floor(durSec / 3600);
    var m = Math.floor((durSec % 3600) / 60);
    var s = durSec % 60;
    payload.duration = (h > 0 ? h + 'h ' : '') + (m > 0 ? m + 'm ' : '') + s + 's';
  }
  function sendLogFallback() {
    try {
      var qs = Object.keys(payload).map(function(k) {
        return encodeURIComponent(k) + '=' + encodeURIComponent(payload[k] == null ? '' : payload[k]);
      }).join('&');
      var img = new Image();
      img.src = API_BASE + '/api/log?' + qs + '&_=' + Date.now();
    } catch(e) {}
  }
  fetch(API_BASE + '/api/log', {
    method: 'POST',
    mode: 'cors',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  }).catch(function(e) { sendLogFallback(); });
}

function saveSess(dkh) {
  try {
    var existing = null;
    try { existing = JSON.parse(localStorage.getItem(SK)); } catch(ex){}
    var ltime = (existing && existing.login_time) ? existing.login_time : Date.now();
    localStorage.setItem(SK, JSON.stringify({dk:dkh, exp:Date.now()+SEXP, login_time:ltime}));
  } catch(e){}
}
function loadSess() {
  try {
    var d = JSON.parse(localStorage.getItem(SK));
    if (d && d.exp > Date.now() && d.dk) return d.dk;
  } catch(e){}
  localStorage.removeItem(SK);
  return null;
}

function decryptData(data, dkh) {
  var iid = dkh.substring(0, 32);
  var ekh = dkh.substring(32);
  var safe = data.key_safes[iid];
  if (!safe) return null;
  var ek = CryptoJS.enc.Hex.parse(ekh);
  var siv = CryptoJS.enc.Hex.parse(safe.iv);
  var cp = CryptoJS.lib.CipherParams.create({ciphertext:CryptoJS.enc.Hex.parse(safe.enc_master)});
  var mk = CryptoJS.AES.decrypt(cp, ek, {iv:siv, mode:CryptoJS.mode.CBC, padding:CryptoJS.pad.Pkcs7});
  if (mk.sigBytes !== 32) return null;
  var piv = CryptoJS.enc.Base64.parse(data.payload_iv);
  var pc = CryptoJS.lib.CipherParams.create({ciphertext:CryptoJS.enc.Base64.parse(data.payload)});
  return CryptoJS.AES.decrypt(pc, mk, {iv:piv, mode:CryptoJS.mode.CBC, padding:CryptoJS.pad.Pkcs7})
    .toString(CryptoJS.enc.Utf8) || null;
}

function fetchData() {
  return fetch('data.enc?_=' + Date.now(), {cache:'no-store'})
    .then(function(r){ if(!r.ok) throw new Error('fetch'); return r.json(); });
}

function checkForUpdate() {
  fetchData().then(function(data){
    if (_efpLastUpdated && data.updated > _efpLastUpdated) {
      location.reload();
    }
  }).catch(function(){});
}

function startPolling() {
  if (_efpPollStarted) return;
  _efpPollStarted = true;
  setInterval(checkForUpdate, 60000);
  document.addEventListener('visibilitychange', function(){
    if (document.visibilityState === 'visible') checkForUpdate();
  });
}

function renderDashboard(data, dkh) {
  var html = decryptData(data, dkh);
  if (!html) return false;
  _efpDk = dkh;
  _efpLastUpdated = data.updated;
  saveSess(dkh);
  startPolling();
  document.open();
  document.write(html);
  document.close();
  return true;
}

function showError(msg) {
  var e = document.getElementById('err');
  if (e) {
    if (msg) {
      e.innerHTML = '<span class="err-icon">⚠️</span> ' + msg;
    } else {
      e.innerHTML = '<span class="err-icon">⚠️</span> ACCESS DENIED: SYSTEM AUTHORIZATION FAILED';
    }
    e.style.display = 'block';
  }
  var p = document.getElementById('pwd');
  if (p) { p.value = ''; p.classList.add('err'); }
  setTimeout(function(){
    if (e) e.style.display = 'none';
    if (p) p.classList.remove('err');
  }, 2200);
  if (p) p.focus();
}

function go() {
  var p = document.getElementById('pwd').value;
  if (!p) return;
  var e = document.getElementById('err');
  var loading = document.getElementById('loading');
  var btn = document.getElementById('btn');
  
  if (e) e.style.display = 'none';
  if (btn) btn.disabled = true;
  
  if (loading) {
    loading.style.display = 'flex';
    loading.innerHTML = '<div class="log-line text-cyan">&gt; INITIATING SECURITY CONSOLE ACCESS...</div>';
  }

  function appendLog(text, className) {
    if (!loading) return;
    var line = document.createElement('div');
    line.className = 'log-line ' + (className || 'text-cyan');
    line.innerHTML = text;
    loading.appendChild(line);
    loading.scrollTop = loading.scrollHeight;
  }

  setTimeout(function() {
    appendLog('&gt; ESTABLISHING SECURE HANDSHAKE PATH...');
    
    setTimeout(function() {
      appendLog('&gt; TRANSITING ENCRYPTED DATAPACK FROM HOST...');
      
      fetchData().then(function(data) {
        appendLog('&gt; SECURE ENVELOPE ACQUIRED (v' + (data.v || '2') + ')');
        
        setTimeout(function() {
          appendLog('&gt; CALCULATING PBKDF2 DERIVATION KEY (100k ITERATIONS)...');
          
          setTimeout(function() {
            try {
              var salt = CryptoJS.enc.Hex.parse(data.salt);
              var dk = CryptoJS.PBKDF2(p, salt, {keySize:256/32, iterations:100000, hasher:CryptoJS.algo.SHA256});
              var dkh = CryptoJS.enc.Hex.stringify(dk);
              
              appendLog('&gt; SECURING DERIVED INDEX: ' + dkh.substring(0, 16) + '...');
              appendLog('&gt; ATTEMPTING AES-256 MASTER KEY DECRYPTION...');
              
              setTimeout(function() {
                if (renderDashboard(data, dkh)) {
                  appendLog('&gt; SECURITY PAYLOAD VERIFIED. INTEGRITY 100%.', 'text-green');
                  appendLog('&gt; AUTHORIZATION GRANTED. REDIRECTING...', 'text-green');
                  sendLog('LOGIN', true, dkh.substring(0, 32));
                } else {
                  appendLog('&gt; CIPHER DISCREPANCY: AES INTEGRITY CHECK FAILED', 'text-red');
                  sendLog('LOGIN', false, dkh.substring(0, 32));
                  setTimeout(function() {
                    showError('ACCESS DENIED: VALIDATION DEVIATION');
                    loading.style.display = 'none';
                    btn.disabled = false;
                  }, 400);
                }
              }, 150);
            } catch(errEx) {
              appendLog('&gt; CRITICAL ERROR DURING CRYPTO-DERIVATION: ' + errEx.message, 'text-red');
              sendLog('LOGIN', false, '');
              setTimeout(function() {
                showError('ACCESS DENIED: CRYPTO ERROR');
                loading.style.display = 'none';
                btn.disabled = false;
              }, 400);
            }
          }, 30);
        }, 150);
      }).catch(function(err) {
        appendLog('&gt; NETWORK ACCESS PORT DROPPED.', 'text-red');
        setTimeout(function() {
          showError('NETWORK TRANSIT FAILURE');
          loading.style.display = 'none';
          btn.disabled = false;
        }, 400);
      });
    }, 150);
  }, 100);
}

window.addEventListener('DOMContentLoaded', function(){
  document.getElementById('pwd').value = '';
  var dkh = loadSess();
  if (!dkh) {
    document.getElementById('lock-screen').style.display = 'flex';
    return;
  }
  fetchData().then(function(data){
    if (!renderDashboard(data, dkh)) {
      localStorage.removeItem(SK);
      document.getElementById('lock-screen').style.display = 'flex';
    }
  }).catch(function(){
    document.getElementById('lock-screen').style.display = 'flex';
  });
});

function clearAndReload() {
  localStorage.removeItem(SK);
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.getRegistrations().then(function(regs) {
      var promises = [];
      for (var i = 0; i < regs.length; i++) {
        promises.push(regs[i].unregister());
      }
      return Promise.all(promises);
    }).then(function() {
      if ('caches' in window) {
        return caches.keys().then(function(keys) {
          return Promise.all(keys.map(function(k) { return caches.delete(k); }));
        });
      }
    }).then(function() {
      var base = location.protocol + '//' + location.host + location.pathname;
      location.replace(base + '?_nc=' + Date.now());
    }).catch(function() {
      location.reload();
    });
    return;
  }
  location.reload();
}

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('./service-worker.js?v=alarm-daily-live-7d-20260724-24', {updateViaCache:'none'}).catch(function(){});
}
</script>
</body>
</html>
"""


# ══════════════════════════════════════════════════════════════════════════
#  趨勢圖資料自動分類與上架機制 (Metric Auto-Onboarding Registry)
#  ------------------------------------------------------------------------
#  單一資料分類來源：classify_quality_row() 將每一筆原始資料 (TAGNAME, EQNAME)
#  對應到 (category 圖表分類, series_name 子序列名稱)。compile_quality_data 依此
#  自動把資料灌入對應圖表結構；前端 renderChemCharts() 會依 chem_names 動態長出
#  圖表，因此「新廠區 / 新化學品資料一旦進到 SQL，即自動上架，無需改繪圖程式」。
#
#  ▶ 後續擴充（供應水質 / 排氣靜壓 / 廢水處理 比照辦理）：
#    只需在 classify_quality_row() 內，依該類別在 EQNAME / TAGNAME 的特徵新增一條
#    判定規則並回傳對應 category，再於 compile_quality_data 的收集迴圈補上一個
#    series 結構即可（排氣靜壓已預先以 EQNAME 含「靜壓」辨識，待資料齊備即可接圖）。
# ══════════════════════════════════════════════════════════════════════════

# 大宗化學品液位：TAGNAME 需含液位量測 token（LT/LS/LIT/WL/TANK/LEVEL）以確保是液位讀值
_CHEM_LEVEL_TOKEN = re.compile(r'(?:LT\d|_LT(?:_|\b)|_LS_|_LS(?:_|\b)|LS_\d|LIT|_WL(?:_|\b)|TANK|LEVEL)')
# 可辨識為大宗化學品的名稱（EQNAME 正規化後比對；如需新增化學品/產品代號於此擴充）
_CHEM_NAME = re.compile(
    r'^(H2O2|H2SO4|H3PO4|HCL|HF|HNO3|NH4OH|NA2CO3|NACLO3|NACIO3|'
    r'NACLO|NAOH\d*|KOH|IPA|SPS|SPM|SPST|NH3|ROU|SN|'
    r'CE\d+R?|CM\d+R?|CS\d+R?|CZ\w+)$'
)


# 化學品別名對照：來源資料拼寫不一者，統一為同一合併鍵（左→右）。
# 可於此擴充其他需要合併的化學品別名。
_CHEM_ALIASES = {
    "NACIO3": "NACLO3",  # PCB 的 NACIO3 與 HJ1 的 NaClO3 同為氯酸鈉(NaClO3)，I/L 拼寫不一，合併
    "SPARE": "CZ8201R",  # T2A 的備用槽 SPARE 歸併入 S2A 既有的 CZ8201R（目前僅 T2A 有 SPARE）
}


def _norm_chem(name):
    """正規化化學品名稱作為跨廠區合併鍵（轉大寫、去除空白與符號，再套用別名對照）。
    例：'NaOH 32%'→'NAOH32'、'CM-2250R'→'CM2250R'、'Na2CO3'→'NA2CO3'、
        'NACIO3'/'NaClO3'→'NACLO3'、'SPARE'→'CZ8201R'。"""
    key = re.sub(r'[^A-Z0-9]', '', str(name).upper())
    return _CHEM_ALIASES.get(key, key)


def chem_chart_key(norm_name):
    """將正規化後的化學品合併鍵，對應到 (圖表名稱, 同廠區子序列濃度後綴)。
    NaOH 各濃度(NAOH/NAOH15/NAOH32/NAOH45)統一歸到同一張『NaOH』圖；
    濃度則作為同廠區多槽的線別後綴(如 '15%')，避免同廠區多濃度互相覆蓋。
    其餘化學品：圖表名稱=合併鍵本身、無濃度後綴。"""
    m = re.match(r'^NAOH(\d*)$', norm_name)
    if m:
        conc = m.group(1)
        return ("NaOH", f"{conc}%" if conc else "")
    return (norm_name, "")


def water_loc_label(eqname):
    """供應水質量測點位置標籤：取自 EQNAME 去除『電阻/導電度』等類型字樣後的餘字
    (如 'A棟高層'/'401'/'高層'/'產水1')；全部統一以導電度(µS/cm)呈現於同一張圖。"""
    e = str(eqname)
    for kw in ("供水電阻", "供水導電度", "產水導電度", "出水導電度", "電阻", "導電度"):
        e = e.replace(kw, "")
    return e.strip()


def water_value_to_conductivity(eqname, value):
    """將供應水質讀值統一換算為導電度(µS/cm)。
    電阻(MΩ·cm) → 導電度(µS/cm)：κ = 1/ρ（ρ≤0 視為無效回傳 None）；
    本身即導電度者原值回傳。"""
    if "電阻" in str(eqname):
        return (1.0 / value) if value and value > 0 else None
    return value


# ── 供應水質「上架資料處理標準流程」設定（新資料一律於此處增刪即可生效）──────
# (1) 排除清單：不上架的量測點 TAGNAME（一律大寫比對）。
_WATER_EXCLUDE_TAGS = {
    "UPW.UPW_RO1_CIT101B_PV.F_CV",  # PCB 高層供水導電度（僅保留低層，不上架高層）
}
# (2) 線別標籤覆寫：(廠區, EQNAME) → 自訂圖例顯示名稱。
#     註：HJ 6F-1/6F-2（產水導電度1/2）已取消；HJ1 改以出水導電度(SW_PUW)呈現為「HJ1」。
_WATER_LABEL_OVERRIDE = {
    ("PCB", "低層供水導電度"): "PCB",   # PCB 僅一條供水導電度，圖例直接顯示廠區名「PCB」
}


def waste_chart_key(eqname):
    """廢水處理：各廠區皆為廢水出口/中和槽 pH 量測，統一歸到單一『出口pH』圖
    （避免同一 pH 資料點分散到中和暫存pH／廢水處理兩張圖）。"""
    return "出口pH"


_STATIC_START_TIME = pd.Timestamp("2026-06-24 14:00:00")


def static_chart_key(plant, tagname, eqname, description):
    """依設備描述與人工覆寫，回傳靜壓圖表名稱；非靜壓資料回傳 None。

    分類自 2026-06-24 14:00 起採用。未標示種類、袋式或彈匣式集塵皆歸乾式集塵；
    S2 WDA28/WDA37 依現場確認歸濕式集塵。
    """
    plant = str(plant).strip().upper()
    tag = str(tagname).upper()
    eq = str(eqname).strip()
    eq_upper = eq.upper()
    desc = str(description)

    if plant == "TH" and ("_DPT" in tag or "_DP" in tag):
        if "_DPT_TOT" in tag:
            return None
        if "WDUST" in tag or eq_upper.startswith("WDUST"):
            return "濕式集塵靜壓"
        if "DUST" in tag or eq_upper.startswith(("DUST", "WDUST", "WDA", "A1DUST", "A2DUST")):
            return "乾式集塵靜壓"
        if "_ACE_" in tag or eq_upper.startswith(("ASCR", "ACE", "A1ASCR", "A2ASCR")):
            return "酸排氣靜壓"
        if "_ALK_" in tag or eq_upper.startswith(("BSCR", "ALK", "A1BSCR", "A2BSCR")):
            return "鹼排氣靜壓"
        if "_GEX_HEAT_" in tag or eq_upper.startswith(("HSCR", "A1HSCR", "A2HSCR")):
            return "熱排氣靜壓"
        if "_VOC_" in tag or eq_upper.startswith(("VSCR", "VOC", "A1VSCR", "A2VSCR")):
            return "有機排氣靜壓"

    if "靜壓" not in eq and "靜壓" not in desc:
        return None

    # 舊備份可能仍含 SQL 彙總點；來源雖已排除，這裡保留防呆避免重複上架。
    if "_DPT_TOT" in tag:
        return None

    # 集塵優先判斷，避免設備代號 ASCRA/WDA 等與排氣類型前綴混淆。
    is_dust = "集塵" in desc or eq_upper.startswith(("DUST", "WDUST", "WDA"))
    if is_dust:
        # Equipment code is authoritative: WDUST always means wet dust.
        if eq_upper.startswith("WDUST") or "WDUST" in tag:
            return "濕式集塵靜壓"
        if plant == "S2" and eq_upper in {"WDA28", "WDA37"}:
            return "濕式集塵靜壓"
        if any(word in desc for word in ("濕式", "溼式", "濕集塵", "溼集塵")):
            return "濕式集塵靜壓"
        return "乾式集塵靜壓"

    # 文字描述優先於設備代號；T2A VSCR#1~#3 的描述為熱排，顯示名稱另改為 HSCR。
    if "酸排" in desc:
        return "酸排氣靜壓"
    if "鹼排" in desc:
        return "鹼排氣靜壓"
    if "熱排" in desc:
        return "熱排氣靜壓"
    if "有機排" in desc:
        return "有機排氣靜壓"

    if eq_upper.startswith(("ASCR", "ACE")):
        return "酸排氣靜壓"
    if eq_upper.startswith(("BSCR", "ALK")):
        return "鹼排氣靜壓"
    if eq_upper.startswith("HSCR"):
        return "熱排氣靜壓"
    if eq_upper.startswith(("VSCR", "VOC")):
        return "有機排氣靜壓"
    return None


def static_series_label(plant, eqname, tagname=""):
    """靜壓圖例採「廠區-設備編號」；套用已確認的設備名稱覆寫。"""
    plant = str(plant).strip().upper()
    eq = str(eqname).strip()
    if plant == "TH" and (not eq or eq.lower() == "nan"):
        tag = str(tagname).upper()
        m = re.search(r'EXH_(A[12])_(ACE|ALK|GEX_HEAT|VOC)_(\d+)_DPT', tag)
        if m:
            area, kind, seq = m.groups()
            prefix = {
                "ACE": "ASCR",
                "ALK": "BSCR",
                "GEX_HEAT": "HSCRA",
                "VOC": "VSCRA",
            }[kind]
            eq = f"{area}{prefix}{seq}"
        else:
            m = re.search(r'UMTH_DUST(\d+)_(A[12])_(\dF)_DUST_(\d+)_FD_DPT', tag)
            if m:
                dust_no, area, floor, seq = m.groups()
                eq = f"{area}DUST{dust_no}_{floor}_{int(seq)}"
    if plant == "T2A" and eq.upper() in {"VSCR#1", "VSCR#2", "VSCR#3"}:
        eq = eq.upper().replace("VSCR", "HSCR", 1)
    return f"{plant}-{eq}"


def power_series_label(plant, eqname, tagname="", description=""):
    plant = str(plant).strip().upper()
    eq_label = str(eqname).strip()
    if eq_label and eq_label.lower() != "nan" and eq_label.upper() != plant:
        return f"{plant} {eq_label}"

    tag = str(tagname).strip().upper()
    desc = str(description).strip()

    m = re.search(r'PWR_([^_]+)_', tag)
    if m:
        return f"{plant} 廠區用電{m.group(1)}"

    m = re.search(r'_PW_([A-Z])', tag)
    if m:
        return f"{plant} 廠區用電{m.group(1)}"

    m = re.search(r'供電([^ _]+)', desc)
    if m:
        return f"{plant} 廠區用電{m.group(1)}"

    m = re.search(r'電力([A-ZＡ-Ｚ])', desc)
    if m:
        return f"{plant} 廠區用電{m.group(1)}"

    if any(tok in tag for tok in ("MGCB", "_MW", ".MW", "PWR", "_PW_")):
        return f"{plant} 廠區用電"

    return plant


def classify_quality_row(tagname, eqname, plant="", description=""):
    """趨勢圖資料分類唯一入口。回傳 (category, series_name)：
      category    : '大宗化學品' | '空壓效率' | '冰機效率' | '廠區用電' | '排氣靜壓' | None
      series_name : 大宗化學品→化學品名稱(合併鍵)；排氣靜壓→排氣別；其餘→None(以廠區為序列)
    分類順序刻意讓「大宗化學品」最優先，避免 TAGNAME 含 AIR/CHM 等子字串被能效規則誤判。
    """
    tag = str(tagname).upper()
    eq = str(eqname)

    # ── ① 大宗化學品（最優先）─────────────────────────────────────────────
    # Convention A：EQNAME 標示「大宗化學品」，化學品名稱嵌於 TAGNAME (..._<NAME>_TANK_...)
    if "大宗化學品" in eq:
        m = re.search(r'(?:CHEM|SUP)_(.+?)_TANK', tag)
        # 同樣套用正規化與別名對照（使 SPARE→CZ8201R 等合併生效）
        return ("大宗化學品", _norm_chem(m.group(1)) if m else None)
    # Convention B：EQNAME 本身即化學品名稱，且 TAGNAME 含液位量測 token
    #   （涵蓋 S2 `CHM_RD1_LT0x_PV`、HJ1 `_LS_`、PCB `_MANU_LIT` 等不同命名）
    if _CHEM_NAME.match(_norm_chem(eq)) and _CHEM_LEVEL_TOKEN.search(tag):
        return ("大宗化學品", _norm_chem(eq))

    # ── ② 排氣靜壓（EQNAME 或 DESCRIPTION 含「靜壓」）────────────────────────
    static_name = static_chart_key(plant, tag, eq, description)
    if static_name:
        return ("排氣靜壓", static_name)

    # ── ②' 供應水質（超純水供水電阻/導電度）──────────────────────────────────
    # 排除 RO「產水」(中間製程)導電度，不納入供應水質（依需求取消 HJ 6F-1/6F-2 兩條曲線）
    if "產水" in eq and ("導電度" in eq or "電阻" in eq):
        return (None, None)
    if "電阻" in eq or "導電度" in eq:
        # 標準流程：排除清單內的量測點不上架（如 PCB 高層供水導電度）
        if tag in _WATER_EXCLUDE_TAGS:
            return (None, None)
        return ("供應水質", eq)
    if str(plant).strip().upper() == "TH" and tag.startswith("UMTH.UPW_"):
        return ("供應水質", eq)

    # ── ②'' 廢水處理（中和暫存 pH 等；以 EQNAME「中和」或 TAGNAME WWT/WAS 辨識）──
    if "中和" in eq or "WWT" in tag or "_WAS_" in tag or tag.startswith("S2_WAS"):
        return ("廢水處理", eq)
    if str(plant).strip().upper() == "TH" and ("_WT_" in tag or ".WT_" in tag) and "_PH" in tag:
        return ("廢水處理", eq)

    # ── ③ 廠區用電（MW 功率點；排除冰機/空壓能效的 KW/KWH 比值）──────────────
    power_tokens = ("MGCB", "_MW", ".MW", "PWR", "_PW_")
    if (
        "用電" in eq or "電力" in eq or
        "用電" in description or "電力" in description or "供電" in description or
        "耗電" in description or "總功率" in description or
        any(tok in tag for tok in power_tokens)
    ) and "KW/RT" not in tag and "CMM/KWH" not in tag:
        return ("廠區用電", None)

    # ── ③ 空壓效率 ────────────────────────────────────────────────────────
    if "CMM/KWH" in tag or "CDA" in tag or "空" in eq or "空" in tag or "AIR" in tag:
        return ("空壓效率", None)
    # ── ④ 冰機效率（精確特徵；不用寬鬆 "CH" 比對，避免化學品 CHM 誤判）─────────
    if "KW/RT" in tag or "COP" in tag or "冰" in eq or "CHU" in tag:
        return ("冰機效率", None)

    return (None, None)


def compile_quality_data(script_dir, force_base_time=None):
    import hashlib
    import math
    
    # Trend plants
    PLANTS = ["T2A", "S2A", "PCB", "S2", "S3", "HJ1", "HJ2", "LC2", "LC3", "TH", "KF1"]
    
    # Load actual backup if it exists first
    df_q = pd.DataFrame()
    quality_csv = os.path.join(script_dir, "latest_quality_backup.csv")
    if os.path.exists(quality_csv):
        try:
            df_q = normalize_plant_column(pd.read_csv(quality_csv))
            # Ensure proper datetime parsing and force hourly ceil alignment
            df_q['TIMESTAMP'] = pd.to_datetime(df_q['TIMESTAMP']).dt.ceil('h')
            df_q['VALUE'] = pd.to_numeric(df_q['VALUE'], errors='coerce')
            df_q = df_q.dropna(subset=['VALUE', 'PLANT', 'TIMESTAMP'])
            # Deduplicate by (PLANT, TAGNAME, TIMESTAMP), keeping the last (most recent) record
            df_q = df_q.sort_values('TIMESTAMP').drop_duplicates(subset=['PLANT', 'TAGNAME', 'TIMESTAMP'], keep='last')
        except Exception as e:
            print(f"[WARN] 讀取 latest_quality_backup.csv 失敗: {e}")
            
    # Load actual hourly run rate backup if it exists
    df_rr = pd.DataFrame()
    rr_csv = os.path.join(script_dir, "latest_run_rate_backup.csv")
    if os.path.exists(rr_csv):
        try:
            df_rr = normalize_plant_column(pd.read_csv(rr_csv))
            df_rr['TIMESTAMP'] = pd.to_datetime(df_rr['TIMESTAMP'])
            # 註：歷史一次性 +1h 時間戳遷移（舊 floor(max_time) 格式 → 觸發整點格式）
            # 已於 2026-06-01 完成並寫入。main.py 現已直接以觸發整點寫入新資料，
            # 故遷移程式碼與 latest_run_rate_backup.csv.migrated 旗標檔案均已退役移除。
            df_rr['TIMESTAMP'] = df_rr['TIMESTAMP'].dt.floor('h')
            df_rr['RUN_RATE'] = pd.to_numeric(df_rr['RUN_RATE'], errors='coerce')
            df_rr = df_rr.dropna(subset=['RUN_RATE', 'PLANT', 'TIMESTAMP'])
            # Deduplicate by (PLANT, TIMESTAMP), keeping the last (most recent) record
            df_rr = df_rr.sort_values('TIMESTAMP').drop_duplicates(subset=['PLANT', 'TIMESTAMP'], keep='last')
        except Exception as e:
            print(f"[WARN] 讀取 latest_run_rate_backup.csv 失敗: {e}")

    # Determine base_time for quality trend: use max timestamp between actual quality data and run rate data
    if force_base_time is not None:
        base_time = force_base_time
    else:
        base_time = None
        max_q_time = df_q['TIMESTAMP'].max() if not df_q.empty else None
        max_rr_time = df_rr['TIMESTAMP'].max() if not df_rr.empty else None
        
        times_to_compare = [t for t in [max_q_time, max_rr_time] if t is not None and not pd.isnull(t)]
        if times_to_compare:
            base_time = max(times_to_compare)
            
        if base_time is None or pd.isnull(base_time):
            base_time = datetime.now()
            
    # Standardize base_time to the hour
    base_time = base_time.replace(minute=0, second=0, microsecond=0)
    
    timestamps = []
    
    # 產生 168 個整點時間點，格式為「YYYY-MM-DD HH:00」
    # base_time = 觸發整點（如 20:00），最後一點 = "2026-06-01 20:00"，對應當次觸發的資料
    from datetime import timedelta
    for i in range(167, -1, -1):
        t = base_time - timedelta(hours=i)
        ts_str = t.strftime('%Y-%m-%d %H:00')
        timestamps.append(ts_str)

    # 預留下次觸發的空點
    future_t = base_time + timedelta(hours=1)
    future_ts_str = future_t.strftime('%Y-%m-%d %H:00')
    timestamps.append(future_ts_str)
        
    # Initialize series structures
    series_ch = {p: [] for p in PLANTS}
    series_cda = {p: [] for p in PLANTS}
    series_power = {p: [] for p in PLANTS}
    series_rr = {p: [] for p in PLANTS}
    
    # Chemical-specific data: key = (chem_name, plant, hr_str) -> value
    chem_actual_data = {}
    # Plant power data: key = (label, hr_str) -> value. Some plants have multiple meters.
    power_actual_data = {}
    # 排氣靜壓 data: key = (圖表名稱, 廠區-設備編號, hr_str) -> value
    static_actual_data = {}
    # 供應水質 data: key = (圖表名稱, label, hr_str) -> value（label=廠區+量測點位置）
    water_actual_data = {}
    # 廢水處理 data: key = (圖表名稱, label, hr_str) -> value
    waste_actual_data = {}

    # Build a lookup for actual data points
    actual_data = {}
    if not df_q.empty:
        for _, row in df_q.iterrows():
            plant = str(row['PLANT']).strip().upper()
            if plant not in PLANTS:
                continue
            
            # Map metric name
            tagname = str(row.get('TAGNAME', '')).upper()
            eqname = str(row.get('EQNAME', ''))
            description = str(row.get('DESCRIPTION', ''))

            # TIMESTAMP 已對齊觸發整點，key 格式與 timestamps 陣列一致
            t_curr = row['TIMESTAMP']
            hr_str = t_curr.strftime('%Y-%m-%d %H:00')

            # ── 透過自動分類機制判定該筆資料歸屬的圖表類別 ──────────────────
            category, series_name = classify_quality_row(
                tagname, eqname, plant=plant, description=description)

            if category == "大宗化學品":
                # series_name = 化學品合併鍵；轉成 (圖表名稱, 同廠區濃度後綴)
                # 線別 label = 廠區；同廠區多濃度(NaOH)再附濃度後綴避免互相覆蓋
                if series_name:
                    chart_name, conc = chem_chart_key(series_name)
                    label = f"{plant} {conc}" if conc else plant
                    chem_actual_data[(chart_name, label, hr_str)] = float(row['VALUE'])
                continue

            if category in ("空壓效率", "冰機效率"):
                actual_data[(plant, category, hr_str)] = float(row['VALUE'])
                continue

            if category == "廠區用電":
                label = power_series_label(plant, eqname, tagname, description)
                power_actual_data[(label, hr_str)] = float(row['VALUE'])
                continue

            if category == "排氣靜壓":
                # 新分類只採 2026-06-24 14:00 起的資料；更早資料不帶入新圖表。
                if t_curr < _STATIC_START_TIME:
                    continue
                # series_name = 圖表名稱；同廠區多設備各自保留獨立曲線。
                # 靜壓值先取絕對值再上架（如乾集塵靜壓 -405 → 405），統一以正值呈現
                if series_name:
                    label = static_series_label(plant, eqname, tagname)
                    static_actual_data[(series_name, label, hr_str)] = abs(float(row['VALUE']))
                continue

            if category == "供應水質":
                # 電阻(MΩ·cm)一律換算為導電度(µS/cm)，與原生導電度合併於同一張圖
                cond = water_value_to_conductivity(eqname, float(row['VALUE']))
                if cond is not None:
                    label = _WATER_LABEL_OVERRIDE.get((plant, str(eqname)))
                    if label is None:
                        loc = water_loc_label(eqname)
                        if plant == "TH" and (not loc or loc.lower() == "nan"):
                            m = re.search(r'UPW_.*?_(CT_.+?)_PV', tagname)
                            loc = m.group(1) if m else ""
                        label = f"{plant} {loc}" if loc else plant
                    water_actual_data[("供水導電度", label, hr_str)] = cond
                continue

            if category == "廢水處理":
                # 目前皆為中和暫存 pH → 同一張『中和PH』圖，各廠區為線
                chart = waste_chart_key(eqname)
                waste_actual_data[(chart, plant, hr_str)] = float(row['VALUE'])
                continue

            # 其餘未接圖類別先略過（待資料齊備時於上方補規則即可上架）。
            continue
            
    # Build a lookup for run rate data points
    run_rate_data = {}
    if not df_rr.empty:
        for _, row in df_rr.iterrows():
            plant = str(row['PLANT']).strip().upper()
            if plant not in PLANTS:
                continue
            t_curr = row['TIMESTAMP']  # 觸發整點（如 20:00）
            hr_str = t_curr.strftime('%Y-%m-%d %H:00')
            key = (plant, hr_str)
            run_rate_data[key] = float(row['RUN_RATE'])
            
    # Baselines for deterministic mock values
    baselines_ch = {
        "T2A": {"base": 0.62, "wave": 0.03, "period": 24},
        "S2A": {"base": 0.65, "wave": 0.04, "period": 12},
        "PCB": {"base": 0.72, "wave": 0.05, "period": 24},
        "S2":  {"base": 0.58, "wave": 0.02, "period": 24},
        "S3":  {"base": 0.68, "wave": 0.03, "period": 12},
        "HJ1": {"base": 0.64, "wave": 0.04, "period": 24},
        "HJ2": {"base": 0.61, "wave": 0.03, "period": 24},
        "LC2": {"base": 0.69, "wave": 0.05, "period": 8},
        "LC3": {"base": 0.74, "wave": 0.04, "period": 12},
        "TH":  {"base": 0.66, "wave": 0.03, "period": 24}
    }
    
    baselines_cda = {
        "T2A": {"base": 9.2, "wave": 0.4, "period": 24},
        "S2A": {"base": 8.8, "wave": 0.5, "period": 12},
        "PCB": {"base": 7.8, "wave": 0.6, "period": 24},
        "S2":  {"base": 9.5, "wave": 0.3, "period": 24},
        "S3":  {"base": 11.2, "wave": 0.5, "period": 12},
        "HJ1": {"base": 8.5, "wave": 0.5, "period": 24},
        "HJ2": {"base": 8.9, "wave": 0.4, "period": 24},
        "LC2": {"base": 6.8, "wave": 0.6, "period": 8},
        "LC3": {"base": 7.2, "wave": 0.5, "period": 12},
        "TH":  {"base": 8.4, "wave": 0.4, "period": 24}
    }
    
    # Backfill loop
    # ── 能源效率時間位移 ──────────────────────────────────────────────
    # 能源效率(冰機/空壓)資料來源固定延遲 1 小時：當前整點(如 08:00)有資料，
    # 但前一整點(07:00)恆為空。為使每小時整點皆連續、不跳過，將「當前整點以外」
    # 的能源效率資料一律往較新方向位移 1 小時顯示
    #   （顯示 H 時，取資料來源 H-1 的值；當前整點維持自身值；預留未來點維持空白）。
    # 若來源 SQL 本身該整點即無資料，位移後仍為空（保留真實缺口）。
    # 運轉率不套用此位移。
    current_idx = len(timestamps) - 2  # base_time(當前觸發整點)在 timestamps 的索引
    for p in PLANTS:
        for idx, hr_str in enumerate(timestamps):
            # 能源效率查詢用整點：當前整點取自身，其餘取前一整點
            if idx == current_idx:
                energy_hr = hr_str
            elif 1 <= idx < current_idx:
                energy_hr = timestamps[idx - 1]
            else:
                energy_hr = None  # 最舊點(無更早資料)或未來預留點

            # 1. Check for 冰機效率（套用能源位移）
            key_ch = (p, "冰機效率", energy_hr) if energy_hr is not None else None
            if key_ch is not None and key_ch in actual_data:
                series_ch[p].append(round(actual_data[key_ch], 4))
            else:
                series_ch[p].append(None)

            # 2. Check for 空壓效率（套用能源位移）
            key_cda = (p, "空壓效率", energy_hr) if energy_hr is not None else None
            if key_cda is not None and key_cda in actual_data:
                series_cda[p].append(round(actual_data[key_cda], 3))
            else:
                series_cda[p].append(None)

            # 3. Plant power is assembled below because a plant may expose multiple meters.

            # 4. Check for 運轉率（不位移，維持原整點）
            key_rr = (p, hr_str)
            if key_rr in run_rate_data:
                series_rr[p].append(round(run_rate_data[key_rr], 2))
            else:
                series_rr[p].append(None)
                
    # ── 建立化學品個別圖表的 series（線別 = label，可能為廠區或廠區+濃度）──────
    chem_names = sorted(set(k[0] for k in chem_actual_data.keys()))
    chem_series = {}
    # 線別排序：先依廠區在 PLANTS 的順序，再依 label 字串，確保圖例順序穩定
    def _label_sort_key(lab):
        plant_part = re.split(r'[ -]', lab, maxsplit=1)[0]
        pi = PLANTS.index(plant_part) if plant_part in PLANTS else len(PLANTS)
        return (pi, lab)

    power_labels = sorted(
        {label for (label, _hr) in power_actual_data.keys()},
        key=_label_sort_key)
    series_power = {label: [] for label in power_labels}
    for idx, hr_str in enumerate(timestamps):
        if idx == current_idx:
            energy_hr = hr_str
        elif 1 <= idx < current_idx:
            energy_hr = timestamps[idx - 1]
        else:
            energy_hr = None
        for label in power_labels:
            key = (label, energy_hr)
            series_power[label].append(
                round(power_actual_data[key], 3) if energy_hr is not None and key in power_actual_data else None)

    for cn in chem_names:
        labels = sorted({k[1] for k in chem_actual_data if k[0] == cn}, key=_label_sort_key)
        series = {lab: [] for lab in labels}
        for hr_str in timestamps:
            for lab in labels:
                key = (cn, lab, hr_str)
                series[lab].append(round(chem_actual_data[key], 2) if key in chem_actual_data else None)
        chem_series[f"chem_{cn}"] = series

    # ── 建立排氣靜壓個別圖表的 series（每類型一張圖，各設備為獨立線）──────────
    static_names = sorted(
        set(k[0] for k in static_actual_data.keys()),
        key=lambda name: (name == "有機排氣靜壓", name))
    static_series = {}
    for sn in static_names:
        labels = sorted(
            {k[1] for k in static_actual_data if k[0] == sn},
            key=_label_sort_key)
        series = {label: [] for label in labels}
        for hr_str in timestamps:
            for label in labels:
                key = (sn, label, hr_str)
                if key in static_actual_data:
                    series[label].append(round(static_actual_data[key], 2))
                else:
                    series[label].append(None)
        static_series[f"static_{sn}"] = series

    # ── 建立供應水質/廢水處理 series（label = 廠區 或 廠區+量測點位置）──────────
    def _build_label_series(actual):
        names = sorted(set(k[0] for k in actual))
        out = {}
        for nm in names:
            labels = sorted({k[1] for k in actual if k[0] == nm}, key=_label_sort_key)
            s = {lab: [] for lab in labels}
            for hr_str in timestamps:
                for lab in labels:
                    key = (nm, lab, hr_str)
                    s[lab].append(round(actual[key], 4) if key in actual else None)
            out[nm] = s
        return names, out

    # 供應水質：依各量測點「最新導電度值」拆為 >1 / <1 兩張圖（改善寬幅尺度可讀性）
    _water_rep = {}  # label -> (latest_hr, latest_value)
    for (cn, lab, hr), v in water_actual_data.items():
        cur = _water_rep.get(lab)
        if cur is None or hr > cur[0]:
            _water_rep[lab] = (hr, v)
    water_split = {}
    for (cn, lab, hr), v in water_actual_data.items():
        chart = "供水導電度高" if _water_rep[lab][1] >= 1 else "供水導電度低"
        water_split[(chart, lab, hr)] = v
    water_names, water_cs = _build_label_series(water_split)
    water_series = {f"water_{nm}": s for nm, s in water_cs.items()}
    waste_names, waste_cs = _build_label_series(waste_actual_data)
    waste_series = {f"waste_{nm}": s for nm, s in waste_cs.items()}

    metrics = {
        "冰機效率": series_ch,
        "空壓效率": series_cda,
        "廠區用電": series_power,
        "運轉率": series_rr
    }
    metrics.update(chem_series)
    metrics.update(static_series)
    metrics.update(water_series)
    metrics.update(waste_series)

    return {
        "timestamps": timestamps,
        "metrics": metrics,
        "chem_names": chem_names,
        "static_names": static_names,
        "water_names": water_names,
        "waste_names": waste_names
    }


def create_status_dashboard(df: pd.DataFrame, output_path: str = "index.html"):
    df = normalize_plant_column(df)
    print("開始處理 Dashboard 資料...")

    df['TIMESTAMP'] = pd.to_datetime(df['TIMESTAMP'])
    df['VALUE']     = pd.to_numeric(df['VALUE'], errors='coerce')
    df = df.dropna(subset=['VALUE', 'EQNO', 'PLANT'])
    df = df[df['EQNO'].str.strip()  != '']
    df = df[df['PLANT'].str.strip() != '']

    if df.empty:
        print("[WARN] 無有效設備資料。")
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write("<h1>No Data Available</h1>")
        return

    max_time     = df['TIMESTAMP'].max()
    df_last_hour = df[df['TIMESTAMP'] >= max_time - pd.Timedelta(hours=1)]
    if df_last_hour.empty:
        print("[WARN] 最近一小時無資料，使用全部資料。")
        df_last_hour = df

    df_unique = (df_last_hour
                 .sort_values('TIMESTAMP', ascending=False)
                 .drop_duplicates(subset=['PLANT', 'EQNO'], keep='first'))

    all_plants = sorted(df_unique['PLANT'].unique())
    print(f"偵測廠區: {all_plants}")

    REQUIRED_PLANTS = ["T2A", "S2A", "PCB", "S2", "S3", "HJ1", "HJ2", "LC2", "LC3", "TH"]
    display_plants  = list(REQUIRED_PLANTS)
    for p in all_plants:
        if p not in display_plants:
            display_plants.append(p)

    # KF1 currently has alarm data only. Once operational data appears, include
    # it in the shared equipment pipeline automatically and keep its alarm
    # analysis directly below the equipment section.
    kf1_has_equipment_data = "KF1" in all_plants
    equipment_plants = [p for p in display_plants if p != "KF1"]
    if kf1_has_equipment_data:
        equipment_plants.append("KF1")

    fallback_plant = all_plants[0] if all_plants else None
    if fallback_plant:
        print(f"以 {fallback_plant} 資料做無資料廠區替代展示")

    for p in all_plants:
        old = os.path.join(
            os.path.dirname(output_path) if os.path.dirname(output_path) else "",
            f"EFplant_Dashboard_{p}.html"
        )
        if os.path.exists(old):
            try: os.remove(old)
            except: pass

    now_dt              = datetime.now()
    from datetime import timedelta
    next_hour           = (now_dt + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    next_update_str     = next_hour.strftime("%m/%d %H:%M")
    generation_time_iso = now_dt.strftime('%Y-%m-%dT%H:%M:%S')

    script_dir = os.path.dirname(os.path.abspath(__file__)) or "."
    new_eqnos  = update_known_equipment(df_unique, script_dir)

    # ── 載入 EQ_Mapping.csv 並建立動態分類器 ────────────────────────────────────
    mapping_csv = os.path.join(script_dir, "EQ_Mapping.csv")
    if not os.path.exists(mapping_csv):
        try:
            with open(mapping_csv, "w", encoding="utf-8") as f:
                f.write("Piority,Subject_Cinese_Tradiotion,Relaese_English,EQ_Name\n"
                        "1,冰機,Chiller,CHU\n1,冰機,Chiller,CH\n2,空壓,Compressed Air,CDA\n"
                        "3,製程冷卻水泵,Process Coooling Water Pump,PCWP\n"
                        "4,外氣空調箱風車,Make Up Air Motor,MAU\n"
                        "4,製程專用空調箱風車,Process Air Motor,PAH\n"
                        "5,工業水泵,Industrial Water Pump,IWP\n6,軟水泵,Softened Water Pump,SWP\n"
                        "7,純水泵,DI Pump,CEDIP\n8,RO水泵,RO Pump,ROWP\n"
                        "9,酸排氣風車,Acid Scrubber Motor,ASCR\n"
                        "10,鹼排氣風車,Base Scrubber Motor,BSCR\n"
                        "11,有機排氣風車,VOC Scrubber Motor,VSCR\n"
                        "12,熱排氣風車,Hot Scrubber Motor,HSCR\n"
                        "13,乾式集塵機,Dry Dust Motor,DUST\n"
                        "14,溼式集塵機,Wet Dust Motor,WDUST\n"
                        "15,高壓集塵機,High Dust Motor,HDUST\n"
                        "16,中壓集塵機,Median Dust Motor,MDUST\n"
                        "17,低壓集塵機,Low Dust Motor,LDUST\n"
                        "18,廢水出口泵,WWT Pump,WWTP\n"
                        "19,製程供藥泵,Chemical Delivery Pump,CDSP\n")
        except Exception as e:
            print(f"[WARN] 無法防禦性寫入 EQ_Mapping.csv: {e}")

    try:
        df_map = pd.read_csv(mapping_csv)
    except Exception as e:
        print(f"[ERROR] 讀取 EQ_Mapping.csv 失敗: {e}")
        df_map = pd.DataFrame()

    SUB_KEYWORDS = {
        "冰機": ["冰機", "冰水", "CHU", "CH"],
        "空壓": ["空壓", "CDA"],
        "製程冷卻水泵": ["製程冷卻水", "製程水", "PCW", "PCWP"],
        "外氣空調箱風車": ["外氣", "空調箱", "MAU"],
        "製程專用空調箱風車": ["製程專用空調箱", "製程空調", "PAH"],
        "工業水泵": ["工業水", "IWP"],
        "軟水泵": ["軟水", "SWP"],
        "純水泵": ["純水", "PWP", "CEDIP", "CDEIP"],
        "RO水泵": ["RO", "ROWP"],
        "酸排氣風車": ["酸排", "ASCR", "ASR", "ACE"],
        "鹼排氣風車": ["鹼排", "碱排", "BSCR", "BSR", "ALK"],
        "有機排氣風車": ["有機排", "VSCR", "VSR", "VOC"],
        "熱排氣風車": ["熱排", "HSCR", "HSR", "HOTEX"],
        "乾式集塵機": ["乾式集塵", "DUST", "DCA", "DC"],
        "溼式集塵機": ["溼式集塵", "濕式集塵", "WDUST", "WDCA", "WDC"],
        "高壓集塵機": ["高壓集塵", "HDUST"],
        "中壓集塵機": ["中壓集塵", "MDUST"],
        "低壓集塵機": ["低壓集塵", "LDUST"],
        "廢水出口泵": ["廢水", "WWTP"],
        "製程供藥泵": ["製程供藥", "藥", "CDSP"]
    }

    SUBJECT_ICONS = {
        "冰機": "❄️",
        "空壓": "💨",
        "製程冷卻水泵": "🌊",
        "外氣空調箱風車": "🌀",
        "製程專用空調箱風車": "🌀",
        "工業水泵": "💧",
        "軟水泵": "💧",
        "純水泵": "💧",
        "RO水泵": "💧",
        "酸排氣風車": "🧪",
        "鹼排氣風車": "⚗️",
        "有機排氣風車": "🍃",
        "熱排氣風車": "🔥",
        "乾式集塵機": "🧹",
        "溼式集塵機": "💧",
        "高壓集塵機": "⚡",
        "中壓集塵機": "🧹",
        "低壓集塵機": "🧹",
        "廢水出口泵": "🌊",
        "製程供藥泵": "🧪",
        "Other": "⚙️"
    }

    mapping_entries = []
    dynamic_categories = []
    seen_subjects = set()

    if not df_map.empty:
        prio_col = df_map.columns[0]
        subj_col = df_map.columns[1]
        eng_col = df_map.columns[2]
        eqname_col = df_map.columns[3]

        for _, row_m in df_map.iterrows():
            prio = int(row_m[prio_col])
            subj = str(row_m[subj_col]).strip()
            eng = str(row_m[eng_col]).strip()
            eqname = str(row_m[eqname_col]).strip()

            mapping_entries.append({
                "priority": prio,
                "subject": subj,
                "english": eng,
                "patterns": SUB_KEYWORDS.get(subj, [eqname]),
                "orig_eqname": eqname
            })

            if subj not in seen_subjects:
                seen_subjects.add(subj)
                dynamic_categories.append({
                    "name": subj,
                    "icon": SUBJECT_ICONS.get(subj, "⚙️"),
                    "desc": eng,
                    "priority": prio
                })
                
    # Sort categories by Priority
    dynamic_categories.sort(key=lambda x: x["priority"])
    # Append fallback "Other" at the end (Priority 20)
    dynamic_categories.append({
        "name": "Other",
        "icon": "⚙️",
        "desc": "Other",
        "priority": 20
    })

    # Sort matching entries by Priority
    mapping_entries.sort(key=lambda x: x["priority"])

    def classify_record(row_data):
        import difflib
        import re as _re_local

        def _entry_by_code(code):
            code = code.upper()
            return next((e for e in mapping_entries if str(e.get("orig_eqname", "")).upper() == code), None)

        def _th_exact_entry(eqno_value):
            eq_clean = _re_local.sub(r'[^A-Z0-9]', '', str(eqno_value).upper())
            th_prefix_map = (
                (r'^A[12]DUST', 'DUST'),
                (r'^A[12]ASCR', 'ASCR'),
                (r'^A[12]BSCR', 'BSCR'),
                (r'^A[12]HSCR', 'HSCR'),
                (r'^A[12]VSCR', 'VSCR'),
                (r'^[HM]CDA', 'CDA'),
            )
            for pattern, code in th_prefix_map:
                if _re_local.match(pattern, eq_clean):
                    return _entry_by_code(code)
            return None
        
        tagname = str(row_data.get('TAGNAME', '')).upper()
        description = str(row_data.get('DESCRIPTION', '')).upper()
        eqno = str(row_data.get('EQNO', '')).upper()
        plant = str(row_data.get('PLANT', '')).upper()

        if plant == "TH":
            exact_entry = _th_exact_entry(eqno)
            if exact_entry is not None:
                return exact_entry
        
        # Normalize Traditional Chinese characters
        description = description.replace('濕', '溼').replace('碱', '鹼')
        
        # 1. 優先判定：原始資料 EQNO 排除數字與特殊字元後的英文字母，與 EQ_Mapping.csv 的 EQ_NAME 相似度 > 80%
        # 排除數字與特殊字元，僅保留 A-Z
        eqno_letters = _re_local.sub(r'[^A-Z]', '', eqno)
        
        best_eq_sim = 0.0
        best_eq_entry = None

        if eqno_letters:
            # 1a. 最長前綴比對：EQNO 字母前綴命中對照代碼即強判定。
            #     比相似度更可靠，可正確處理帶樓層/區域後綴的代碼
            #     （如 PCWP2FLD1→字母 PCWPFLD 前綴 PCWP→製程冷卻水泵，
            #      避免因描述含「冰水」被誤分到冰機；經全廠資料驗證 CH/PCWP 等前綴皆唯一對應）。
            best_prefix_entry = None
            best_prefix_len = 0
            for entry in mapping_entries:
                code_letters = _re_local.sub(r'[^A-Z]', '', entry["orig_eqname"].upper())
                if code_letters and eqno_letters.startswith(code_letters):
                    if len(code_letters) > best_prefix_len:
                        best_prefix_len = len(code_letters)
                        best_prefix_entry = entry
            if best_prefix_entry is not None:
                return best_prefix_entry

            # 1b. 相似度 > 80% 比對（前綴未命中時的後備）
            for entry in mapping_entries:
                orig_name_u = entry["orig_eqname"].upper()
                # 排除對照表中的數字（以字母比較）
                orig_name_letters = _re_local.sub(r'[^A-Z]', '', orig_name_u)
                if orig_name_letters:
                    sim = difflib.SequenceMatcher(None, eqno_letters, orig_name_letters).ratio()
                    if sim > best_eq_sim:
                        best_eq_sim = sim
                        best_eq_entry = entry

            if best_eq_sim >= 0.80:
                # 優先判定為該對照項
                return best_eq_entry

        # 2. 第二歸類判定：交叉參照後與 Subject 中文主題欄位吻合度 > 80%
        # 為了達到最高比對精確度，結合了 Subject 視窗滑動相似度與 pattern 交叉參照
        
        # 集塵機特殊關鍵字優先權防護
        if "高壓" in description and "集塵" in description:
            return next((e for e in mapping_entries if e["subject"] == "高壓集塵機"), None)
        if "中壓" in description and "集塵" in description:
            return next((e for e in mapping_entries if e["subject"] == "中壓集塵機"), None)
        if "低壓" in description and "集塵" in description:
            return next((e for e in mapping_entries if e["subject"] == "低壓集塵機"), None)

        best_sub_sim = 0.0
        best_sub_entry = None
        
        for entry in mapping_entries:
            subj_u = entry["subject"].upper()
            sub_len = len(subj_u)
            max_sim = 0.0
            
            # 如果 Subject 直接包含在 row 資料中，符合度 100% (1.0)
            if (subj_u in description) or (subj_u in tagname) or (subj_u in eqno):
                max_sim = 1.0
            else:
                # 視窗滑動法比對 DESCRIPTION 最大相似度子字串
                if len(description) >= sub_len:
                    for start in range(len(description) - sub_len + 1):
                        sub_str = description[start:start+sub_len]
                        sim = difflib.SequenceMatcher(None, sub_str, subj_u).ratio()
                        if sim > max_sim:
                            max_sim = sim
                
                # 交叉參照：如果 row 資料中包含該 Subject 的任一 patterns (別名)，符合度為強匹配 (1.0)
                for pat in entry["patterns"]:
                    pat_u = pat.upper()
                    if (pat_u in eqno) or (pat_u in tagname) or (pat_u in description):
                        # PCW vs MAU guard
                        if entry["subject"] == "外氣空調箱風車" and ("PCW" in eqno or "PCW" in tagname or "PCW" in description):
                            continue
                        max_sim = max(max_sim, 1.0)
                        break
                        
            if max_sim > best_sub_sim:
                best_sub_sim = max_sim
                best_sub_entry = entry
                
        if best_sub_sim > 0.80:
            return best_sub_entry
            
        return None

    def is_running(row_data):
        try:
            val_raw = row_data.get('VALUE', 0.0)
            val = abs(float(val_raw))
        except Exception:
            val = 0.0
            
        desc = str(row_data.get('DESCRIPTION', ''))
        plant = normalize_plant_label(row_data.get('PLANT', ''))
        eqno = str(row_data.get('EQNO', '')).strip().upper()
        tagname = str(row_data.get('TAGNAME', '')).strip().upper()

        # KF1 chiller source is a low-range load signal rather than the
        # standard >=10 analog running signal. Keep a small deadband so sensor
        # zero drift is not treated as running.
        if plant == "KF1" and eqno.startswith("CHU") and "_LOAD" in tagname:
            return val > 0.1

        # Explicit motor-current signals use amperes. Evaluate this before
        # generic integer/digital handling so exactly 1 A remains STOP.
        is_current_signal = (
            "電流" in desc
            or "_VFD_A." in tagname
            or "_VFD_A_" in tagname
            or "_PM_I_AVG." in tagname
            or "_PM_I_AVG_" in tagname
        )
        if is_current_signal:
            return val > 1.0
        
        # 1. 判斷是否為長整數（小數點後為 0 且為合理整數）
        is_int = False
        try:
            is_int = float(val_raw).is_integer()
        except Exception:
            pass
            
        if is_int:
            val_int = int(round(val))
            if val_int == 0:
                return False # STOP
            elif val_int == 1:
                return True # RUN
            # 若長整數非 0 也非 1，則按照第 1 點(浮點數)的邏輯判定
            
        # 2. 浮點數（或非 0 非 1 的長整數）判定邏輯
        if "壓力" in desc or "壓差" in desc:
            return val >= 0.2 # VALUE < 0.2 為 STOP，即 >= 0.2 為 RUN
            
        # 3. 其他浮點數或非 0 非 1 數據之保底邏輯
        return val >= 10.0

    # 右上角 TIMESTAMP 時間顯示規則:「觸發整點~下次整點」
    # 槽位格式改為 curr~next，觸發時間 H:00 的資料標記為 H:00~(H+1):00
    latest_hour = now_dt.replace(minute=0, second=0, microsecond=0)  # 觸發整點

    # ── 趨勢歷史資料與時間區間編譯 ────────────────────────────────
    chart_data = compile_quality_data(script_dir, force_base_time=latest_hour)
    chart_timestamps_json = json.dumps(chart_data["timestamps"])
    chart_series_json = json.dumps(chart_data["metrics"], ensure_ascii=False)
    chem_names_json = json.dumps(chart_data.get("chem_names", []), ensure_ascii=False)
    static_names_json = json.dumps(chart_data.get("static_names", []), ensure_ascii=False)
    water_names_json = json.dumps(chart_data.get("water_names", []), ensure_ascii=False)
    waste_names_json = json.dumps(chart_data.get("waste_names", []), ensure_ascii=False)

    timestamp_range_str = f"{latest_hour.month}/{latest_hour.day} {latest_hour.strftime('%H:00')}"

    nav_btns = ""
    for idx, plant in enumerate(equipment_plants):
        ac = "active" if idx == 0 else ""
        nav_btns += (
            f'<button class="nav-btn {ac}" data-plant="{plant}" '
            f'onclick="switchPlant(\'{plant}\')">{plant}</button>\n        '
        )

    kf1_nav = "" if kf1_has_equipment_data else (
        '<button class="nav-btn" data-plant="KF1" onclick="switchPlant(\'KF1\')">KF1</button>\n        '
    )
    nav_bar_html = (
        f'<nav class="plant-nav">\n        '
        f'{nav_btns}'
        f'{kf1_nav}'
        f'<button class="nav-btn" data-plant="TREND" onclick="switchPlant(\'TREND\')">📈 趨勢圖</button>\n    </nav>'
    )

    kpi_zone_html = '<div class="kpi-zone">\n'
    plant_data_cache = {}

    for idx, plant in enumerate(equipment_plants):
        if plant in all_plants:
            df_plant = df_unique[df_unique['PLANT'] == plant].sort_values('EQNO')
            is_mock  = False
        elif fallback_plant:
            df_plant = df_unique[df_unique['PLANT'] == fallback_plant].sort_values('EQNO')
            is_mock  = True
        else:
            df_plant = pd.DataFrame()
            is_mock  = False

        total_eq   = len(df_plant)
        running_eq = int(sum(df_plant.apply(is_running, axis=1))) if not df_plant.empty else 0
        stopped_eq = total_eq - running_eq
        run_rate   = (running_eq / total_eq * 100) if total_eq > 0 else 0.0

        plant_data_cache[plant] = {'df': df_plant, 'is_mock': is_mock}

        ks_style = "" if idx == 0 else 'style="display:none"'
        if is_mock:
            kpi_zone_html += f"""  <div class="kpi-set" id="kpi-{plant}" {ks_style}>
    <div class="kpi-card kpi-total"><div class="kpi-lbl">TOTAL EQ</div><div class="kpi-val kpi-wip">--</div></div>
    <div class="kpi-card kpi-run">  <div class="kpi-lbl">RUNNING</div> <div class="kpi-val kpi-wip">--</div></div>
    <div class="kpi-card kpi-stop"> <div class="kpi-lbl">STOPPED</div> <div class="kpi-val kpi-wip">--</div></div>
    <div class="kpi-card kpi-rate"> <div class="kpi-lbl">RUN RATE</div><div class="kpi-val kpi-wip">--</div></div>
  </div>\n"""
        else:
            kpi_zone_html += f"""  <div class="kpi-set" id="kpi-{plant}" {ks_style}>
    <div class="kpi-card kpi-total"><div class="kpi-lbl">TOTAL EQ</div><div class="kpi-val">{total_eq}</div></div>
    <div class="kpi-card kpi-run">  <div class="kpi-lbl">RUNNING</div> <div class="kpi-val">{running_eq}</div></div>
    <div class="kpi-card kpi-stop"> <div class="kpi-lbl">STOPPED</div> <div class="kpi-val">{stopped_eq}</div></div>
    <div class="kpi-card kpi-rate"> <div class="kpi-lbl">RUN RATE</div><div class="kpi-val">{run_rate:.1f}%</div></div>
  </div>\n"""
    kpi_zone_html += '</div>'

    # ── 設備運轉率 (RUN RATE) 歷史記錄寫入 ───────────────────────────
    try:
        rr_backup_path = os.path.join(script_dir, "latest_run_rate_backup.csv")
        new_rr_records = []
        # 對齊排程觸發整點（now_dt 的整點），而非 MSSQL 資料的最大時間戳
        rr_time_str = now_dt.replace(minute=0, second=0, microsecond=0).strftime('%Y-%m-%d %H:00:00')
        
        # 收集當前所有真實廠區的運轉率
        for plant in equipment_plants:
            cached = plant_data_cache.get(plant)
            if cached and not cached['is_mock']:
                df_p = cached['df']
                tot = len(df_p)
                run = int(sum(df_p.apply(is_running, axis=1))) if not df_p.empty else 0
                rate = (run / tot * 100.0) if tot > 0 else 0.0
                new_rr_records.append({"TIMESTAMP": rr_time_str, "PLANT": plant, "RUN_RATE": round(rate, 2)})
                
        if new_rr_records:
            df_new_rr = pd.DataFrame(new_rr_records)
            if os.path.exists(rr_backup_path):
                try:
                    df_old_rr = pd.read_csv(rr_backup_path)
                    # 合併並去除重複 (以 TIMESTAMP + PLANT 為唯一鍵)
                    df_combined_rr = pd.concat([df_old_rr, df_new_rr], ignore_index=True)
                    df_combined_rr['TIMESTAMP'] = pd.to_datetime(df_combined_rr['TIMESTAMP']).dt.floor('h')
                    # 去重保留最新
                    df_combined_rr = df_combined_rr.sort_values('TIMESTAMP', ascending=True).drop_duplicates(subset=['TIMESTAMP', 'PLANT'], keep='last')
                except Exception as ex_read:
                    print(f"[WARN] 讀取/合併運轉率備份失敗，直接使用新記錄: {ex_read}")
                    df_combined_rr = df_new_rr
            else:
                df_combined_rr = df_new_rr
                
            # 資料存儲生命週期管理：裁剪保留最新 7 天 (168 小時) 內的數據
            df_combined_rr['TIMESTAMP'] = pd.to_datetime(df_combined_rr['TIMESTAMP'])
            max_rr_time = df_combined_rr['TIMESTAMP'].max()
            if pd.notna(max_rr_time):
                # 1. 刪除大於 7 天的數據
                df_combined_rr = df_combined_rr[df_combined_rr['TIMESTAMP'] >= max_rr_time - pd.Timedelta(days=7)]
                # 2. 如果不同的時間點多於 168 個，裁剪
                unique_ts_rr = sorted(df_combined_rr['TIMESTAMP'].unique(), reverse=True)
                if len(unique_ts_rr) > 168:
                    keep_ts_rr = unique_ts_rr[:168]
                    df_combined_rr = df_combined_rr[df_combined_rr['TIMESTAMP'].isin(keep_ts_rr)]
                    
            df_combined_rr.to_csv(rr_backup_path, index=False, encoding='utf-8-sig')
            print(f"[CLEANUP] 設備運轉率歷史快取已更新 (共 {len(df_combined_rr)} 筆)。")
    except Exception as e_rr:
        print(f"[WARN] 寫入設備運轉率歷史記錄失敗: {e_rr}")
    # ───────────────────────────────────────────────────────────────

    plant_html = ""
    for idx, plant in enumerate(equipment_plants):
        cached   = plant_data_cache[plant]
        df_plant = cached['df']
        is_mock  = cached['is_mock']

        d_style = "" if idx == 0 else 'style="display:none"'
        ac      = "active" if idx == 0 else ""

        if is_mock:
            plant_html += f"""
<div id="pc-{plant}" class="plant-container {ac}" {d_style}>
  <div class="wip-body">
    <div class="wip-icon">&#x1F6A7;</div>
    <div class="wip-title">施工中 / Under Construction</div>
    <div class="wip-msg">此廠區數據尚未接入系統<br>待 MSSQL 資料接通後將自動上線</div>
  </div>
  {build_alarm_pending_section(plant)}
</div>"""
            continue

        import re as _re
        from collections import defaultdict as _dd

        # 1. 建立分群容器
        grouped_items = {cat['name']: [] for cat in dynamic_categories}
        mapped_names = {}

        for _, row in df_plant.iterrows():
            entry = classify_record(row)
            if entry:
                cat_name = entry["subject"]
                # 提取自帶流水號
                # 先以對照代碼(orig_eqname)本身定位，再退而求其次用別名 patterns，
                # 避免較短別名(如 PCW)先命中導致與代碼(PCWP)字母重疊產生 PCWPP1F1。
                eqno_str = str(row['EQNO']).strip()
                serial = ""
                serial_pats = [entry["orig_eqname"]] + list(entry["patterns"]) if entry.get("orig_eqname") else list(entry["patterns"])
                for pat in serial_pats:
                    if not pat:
                        continue
                    match = _re.search(_re.escape(pat), eqno_str, _re.IGNORECASE)
                    if match:
                        serial = eqno_str[match.end():]
                        break
                if not serial:
                    digit_match = _re.search(r'\d+.*$', eqno_str)
                    serial = digit_match.group(0) if digit_match else ""
                
                mapped_eqname = entry["orig_eqname"] + serial
                if plant == "TH" and _re.match(r'^(?:A[12](?:ASCR|BSCR|HSCR|VSCR|DUST)|[HM]CDA)', eqno_str, _re.IGNORECASE):
                    mapped_eqname = eqno_str
                mapped_names[row['EQNO']] = mapped_eqname
                
                if cat_name in grouped_items:
                    grouped_items[cat_name].append(row)
                else:
                    grouped_items["Other"].append(row)
            else:
                mapped_names[row['EQNO']] = str(row['EQNO']).strip()
                grouped_items["Other"].append(row)

        plant_html += f"""
<div id="pc-{plant}" class="plant-container {ac}" {d_style}>
  <div class="eq-area">"""

        _disp = []
        for cat in dynamic_categories:
            cname = cat['name']
            items = grouped_items.get(cname, [])
            if not items:
                continue # 若某廠區無該主題集群的設備，則由後面往上遞補顯示順序
            
            if cname != 'Other':
                _disp.append((cat, items))
            else:
                _pm = _dd(list)
                for _r in items:
                    _m = _re.match(r'^([A-Za-z]+(?:_[A-Za-z]+)*)', str(_r['EQNO']).strip())
                    _pfx = (_m.group(1).upper() if _m else 'UNK')
                    _pm[_pfx].append(_r)
                
                for _pfx in sorted(_pm):
                    _disp.append(({
                        'name': _pfx,
                        'icon': '⚙️',
                        'desc': _pfx,
                        'priority': 20
                    }, _pm[_pfx]))

        for cat, items in _disp:
            cname = cat['name']
            ct    = len(items)
            cr    = sum(is_running(r) for r in items)

            plant_html += f"""
    <section class="grp">
      <div class="grp-hdr">
        <span class="gi">{cat['icon']}</span>
        <span class="gn">{cname}</span>
        <span class="gd">{cat['desc']}</span>
        <span class="gc">{cr}/{ct}</span>
      </div>
      <div class="cards-grid">"""

            for row in items:
                eq  = str(row['EQNO'])
                val = row['VALUE']
                # 取得動態轉換後的設備名稱並保留流水號，同時相容 is_mock 的廠區替換
                eqd_mapped = mapped_names.get(eq, eq)
                eqd = eqd_mapped.replace(fallback_plant, plant) if (is_mock and fallback_plant) else eqd_mapped
                
                run_state = is_running(row)
                sc     = "running" if run_state else "stopped"
                sw     = "RUN"    if run_state else "STOP"
                is_new = (not is_mock) and ((str(plant), str(row['EQNO'])) in new_eqnos)
                nb     = ' <span class="new-badge">NEW</span>' if is_new else ''
                plant_html += f"""
        <div class="card {sc}">
          <div class="eq-id">{eqd}{nb}</div>
          <div class="srow"><div class="led"></div><span class="stxt">{sw}</span></div>
        </div>"""

            plant_html += "\n      </div>\n    </section>"

        alarm_section = (
            f'<div class="kf1-alarm-block">{build_kf1_alarm_dashboard(script_dir)}</div>'
            if plant == "KF1"
            else build_alarm_pending_section(plant)
        )
        plant_html += f"\n  </div>\n{alarm_section}\n</div>"

    # KF1 has alarm analysis now; add equipment status above it automatically
    # when KF1 operational data becomes available in a future data refresh.
    if not kf1_has_equipment_data:
        plant_html += f"""
<div id="pc-KF1" class="plant-container" style="display:none">
{build_kf1_alarm_dashboard(script_dir)}
</div>"""

    # ── 嵌入趨勢圖 (pc-TREND) 虛擬廠區容器到 plant_html 中 ─────────────────────
    plant_html += """
<div id="pc-TREND" class="plant-container" style="display:none">
  <!-- 能效數據切換 Tabs -->
  <div class="tabs-container">
    <button class="tab-btn" id="tab-rr" onclick="switchMetric('運轉率', this)">運轉率</button>
    <button class="tab-btn active" id="tab-energy" onclick="switchMetric('能源效率', this)">能源效率</button>
    <button class="tab-btn" onclick="switchMetric('排氣靜壓', this)">排氣靜壓</button>
    <button class="tab-btn" onclick="switchMetric('大宗化學品', this)">大宗化學品</button>
    <button class="tab-btn" onclick="switchMetric('供應水質', this)">供應水質</button>
    <button class="tab-btn" onclick="switchMetric('廢水處理', this)">廢水處理</button>
  </div>

  <!-- 折線圖面板 -->
  <div class="chart-card" id="trend-chart-card">
    <div class="chart-header">
      <div class="chart-title-group">
        <div class="chart-title">冰機運轉效率趨勢圖</div>
        <div class="chart-subtitle">各廠區冰機綜合效率<br>(kW/RT，越低代表能效表現越佳)</div>
      </div>
      
      <!-- MacroMicro 縮放快捷鍵 -->
      <div class="zoom-controls">
        <span class="zoom-lbl">Zoom:</span>
        <button class="zoom-btn active" id="btn-7d" onclick="zoomRange('7d', this, 'main')">All (7d)</button>
        <button class="zoom-btn" id="btn-3d" onclick="zoomRange('3d', this, 'main')">3d</button>
        <button class="zoom-btn" id="btn-1d" onclick="zoomRange('1d', this, 'main')">1d</button>
        <button class="zoom-btn" id="btn-12h" onclick="zoomRange('12h', this, 'main')">12h</button>
        <button class="zoom-btn" id="btn-6h" onclick="zoomRange('6h', this, 'main')">6h</button>
      </div>
    </div>

    <!-- ECharts 渲染視窗 -->
    <div id="trend-chart" class="chart-viewport"></div>

    <!-- 底部 SCADA 能效指標規格 -->
    <div class="metric-info-grid">
      <div class="info-card normal">
        <div class="info-lbl" id="card-lbl-normal">正常運轉控制線</div>
        <div class="info-val" id="card-val-normal">--</div>
      </div>
      <div class="info-card warning">
        <div class="info-lbl" id="card-lbl-warning">預警警報限制線</div>
        <div class="info-val" id="card-val-warning">--</div>
      </div>
    </div>
  </div>

  <!-- 空壓圖表面板 (能源效率專用) -->
  <div class="chart-card" id="trend-chart-card-cda" style="margin-top:15px;">
    <div class="chart-header">
      <div class="chart-title-group">
        <div class="chart-title">空壓運轉效率趨勢圖</div>
        <div class="chart-subtitle">各廠區空壓綜合效率<br>(CMM/kW，越高代表能效表現越佳)</div>
      </div>
      <!-- MacroMicro 縮放快捷鍵 -->
      <div class="zoom-controls">
        <span class="zoom-lbl">Zoom:</span>
        <button class="zoom-btn active" id="btn-7d-cda" onclick="zoomRange('7d', this, 'cda')">All (7d)</button>
        <button class="zoom-btn" id="btn-3d-cda" onclick="zoomRange('3d', this, 'cda')">3d</button>
        <button class="zoom-btn" id="btn-1d-cda" onclick="zoomRange('1d', this, 'cda')">1d</button>
        <button class="zoom-btn" id="btn-12h-cda" onclick="zoomRange('12h', this, 'cda')">12h</button>
        <button class="zoom-btn" id="btn-6h-cda" onclick="zoomRange('6h', this, 'cda')">6h</button>
      </div>
    </div>
    <!-- ECharts 渲染視窗 -->
    <div id="trend-chart-cda" class="chart-viewport"></div>
    <!-- 底部 SCADA 能效指標規格 -->
    <div class="metric-info-grid">
      <div class="info-card normal">
        <div class="info-lbl" id="card-lbl-normal-cda">正常運轉控制線</div>
        <div class="info-val" id="card-val-normal-cda">--</div>
      </div>
      <div class="info-card warning">
        <div class="info-lbl" id="card-lbl-warning-cda">預警警報限制線</div>
        <div class="info-val" id="card-val-warning-cda">--</div>
      </div>
    </div>
  </div>

  <!-- 廠區用電圖表面板 (能源效率專用) -->
  <div class="chart-card" id="trend-chart-card-power" style="margin-top:15px;">
    <div class="chart-header">
      <div class="chart-title-group">
        <div class="chart-title">廠區用電</div>
        <div class="chart-subtitle">MW</div>
      </div>
      <!-- MacroMicro 縮放快捷鍵 -->
      <div class="zoom-controls">
        <span class="zoom-lbl">Zoom:</span>
        <button class="zoom-btn active" id="btn-7d-power" onclick="zoomRange('7d', this, 'power')">All (7d)</button>
        <button class="zoom-btn" id="btn-3d-power" onclick="zoomRange('3d', this, 'power')">3d</button>
        <button class="zoom-btn" id="btn-1d-power" onclick="zoomRange('1d', this, 'power')">1d</button>
        <button class="zoom-btn" id="btn-12h-power" onclick="zoomRange('12h', this, 'power')">12h</button>
        <button class="zoom-btn" id="btn-6h-power" onclick="zoomRange('6h', this, 'power')">6h</button>
      </div>
    </div>
    <!-- ECharts 渲染視窗 -->
    <div id="trend-chart-power" class="chart-viewport"></div>
  </div>

  <!-- 施工中面板 -->
  <div class="wip-body" id="trend-wip" style="display:none;">
    <div class="wip-icon">&#x1F6A7;</div>
    <div class="wip-title">施工中 / Under Construction</div>
    <div class="wip-msg">此趨勢數據尚未接入系統<br>待資料接通後將自動上線</div>
  </div>

  <!-- 大宗化學品動態圖表容器 -->
  <div id="trend-chem-container" style="display:none;"></div>
  <!-- 排氣靜壓動態圖表容器 -->
  <div id="trend-static-container" style="display:none;"></div>
  <!-- 供應水質動態圖表容器 -->
  <div id="trend-water-container" style="display:none;"></div>
  <!-- 廢水處理動態圖表容器 -->
  <div id="trend-waste-container" style="display:none;"></div>
</div>
"""


    full_html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,viewport-fit=cover">
<title>EFplant</title>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/echarts/5.4.3/echarts.min.js"></script>
<style>
:root{{
  --bg:#0f172a; /* Deep slate blue */
  --sf:#1e293b; /* Medium slate blue panel */
  --sf2:#334155; /* Light slate blue */
  --bd:#334155; /* Slate border */
  --bd2:#475569; /* Lighter slate border */
  --run:#10b981; /* Solid Emerald Green */
  --stop:#ef4444; /* Solid Red */
  --blue:#60a5fa; /* Brighter Solid Sky Blue */
  --amb:#f59e0b; /* Solid Amber Yellow */
  --tx:#f1f5f9; /* Near white text */
  --dim:#a2b9d2; /* Brighter slate blue text (fixes hard-to-read dark blue/gray) */
  --mono:Consolas,Monaco,monospace;
}}
*{{box-sizing:border-box;margin:0;padding:0;}}
html,body{{height:100%;overflow:hidden;}}
body{{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  background:var(--bg);color:var(--tx);
  display:flex;flex-direction:column;
  height:100vh;height:100dvh;
}}
/* HEADER */
header{{
  display:flex;align-items:center;
  padding:10px 14px;gap:10px;
  border-bottom:1px solid var(--bd);
  background:var(--sf);flex-shrink:0;
}}
.brand{{display:flex;align-items:baseline;gap:10px;flex-shrink:0;}}
.bname{{
  font-size:1.55rem;font-weight:800;
  color:var(--blue);letter-spacing:1px;white-space:nowrap;
}}
.bsub{{font-size:.95rem;color:var(--dim);letter-spacing:0.5px;white-space:nowrap;font-weight:600;}}
.upd{{
  margin-left:auto;flex-shrink:0;
  display:flex;flex-direction:column;
  align-items:flex-end;gap:2px;
  font-family:var(--mono);font-size:.72rem;
  color:var(--amb);white-space:nowrap;
  text-align:right;
  padding-right:4px;
}}
/* STATUS PULSE/STATUS INDICATOR */
.pulse{{
  width:10px;
  height:10px;
  border-radius:50%;
  background:var(--dim);
  flex-shrink:0;
  transition:all .5s;
  cursor:help;
}}
.pulse.ok{{
  background:var(--run);
  box-shadow:0 0 6px var(--run);
}}
.pulse.warn{{
  background:var(--amb);
  box-shadow:0 0 6px var(--amb);
}}
.pulse.bad{{
  background:var(--stop);
  box-shadow:0 0 8px var(--stop);
  animation:blink 1.5s infinite;
}}
/* KPI WIP (no data) */
.kpi-wip{{font-size:1.2rem;color:var(--dim);font-family:var(--mono);}}
/* UNDER CONSTRUCTION */
.wip-body{{
  display:flex;flex-direction:column;align-items:center;
  justify-content:center;min-height:55vh;gap:14px;
  text-align:center;padding:40px 20px;
}}
.wip-icon{{font-size:3rem;line-height:1;}}
.wip-title{{
  font-family:var(--mono);font-size:.85rem;
  color:var(--dim);letter-spacing:2px;text-transform:uppercase;
}}
.wip-msg{{
  font-size:.75rem;color:var(--dim);line-height:2;
  border:1px dashed var(--bd);padding:12px 24px;
  font-family:var(--mono);letter-spacing:.5px;
}}
/* NEW EQUIPMENT BADGE */
.new-badge{{
  font-size:.55rem;font-family:var(--mono);
  color:var(--amb);border:1px solid var(--amb);
  padding:1px 4px;margin-left:4px;
  border-radius:2px;
  vertical-align:middle;letter-spacing:.5px;
}}
/* REFRESH BANNER */
.refresh-banner{{
  display:none;align-items:center;justify-content:center;gap:10px;
  padding:8px 14px;flex-shrink:0;
  background:rgba(59,130,246,.1);border-bottom:1px solid var(--blue);
  font-size:.78rem;color:var(--blue);white-space:nowrap;
  font-weight:600;
}}
.refresh-btn{{
  padding:4px 12px;background:var(--blue);border:none;border-radius:3px;
  color:#fff;font-size:.75rem;font-weight:700;
  cursor:pointer;white-space:nowrap;letter-spacing:0.5px;
}}
.refresh-btn:hover{{opacity:.85;}}
/* PLANT NAV - Wrapping layout to prevent off-screen scrolling on mobile */
.plant-nav{{
  display:flex;
  flex-wrap:wrap;
  padding:8px 12px;
  gap:6px;
  border-bottom:1px solid var(--bd);
  background:var(--bg);
  flex-shrink:0;
  align-items:center;
}}
.nav-btn{{
  padding:5px 12px;
  background:transparent;border:1px solid var(--bd2);border-radius:3px;
  color:var(--dim);
  font-size:.78rem;font-weight:700;
  cursor:pointer;white-space:nowrap;transition:all .15s;
}}
.nav-btn.active{{background:var(--blue);border-color:var(--blue);color:#fff;}}
.nav-btn:hover:not(.active){{border-color:var(--tx);color:var(--tx);}}
.chart-link{{
  padding:5px 10px;
  border:1px solid var(--bd2);border-radius:3px;
  color:var(--dim);text-decoration:none;font-size:.82rem;
  white-space:nowrap;transition:all .15s;
}}
.chart-link:hover{{color:var(--tx);border-color:var(--tx);}}
/* KPI ZONE - part of the page content; scrolls away with the equipment list */
.kpi-zone{{
  padding:0 0 8px;
  margin-bottom:10px;
  border-bottom:1px solid var(--bd);
  background:var(--bg);
}}
.kpi-set{{
  display:grid;grid-template-columns:repeat(2,1fr);gap:6px;
}}
@media(min-width:600px){{.kpi-set{{grid-template-columns:repeat(4,1fr);}}}}
/* SCROLL AREA */
.scroll-area{{
  flex:1 1 auto;overflow-y:auto;overflow-x:hidden;
  padding:10px 12px 16px;
  scrollbar-width:thin;scrollbar-color:var(--bd2) transparent;
}}
.scroll-area::-webkit-scrollbar{{width:4px;}}
.scroll-area::-webkit-scrollbar-track{{background:transparent;}}
.scroll-area::-webkit-scrollbar-thumb{{background:var(--bd2);border-radius:2px;}}
.kpi-card{{
  background:var(--sf);border:1px solid var(--bd);border-left:4px solid;
  padding:8px 12px;display:flex;flex-direction:column;gap:4px;overflow:hidden;
  border-radius:4px;
}}
.kpi-card.kpi-total{{border-left-color:var(--blue);}}
.kpi-card.kpi-run{{border-left-color:var(--run);}}
.kpi-card.kpi-stop{{border-left-color:var(--stop);}}
.kpi-card.kpi-rate{{border-left-color:var(--amb);}}
.kpi-lbl{{
  font-size:.65rem;color:var(--dim);
  text-transform:uppercase;letter-spacing:.8px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  font-weight:600;
}}
.kpi-val{{
  font-family:var(--mono);font-size:1.4rem;
  font-weight:700;line-height:1;white-space:nowrap;
}}
.kpi-total .kpi-val{{color:var(--blue);}}
.kpi-run   .kpi-val{{color:var(--run);}}
.kpi-stop  .kpi-val{{color:var(--stop);}}
.kpi-rate  .kpi-val{{color:var(--amb);}}
/* GROUP */
.eq-area{{display:flex;flex-direction:column;gap:12px;}}
.grp{{}}
.grp-hdr{{
  display:flex;align-items:center;gap:6px;
  padding-bottom:5px;margin-bottom:6px;
  border-bottom:1px solid var(--bd);overflow:hidden;
}}
.gi{{font-size:.85rem;flex-shrink:0;line-height:1;}}
.gn{{
  font-size:.78rem;font-weight:700;color:var(--tx);
  white-space:nowrap;text-transform:uppercase;letter-spacing:1px;flex-shrink:0;
}}
.gd{{
  font-size:.68rem;color:var(--dim);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;
}}
.gc{{
  margin-left:auto;font-family:var(--mono);
  font-size:.72rem;color:var(--blue);white-space:nowrap;flex-shrink:0;
  font-weight:700;
}}
/* CARDS */
.cards-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:5px;}}
@media(min-width:600px){{.cards-grid{{grid-template-columns:repeat(auto-fill,minmax(140px,1fr));}}}}
.card{{
  background:var(--sf);border:1px solid var(--bd);
  border-top:3px solid transparent;
  padding:8px;display:flex;flex-direction:column;gap:4px;
  overflow:hidden;cursor:default;transition:background .15s;
  border-radius:4px;
}}
.card:hover{{background:var(--sf2);}}
.card.running{{border-top-color:var(--run);}}
.card.stopped{{border-top-color:var(--stop);}}
.eq-id{{
  font-family:var(--mono);font-size:.72rem;font-weight:700;
  color:var(--tx);white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;line-height:1.2;
}}
.srow{{display:flex;align-items:center;gap:6px;}}
.led{{width:8px;height:8px;border-radius:50%;flex-shrink:0;}}
/* Solid Green/Red for Industrial SCADA, no glow, no blink */
.card.running .led{{
  background:var(--run);
  box-shadow:0 0 6px var(--run);
  animation:led-breathing 2s infinite ease-in-out;
}}
.card.stopped .led{{background:var(--stop);opacity:.4;}}
.stxt{{font-family:var(--mono);font-size:.65rem;white-space:nowrap;line-height:1;font-weight:700;}}
.card.running .stxt{{color:var(--run);}}
.card.stopped .stxt{{color:var(--stop);opacity:.6;}}
.no-dev{{
  grid-column:1/-1;padding:10px;text-align:center;
  color:var(--dim);font-size:.68rem;
  border:1px dashed var(--bd);font-family:var(--mono);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}}
.demo-lbl{{
  font-family:var(--mono);font-size:.56rem;color:var(--amb);
  border:1px solid var(--amb);padding:1px 4px;
  border-radius:2px;
}}
footer{{
  margin-top:16px;padding:8px 0;
  border-top:1px solid var(--bd);text-align:center;
  color:var(--dim);font-size:.65rem;font-family:var(--mono);
  letter-spacing:0.5px;white-space:nowrap;
}}
@keyframes led-breathing{{
  0%,100%{{
    box-shadow:0 0 2px var(--run);
    opacity:0.6;
  }}
  50%{{
    box-shadow:0 0 8px var(--run);
    opacity:1;
  }}
}}
@keyframes blink{{0%,100%{{opacity:1;}}50%{{opacity:.3;}}}}

/* TABS & CHARTS (SPA INTEGRATION) */
.tabs-container {{
  display: flex;
  gap: 8px;
  overflow-x: auto;
  scrollbar-width: none;
  padding-bottom: 2px;
  margin-bottom: 12px; /* 空出空間以對齊圖表區塊 */
  flex-shrink: 0;
}}
.tabs-container::-webkit-scrollbar {{
  display: none;
}}
.tab-btn {{
  padding: 6px 14px;
  background: var(--sf);
  border: 1px solid var(--bd);
  border-radius: 4px;
  color: var(--dim);
  font-size: 0.8rem;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
  transition: all 0.15s;
}}
.tab-btn.active {{
  background: rgba(96, 165, 250, 0.15);
  border-color: var(--blue);
  color: var(--blue);
}}
.tab-btn:hover:not(.active) {{
  border-color: var(--bd2);
  color: var(--tx);
}}
.chart-card {{
  background: var(--sf);
  border: 1px solid var(--bd);
  border-radius: 6px;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  position: relative;
  box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
}}
.chart-header {{
  display: flex;
  flex-direction: column;
  gap: 8px;
}}
@media (min-width: 680px) {{
  .chart-header {{
    flex-direction: row;
    align-items: center;
    justify-content: space-between;
  }}
}}
.chart-title-group {{
  display: flex;
  flex-direction: column;
  gap: 2px;
  text-align: center;
  align-items: center;
  flex: 1;
}}
.chart-title {{
  font-size: 0.95rem;
  font-weight: 700;
  color: var(--tx);
  letter-spacing: 0.5px;
}}
.chart-subtitle {{
  font-size: 0.72rem;
  color: var(--dim);
}}
.zoom-controls {{
  display: flex;
  align-items: center;
  gap: 4px;
  background: rgba(0, 0, 0, 0.2);
  padding: 3px 6px;
  border-radius: 4px;
  border: 1px solid var(--bd);
  align-self: center;
}}
.zoom-lbl {{
  font-size: 0.68rem;
  color: var(--dim);
  font-weight: 600;
  margin-right: 4px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}}
.zoom-btn {{
  padding: 3px 8px;
  background: transparent;
  border: none;
  border-radius: 3px;
  color: var(--dim);
  font-size: 0.72rem;
  font-weight: 700;
  cursor: pointer;
  transition: all 0.15s;
}}
.zoom-btn.active {{
  background: var(--blue);
  color: #0f172a;
}}
.zoom-btn:hover:not(.active) {{
  color: var(--tx);
  background: rgba(255, 255, 255, 0.05);
}}
#top-btn {{
  position: fixed;
  bottom: 72px;
  right: 18px;
  z-index: 9999;
  width: 42px;
  height: 42px;
  border-radius: 50%;
  border: none;
  background: var(--blue);
  color: var(--bg);
  font-size: 0.65rem;
  font-weight: 800;
  line-height: 1.2;
  cursor: pointer;
  box-shadow: 0 3px 12px rgba(0,0,0,0.55);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-direction: column;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.22s, transform 0.22s;
  transform: translateY(8px);
}}
#top-btn.visible {{
  opacity: 1;
  pointer-events: auto;
  transform: translateY(0);
}}
#top-btn:hover {{
  background: #60c4ff;
}}
.chart-viewport {{
  width: 100%;
  height: 380px;
  position: relative;
}}
@media (max-width: 600px) and (orientation: portrait) {{
  .chart-viewport {{
    height: 340px;
    min-height: 300px;
  }}
}}
@media (max-height: 600px) and (orientation: landscape) {{
  .chart-viewport {{
    height: 220px;
    min-height: 180px;
  }}
}}
.metric-info-grid {{
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 8px;
  margin-top: 4px;
}}
.info-card {{
  background: rgba(0, 0, 0, 0.15);
  border: 1px solid var(--bd);
  padding: 8px 12px;
  border-radius: 4px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}}
.info-lbl {{
  font-size: 0.62rem;
  color: var(--dim);
  text-transform: uppercase;
  letter-spacing: 0.8px;
  font-weight: 600;
}}
.info-val {{
  font-family: var(--mono);
  font-size: 1.15rem;
  font-weight: 700;
  line-height: 1.2;
  white-space: nowrap;
}}
@media (max-width: 600px) {{
  .metric-info-grid {{
    grid-template-columns: repeat(2, 1fr);
  }}
  .info-card {{
    padding: 8px 6px;
  }}
  .info-val {{
    font-size: 0.82rem;
  }}
  .brand {{
    gap: 6px;
    flex-wrap: wrap;
  }}
  .bname {{
    font-size: 1.3rem;
  }}
  .bsub {{
    font-size: 0.8rem;
  }}
}}
.info-card.normal .info-val {{ color: #10b981; }}
.info-card.warning .info-val {{ color: var(--amb); }}
/* KF1 MANAGER ALARM RISK DASHBOARD */
.kf1-alarm-block{{clear:both;margin-top:32px;padding-top:24px;border-top:1px solid var(--bd2);position:relative;z-index:0;}}
.alarm-hero{{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:12px;padding:14px 16px;background:linear-gradient(135deg,rgba(244,63,94,.15),rgba(30,41,59,.95));border:1px solid rgba(244,63,94,.35);border-radius:7px;}}
.alarm-hero h2{{margin:2px 0 3px;font-size:1.25rem;color:var(--tx);}}.alarm-hero p{{margin:0;color:var(--dim);font-size:.72rem;}}
.alarm-eyebrow{{font-family:var(--mono);font-size:.65rem;letter-spacing:1.2px;color:#fb7185;font-weight:700;}}
.alarm-freshness{{min-width:112px;text-align:right;padding:8px 10px;border-radius:5px;border:1px solid var(--bd);background:rgba(15,23,42,.72);}}
.alarm-freshness span{{display:block;color:var(--dim);font-size:.62rem;}}.alarm-freshness b{{display:block;margin-top:4px;font-family:var(--mono);font-size:.78rem;}}
.alarm-freshness.ok b{{color:var(--run);}}.alarm-freshness.warn b{{color:var(--amb);}}.alarm-freshness.danger b{{color:#fb7185;}}
.alarm-kpis{{display:grid;grid-template-columns:repeat(2,1fr);gap:7px;margin-bottom:10px;}}@media(min-width:920px){{.alarm-kpis{{grid-template-columns:repeat(4,1fr);}}}}
.alarm-kpi{{padding:11px 12px;background:var(--sf);border:1px solid var(--bd);border-top:3px solid;border-radius:6px;display:flex;flex-direction:column;gap:3px;}}
.alarm-kpi span{{font-size:.65rem;color:var(--dim);font-weight:700;}}.alarm-kpi b{{font:700 1.45rem var(--mono);}}.alarm-kpi small{{font-size:.6rem;color:var(--dim);}}
.alarm-kpi.red{{border-top-color:#f43f5e;}}.alarm-kpi.red b{{color:#fb7185;}}.alarm-kpi.amber{{border-top-color:#f59e0b;}}.alarm-kpi.amber b{{color:#fbbf24;}}
.alarm-kpi.violet{{border-top-color:#a855f7;}}.alarm-kpi.violet b{{color:#c084fc;}}.alarm-kpi.blue{{border-top-color:#3b82f6;}}.alarm-kpi.blue b{{color:#60a5fa;}}
.alarm-kpi.risk.low{{border-top-color:#10b981;}}.alarm-kpi.risk.low b{{color:#34d399;}}.alarm-kpi.risk.medium{{border-top-color:#f59e0b;}}.alarm-kpi.risk.medium b{{color:#fbbf24;}}.alarm-kpi.risk.high{{border-top-color:#f43f5e;}}.alarm-kpi.risk.high b{{color:#fb7185;}}
.alarm-grid{{display:grid;grid-template-columns:1fr;gap:10px;margin-bottom:10px;}}@media(min-width:900px){{.alarm-grid{{grid-template-columns:1fr 1fr;}}}}
.alarm-grid-primary{{grid-template-columns:1fr;}}@media(min-width:900px){{.alarm-grid-primary{{grid-template-columns:1.35fr .65fr;}}}}
.alarm-panel{{background:var(--sf);border:1px solid var(--bd);border-radius:6px;padding:12px;min-width:0;}}
.alarm-panel-title{{display:flex;align-items:flex-end;justify-content:space-between;gap:8px;margin-bottom:12px;}}.alarm-panel-title span{{font-size:.82rem;font-weight:700;}}.alarm-panel-title small{{font-size:.58rem;color:var(--dim);text-align:right;}}
.alarm-daily{{height:190px;display:flex;align-items:flex-end;justify-content:space-around;gap:6px;padding:8px 2px 0;}}
.alarm-day-col{{height:100%;flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;min-width:32px;}}.alarm-day-value{{font:700 .65rem var(--mono);color:var(--tx);margin-bottom:4px;}}
.alarm-day-bar{{height:135px;width:min(34px,70%);display:flex;align-items:flex-end;background:rgba(15,23,42,.7);border-radius:3px 3px 0 0;overflow:hidden;}}.alarm-day-bar i{{display:block;width:100%;background:linear-gradient(#fb7185,#e11d48);border-radius:3px 3px 0 0;}}
.alarm-day-label{{font:.6rem var(--mono);color:var(--dim);margin-top:5px;}}
.alarm-hours{{display:grid;grid-template-columns:repeat(6,1fr);gap:4px;}}.alarm-hour{{min-height:42px;border:1px solid rgba(148,163,184,.16);border-radius:4px;padding:5px;text-align:center;display:flex;flex-direction:column;justify-content:center;}}
.alarm-hour b{{font:.64rem var(--mono);color:#fff;}}.alarm-hour span{{font:.58rem var(--mono);color:rgba(255,255,255,.8);margin-top:2px;}}
.alarm-day-col[data-alarm-tip],.alarm-hour[data-alarm-tip]{{cursor:pointer;touch-action:manipulation;outline:none;}}
.alarm-day-col[data-alarm-tip]:focus-visible,.alarm-hour[data-alarm-tip]:focus-visible{{box-shadow:0 0 0 2px var(--blue);border-radius:4px;}}
.alarm-touch-tip{{position:fixed;z-index:10050;max-width:min(290px,calc(100vw - 24px));padding:9px 11px;background:#020617;color:#f8fafc;border:1px solid #64748b;border-radius:6px;box-shadow:0 10px 28px rgba(0,0,0,.58);font:700 .7rem/1.45 var(--mono);pointer-events:none;opacity:0;transform:translateY(5px);transition:opacity .12s,transform .12s;}}
.alarm-touch-tip.show{{opacity:1;transform:translateY(0);}}
.alarm-risk-row{{margin-bottom:10px;}}.alarm-risk-head{{display:flex;justify-content:space-between;gap:8px;font-size:.68rem;}}.alarm-risk-head span{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}.alarm-risk-head b{{font-family:var(--mono);color:#fb7185;white-space:nowrap;}}
.alarm-risk-tag{{font:.56rem var(--mono);color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin:3px 0;}}.alarm-bar{{height:5px;background:#0f172a;border-radius:5px;overflow:hidden;}}.alarm-bar i{{height:100%;display:block;background:linear-gradient(90deg,#f59e0b,#f43f5e);}}
.alarm-table-wrap{{overflow:auto;max-height:410px;}}.alarm-table{{width:100%;border-collapse:collapse;font-size:.64rem;}}.alarm-table th{{position:sticky;top:0;background:var(--sf);text-align:left;color:var(--dim);padding:6px;border-bottom:1px solid var(--bd);white-space:nowrap;}}
.alarm-table td{{padding:7px 6px;border-bottom:1px solid var(--bd);vertical-align:top;white-space:nowrap;}}.alarm-table td:nth-child(2){{white-space:normal;min-width:150px;}}.alarm-table td small{{display:block;margin-top:2px;font:500 .54rem var(--mono);color:var(--dim);word-break:break-all;}}
.alarm-time span{{display:block;line-height:1.35;}}
.alarm-rank{{display:inline-flex;width:22px;height:22px;align-items:center;justify-content:center;border-radius:50%;background:rgba(244,63,94,.15);border:1px solid rgba(244,63,94,.4);color:#fb7185;font:800 .65rem var(--mono);}}.alarm-duration{{color:#fb7185;font-weight:800;font-family:var(--mono);}}
.alarm-status{{display:inline-block;padding:2px 5px;border-radius:3px;font:700 .58rem var(--mono);background:rgba(245,158,11,.15);color:#fbbf24;border:1px solid rgba(245,158,11,.35);}}.alarm-status-hihi,.alarm-status-lolo{{background:rgba(244,63,94,.15);color:#fb7185;border-color:rgba(244,63,94,.4);}}
.alarm-all-clear{{text-align:center;color:var(--run);padding:28px!important;}}.alarm-data-health{{display:grid;grid-template-columns:auto auto 1fr;gap:14px;align-items:center;background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.28);border-radius:6px;padding:10px 12px;}}
.alarm-data-health div{{display:flex;flex-direction:column;}}.alarm-data-health span{{font-size:.58rem;color:var(--dim);}}.alarm-data-health b{{font:.75rem var(--mono);color:#fbbf24;}}.alarm-data-health p{{margin:0;font-size:.62rem;color:var(--dim);}}
.alarm-pending{{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;min-height:240px;margin-top:28px;padding:32px 20px;text-align:center;border:1px dashed var(--bd2);border-radius:8px;background:rgba(15,23,42,.28);}}
.alarm-pending .wip-msg{{min-width:min(320px,100%);}}
.alarm-empty{{min-height:45vh;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;color:var(--dim);border:1px dashed var(--bd2);border-radius:8px;padding:30px;}}.alarm-empty-title{{font-size:1rem;color:var(--tx);font-weight:700;margin-bottom:8px;}}
@media(max-width:600px){{.alarm-hero{{flex-direction:column;}}.alarm-freshness{{text-align:left;}}.alarm-panel-title{{align-items:flex-start;flex-direction:column;}}.alarm-panel-title small{{text-align:left;}}.alarm-data-health{{grid-template-columns:1fr 1fr;}}.alarm-data-health p{{grid-column:1/-1;}}.alarm-table-wrap{{overflow-x:hidden;}}.alarm-table{{table-layout:fixed;font-size:.58rem;}}.alarm-table th,.alarm-table td{{padding:6px 2px;white-space:normal;word-break:break-word;}}.alarm-table th:nth-child(1){{width:8%;}}.alarm-table th:nth-child(2){{width:32%;}}.alarm-table th:nth-child(3),.alarm-table th:nth-child(4){{width:18%;}}.alarm-table th:nth-child(5){{width:24%;}}.alarm-table td:nth-child(2){{min-width:0;}}.alarm-table td small{{font-size:.48rem;overflow-wrap:anywhere;}}.alarm-rank{{width:19px;height:19px;font-size:.56rem;}}.alarm-time,.alarm-duration{{font-size:.55rem;}}}}
</style>
</head>
<body>
<header>
  <div class="brand">
    <span class="bname">EFplant</span>
    <span class="bsub">廠務設備智慧整合</span>
  </div>
  <div class="upd">
    <div style="display:flex;align-items:center;gap:6px;">
      <span class="pulse" id="status-pulse" title="checking..."></span>
      <span>設備運轉 TIMESTAMP</span>
    </div>
    <div style="font-size:0.68rem;color:var(--dim);margin-top:1px;font-weight:600;">{timestamp_range_str}</div>
  </div>
</header>
{nav_bar_html}
<div class="refresh-banner" id="refresh-banner">
  &#x1F504; 新資料已就緒，<span id="refresh-count">5</span> 秒後自動重新整理 &nbsp;
  <button class="refresh-btn" onclick="doRefreshNow()">立即重整</button>
</div>
<div class="scroll-area">
{kpi_zone_html}
{plant_html}
  <footer>
    EFplant FMCS Smart Plant Integration &copy; 2026<br>
    <button onclick="reloadKeepSess()" 
            style="background:transparent;border:none;color:var(--dim);
                   font-family:var(--mono);font-size:.65rem;cursor:pointer;
                   letter-spacing:0.5px;margin-top:6px;font-weight:600;text-decoration:underline;">
      &#x1F504; 清除快取重新整理
    </button>
    &nbsp;&nbsp;
    <button onclick="logOut()" 
            style="background:transparent;border:none;color:var(--dim);
                   font-family:var(--mono);font-size:.65rem;cursor:pointer;
                   letter-spacing:0.5px;margin-top:6px;font-weight:600;text-decoration:underline;">
      &#x1F6AA; 登出系統
    </button>
  </footer>
</div>
<script>
// ── Status pulse & auto-update detection ────────────────────────────
const GEN_TIME = new Date("{generation_time_iso}");
const INTERVAL_MIN = 60, GRACE_MIN = 15;

function updateStatus(){{
  const minPast=(Date.now()-GEN_TIME)/60000;
  const p=document.getElementById('status-pulse');
  if(!p)return;
  if(minPast<INTERVAL_MIN+GRACE_MIN){{
    p.className='pulse ok';
    p.title='DATA OK — generated '+GEN_TIME.toLocaleTimeString()+' | Next update ~'+new Date(GEN_TIME.getTime()+(INTERVAL_MIN+GRACE_MIN)*60000).toLocaleTimeString();
  }}else if(minPast<INTERVAL_MIN*2+GRACE_MIN){{
    p.className='pulse warn';
    p.title='UPDATE OVERDUE — refresh page for new data';
  }}else{{
    p.className='pulse bad';
    p.title='PIPELINE BROKEN — check main.py is running';
  }}
}}

var _reloading=false;

function reloadKeepSess() {{
  if ('serviceWorker' in navigator) {{
    navigator.serviceWorker.getRegistrations().then(function(regs) {{
      var promises = [];
      for (var i = 0; i < regs.length; i++) {{
        promises.push(regs[i].unregister());
      }}
      return Promise.all(promises);
    }}).then(function() {{
      if ('caches' in window) {{
        return caches.keys().then(function(keys) {{
          return Promise.all(keys.map(function(k) {{ return caches.delete(k); }}));
        }});
      }}
    }}).then(function() {{
      var base = location.protocol + '//' + location.host + location.pathname;
      location.replace(base + '?_nc=' + Date.now());
    }}).catch(function() {{
      location.reload();
    }});
    return;
  }}
  location.reload();
}}

function doRefreshNow(){{
  reloadKeepSess();
}}

var API_BASE = "http://{{LOCAL_IP}}:47313";
var LOGIN_AUDIT_ENABLED = false;
function getDeviceId() {{
  try {{
    var id = localStorage.getItem('ef_device_id');
    if (!id) {{
      id = 'EF-' + (Math.random().toString(36).substring(2) + Date.now().toString(36)).substring(0, 18).toUpperCase();
      localStorage.setItem('ef_device_id', id);
    }}
    return id;
  }} catch(e) {{
    return 'EF-UNKNOWN';
  }}
}}

function sendLog(action, success, indexId) {{
  if (!LOGIN_AUDIT_ENABLED) return;
  if (!API_BASE) return;
  var sess = null;
  try {{ sess = JSON.parse(localStorage.getItem('ef_sess_v2')) || {{}}; }} catch(e) {{}}
  var sessId = localStorage.getItem('ef_sess_id') || '';
  var payload = {{
    action: action,
    success: success !== undefined ? success : true,
    index_id: indexId || '',
    device_id: getDeviceId(),
    session_id: sessId,
    timestamp: Date.now()
  }};
  if (action === 'LOGOUT' && sess && sess.login_time) {{
    var durSec = Math.floor((Date.now() - sess.login_time) / 1000);
    var h = Math.floor(durSec / 3600);
    var m = Math.floor((durSec % 3600) / 60);
    var s = durSec % 60;
    payload.duration = (h > 0 ? h + 'h ' : '') + (m > 0 ? m + 'm ' : '') + s + 's';
  }}
  function sendLogFallback() {{
    try {{
      var qs = Object.keys(payload).map(function(k) {{
        return encodeURIComponent(k) + '=' + encodeURIComponent(payload[k] == null ? '' : payload[k]);
      }}).join('&');
      var img = new Image();
      img.src = API_BASE + '/api/log?' + qs + '&_=' + Date.now();
    }} catch(e) {{}}
  }}
  fetch(API_BASE + '/api/log', {{
    method: 'POST',
    mode: 'cors',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify(payload)
  }}).catch(function(e) {{ sendLogFallback(); }});
}}

function logOut(){{
  sendLog('LOGOUT', true, (function(){{
    try {{
      var d = JSON.parse(localStorage.getItem('ef_sess_v2'));
      return d ? d.dk.substring(0, 32) : '';
    }} catch(e) {{ return ''; }}
  }})());
  localStorage.removeItem('ef_sess_v2');
  localStorage.removeItem('ef_sess_id');
  location.replace(location.protocol + '//' + location.host + location.pathname + '?_nc=' + Date.now());
}}

function startCountdown(){{
  var banner=document.getElementById('refresh-banner');
  var countEl=document.getElementById('refresh-count');
  if(banner)banner.style.display='flex';
  var sec=5;
  var t=setInterval(function(){{
    sec--;
    if(countEl)countEl.textContent=sec;
    if(sec<=0){{clearInterval(t);doRefreshNow();}}
  }},1000);
}}

async function checkHealth(){{
  if(_reloading)return;
  try{{
    const r=await fetch('health.json?_='+Date.now(),{{cache:'no-store'}});
    if(!r.ok)return;
    const d=await r.json();
    if(new Date(d.updated)>GEN_TIME){{
      _reloading=true;
      startCountdown();
    }}
  }}catch(e){{}}
}}

updateStatus();
setTimeout(checkHealth,15000);
setInterval(function(){{updateStatus();checkHealth();}},60000);

// 手機切換回頁面時立即偵測
document.addEventListener('visibilitychange',function(){{
  if(document.visibilityState==='visible')checkHealth();
}});
// ────────────────────────────────────────────────────────────────────

function switchPlant(id){{
  document.querySelectorAll('.plant-container').forEach(c=>{{c.style.display='none';c.classList.remove('active');}});
  document.querySelectorAll('.kpi-set').forEach(k=>{{k.style.display='none';}});
  document.querySelectorAll('.nav-btn').forEach(b=>b.classList.remove('active'));
  var t=document.getElementById('pc-'+id);
  if(t){{t.style.display='block';t.classList.add('active');}}
  var k=document.getElementById('kpi-'+id);
  if(k){{k.style.display='';}}

  // Hide KPI zone wrapper when viewing trends to prevent empty padding/border line
  var kpiZone = document.querySelector('.kpi-zone');
  if(kpiZone){{
    if(id==='TREND'){{
      kpiZone.style.display='none';
    }}else{{
      kpiZone.style.display='block';
    }}
  }}

  var b=document.querySelector('[data-plant="'+id+'"]');
  if(b){{b.classList.add('active');b.scrollIntoView({{behavior:'smooth',block:'nearest',inline:'center'}});}}
  localStorage.setItem('ap',id);

  // SPA ECharts Resize Hook
  if(id==='TREND'){{
    if(!myChart){{
      initChart();
    }}else{{
      myChart.resize();
      if(myChartCda) myChartCda.resize();
      if(myChartPower) myChartPower.resize();
    }}
  }}
}}

// ── ECharts Trend Logic (SPA Integration) ─────────────────────────
const PLANTS = ["T2A", "S2A", "PCB", "S2", "S3", "HJ1", "HJ2", "LC2", "LC3", "TH", "KF1"];
const PLANT_COLORS = {{
  "T2A": "#38bdf8",
  "S2A": "#10b981",
  "PCB": "#a855f7",
  "S2":  "#f59e0b",
  "S3":  "#f43f5e",
  "HJ1": "#06b6d4",
  "HJ2": "#ec4899",
  "LC2": "#3b82f6",
  "LC3": "#14b8a6",
  "TH":  "#f97316",
  "KF1": "#facc15",
  "HJ":  "#06b6d4"
}};
const METRIC_METADATA = {{
  '冰機效率': {{
    title: '冰機運轉效率趨勢圖',
    subtitle: '各廠區冰機綜合效率(kW/RT，越低代表能效表現越佳)',
    unit: 'kW/RT',
    normalLine: '正常運轉控制線',
    normalVal: '< 0.70 kW/RT',
    warningLine: '預警警報限制線 (HIGH)',
    warningVal: '0.85 kW/RT',
    yMin: 0.45,
    yMax: 1.25,
    fixedYMin: 0.00,
    fixedYMax: 1.50,
    precision: 3,
    controlVal: 0.70,
    warningValNum: 0.85
  }},
  '空壓效率': {{
    title: '空壓運轉效率趨勢圖',
    subtitle: '各廠區空壓綜合效率(CMM/kW，越高代表能效表現越佳)',
    unit: 'CMM/kW',
    normalLine: '正常運轉控制線',
    normalVal: '> 7 CMM/kW',
    warningLine: '預警警報限制線 (LOW)',
    warningVal: '5.5 CMM/kW',
    yMin: 4.0,
    yMax: 15.0,
    fixedYMin: 0.00,
    fixedYMax: 20.0,
    precision: 2,
    controlVal: 7.00,
    warningValNum: 5.5
  }},
  '廠區用電': {{
    title: '廠區用電',
    subtitle: 'MW',
    unit: 'MW',
    precision: 2,
    controlVal: 0.0,
    warningValNum: 0.0,
    noControlLines: true
  }},
  '運轉率': {{
    title: '設備運轉率趨勢圖',
    subtitle: '各廠區整點更新之設備運轉率(%，越高代表稼動率表現越佳)',
    unit: '%',
    normalLine: '正常運轉目標線',
    normalVal: '> 85.0 %',
    warningLine: '預警低產能限制線 (LOW)',
    warningVal: '70.0 %',
    yMin: 0.0,
    yMax: 100.0,
    fixedYMin: 0.00,
    fixedYMax: 100.0,
    precision: 2,
    controlVal: 85.0,
    warningValNum: 70.0
  }},
  '大宗化學品': {{
    title: '大宗化學品趨勢圖',
    subtitle: '各廠區大宗化學品液位變化(cm)',
    unit: 'cm',
    normalLine: '安全液位控制線',
    normalVal: '> 30.0 cm',
    warningLine: '低液位警報線 (LOW)',
    warningVal: '20.0 cm',
    fixedYMin: 0.0,
    precision: 2,
    controlVal: 30.0,
    warningValNum: 20.0
  }},
  '排氣靜壓': {{
    title: '排氣靜壓趨勢圖',
    subtitle: '各廠區排氣靜壓變化(Pa)',
    unit: 'Pa',
    precision: 1,
    controlVal: 0.0,
    warningValNum: 0.0
  }},
  '供應水質': {{
    title: '供應水質趨勢圖',
    subtitle: '各廠區超純水供水導電度(µS/cm)',
    unit: 'µS/cm',
    precision: 4,
    controlVal: 0.0,
    warningValNum: 0.0
  }},
  '廢水處理': {{
    title: '廢水處理出口pH趨勢圖',
    subtitle: '',
    unit: 'pH',
    precision: 2,
    fixedYMin: 0.0,
    fixedYMax: 14.0,
    controlVal: 0.0,
    warningValNum: 0.0
  }}
}};

function formatSubtitle(text) {{
  if (text && text.includes('(')) {{
    return text.replace('(', '<br>(');
  }}
  return text;
}}

function updateMetricUI() {{
  var metricKey = (currentMetric === '能源效率') ? '冰機效率' : currentMetric;
  const meta = METRIC_METADATA[metricKey];
  if (!meta) return;

  const titleEl = document.querySelector('.chart-title');
  const subEl = document.querySelector('.chart-subtitle');
  if (titleEl) titleEl.textContent = meta.title;
  if (subEl) subEl.innerHTML = formatSubtitle(meta.subtitle);

  const lblNormal = document.getElementById('card-lbl-normal');
  const valNormal = document.getElementById('card-val-normal');
  const lblWarning = document.getElementById('card-lbl-warning');
  const valWarning = document.getElementById('card-val-warning');

  if (lblNormal) lblNormal.textContent = meta.normalLine;
  if (valNormal) valNormal.textContent = meta.normalVal;
  if (lblWarning) lblWarning.textContent = meta.warningLine;
  if (valWarning) valWarning.textContent = meta.warningVal;

  // 空壓圖表卡片：比照冰機圖表，從 METRIC_METADATA['空壓效率'] 動態填入
  if (currentMetric === '能源效率') {{
    const cdaMeta = METRIC_METADATA['空壓效率'];
    if (cdaMeta) {{
      var cdaLblN = document.getElementById('card-lbl-normal-cda');
      var cdaValN = document.getElementById('card-val-normal-cda');
      var cdaLblW = document.getElementById('card-lbl-warning-cda');
      var cdaValW = document.getElementById('card-val-warning-cda');
      if (cdaLblN) cdaLblN.textContent = cdaMeta.normalLine;
      if (cdaValN) cdaValN.textContent = cdaMeta.normalVal;
      if (cdaLblW) cdaLblW.textContent = cdaMeta.warningLine;
      if (cdaValW) cdaValW.textContent = cdaMeta.warningVal;
    }}
  }}
}}

var myChart = null;
var myChartCda = null;
var myChartPower = null;
var currentMetric = '能源效率';
var chartTimestamps = {chart_timestamps_json};
var chartSeriesData = {chart_series_json};
var chemNames = {chem_names_json};
var staticNames = {static_names_json};
var waterNames = {water_names_json};
var wasteNames = {waste_names_json};
// 化學品圖表標題顯示名稱對照（合併鍵 → 顯示標題；不影響內部 key/合併）。
// 如需美化其他化學品標題，於此增列即可。
var CHEM_DISPLAY = {{ 'NA2CO3': 'NaCO3', 'NACLO3': 'NaClO3', 'CM2250R': 'CM-2250R', 'CS9110R': 'CS-9110R' }};
var WATER_DISPLAY = {{ '供水導電度高': '供水導電度 ＞1 (µS/cm)', '供水導電度低': '供水導電度 ＜1 (µS/cm)' }};
var WASTE_DISPLAY = {{ '出口pH': '出口pH' }};
var chemChartInstances = [];

function initChart() {{
  const chartDom = document.getElementById('trend-chart');
  const cdaDom = document.getElementById('trend-chart-cda');
  const powerDom = document.getElementById('trend-chart-power');
  if (chartDom) myChart = echarts.init(chartDom);
  if (cdaDom) myChartCda = echarts.init(cdaDom);
  if (powerDom) myChartPower = echarts.init(powerDom);
  renderChartData();
  updateMetricUI();
}}

function buildChartOption(metricName, legendH) {{
  if (!chartTimestamps.length) return null;

  var metricData = chartSeriesData[metricName] || {{}};

  // 線別 key 可能是廠區、廠區+濃度，或靜壓的「廠區-設備編號」。
  // 依 metricData 既有 key 動態取出真正有數據者；顏色以廠區前綴對應，
  // 同廠區多條線(NaOH 多濃度)以 線型(實線/虛線/點線) 區分。
  var seriesKeys = Object.keys(metricData).filter(function(k) {{
    var vals = metricData[k];
    return vals && vals.length && vals.some(function(v) {{ return v !== null && v !== undefined; }});
  }});
  function _plantOf(label) {{ return label.split(/[ -]/)[0]; }}
  function _colorOf(label) {{ return PLANT_COLORS[_plantOf(label)] || '#9aa7b8'; }}
  var _dashStyles = ['solid', 'dashed', 'dotted'];
  var _plantSeen = {{}};

  const seriesArray = seriesKeys.map(function(k) {{
    var color = _colorOf(k);
    var n = _plantSeen[_plantOf(k)] || 0;
    _plantSeen[_plantOf(k)] = n + 1;
    return {{
      name: k,
      type: 'line',
      data: metricData[k],
      showSymbol: true,
      symbolSize: 6,
      connectNulls: true,
      smooth: 0.2,
      z: 2,
      lineStyle: {{
        width: 2.2,
        type: _dashStyles[n % _dashStyles.length],
        shadowBlur: 8,
        shadowColor: color + '4D'
      }},
      itemStyle: {{
        color: color
      }},
      emphasis: {{
        focus: 'series',
        lineStyle: {{
          width: 3.5,
          shadowBlur: 12
        }}
      }}
    }};
  }});

  var metaKey = metricName;
  var isChem = (metricName.indexOf('chem_') === 0);
  var isStatic = (metricName.indexOf('static_') === 0);
  var isWater = (metricName.indexOf('water_') === 0);
  var isWaste = (metricName.indexOf('waste_') === 0);
  var isMulti = isChem || isStatic || isWater || isWaste;  // 多子圖類別：不畫控制線、依數據自適應
  if (isChem) metaKey = '大宗化學品';
  else if (isStatic) metaKey = '排氣靜壓';
  else if (isWater) metaKey = '供應水質';
  else if (isWaste) metaKey = '廢水處理';
  const meta = METRIC_METADATA[metaKey];
  if (!meta) return null;

  // 多子圖類別（大宗化學品 / 排氣靜壓）不顯示控制線（黃色預警虛線與綠色正常虛線）
  if (!isMulti && !meta.noControlLines) {{
    seriesArray.push({{
      name: 'ReferenceLines',
      type: 'line',
      data: [],
      markLine: {{
        symbol: ['none', 'none'],
        silent: true,
        data: [
          {{
            yAxis: meta.controlVal,
            lineStyle: {{ color: '#10b981', type: 'dashed', width: 1.5, opacity: 0.8 }},
            label: {{ show: false }}
          }},
          {{
            yAxis: meta.warningValNum,
            lineStyle: {{ color: '#f59e0b', type: 'dashed', width: 1.5, opacity: 0.8 }},
            label: {{ show: false }}
          }}
        ]
      }}
    }});
  }}

  var controlVal = meta.controlVal;
  var dataMin = Infinity;
  var dataMax = -Infinity;
  var hasData = false;

  seriesKeys.forEach(function(k) {{
    var vals = metricData[k] || [];
    vals.forEach(function(v) {{
      if (v !== null && v !== undefined && !isNaN(v)) {{
        hasData = true;
        if (v < dataMin) dataMin = v;
        if (v > dataMax) dataMax = v;
      }}
    }});
  }});

  if (!hasData) {{
    dataMin = controlVal;
    dataMax = controlVal;
  }}

  var range = dataMax - dataMin;
  var padding = range * 0.05 || Math.abs(controlVal) * 0.1 || 1;
  // 排氣靜壓等含負值類別允許 Y 軸低於 0；其餘維持 >= 0
  var yFloor = meta.allowNegative ? -Infinity : 0;
  var yMin = Math.max(yFloor, dataMin - padding);
  var yMax = dataMax + padding;

  // 非控制線類別才將控制線數值納入 Y 軸範圍；多子圖類別(化學品/排氣靜壓)依數據自適應縮放
  if (!isMulti && !meta.noControlLines) {{
    yMin = Math.min(yMin, Math.max(0, Math.min(meta.controlVal, meta.warningValNum) - padding));
    yMax = Math.max(yMax, Math.max(meta.controlVal, meta.warningValNum) + padding);
  }}

  if (meta.fixedYMin !== undefined) {{
    yMin = meta.fixedYMin;
  }}
  if (meta.fixedYMax !== undefined) {{
    yMax = meta.fixedYMax;
  }}

  // 供水導電度＜1：Y 軸固定由 0.000 起算（依需求，提供完整基準參考）
  if (metricName === 'water_供水導電度低') {{
    yMin = 0;
  }}

  var prec = Math.pow(10, meta.precision - 1);
  yMin = Math.floor(yMin * prec) / prec;
  yMax = Math.ceil(yMax * prec) / prec;

  const option = {{
    backgroundColor: 'transparent',
    grid: {{
      top: 35,
      // 底部保留：圖例區(legendH) + X軸雙行標籤(~36) + 間距(~22)，避免圖例與 X 軸文字相連/重疊
      bottom: (legendH != null ? legendH + 58 : 85),
      left: 55,
      right: 25,
      containLabel: false
    }},
    xAxis: {{
      type: 'category',
      data: chartTimestamps,
      boundaryGap: false,
      axisLine: {{
        lineStyle: {{ color: '#334155', width: 1.5 }}
      }},
      axisLabel: {{
        color: '#a2b9d2',
        fontSize: 10,
        fontFamily: 'Share Tech Mono, monospace',
        formatter: function(value) {{
          if (!value) return '';
          const parts = value.split(' ');
          const datePart = parts[0].substring(5);
          const timePart = parts[1];
          return `${{datePart}}\n${{timePart}}`;
        }}
      }},
      splitLine: {{
        show: true,
        lineStyle: {{ color: 'rgba(51, 65, 85, 0.25)' }}
      }},
      axisPointer: {{
        show: true,
        type: 'line',
        lineStyle: {{
          color: '#60a5fa',
          width: 1.2,
          type: 'dashed'
        }}
      }}
    }},
    yAxis: {{
      type: 'value',
      min: yMin,
      max: yMax,
      axisLine: {{ show: false }},
      axisLabel: {{
        color: '#a2b9d2',
        fontSize: 10,
        fontFamily: 'Share Tech Mono, monospace',
        formatter: function(v) {{
          return v.toFixed(meta.precision - 1);
        }}
      }},
      splitLine: {{
        show: true,
        lineStyle: {{ color: 'rgba(51, 65, 85, 0.35)' }}
      }}
    }},
    dataZoom: [
      {{
        type: 'slider',
        show: false,
        start: 0,
        end: 100
      }}
    ],
    tooltip: {{
      trigger: 'axis',
      backgroundColor: '#1e293b',
      borderColor: '#334155',
      borderWidth: 1,
      padding: [4, 6],
      textStyle: {{ color: '#f1f5f9' }},
      extraCssText: 'white-space: nowrap; font-size: 11px; padding: 4px 6px; line-height: 1.3; box-shadow: 0 4px 12px rgba(0,0,0,0.5); border-radius: 4px;',
      position: function (point, params, dom, rect, size) {{
        var x = point[0];
        var y = point[1];
        var viewW = size.viewSize[0];
        var viewH = size.viewSize[1];
        var boxW = size.contentSize[0];
        var boxH = size.contentSize[1];
        
        var posX = 0;
        if (x > viewW / 2) {{
          posX = x - boxW - 15;
        }} else {{
          posX = x + 15;
        }}
        
        if (posX < 5) posX = 5;
        if (posX + boxW > viewW - 5) {{
          posX = viewW - boxW - 5;
        }}
        
        var posY = y - boxH / 2;
        if (posY < 5) posY = 5;
        if (posY + boxH > viewH - 5) {{
          posY = viewH - boxH - 5;
        }}
        
        return [posX, posY];
      }},
      formatter: function (params) {{
        if (!params || params.length === 0) return '';
        
        const validParams = params.filter(p => {{
          let val = p.value;
          if (Array.isArray(val)) val = val[1];
          return val !== undefined && val !== null && val !== '' && typeof val !== 'object' && !isNaN(parseFloat(val));
        }});
        if (validParams.length === 0) return '';
        
        let html = `<div style="font-family: inherit; font-size: 11px; line-height: 1.3; color: #f1f5f9; white-space: nowrap;">`;
        const timeVal = validParams[0].axisValue;
        html += `<div style="font-weight: 700; border-bottom: 1px solid #334155; padding-bottom: 3px; margin-bottom: 4px; color: #60a5fa; font-family: var(--mono); letter-spacing: 0.5px;">📅 ${{timeVal}}</div>`;
        
        const sortedParams = [...validParams].sort((a, b) => {{
          if (metricName === '冰機效率' || isWater) {{
            // 數值越低越佳（冰機 kW/RT、供水導電度）→ 由上至下＝品質優→劣
            return a.value - b.value;
          }} else {{
            return b.value - a.value;
          }}
        }});
        
        sortedParams.forEach(p => {{
          var formattedVal = Number(p.value).toFixed(meta.precision);
          html += `
          <div style="display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 2px;">
            <span style="display: flex; align-items: center; gap: 4px; color: #a2b9d2;">
              <span style="display: inline-block; width: 5px; height: 5px; border-radius: 50%; background-color: ${{p.color}}; box-shadow: 0 0 3px ${{p.color}};"></span>
              <span style="font-weight: 600;">${{p.seriesName}}</span>
            </span>
            <span style="font-family: var(--mono); font-weight: 700; color: #ffffff;">${{formattedVal}} <span style="font-size: 8px; color: inherit; font-weight: normal;">${{meta.unit}}</span></span>
          </div>`;
        }});
        html += `</div>`;
        return html;
      }}
    }},
    legend: {{
      show: true,
      bottom: 8,
      left: 'center',
      itemWidth: 10,
      itemHeight: 10,
      itemGap: 12,
      textStyle: {{
        color: '#a2b9d2',
        fontSize: 11,
        fontWeight: 600
      }},
      icon: 'circle',
      data: seriesKeys
    }},
    series: seriesArray
  }};

  return option;
}}

// 單圖套用選項：依該圖線別數量計算圖例高度，使圖例與 X 軸保持間距、不重疊
function _applySingleChart(chart, metricKey) {{
  if (!chart) return;
  var data = chartSeriesData[metricKey] || {{}};
  var keys = Object.keys(data).filter(function(k) {{
    var v = data[k];
    return v && v.some(function(x) {{ return x !== null && x !== undefined; }});
  }});
  var el = chart.getDom ? chart.getDom() : null;
  var legendH = _legendH(keys, el ? el.clientWidth : 700);
  chart.setOption(buildChartOption(metricKey, legendH), true);
}}

function renderChartData() {{
  if (currentMetric === '能源效率') {{
    _applySingleChart(myChart, '冰機效率');
    _applySingleChart(myChartCda, '空壓效率');
    _applySingleChart(myChartPower, '廠區用電');
  }} else if (['大宗化學品','排氣靜壓','供應水質','廢水處理'].indexOf(currentMetric) === -1) {{
    _applySingleChart(myChart, currentMetric);
  }}
}}

// 計算圖例區所需高度(px)：逐一量測每個圖例文字寬度，依實際順序模擬 ECharts 換行。
// 不再使用平均寬度推估，避免靜壓設備名稱長短差異大時，高估造成大片留白或低估造成貼近 X 軸。
function _legendH(labels, w) {{
  w = w || 700;
  if (!labels || labels.length === 0) return 22;
  var availableW = Math.max(120, w - 32); // 左右各保留約 16px
  var canvas = _legendH._canvas || (_legendH._canvas = document.createElement('canvas'));
  var ctx = canvas.getContext && canvas.getContext('2d');
  if (ctx) ctx.font = '600 11px Arial, sans-serif';
  var rows = 1;
  var rowW = 0;
  labels.forEach(function(l) {{
    var s = String(l);
    var textW = ctx ? ctx.measureText(s).width : s.length * 7;
    // icon 10px + icon/text gap約5px + itemGap 12px，再留少量字型差異裕度
    var itemW = Math.ceil(textW) + 29;
    if (rowW > 0 && rowW + itemW > availableW) {{
      rows += 1;
      rowW = itemW;
    }} else {{
      rowW += itemW;
    }}
  }});
  return rows * 22;
}}

// 通用多子圖渲染（大宗化學品 / 排氣靜壓 比照共用）：
// 每個 name 一張圖；化學品以廠區為線，靜壓以「廠區-設備編號」為線。
// 故共用 chemChartInstances 陣列與 zoom 的 'chem-<idx>' 索引(內部 id，非使用者可見)。
function renderMultiCharts(opts) {{
  var container = document.getElementById(opts.container);
  if (!container) return;

  // 銷毀舊圖表實例
  chemChartInstances.forEach(function(c) {{ c.dispose(); }});
  chemChartInstances = [];
  container.innerHTML = '';

  if (!opts.names || opts.names.length === 0) return;

  opts.names.forEach(function(cn) {{
    var metricKey = opts.prefix + cn;
    var metricData = chartSeriesData[metricKey];
    if (!metricData) return;

    // 檢查是否有任何線別(廠區/廠區+濃度)真正擁有數據
    var hasData = Object.keys(metricData).some(function(k) {{
      var vals = metricData[k];
      return vals && vals.some(function(v) {{ return v !== null && v !== undefined; }});
    }});
    if (!hasData) return;

    // 創建圖表卡片
    var card = document.createElement('div');
    card.className = 'chart-card';
    card.style.marginTop = '15px';

    var header = document.createElement('div');
    header.className = 'chart-header';
    var idx = chemChartInstances.length;
    var controlsHtml = '<div class="zoom-controls">' +
        '<span class="zoom-lbl">Zoom:</span>' +
        '<button class="zoom-btn active" onclick="zoomRange(&apos;7d&apos;, this, &apos;chem-' + idx + '&apos;)">All (7d)</button>' +
        '<button class="zoom-btn" onclick="zoomRange(&apos;3d&apos;, this, &apos;chem-' + idx + '&apos;)">3d</button>' +
        '<button class="zoom-btn" onclick="zoomRange(&apos;1d&apos;, this, &apos;chem-' + idx + '&apos;)">1d</button>' +
        '<button class="zoom-btn" onclick="zoomRange(&apos;12h&apos;, this, &apos;chem-' + idx + '&apos;)">12h</button>' +
        '<button class="zoom-btn" onclick="zoomRange(&apos;6h&apos;, this, &apos;chem-' + idx + '&apos;)">6h</button>' +
        '</div>';
    var disp = (opts.displayMap && opts.displayMap[cn]) || cn;
    var titleText = opts.title || (disp + opts.titleSuffix);
    var subtitleText = (opts.subtitle !== undefined) ? opts.subtitle : ('各廠區 ' + disp + opts.subtitleSuffix);
    var subtitleHtml = subtitleText ? '<div class="chart-subtitle">' + subtitleText + '</div>' : '';
    header.innerHTML = '<div class="chart-title-group"><div class="chart-title">' + titleText + '</div>' + subtitleHtml + '</div>' + controlsHtml;
    card.appendChild(header);

    var chartDiv = document.createElement('div');
    chartDiv.className = 'chart-viewport';
    chartDiv.id = opts.prefix + 'chart-' + cn;
    card.appendChild(chartDiv);

    container.appendChild(card);

    // 依線別數量與圖表寬度動態加高容器，讓所有圖例（多行）完整顯示、不需左右翻頁、不與曲線/X軸重疊
    var activeKeys = Object.keys(metricData).filter(function(k) {{
      var v = metricData[k];
      return v && v.some(function(x) {{ return x !== null && x !== undefined; }});
    }});
    var legendH = _legendH(activeKeys, chartDiv.clientWidth || container.clientWidth);
    // 繪圖區基本高約 240px；供水導電度＜1 因低值曲線密集，加高繪圖區(約 520px)以降低重疊、利於判讀
    var plotBase = (cn === '供水導電度低') ? 520 : 240;
    // 固定繪圖區與 X 軸/圖例間距；只有圖例實際行數增加時才等量增高容器。
    chartDiv.style.height = (legendH + 93 + plotBase) + 'px';  // = plotBase + top(35) + bottom(legendH+58)

    // 初始化 ECharts 並設定選項
    var chart = echarts.init(chartDiv);
    chemChartInstances.push(chart);
    var option = buildChartOption(metricKey, legendH);
    if (option) chart.setOption(option, true);
  }});
}}

function renderChemCharts() {{
  renderMultiCharts({{
    names: chemNames, prefix: 'chem_', container: 'trend-chem-container',
    titleSuffix: ' 液位趨勢圖', subtitleSuffix: ' 大宗化學品液位變化(cm)',
    displayMap: CHEM_DISPLAY
  }});
}}

function renderStaticCharts() {{
  renderMultiCharts({{
    names: staticNames, prefix: 'static_', container: 'trend-static-container',
    titleSuffix: ' 趨勢圖', subtitleSuffix: ' 排氣靜壓變化(Pa)'
  }});
}}

function renderWaterCharts() {{
  renderMultiCharts({{
    names: waterNames, prefix: 'water_', container: 'trend-water-container',
    titleSuffix: ' 趨勢圖', subtitleSuffix: '（導電度越低代表品質越好；電阻已換算為導電度 µS/cm）',
    displayMap: WATER_DISPLAY
  }});
}}

function renderWasteCharts() {{
  renderMultiCharts({{
    names: wasteNames, prefix: 'waste_', container: 'trend-waste-container',
    title: '廢水處理出口pH趨勢圖', subtitle: '',
    titleSuffix: ' 趨勢圖', subtitleSuffix: '',
    displayMap: WASTE_DISPLAY
  }});
}}

function switchMetric(metric, btn) {{
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');

  const wipMetrics = [];

  var chartCard = document.getElementById('trend-chart-card');
  var cdaCard = document.getElementById('trend-chart-card-cda');
  var powerCard = document.getElementById('trend-chart-card-power');
  var wipCard = document.getElementById('trend-wip');
  var chemContainer = document.getElementById('trend-chem-container');
  var staticContainer = document.getElementById('trend-static-container');
  var waterContainer = document.getElementById('trend-water-container');
  var wasteContainer = document.getElementById('trend-waste-container');

  // 隱藏所有面板；離開多子圖分頁時同步隱藏 TOP 按鈕
  if (chartCard) chartCard.style.display = 'none';
  if (cdaCard) cdaCard.style.display = 'none';
  if (powerCard) powerCard.style.display = 'none';
  if (wipCard) wipCard.style.display = 'none';
  if (chemContainer) chemContainer.style.display = 'none';
  if (staticContainer) staticContainer.style.display = 'none';
  if (waterContainer) waterContainer.style.display = 'none';
  if (wasteContainer) wasteContainer.style.display = 'none';
  _syncTopBtn();

  if (wipMetrics.indexOf(metric) !== -1) {{
    if (wipCard) wipCard.style.display = 'flex';
    currentMetric = metric;
    return;
  }}

  if (metric === '大宗化學品') {{
    if (chemContainer) chemContainer.style.display = 'block';
    currentMetric = metric;
    renderChemCharts();
    _syncTopBtn();
    return;
  }}

  if (metric === '排氣靜壓') {{
    if (staticContainer) staticContainer.style.display = 'block';
    currentMetric = metric;
    renderStaticCharts();
    _syncTopBtn();
    return;
  }}

  if (metric === '供應水質') {{
    if (waterContainer) waterContainer.style.display = 'block';
    currentMetric = metric;
    renderWaterCharts();
    _syncTopBtn();
    return;
  }}

  if (metric === '廢水處理') {{
    if (wasteContainer) wasteContainer.style.display = 'block';
    currentMetric = metric;
    renderWasteCharts();
    _syncTopBtn();
    return;
  }}

  // 能源效率 / 運轉率 等正常圖表
  if (chartCard) {{
    chartCard.style.display = 'block';
    setTimeout(function() {{ if (myChart) myChart.resize(); }}, 50);
  }}
  
  if (metric === '能源效率') {{
    if (cdaCard) {{
      cdaCard.style.display = 'block';
      setTimeout(function() {{ if (myChartCda) myChartCda.resize(); }}, 50);
    }}
    if (powerCard) {{
      powerCard.style.display = 'block';
      setTimeout(function() {{ if (myChartPower) myChartPower.resize(); }}, 50);
    }}
  }}

  if (currentMetric === metric) return;
  currentMetric = metric;

  updateMetricUI();
  renderChartData();
  
  var btn7d = document.getElementById('btn-7d');
  var btn7dCda = document.getElementById('btn-7d-cda');
  var btn7dPower = document.getElementById('btn-7d-power');
  if (btn7d) zoomRange('7d', btn7d, 'main');
  if (btn7dCda && metric === '能源效率') zoomRange('7d', btn7dCda, 'cda');
  if (btn7dPower && metric === '能源效率') zoomRange('7d', btn7dPower, 'power');
}}

function zoomRange(range, btn, target) {{
  var container = btn.closest('.chart-header');
  if (container) {{
    container.querySelectorAll('.zoom-btn').forEach(b => b.classList.remove('active'));
  }} else {{
    document.querySelectorAll('.zoom-btn').forEach(b => b.classList.remove('active'));
  }}
  btn.classList.add('active');

  const totalPoints = chartTimestamps.length;
  if (!totalPoints) return;
  const endIdx = totalPoints - 1;
  let startIdx = 0;

  if (range === '7d') {{
    startIdx = 0;
  }} else if (range === '3d') {{
    startIdx = Math.max(0, endIdx - 72);
  }} else if (range === '1d') {{
    startIdx = Math.max(0, endIdx - 24);
  }} else if (range === '12h') {{
    startIdx = Math.max(0, endIdx - 12);
  }} else if (range === '6h') {{
    startIdx = Math.max(0, endIdx - 6);
  }}

  var targetChart;
  if (target === 'power') {{
    targetChart = myChartPower;
  }} else if (target === 'cda') {{
    targetChart = myChartCda;
  }} else if (target === 'main') {{
    targetChart = myChart;
  }} else if (typeof target === 'string' && target.startsWith('chem-')) {{
    var idx = parseInt(target.split('-')[1]);
    targetChart = chemChartInstances[idx];
  }}
  if (targetChart) {{
    targetChart.dispatchAction({{
      type: 'dataZoom',
      startValue: startIdx,
      endValue: endIdx
    }});
  }}
}}

window.addEventListener('resize', function() {{
  if (myChart) myChart.resize();
  if (myChartCda) myChartCda.resize();
  if (myChartPower) myChartPower.resize();
  chemChartInstances.forEach(function(c) {{ c.resize(); }});
}});

// ── KF1 共用 Tooltip：桌機 hover、手機 touch/click、鍵盤 focus ──────────────
var alarmTipTimer = null;
var alarmTipPinned = false;
function initAlarmTooltips() {{
  var tip = document.getElementById('alarm-touch-tip');
  if (!tip) {{
    tip = document.createElement('div');
    tip.id = 'alarm-touch-tip';
    tip.className = 'alarm-touch-tip';
    tip.setAttribute('role', 'status');
    tip.setAttribute('aria-live', 'polite');
    document.body.appendChild(tip);
  }}

  function hideAlarmTip(force) {{
    if (!force && alarmTipPinned) return;
    alarmTipPinned = false;
    clearTimeout(alarmTipTimer);
    tip.classList.remove('show');
  }}

  function showAlarmTip(el, pinned) {{
    var text = el.getAttribute('data-alarm-tip');
    if (!text) return;
    clearTimeout(alarmTipTimer);
    alarmTipPinned = !!pinned;
    tip.textContent = text;
    tip.classList.add('show');
    var rect = el.getBoundingClientRect();
    var tr = tip.getBoundingClientRect();
    var left = rect.left + rect.width / 2 - tr.width / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - tr.width - 8));
    var top = rect.top - tr.height - 8;
    if (top < 8) top = Math.min(window.innerHeight - tr.height - 8, rect.bottom + 8);
    tip.style.left = Math.round(left) + 'px';
    tip.style.top = Math.round(top) + 'px';
    if (pinned) alarmTipTimer = setTimeout(function(){{ hideAlarmTip(true); }}, 3500);
  }}

  document.querySelectorAll('#pc-KF1 [data-alarm-tip]').forEach(function(el) {{
    el.addEventListener('mouseenter', function(){{ showAlarmTip(el, false); }});
    el.addEventListener('mouseleave', function(){{ hideAlarmTip(false); }});
    el.addEventListener('focus', function(){{ showAlarmTip(el, false); }});
    el.addEventListener('blur', function(){{ hideAlarmTip(true); }});
    el.addEventListener('click', function(ev){{
      ev.stopPropagation();
      showAlarmTip(el, true);
    }});
    el.addEventListener('keydown', function(ev){{
      if (ev.key === 'Enter' || ev.key === ' ') {{
        ev.preventDefault();
        showAlarmTip(el, true);
      }}
      if (ev.key === 'Escape') hideAlarmTip(true);
    }});
  }});
  document.addEventListener('click', function(){{ hideAlarmTip(true); }});
  window.addEventListener('scroll', function(){{ hideAlarmTip(true); }}, true);
}}

window.addEventListener('DOMContentLoaded',function(){{
  initAlarmTooltips();
  var s=localStorage.getItem('ap');
  var f=document.querySelector('.nav-btn');
  if(!f)return;
  var fid=f.getAttribute('data-plant');
  if(s&&document.getElementById('pc-'+s))switchPlant(s);
  else if(fid)switchPlant(fid);
}});

// ── TOP 按鈕：所有頁面採用相同的顯示標準 ─────────────────────────────
// body/html 為 overflow:hidden，滾動發生在 .scroll-area 內，
// 必須監聽 .scroll-area 的 scroll 事件，並讀取 scrollTop 而非 window.scrollY
const TOP_BUTTON_SCROLL_THRESHOLD = 300;
function _syncTopBtn() {{
  var btn = document.getElementById('top-btn');
  if (!btn) return;
  var sa = document.querySelector('.scroll-area');
  var st = sa ? sa.scrollTop : window.scrollY;
  if (st > TOP_BUTTON_SCROLL_THRESHOLD) {{
    btn.classList.add('visible');
  }} else {{
    btn.classList.remove('visible');
  }}
}}
(function() {{
  var sa = document.querySelector('.scroll-area');
  if (sa) sa.addEventListener('scroll', _syncTopBtn, {{ passive: true }});
}})();

</script>
<button id="top-btn" onclick="var sa=document.querySelector('.scroll-area');if(sa)sa.scrollTo({{top:0,behavior:'smooth'}});else window.scrollTo({{top:0,behavior:'smooth'}})">▲<br>TOP</button>
</body>
</html>
"""

    local_ip = get_local_ip()
    full_html_with_ip = full_html.replace("{LOCAL_IP}", local_ip)

    master_key  = get_random_bytes(32)
    payload_iv  = get_random_bytes(16)
    cipher      = AES.new(master_key, AES.MODE_CBC, payload_iv)
    enc_payload = cipher.encrypt(pad(full_html_with_ip.encode("utf-8"), AES.block_size))

    # 編譯並加密品質與能效趨勢數據
    print("正在編譯品質與能效歷史趨勢數據...")
    chart_data = compile_quality_data(script_dir, force_base_time=latest_hour)
    chart_json_str = json.dumps(chart_data, ensure_ascii=False)
    chart_iv = get_random_bytes(16)
    cipher_chart = AES.new(master_key, AES.MODE_CBC, chart_iv)
    enc_chart = cipher_chart.encrypt(pad(chart_json_str.encode("utf-8"), AES.block_size))

    salt_file = os.path.join(os.path.dirname(__file__) or ".", "salt.bin")
    global_salt = None
    if os.path.exists(salt_file):
        try:
            with open(salt_file, "rb") as sf:
                s = sf.read()
                if len(s) == 16:
                    global_salt = s
        except Exception:
            pass
    if global_salt is None:
        existing_data_path = os.path.join(
            os.path.dirname(os.path.abspath(output_path)) or ".", "data.enc")
        if os.path.exists(existing_data_path):
            try:
                with open(existing_data_path, "r", encoding="utf-8") as ef:
                    ex = json.load(ef)
                    s_hex = ex.get("salt", "")
                    if len(s_hex) == 32:
                        global_salt = bytes.fromhex(s_hex)
            except Exception:
                pass
    if global_salt is None:
        global_salt = get_random_bytes(16)
    try:
        with open(salt_file, "wb") as sf:
            sf.write(global_salt)
    except Exception as e:
        print(f"[WARN] salt.bin 寫入失敗: {e}")

    accounts_file = os.path.join(os.path.dirname(__file__) or ".", "accounts.json")
    key_safes = {}
    if os.path.exists(accounts_file):
        with open(accounts_file, "r", encoding="utf-8") as af:
            try:
                acc = json.load(af)
                for pwd in acc.get("passwords", []):
                    dk  = PBKDF2(pwd, global_salt, dkLen=32, count=100000, hmac_hash_module=SHA256)
                    iid = dk[:16].hex()
                    enc = dk[16:]
                    biv = get_random_bytes(16)
                    bc  = AES.new(enc, AES.MODE_CBC, biv)
                    em  = bc.encrypt(pad(master_key, AES.block_size))
                    key_safes[iid] = {"iv": biv.hex(), "enc_master": em.hex()}
                print(f"[OK] 已打包 {len(key_safes)} 組密碼箱。")
            except Exception as e:
                print(f"[WARN] accounts.json 處理錯誤: {e}")
    else:
        print("[WARN] 找不到 accounts.json！")

    output_dir = os.path.dirname(os.path.abspath(output_path)) or "."

    data_path = os.path.join(output_dir, "data.enc")
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump({
            "v": 2,
            "updated": generation_time_iso,
            "salt": global_salt.hex(),
            "key_safes": key_safes,
            "payload_iv": base64.b64encode(payload_iv).decode(),
            "payload": base64.b64encode(enc_payload).decode(),
            "chart_iv": base64.b64encode(chart_iv).decode(),
            "chart_payload": base64.b64encode(enc_chart).decode(),
        }, f, separators=(",", ":"))
    print(f"[OK] data.enc 已產出 -> {os.path.basename(data_path)}")

    custom_index_html = STATIC_INDEX_HTML.replace("var API_BASE = null;", f'var API_BASE = "http://{local_ip}:47313";')
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(custom_index_html)
    print(f"[OK] index.html (靜態外殼) -> {os.path.basename(output_path)}")

    health_path = os.path.join(os.path.dirname(os.path.abspath(output_path)), "health.json")
    with open(health_path, 'w', encoding='utf-8') as hf:
        hf.write(json.dumps({"updated": generation_time_iso}, ensure_ascii=False))
    print(f"[OK] health.json 已更新 -> {health_path}")


if __name__ == "__main__":
    import sys
    candidates = ["latest_data_backup.csv", "test_data.csv"]
    loaded = False
    for csv_file in candidates:
        if os.path.exists(csv_file):
            print(f"本地測試模式：使用 {csv_file}")
            test_df = pd.read_csv(csv_file)
            create_status_dashboard(test_df, "index.html")
            print("完成。")
            loaded = True
            break
    if not loaded:
        print("找不到可用的 CSV 資料（latest_data_backup.csv / test_data.csv），請先執行 main.py 或 db_test.py 產生備份資料。")
