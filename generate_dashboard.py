import pandas as pd
from datetime import datetime
import os
import json
import base64
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Hash import SHA256
from Crypto.Util.Padding import pad

# 定義設備類別與其對應字串、圖示、英文敘述
CATEGORIES = [
    {"name": "冰機",     "pattern": "CHU",   "icon": "❄️", "desc": "Chiller"},
    {"name": "空壓",     "pattern": "CDA",   "icon": "💨", "desc": "Compressed Air"},
    {"name": "外氣空調箱","pattern": "MAU",   "icon": "🌀", "desc": "MAU"},
    {"name": "酸排氣",   "pattern": "ASCR",  "icon": "🧪", "desc": "Acid Scrubber"},
    {"name": "鹼排氣",   "pattern": "BSCR",  "icon": "⚗️", "desc": "Base Scrubber"},
    {"name": "有機排氣", "pattern": "VSCR",  "icon": "🍃", "desc": "VOC Scrubber"},
    {"name": "熱排氣",   "pattern": "HSCR",  "icon": "🔥", "desc": "Hot Scrubber"},
    {"name": "乾式集塵", "pattern": "DUST",  "exclude": "WDUST", "icon": "🧹", "desc": "Dry Dust"},
    {"name": "溼式集塵", "pattern": "WDUST", "icon": "💧", "desc": "Wet Dust"},
    {"name": "製程冷卻水","pattern": "PCW",   "icon": "🌊", "desc": "PCW"},
    {"name": "其他設備", "pattern": None,    "icon": "⚙️", "desc": "Other"},
]

def classify_equipment(eqno: str) -> str:
    if not isinstance(eqno, str):
        return "其他設備"
    u = eqno.strip().upper()
    if "CHU"   in u: return "冰機"
    if "CDA"   in u: return "空壓"
    if "MAU"   in u: return "外氣空調箱"
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
    - 首次執行：建立 known_equipment.json 做為基準，不標記任何設備為 NEW
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


def create_status_dashboard(df: pd.DataFrame, output_path: str = "index.html"):
    """
    工業風格 SCADA 儀表板 — 手機優先版本
    - 工業 SCADA 配色 (深色 + 螢光綠/紅 LED)
    - 所有元素 white-space:nowrap，不換行
    - 手機三欄卡片、橫向捲動廠區切換列
    - KPI 2×2 格，桌機展開為 4×1
    - Header/Nav 固定，內容區域垂直捲動
    """
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

    REQUIRED_PLANTS = ["T2A", "S2A", "PCB", "S2", "S3", "HJ1", "HJ2", "LC2", "LC3"]
    display_plants  = list(REQUIRED_PLANTS)
    for p in all_plants:
        if p not in display_plants:
            display_plants.append(p)

    fallback_plant = all_plants[0] if all_plants else None
    if fallback_plant:
        print(f"以 {fallback_plant} 資料做無資料廠區替代展示")

    # 清理舊廠區個別 HTML
    for p in all_plants:
        old = os.path.join(
            os.path.dirname(output_path) if os.path.dirname(output_path) else "",
            f"EFplant_Dashboard_{p}.html"
        )
        if os.path.exists(old):
            try: os.remove(old)
            except: pass

    now_dt              = datetime.now()
    # 下次更新 = 下一個整點（:00），例如現在 16:35 → 顯示 17:00
    from datetime import timedelta
    next_hour           = (now_dt + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    next_update_str     = next_hour.strftime("%m/%d %H:%M")
    generation_time_iso = now_dt.strftime('%Y-%m-%dT%H:%M:%S')

    # ── 全自動新設備偵測 ──────────────────────────────────────────────────────
    script_dir = os.path.dirname(os.path.abspath(__file__)) or "."
    new_eqnos  = update_known_equipment(df_unique, script_dir)

    # ── 導覽列 ──────────────────────────────────────────────────────────────
    nav_btns = ""
    for idx, plant in enumerate(display_plants):
        ac = "active" if idx == 0 else ""
        nav_btns += (
            f'<button class="nav-btn {ac}" data-plant="{plant}" '
            f'onclick="switchPlant(\'{plant}\')">{plant}</button>\n        '
        )

    nav_bar_html = (
        f'<nav class="plant-nav">\n        '
        f'{nav_btns}'
        f'<a href="chart.html" class="chart-link">&#x1F4CA;</a>\n    </nav>'
    )

    # ── KPI 區（固定於頂部，不進入捲動區）──────────────────────────────────
    kpi_zone_html = '<div class="kpi-zone">\n'
    plant_data_cache = {}   # 暫存各廠區資料供後續設備區使用

    for idx, plant in enumerate(display_plants):
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
        running_eq = int(sum(df_plant['VALUE'] >= 10.0))
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

    # ── 廠區容器（捲動區，僅含設備群組）────────────────────────────────────
    plant_html = ""
    for idx, plant in enumerate(display_plants):
        cached   = plant_data_cache[plant]
        df_plant = cached['df']
        is_mock  = cached['is_mock']

        d_style = "" if idx == 0 else 'style="display:none"'
        ac      = "active" if idx == 0 else ""

        if is_mock:
            # 此廠區在 MSSQL 尚無資料 → 顯示施工中頁面
            # 日後 MSSQL 接通後會自動切換為真實資料
            plant_html += f"""
<div id="pc-{plant}" class="plant-container {ac}" {d_style}>
  <div class="wip-body">
    <div class="wip-icon">&#x1F6A7;</div>
    <div class="wip-title">施工中 / Under Construction</div>
    <div class="wip-msg">此廠區數據尚未接入系統<br>待 MSSQL 資料接通後將自動上線</div>
  </div>
</div>"""
            continue

        grouped = {cat['name']: [] for cat in CATEGORIES}
        for _, row in df_plant.iterrows():
            grouped[classify_equipment(row['EQNO'])].append(row)

        plant_html += f"""
<div id="pc-{plant}" class="plant-container {ac}" {d_style}>
  <div class="eq-area">"""

        # ── 自動展開：「其他設備」依 EQNO 前綴自動分群 ──────────────────────
        # 已知類別照 CATEGORIES 順序顯示；
        # 落入「其他設備」的設備依前綴字母自動拆成子群，日後新設備類型也能整齊顯示。
        import re as _re
        from collections import defaultdict as _dd
        _disp = []
        for _c in CATEGORIES:
            if _c['name'] != '其他設備':
                _disp.append((_c, grouped[_c['name']]))
            else:
                _oth = grouped['其他設備']
                if _oth:
                    _pm = _dd(list)
                    for _r in _oth:
                        _m = _re.match(r'^([A-Za-z]+(?:_[A-Za-z]+)*)',
                                       str(_r['EQNO']).strip())
                        _pm[(_m.group(1).upper() if _m else 'UNK')].append(_r)
                    for _pfx in sorted(_pm):
                        _disp.append(({'name': _pfx, 'icon': '⚙️', 'desc': _pfx},
                                      _pm[_pfx]))

        for cat, items in _disp:
            cname = cat['name']
            if not items:
                continue
            ct    = len(items)
            cr    = sum(r['VALUE'] >= 10.0 for r in items)

            plant_html += f"""
    <section class="grp">
      <div class="grp-hdr">
        <span class="gi">{cat['icon']}</span>
        <span class="gn">{cname}</span>
        <span class="gd">{cat['desc']}</span>
        <span class="gc">{cr}/{ct}</span>
      </div>
      <div class="cards-grid">"""

            if ct == 0:
                plant_html += f'<div class="no-dev">-- NO {cat["desc"].upper()} DATA --</div>'
            else:
                for row in items:
                    eq  = str(row['EQNO'])
                    val = row['VALUE']
                    eqd = eq.replace(fallback_plant, plant) if (is_mock and fallback_plant) else eq
                    sc     = "running" if val >= 10.0 else "stopped"
                    sw     = "RUN"    if val >= 10.0 else "STOP"
                    is_new = (not is_mock) and ((str(plant), str(row['EQNO'])) in new_eqnos)
                    nb     = ' <span class="new-badge">NEW</span>' if is_new else ''
                    plant_html += f"""
        <div class="card {sc}">
          <div class="eq-id">{eqd}{nb}</div>
          <div class="srow"><div class="led"></div><span class="stxt">{sw}</span></div>
        </div>"""

            plant_html += "\n      </div>\n    </section>"

        plant_html += "\n  </div>\n</div>"

    # ── 完整內層 HTML (將被 AES 加密) ───────────────────────────────────────
    full_html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,viewport-fit=cover">
<title>EFplant</title>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&display=swap" rel="stylesheet">
<style>
:root{{
  --bg:#080c10; --sf:#0c1420; --sf2:#101928;
  --bd:#1a2d45; --bd2:#243d5a;
  --run:#00e676; --stop:#ff3d00;
  --blue:#00aaff; --amb:#ffa726;
  --tx:#c0d0e0; --dim:#3a5570;
  --mono:'Share Tech Mono','Courier New',monospace;
}}
*{{box-sizing:border-box;margin:0;padding:0;}}
html,body{{height:100%;overflow:hidden;}}
body{{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:var(--bg);color:var(--tx);
  display:flex;flex-direction:column;
  height:100vh;height:100dvh;
}}
/* HEADER */
header{{
  display:flex;align-items:center;
  padding:7px 14px;gap:10px;
  border-bottom:1px solid var(--bd);
  background:var(--sf);flex-shrink:0;
}}
.brand{{display:flex;align-items:baseline;gap:8px;flex-shrink:0;}}
.bname{{
  font-family:var(--mono);font-size:.95rem;
  color:var(--blue);letter-spacing:3px;white-space:nowrap;
}}
.bsub{{font-size:.6rem;color:var(--tx);letter-spacing:1px;white-space:nowrap;}}
.upd{{
  margin-left:auto;flex-shrink:0;
  display:flex;align-items:center;gap:6px;
  font-family:var(--mono);font-size:.65rem;
  color:var(--amb);white-space:nowrap;
}}
.pulse{{
  width:6px;height:6px;border-radius:50%;
  background:var(--run);flex-shrink:0;
  animation:blink 2s infinite;
}}
/* BRAIN STATUS INDICATOR */
.brain{{
  font-size:1.1rem;cursor:help;flex-shrink:0;margin-left:6px;
  filter:grayscale(1) brightness(2.2);
  transition:filter .5s;user-select:none;
}}
.brain.ok{{
  filter:grayscale(1) brightness(2.2)
    drop-shadow(0 0 4px #00e676)
    drop-shadow(0 0 10px #00e676)
    drop-shadow(0 0 22px #00e676)
    drop-shadow(0 0 40px #00e676);
}}
.brain.warn{{
  filter:grayscale(1) brightness(2.2)
    drop-shadow(0 0 4px #ffa726)
    drop-shadow(0 0 10px #ffa726)
    drop-shadow(0 0 22px #ffa726)
    drop-shadow(0 0 40px #ffa726);
}}
.brain.bad{{
  filter:grayscale(1) brightness(2.2)
    drop-shadow(0 0 4px #ff3d00)
    drop-shadow(0 0 10px #ff3d00)
    drop-shadow(0 0 22px #ff3d00)
    drop-shadow(0 0 40px #ff3d00);
  animation:blink 1s infinite;
}}
/* KPI WIP (no data) */
.kpi-wip{{font-size:1.2rem;color:var(--dim);}}
/* UNDER CONSTRUCTION */
.wip-body{{
  display:flex;flex-direction:column;align-items:center;
  justify-content:center;min-height:55vh;gap:14px;
  text-align:center;padding:40px 20px;
}}
.wip-icon{{font-size:3rem;line-height:1;}}
.wip-title{{
  font-family:var(--mono);font-size:.85rem;
  color:var(--dim);letter-spacing:3px;text-transform:uppercase;
}}
.wip-msg{{
  font-size:.7rem;color:var(--dim);line-height:2;
  border:1px dashed var(--bd);padding:12px 24px;
  font-family:var(--mono);letter-spacing:.5px;
}}
/* NEW EQUIPMENT BADGE */
.new-badge{{
  font-size:.5rem;font-family:var(--mono);
  color:var(--amb);border:1px solid var(--amb);
  padding:0 3px;margin-left:3px;
  vertical-align:middle;letter-spacing:.5px;
  animation:blink 2s infinite;
}}
/* REFRESH BANNER */
.refresh-banner{{
  display:none;align-items:center;justify-content:center;gap:10px;
  padding:6px 14px;flex-shrink:0;
  background:rgba(0,170,255,.1);border-bottom:1px solid var(--blue);
  font-family:var(--mono);font-size:.72rem;color:var(--blue);white-space:nowrap;
}}
.refresh-btn{{
  padding:3px 14px;background:var(--blue);border:none;border-radius:2px;
  color:#000;font-family:var(--mono);font-size:.72rem;font-weight:700;
  cursor:pointer;white-space:nowrap;letter-spacing:1px;
}}
.refresh-btn:hover{{opacity:.85;}}
/* PLANT NAV */
.plant-nav{{
  display:flex;flex-wrap:nowrap;
  overflow-x:auto;overflow-y:hidden;
  scrollbar-width:none;-webkit-overflow-scrolling:touch;
  padding:6px 14px;gap:5px;
  border-bottom:1px solid var(--bd);
  background:var(--bg);flex-shrink:0;align-items:center;
}}
.plant-nav::-webkit-scrollbar{{display:none;}}
.nav-btn{{
  flex-shrink:0;padding:4px 12px;
  background:transparent;border:1px solid var(--bd2);border-radius:2px;
  color:var(--dim);font-family:var(--mono);
  font-size:.78rem;font-weight:700;letter-spacing:1px;
  cursor:pointer;white-space:nowrap;transition:all .15s;
}}
.nav-btn.active{{background:var(--blue);border-color:var(--blue);color:#000;}}
.nav-btn:hover:not(.active){{border-color:var(--tx);color:var(--tx);}}
.chart-link{{
  margin-left:auto;flex-shrink:0;padding:4px 10px;
  border:1px solid var(--bd2);border-radius:2px;
  color:var(--dim);text-decoration:none;font-size:.82rem;
  white-space:nowrap;transition:all .15s;
}}
.chart-link:hover{{color:var(--tx);border-color:var(--tx);}}
/* KPI ZONE - fixed, never scrolls */
.kpi-zone{{
  flex-shrink:0;
  padding:8px 12px;
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
  background:var(--sf);border:1px solid var(--bd);border-left:3px solid;
  padding:8px 10px;display:flex;flex-direction:column;gap:2px;overflow:hidden;
}}
.kpi-card.kpi-total{{border-left-color:var(--blue);}}
.kpi-card.kpi-run{{border-left-color:var(--run);}}
.kpi-card.kpi-stop{{border-left-color:var(--stop);}}
.kpi-card.kpi-rate{{border-left-color:var(--amb);}}
.kpi-lbl{{
  font-size:.58rem;color:var(--tx);
  text-transform:uppercase;letter-spacing:.8px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}}
.kpi-val{{
  font-family:var(--mono);font-size:1.5rem;
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
.gi{{font-size:.82rem;flex-shrink:0;line-height:1;}}
.gn{{
  font-size:.7rem;font-weight:700;color:var(--tx);
  white-space:nowrap;text-transform:uppercase;letter-spacing:1.5px;flex-shrink:0;
}}
.gd{{
  font-family:var(--mono);font-size:.6rem;color:var(--tx);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;
}}
.gc{{
  margin-left:auto;font-family:var(--mono);
  font-size:.65rem;color:var(--blue);white-space:nowrap;flex-shrink:0;
}}
/* CARDS */
.cards-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:5px;}}
@media(min-width:600px){{.cards-grid{{grid-template-columns:repeat(auto-fill,minmax(140px,1fr));}}}}
.card{{
  background:var(--sf);border:1px solid var(--bd);
  border-top:2px solid transparent;
  padding:7px 8px;display:flex;flex-direction:column;gap:4px;
  overflow:hidden;cursor:default;transition:background .15s;
}}
.card:hover{{background:var(--sf2);}}
.card.running{{border-top-color:var(--run);}}
.card.stopped{{border-top-color:var(--stop);}}
.eq-id{{
  font-family:var(--mono);font-size:.7rem;font-weight:700;
  color:var(--tx);white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;line-height:1.2;
}}
.srow{{display:flex;align-items:center;gap:5px;}}
.led{{width:7px;height:7px;border-radius:50%;flex-shrink:0;}}
.card.running .led{{background:var(--run);box-shadow:0 0 5px var(--run);animation:blink 2s infinite;}}
.card.stopped .led{{background:var(--stop);opacity:.5;}}
.stxt{{font-family:var(--mono);font-size:.6rem;white-space:nowrap;line-height:1;}}
.card.running .stxt{{color:var(--run);}}
.card.stopped .stxt{{color:var(--stop);}}
.no-dev{{
  grid-column:1/-1;padding:10px;text-align:center;
  color:var(--dim);font-size:.68rem;
  border:1px dashed var(--bd);font-family:var(--mono);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}}
.demo-lbl{{
  font-family:var(--mono);font-size:.56rem;color:var(--amb);
  border:1px solid var(--amb);padding:1px 4px;
}}
footer{{
  margin-top:16px;padding:8px 0;
  border-top:1px solid var(--bd);text-align:center;
  color:var(--dim);font-size:.58rem;font-family:var(--mono);
  letter-spacing:1px;white-space:nowrap;
}}
@keyframes blink{{0%,100%{{opacity:1;}}50%{{opacity:.3;}}}}
</style>
</head>
<body>
<header>
  <div class="brand">
    <span class="bname">EFplant</span>
    <span class="bsub">廠務設備智慧整合</span>
    <span class="brain" id="brain" title="checking...">&#x1F9E0;</span>
  </div>
  <div class="upd">
    <span class="pulse"></span>
    NEXT: {next_update_str}
  </div>
</header>
{nav_bar_html}
{kpi_zone_html}
<div class="refresh-banner" id="refresh-banner">
  &#x1F504; NEW DATA AVAILABLE &nbsp;
  <button class="refresh-btn" onclick="location.reload()">&#x21BB; REFRESH NOW</button>
</div>
<div class="scroll-area">
{plant_html}
  <footer>EFplant FMCS Smart Plant Integration &copy; 2026</footer>
</div>
<script>
// ── Brain status & auto-update detection ────────────────────────────
const GEN_TIME = new Date("{generation_time_iso}");
const INTERVAL_MIN = 60, GRACE_MIN = 15;

function updateBrain(){{
  const minPast=(Date.now()-GEN_TIME)/60000;
  const b=document.getElementById('brain');
  if(!b)return;
  if(minPast<INTERVAL_MIN+GRACE_MIN){{
    b.className='brain ok';
    b.title='DATA OK — generated '+GEN_TIME.toLocaleTimeString()+' | Next update ~'+new Date(GEN_TIME.getTime()+(INTERVAL_MIN+GRACE_MIN)*60000).toLocaleTimeString();
  }}else if(minPast<INTERVAL_MIN*2+GRACE_MIN){{
    b.className='brain warn';
    b.title='UPDATE OVERDUE — refresh page for new data';
  }}else{{
    b.className='brain bad';
    b.title='PIPELINE BROKEN — check main.py is running';
  }}
}}

async function checkHealth(){{
  try{{
    const r=await fetch('health.json?_='+Date.now(),{{cache:'no-store'}});
    if(!r.ok)return;
    const d=await r.json();
    if(new Date(d.updated)>GEN_TIME){{
      document.getElementById('refresh-banner').style.display='flex';
      const b=document.getElementById('brain');
      if(b&&b.className.includes('bad')){{b.className='brain warn';b.title='Newer data on server — refresh to load';}}
    }}
  }}catch(e){{}}
}}

updateBrain();
setTimeout(checkHealth,15000);
setInterval(function(){{updateBrain();checkHealth();}},60000);
// ────────────────────────────────────────────────────────────────────

function switchPlant(id){{
  document.querySelectorAll('.plant-container').forEach(c=>{{c.style.display='none';c.classList.remove('active');}});
  document.querySelectorAll('.kpi-set').forEach(k=>{{k.style.display='none';}});
  document.querySelectorAll('.nav-btn').forEach(b=>b.classList.remove('active'));
  var t=document.getElementById('pc-'+id);
  if(t){{t.style.display='block';t.classList.add('active');}}
  var k=document.getElementById('kpi-'+id);
  if(k){{k.style.display='';}}
  var b=document.querySelector('[data-plant="'+id+'"]');
  if(b){{b.classList.add('active');b.scrollIntoView({{behavior:'smooth',block:'nearest',inline:'center'}});}}
  localStorage.setItem('ap',id);
}}
window.addEventListener('DOMContentLoaded',function(){{
  var s=localStorage.getItem('ap');
  var f=document.querySelector('.nav-btn');
  if(!f)return;
  var fid=f.getAttribute('data-plant');
  if(s&&document.getElementById('pc-'+s))switchPlant(s);
  else if(fid)switchPlant(fid);
}});

</script>
</body>
</html>
"""

    # ── AES 多帳號安全加密 ───────────────────────────────────────────────────
    print("執行靜態多帳號 AES 加密打包...")

    master_key  = get_random_bytes(32)
    payload_iv  = get_random_bytes(16)
    cipher      = AES.new(master_key, AES.MODE_CBC, payload_iv)
    enc_payload = cipher.encrypt(pad(full_html.encode('utf-8'), AES.block_size))

    payload_b64    = base64.b64encode(enc_payload).decode()
    payload_iv_b64 = base64.b64encode(payload_iv).decode()

    accounts_file = os.path.join(os.path.dirname(__file__) or ".", "accounts.json")
    key_safes     = {}
    global_salt   = get_random_bytes(16)
    salt_hex      = global_salt.hex()

    if os.path.exists(accounts_file):
        with open(accounts_file, 'r', encoding='utf-8') as af:
            try:
                data      = json.load(af)
                passwords = data.get("passwords", [])
                for pwd in passwords:
                    dk       = PBKDF2(pwd, global_salt, dkLen=32, count=100000, hmac_hash_module=SHA256)
                    index_id = dk[:16].hex()
                    enc_key  = dk[16:]
                    box_iv   = get_random_bytes(16)
                    bc       = AES.new(enc_key, AES.MODE_CBC, box_iv)
                    enc_mk   = bc.encrypt(pad(master_key, AES.block_size))
                    key_safes[index_id] = {"iv": box_iv.hex(), "enc_master": enc_mk.hex()}
                print(f"[OK] 已打包 {len(passwords)} 組密碼箱。")
            except Exception as e:
                print(f"[WARN] accounts.json 處理錯誤: {e}")
    else:
        print("[WARN] 找不到 accounts.json！")

    # ── 登入介面外殼 HTML ────────────────────────────────────────────────────
    wrapper_html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>EFplant - Secure Access</title>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/crypto-js/4.1.1/crypto-js.min.js"></script>
<style>
:root{{--bg:#0a0f17;--sf:#0d1520;--cyan:#00aaff;--red:#ff3d00;--bd:#1e3045;}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{
  margin:0;background:var(--bg);color:var(--cyan);
  font-family:'Share Tech Mono','Courier New',monospace;
  height:100vh;display:flex;align-items:center;justify-content:center;
}}
#lock-screen{{width:100%;display:flex;justify-content:center;padding:16px;}}
.panel{{
  border:1px solid var(--bd);border-top:3px solid var(--cyan);
  padding:32px 26px;display:flex;flex-direction:column;
  align-items:center;gap:14px;background:var(--sf);
  border-radius:3px;width:100%;max-width:360px;
}}
.logo{{
  font-size:1.7em;font-weight:700;letter-spacing:5px;
  color:var(--cyan);white-space:nowrap;text-align:center;
}}
.sub{{
  color:#3a5570;font-size:.72em;letter-spacing:3px;
  margin-top:-8px;text-transform:uppercase;white-space:nowrap;
}}
hr{{width:100%;border:none;border-top:1px solid #1a2535;}}
.lbl{{color:#3a5570;font-size:.72em;letter-spacing:2px;align-self:flex-start;white-space:nowrap;}}
#pwd{{
  width:100%;background:rgba(0,0,0,.6);
  border:1px solid #222;border-bottom:2px solid var(--cyan);
  color:var(--cyan);font-family:inherit;font-size:1.1em;
  padding:11px 14px;outline:none;letter-spacing:4px;
}}
#pwd:focus{{box-shadow:0 0 10px rgba(0,170,255,.25);}}
#pwd.err{{border-bottom-color:var(--red);}}
#btn{{
  width:100%;padding:11px;background:var(--cyan);border:none;
  color:#0a0f17;font-family:inherit;font-size:.88em;
  font-weight:700;letter-spacing:2px;cursor:pointer;
  border-radius:2px;white-space:nowrap;transition:opacity .2s;
}}
#btn:hover{{opacity:.85;}}
#btn:disabled{{opacity:.5;cursor:not-allowed;}}
#err{{color:var(--red);display:none;font-size:.78em;letter-spacing:1px;white-space:nowrap;}}
#loading{{display:none;color:var(--cyan);font-size:.75em;letter-spacing:1px;white-space:nowrap;}}
</style>
</head>
<body>
<div id="lock-screen" style="display:none">
  <div class="panel">
    <div class="logo">EFplant</div>
    <div class="sub">Secure Access Terminal</div>
    <hr>
    <div class="lbl">ACCESS CODE</div>
    <input type="password" id="pwd" placeholder="&#x25CF;&#x25CF;&#x25CF;&#x25CF;&#x25CF;&#x25CF;&#x25CF;&#x25CF;"
           autocomplete="new-password" onkeydown="if(event.key==='Enter')go()">
    <button id="btn" onclick="go()">[ AUTHENTICATE ]</button>
    <div id="err">&#9888; ACCESS DENIED</div>
    <div id="loading">DECRYPTING... &#x23F3;</div>
  </div>
</div>
<script>
const EP="{payload_b64}",PIV="{payload_iv_b64}",SLT="{salt_hex}";
const KS={json.dumps(key_safes)};
document.getElementById('pwd').value="";

// ── 30-minute session management (localStorage) ──────────────────────
var SK='ef_sess_v1', SEXP=30*60*1000;

function saveSess(dkh){{
  try{{localStorage.setItem(SK,JSON.stringify({{dk:dkh,exp:Date.now()+SEXP}}));}}catch(e){{}}
}}
function loadSess(){{
  try{{
    var d=JSON.parse(localStorage.getItem(SK));
    if(d&&d.exp>Date.now()&&d.dk)return d.dk;
  }}catch(e){{}}
  localStorage.removeItem(SK);
  return null;
}}
function doDecrypt(dkh){{
  var iid=dkh.substring(0,32),ekh=dkh.substring(32);
  var safe=KS[iid];
  if(!safe)return null;
  var ek=CryptoJS.enc.Hex.parse(ekh);
  var siv=CryptoJS.enc.Hex.parse(safe.iv);
  var cp=CryptoJS.lib.CipherParams.create({{ciphertext:CryptoJS.enc.Hex.parse(safe.enc_master)}});
  var mk=CryptoJS.AES.decrypt(cp,ek,{{iv:siv,mode:CryptoJS.mode.CBC,padding:CryptoJS.pad.Pkcs7}});
  if(mk.sigBytes!==32)return null;
  var piv=CryptoJS.enc.Base64.parse(PIV);
  var pc=CryptoJS.lib.CipherParams.create({{ciphertext:CryptoJS.enc.Base64.parse(EP)}});
  var html=CryptoJS.AES.decrypt(pc,mk,{{iv:piv,mode:CryptoJS.mode.CBC,padding:CryptoJS.pad.Pkcs7}}).toString(CryptoJS.enc.Utf8);
  return html||null;
}}

// On page load: try auto-login with stored session
window.addEventListener('DOMContentLoaded',function(){{
  var dkh=loadSess();
  if(dkh){{
    var html=doDecrypt(dkh);
    if(html){{
      saveSess(dkh); // extend 30 min from now (rolling window)
      document.open();document.write(html);document.close();
      return;
    }}
    localStorage.removeItem(SK); // session invalid (data updated)
  }}
  document.getElementById('lock-screen').style.display='flex';
}});

function go(){{
  var p=document.getElementById('pwd').value;
  if(!p)return;
  document.getElementById('err').style.display='none';
  document.getElementById('loading').style.display='block';
  document.getElementById('btn').disabled=true;
  setTimeout(function(){{
    try{{
      var salt=CryptoJS.enc.Hex.parse(SLT);
      var dk=CryptoJS.PBKDF2(p,salt,{{keySize:256/32,iterations:100000,hasher:CryptoJS.algo.SHA256}});
      var dkh=CryptoJS.enc.Hex.stringify(dk);
      var html=doDecrypt(dkh);
      if(!html)throw new Error("bad pwd");
      saveSess(dkh);
      document.open();document.write(html);document.close();
    }}catch(e){{
      console.error(e);
      var pw=document.getElementById('pwd');
      document.getElementById('err').style.display='block';
      pw.value="";pw.classList.add('err');
      setTimeout(function(){{document.getElementById('err').style.display='none';pw.classList.remove('err');}},2000);
      pw.focus();
    }}finally{{
      document.getElementById('loading').style.display='none';
      document.getElementById('btn').disabled=false;
    }}
  }},50);
}}
</script>
</body>
</html>
"""

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(wrapper_html)
    print(f"[OK] 儀表板產出完成 -> {os.path.basename(output_path)}")

    # chart.html：若尚不存在則建立「施工中」版本
    # （generate_chart.py 上線後會覆蓋為真實趨勢圖）
    chart_path = os.path.join(os.path.dirname(os.path.abspath(output_path)), "chart.html")
    if not os.path.exists(chart_path):
        chart_wip = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>EFplant - Trend Chart</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{
  background:#080c10;color:#3a5570;
  font-family:'Courier New',monospace;
  min-height:100vh;display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:14px;
}
.back{
  position:fixed;top:12px;left:14px;padding:5px 12px;
  border:1px solid #1a2d45;border-radius:2px;
  color:#00aaff;text-decoration:none;font-size:.75rem;letter-spacing:1px;
}
.back:hover{background:#0c1420;}
.icon{font-size:3rem;}
.title{font-size:.85rem;letter-spacing:4px;color:#3a5570;text-transform:uppercase;}
.box{
  border:1px dashed #1a2d45;padding:14px 28px;text-align:center;
  font-size:.68rem;color:#3a5570;line-height:2.2;letter-spacing:.5px;
}
</style>
</head>
<body>
<a href="index.html" class="back">&#x2190; BACK</a>
<div class="icon">&#x1F4CA;</div>
<div class="title">Trend Chart</div>
<div class="box">
  趨勢圖功能建置中<br>
  Under Construction<br>
  Trend chart coming soon
</div>
</body>
</html>
"""
        with open(chart_path, 'w', encoding='utf-8') as cf:
            cf.write(chart_wip)
        print(f"[OK] chart.html (施工中) 已建立 -> {os.path.basename(chart_path)}")

    # 同步寫出 health.json（供網頁 JS 偵測是否有更新）
    health_path = os.path.join(os.path.dirname(os.path.abspath(output_path)), "health.json")
    with open(health_path, 'w', encoding='utf-8') as hf:
        hf.write(json.dumps({"updated": generation_time_iso}, ensure_ascii=False))
    print(f"[OK] health.json 已更新 -> {health_path}")


if __name__ == "__main__":
    # 優先使用最新 MSSQL 備份（latest_data_backup.csv），其次才用 test_data.csv
    # 確保手動執行時也能拿到最新的設備資料，不會因舊資料導致類別消失
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
