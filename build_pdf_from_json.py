#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
FMCS 週報 PDF 建立器（資料驅動版）
固化格式原則 v2.2（2026-07）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[原則 1] 所有表頭文字不換行（欄寬依最長表頭字數預先驗證）
[原則 2] 所有欄位描述完整呈現，禁止截斷（無 CLIM / _t）
[原則 3] 版面結構（橫式 A4，共 7 頁）
    P1  主管摘要  ─ KPI 總覽 ＋ 風險等級定義 ＋ 本週重點 TOP 3
    P2  整頁      ─ 本週異常事件清單
    P3  整頁      ─ 未來風險預警
    P4  整頁      ─ 各廠區健康度評分
    P5  整頁      ─ 各指標最佳表現
    P6  整頁      ─ 各指標待加強方向
    P7  整頁      ─ 管理建議與追蹤事項

P2-P7 各表格列高動態計算：自動撐滿整頁。
腳本邏輯與格式固定，不需每週修改。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
執行：
  .venv\\Scripts\\python.exe build_pdf_from_json.py
"""

import argparse, sys, json, shutil, unicodedata, subprocess
from datetime import datetime, timedelta
from pathlib import Path

# ── Windows console UTF-8 強制設定（防 CP950 亂碼，固化）──────────────
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE_DIR  = Path(__file__).parent
JSON_PATH = BASE_DIR / "_weekly_analysis.json"
SPECIAL_CASE_PATH = BASE_DIR / "specialcase-weekly.txt"


def ensure_analysis_current() -> None:
    """Refresh analysis when the manually maintained special-case file changed."""
    if not SPECIAL_CASE_PATH.exists():
        return
    if JSON_PATH.exists() and SPECIAL_CASE_PATH.stat().st_mtime <= JSON_PATH.stat().st_mtime:
        return
    print("[INFO] specialcase-weekly.txt 較新，先重新產生分析 JSON。")
    result = subprocess.run(
        [sys.executable, str(BASE_DIR / "generate_weekly_analysis.py"), "--output", str(JSON_PATH)],
        cwd=str(BASE_DIR), check=False,
    )
    if result.returncode != 0 or not JSON_PATH.exists():
        raise RuntimeError("specialcase-weekly.txt 已更新，但分析 JSON 重新產生失敗；已停止 PDF 產製。")
LOCAL_CONTEXT_DIR = BASE_DIR / "localcontext"
LOCAL_CONTEXT_RETENTION_DAYS = 31

# ── 腳本版本（固化後請勿任意修改格式常數）──────────────────────────────
SCRIPT_VERSION = "v2.2-fixed-2026-07-03"
# 格式固化原則已寫入本檔 docstring，修改前請閱讀 [原則 1~3]

def _pip(pkg, name):
    try: __import__(pkg)
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", name, "-q"])

_pip("reportlab", "reportlab")

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame,
    Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak,
)
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics

# ══ 字型 ══════════════════════════════════════════════════════════════
def _reg_font():
    for n, p in [
        ("MSJH", r"C:\Windows\Fonts\msjh.ttc"),
        ("Sim",  r"C:\Windows\Fonts\simsun.ttc"),
        ("Ming", r"C:\Windows\Fonts\mingliu.ttc"),
    ]:
        if Path(p).exists():
            try:
                pdfmetrics.registerFont(TTFont(n, p))
                return n
            except Exception:
                continue
    raise RuntimeError("找不到可用的中文字型")

F  = _reg_font()
SZ = 9   # 基礎字級 pt

# ══ 色彩 ══════════════════════════════════════════════════════════════
C = {
    "hdr":      colors.HexColor("#1A3F6F"),
    "critical": colors.HexColor("#C0392B"),
    "major":    colors.HexColor("#D35400"),
    "warning":  colors.HexColor("#B7770D"),
    "normal":   colors.HexColor("#1E8449"),
    "neutral":  colors.HexColor("#2E4057"),
    "bg_crit":  colors.HexColor("#FDEDEC"),
    "bg_major": colors.HexColor("#FEF0E7"),
    "bg_warn":  colors.HexColor("#FEF9E7"),
    "bg_norm":  colors.HexColor("#EAFAF1"),
    "bg_hdr":   colors.HexColor("#EAF0FA"),
    "gray":     colors.HexColor("#7F8C8D"),
    "white":    colors.white,
    "black":    colors.HexColor("#1A1A1A"),
}

# ══ 版面常數（橫式 A4）════════════════════════════════════════════════
PAGE_W, PAGE_H = landscape(A4)
W        = PAGE_W - 3.0*cm   # 可用內容寬 26.7 cm
FRAME_H  = PAGE_H - 2.5*cm   # 可用內容高 18.5 cm
CP       = 5                  # 儲存格上下 padding (pt)
CLP      = 6                  # 儲存格左右 padding (pt)

# ── 整頁列高計算參數 ──────────────────────────────────────────────────
# sec() 列高 = (SZ+1 字級 leading SZ+5=14pt) + pad(4+4) = 22pt = 0.776 cm
# 標準 overhead = sec(0.776) + SP(0.12) = 0.896 ≈ 0.90 cm
# 健康度 P4 extra: SP(0.1) + caption(12pt=0.42cm) + SP(0.1) = 0.62 cm
# 建議 P7 extra(before+after): SP(0.15-0.12=0.03)+SP(0.4)+HR(0.14)+footer(0.42) = 0.99 cm
HDR_H     = 0.80*cm    # 表格標題列固定高度
SAFE      = 0.30*cm    # 底部安全邊距（防止浮點溢出）
OVH_STD   = 0.90*cm    # P2 P3 P5 P6 頁面 overhead
OVH_RULE  = 1.42*cm    # 含一行評比方式的標題區塊
OVH_P4    = 1.52*cm    # P4  sec + SP(0.1) + caption + SP(0.1)
OVH_P7    = 1.89*cm    # P7  sec + spacers + HR + footer（含表格前後）
P1_KPI_H  = 1.85*cm    # P1 KPI 卡片固定高度：0.65 + 1.20 cm
P1_RISK_ROW_H = 0.90*cm
OVH_P1    = (
    P1_KPI_H + 0.30*cm +
    OVH_STD + HDR_H + 4*P1_RISK_ROW_H + 0.30*cm +
    OVH_STD
)

def _rh(n_data, ovh=None):
    """動態計算資料列高，使表格填滿整頁（原則 3）"""
    if ovh is None:
        ovh = OVH_STD
    if n_data <= 0:
        return 1.0*cm
    avail = FRAME_H - ovh - SAFE - HDR_H
    return max(avail / n_data, 0.5*cm)

# ══ 樣式 ══════════════════════════════════════════════════════════════
def _ps(name, col=None, align=0, sz=None, ld=None, **kw):
    fs = sz if sz is not None else SZ
    return ParagraphStyle(
        name, fontName=F,
        fontSize=fs,
        leading=ld if ld is not None else fs + 4,
        textColor=col or C["black"],
        alignment=align, **kw,
    )

ST = {
    "body":    _ps("bo"),
    "bodyc":   _ps("bc",  align=1),
    "sm":      _ps("sm",  col=C["gray"]),
    "hdr_w":   _ps("hw",  col=C["white"],    align=1),
    "crit":    _ps("cr",  col=C["critical"]),
    "major":   _ps("ma",  col=C["major"]),
    "warn":    _ps("wa",  col=C["warning"]),
    "norm":    _ps("no",  col=C["normal"]),
    "neutral": _ps("nt",  col=C["neutral"]),
    "crit_c":  _ps("crc", col=C["critical"], align=1),
    "major_c": _ps("mac", col=C["major"],    align=1),
    "warn_c":  _ps("wac", col=C["warning"],  align=1),
    "norm_c":  _ps("noc", col=C["normal"],   align=1),
    "h1":      _ps("h1",  col=C["hdr"],      sz=SZ+1, ld=SZ+5),
    "foot":    _ps("ft",  col=C["gray"],     align=1, sz=SZ-1, ld=SZ+2),
}

# 趨勢欄著色：依方向，非依等級
TREND_ST = {"惡化中": "crit", "偏低": "warn", "持平": "neutral"}

def P(t, s="body"): return Paragraph(str(t), ST[s])
def Ph(t):          return Paragraph(str(t), ST["hdr_w"])
def SP(h=0.25):     return Spacer(1, h*cm)


def pretty_num(value, digits=1):
    """Display a number without meaningless trailing decimal zeroes."""
    try:
        text = f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)
    return text.rstrip("0").rstrip(".")


def auto_col_widths(headers, preferred):
    """Allocate widths with a hard minimum for every one-line header."""
    widths = [float(value) for value in preferred]

    def units(text):
        total = 0.0
        for char in str(text):
            if char.isspace():
                total += 0.35
            elif unicodedata.east_asian_width(char) in ("W", "F", "A"):
                total += 1.0
            else:
                total += 0.58
        return total

    required = [units(label) * SZ * 0.96 + 2 * CLP + 4 for label in headers]
    minimum_total = sum(required)
    if minimum_total <= W:
        # First satisfy every header, then distribute remaining space by the
        # original preferred proportions.  This avoids a later global scale
        # shrinking a just-satisfied header back into a wrapped state.
        widths = required[:]
        spare = W - minimum_total
        weights = [max(float(value), 0.01) for value in preferred]
        weight_total = sum(weights)
        return [widths[i] + spare * weights[i] / weight_total for i in range(len(widths))]
    # Extremely dense tables cannot mathematically fit all labels at the
    # current font size; preserve the best possible proportional layout.
    scale = W / minimum_total
    return [value * scale for value in required]

LEVEL_BG = {
    "CRITICAL": C["bg_crit"],  "MAJOR": C["bg_major"],
    "WARNING":  C["bg_warn"],  "NORMAL": C["bg_norm"],
}
LEVEL_ST = {
    "CRITICAL": "crit", "MAJOR": "major",
    "WARNING":  "warn", "NORMAL": "norm",
}
DOT_LABEL = {
    "CRITICAL": "● Critical", "MAJOR": "● Major",
    "WARNING":  "● Warning",  "NORMAL": "● Normal",
}

def dot(level):
    lv = str(level).upper()
    return P(DOT_LABEL.get(lv, ""), LEVEL_ST.get(lv, "body"))

# ══ 共用 TableStyle ════════════════════════════════════════════════════
BASE_TS = [
    ("FONTNAME",      (0,0), (-1,-1), F),
    ("FONTSIZE",      (0,0), (-1,-1), SZ),
    ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
    ("TOPPADDING",    (0,0), (-1,-1), CP),
    ("BOTTOMPADDING", (0,0), (-1,-1), CP),
    ("LEFTPADDING",   (0,0), (-1,-1), CLP),
    ("RIGHTPADDING",  (0,0), (-1,-1), CLP),
    ("GRID",          (0,0), (-1,-1), 0.35, colors.HexColor("#CCCCCC")),
]
HDR_TS = [
    ("BACKGROUND",    (0,0), (-1,0), C["hdr"]),
    ("FONTNAME",      (0,0), (-1,0), F),
    ("FONTSIZE",      (0,0), (-1,0), SZ),
    ("TOPPADDING",    (0,0), (-1,0), CP),
    ("BOTTOMPADDING", (0,0), (-1,0), CP),
]

def dot_style(col):
    """等級欄：左對齊、縮排"""
    return [
        ("ALIGN",        (col,0), (col,-1), "LEFT"),
        ("LEFTPADDING",  (col,0), (col,-1), 8),
        ("RIGHTPADDING", (col,0), (col,-1), 4),
    ]

def sec(text):
    """藍底區段標題列（固化格式）"""
    return Table(
        [[P(f"  {text}", "h1")]],
        colWidths=[W],
        style=TableStyle([
            ("BACKGROUND",    (0,0), (-1,-1), C["bg_hdr"]),
            ("LINEBELOW",     (0,0), (-1,-1), 1.5, C["hdr"]),
            ("TOPPADDING",    (0,0), (-1,-1), 4),
            ("BOTTOMPADDING", (0,0), (-1,-1), 4),
            ("LEFTPADDING",   (0,0), (-1,-1), 6),
        ]),
    )

# ══ 頁首 / 頁碼 ════════════════════════════════════════════════════════
def sec_rule(title, rule):
    """Title and a plain-language scoring rule in the same header block."""
    rule_style = _ps(f"rule_{abs(hash(title))}", col=C["neutral"], sz=SZ-1, ld=SZ+2)
    rows = [[P(f"  {title}", "h1")]]
    heights = [0.70*cm]
    if rule:
        rows.append([Paragraph(f"  評比方式：{rule}", rule_style)])
        heights.append(0.55*cm)
    return Table(
        rows,
        colWidths=[W],
        rowHeights=heights,
        style=TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), C["bg_hdr"]),
            ("LINEBELOW", (0,-1), (-1,-1), 1.5, C["hdr"]),
            ("TOPPADDING", (0,0), (-1,-1), 2),
            ("BOTTOMPADDING", (0,0), (-1,-1), 2),
            ("LEFTPADDING", (0,0), (-1,-1), 6),
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ]),
    )


def _draw_hdr(canvas, doc, wk, rd, dr):
    canvas.saveState()
    canvas.setFillColor(C["hdr"])
    canvas.rect(0, PAGE_H - 1.3*cm, PAGE_W, 1.3*cm, fill=1, stroke=0)
    canvas.setFillColor(colors.white)
    canvas.setFont(F, 13)
    canvas.drawString(1.0*cm, PAGE_H - 0.9*cm,
                      f"FMCS  EFplant  W{wk}  管理週報")
    canvas.setFont(F, 8)
    canvas.drawRightString(PAGE_W - 1.0*cm, PAGE_H - 0.9*cm,
                           f"產製日期：{rd}　｜　資料期間：{dr}")
    canvas.setFillColor(C["gray"])
    canvas.setFont(F, 7.5)
    canvas.drawCentredString(PAGE_W / 2, 0.45*cm, f"第 {doc.page} 頁")
    canvas.restoreState()

# ══ Document ════════════════════════════════════════════════════════════
def build_doc(out, wk, rd, dr):
    frame = Frame(
        1.5*cm, 0.8*cm, W, FRAME_H,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
    )
    def on_page(c, d): _draw_hdr(c, d, wk, rd, dr)
    doc = BaseDocTemplate(
        str(out), pagesize=landscape(A4),
        leftMargin=1.5*cm, rightMargin=1.5*cm,
        topMargin=2.0*cm,  bottomMargin=1.0*cm,
        title=f"FMCS W{wk} 管理週報", author="EFplant FMCS",
    )
    doc.addPageTemplates([
        PageTemplate(id="main", frames=[frame], onPage=on_page),
    ])
    return doc


def prune_local_context():
    LOCAL_CONTEXT_DIR.mkdir(exist_ok=True)
    cutoff = datetime.now() - timedelta(days=LOCAL_CONTEXT_RETENTION_DAYS)
    for path in LOCAL_CONTEXT_DIR.iterdir():
        if not path.is_file():
            continue
        try:
            if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                path.unlink()
        except OSError:
            pass


def save_report_copy_to_localcontext(pdf_path, data):
    prune_local_context()
    meta = data.get("meta", {})
    week = meta.get("week_num", "NA")
    data_to = str(meta.get("data_to", datetime.now().strftime("%Y-%m-%d"))).replace("-", "")
    stem = f"{data_to}_W{week}_weekly_report"
    target_pdf = LOCAL_CONTEXT_DIR / f"{stem}.pdf"
    target_json = LOCAL_CONTEXT_DIR / f"{stem}.json"
    try:
        shutil.copy2(pdf_path, target_pdf)
    except OSError as exc:
        print(f"[WARN] 無法複製 PDF 至 localcontext：{exc}")
    try:
        target_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"[WARN] 無法寫入 JSON 至 localcontext：{exc}")


# ════════════════════════════════════════════════════════════════════════
# P1  主管摘要（結構不變，但描述完整不截斷）
#   欄寬驗算：TOP3 排名欄 1.1cm（"排名"需 0.636cm，可用 0.677cm）✓
# ════════════════════════════════════════════════════════════════════════
def p1_summary(d):
    ov    = d["overview"]
    story = []

    # ── KPI 總覽卡片 ──────────────────────────────────────────────────
    hdr_txts = ["Critical 異常","Major 異常","Warning 趨勢","表現最佳廠區","待改善廠區"]
    hdr_bgs  = [C["critical"],C["major"],C["warning"],C["normal"],C["critical"]]
    val_txts = [
        f"{ov['critical_count']} 項", f"{ov['major_count']} 項",
        f"{ov['warning_count']} 項", ov["best_plants"], ov["worst_plants"],
    ]
    val_bgs  = [C["bg_crit"],C["bg_major"],C["bg_warn"],C["bg_norm"],C["bg_crit"]]
    val_cols = [C["critical"],C["major"],C["warning"],C["normal"],C["critical"]]

    hdr_row = [Ph(t) for t in hdr_txts]
    val_row = [
        Paragraph(str(v), _ps(f"ov{i}", col=val_cols[i], align=1, sz=SZ+3, ld=SZ+7))
        for i, v in enumerate(val_txts)
    ]
    ov_t = Table([hdr_row, val_row], colWidths=[W/5]*5,
                 rowHeights=[0.65*cm, 1.2*cm])
    kts = [
        ("FONTNAME",(0,0),(-1,-1),F), ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ("ALIGN",(0,0),(-1,-1),"CENTER"), ("TOPPADDING",(0,0),(-1,-1),4),
        ("BOTTOMPADDING",(0,0),(-1,-1),4),
        ("BOX",(0,0),(-1,-1),0.5,C["gray"]),
        ("INNERGRID",(0,0),(-1,-1),0.5,C["gray"]),
    ]
    for i, bg in enumerate(hdr_bgs): kts.append(("BACKGROUND",(i,0),(i,0),bg))
    for i, bg in enumerate(val_bgs):  kts.append(("BACKGROUND",(i,1),(i,1),bg))
    ov_t.setStyle(TableStyle(kts))
    story.append(ov_t); story.append(SP(0.3))

    # ── 風險等級定義 ─────────────────────────────────────────────────
    story.append(sec("風險等級定義")); story.append(SP(0.12))
    cw_lv = [W*0.18, W*0.57, W*0.25]
    lvl_rows = [
        [Ph("等級"),      Ph("定義"),                               Ph("顏色")],
        [dot("CRITICAL"), P("近期走勢大幅弱於前段歷史水準"),         P("● 紅色","crit_c")],
        [dot("MAJOR"),    P("近期走勢明顯弱於前段歷史水準"),         P("● 橘色","major_c")],
        [dot("WARNING"),  P("近期走勢輕度弱於前段歷史水準"),         P("● 黃色","warn_c")],
        [dot("NORMAL"),   P("近期走勢持平或優於前段歷史水準"),       P("● 綠色","norm_c")],
    ]
    lt = Table(
        lvl_rows,
        colWidths=cw_lv,
        rowHeights=[HDR_H] + [P1_RISK_ROW_H]*4,
        repeatRows=1,
    )
    lt.setStyle(TableStyle(BASE_TS + HDR_TS + [
        ("BACKGROUND",(0,1),(-1,1),C["bg_crit"]),
        ("BACKGROUND",(0,2),(-1,2),C["bg_major"]),
        ("BACKGROUND",(0,3),(-1,3),C["bg_warn"]),
        ("BACKGROUND",(0,4),(-1,4),C["bg_norm"]),
        ("ALIGN",(2,0),(2,-1),"CENTER"),
    ] + dot_style(0)))
    story.append(lt); story.append(SP(0.3))

    # ── 本週重點 TOP 3 ───────────────────────────────────────────────
    # 欄寬：排名1.1 | 事件7.4 | 影響說明7.4 | 等級2.8 | 建議行動8.0  合計=26.7✓
    # 表頭驗算："排名"(0.636cm) < 可用(0.677cm)✓；"影響說明"(1.272cm) < 6.977cm✓
    story.append(sec("重點預警TOP3")); story.append(SP(0.12))
    # "排名" must remain one line; the previous 1.0 cm fixed width wrapped it.
    cw3 = [1.45*cm, 7.0*cm, 6.5*cm, 2.5*cm, W-17.45*cm]
    rows3 = [[Ph("排名"), Ph("事件"), Ph("影響說明"), Ph("等級"), Ph("建議行動")]]
    bg3 = []
    top3_items = d.get("top3", [])
    n3 = len(top3_items)
    dh3 = _rh(n3, ovh=OVH_P1)
    for item in top3_items:
        rows3.append([
            P(item["rank"], "bodyc"),
            P(item["event"]),              # 原則 2：完整呈現，不截斷
            P(item["impact"]),
            dot(item["level"]),
            P(item["action"]),
        ])
        bg3.append(("BACKGROUND", (0,len(rows3)-1), (-1,len(rows3)-1),
                    LEVEL_BG.get(item["level"].upper(), C["white"])))
    t3 = Table(rows3, colWidths=cw3, rowHeights=[HDR_H] + [dh3]*n3, repeatRows=1)
    t3.setStyle(TableStyle(BASE_TS + HDR_TS + bg3 + dot_style(3)))
    story.append(t3)
    return story


# ════════════════════════════════════════════════════════════════════════
# P2  本週異常事件清單（整頁）
#   欄寬：日期1.8 | 廠區1.5 | 項目2.8 | 現象9.6 | 影響評估8.0 | 等級3.0 = 26.7✓
#   最長表頭："影響評估"(4字,1.272cm) < 7.577cm ✓
# ════════════════════════════════════════════════════════════════════════
def p2_events(d):
    story = [PageBreak()]
    story.append(sec_rule(
        "本週特別說明事項",
        ""
    )); story.append(SP(0.12))

    events = d.get("events", [])
    n = len(events)
    dh = _rh(n, ovh=OVH_RULE)
    cw_ev = auto_col_widths(
        ["日期", "廠區", "項目", "異常現象", "影響與風險", "等級"],
        [1.5*cm, 1.3*cm, 3.5*cm, 9.0*cm, 8.5*cm, W-23.8*cm],
    )
    rows_ev = [[Ph("日期"), Ph("廠區"), Ph("項目"),
                Ph("現象"), Ph("影響評估"), Ph("等級")]]
    bg_ev = []
    for e in events:
        rows_ev.append([
            P(e["date"]), P(e["plant"], "bodyc"), P(e["item"]),
            P(e["phenomenon"]), P(e["impact"]), dot(e["level"]),
        ])
        bg_ev.append(("BACKGROUND", (0,len(rows_ev)-1), (-1,len(rows_ev)-1),
                      LEVEL_BG.get(e["level"].upper(), C["white"])))
    # The special-notes topic deliberately omits date and level.  Keep only
    # plant, item, phenomenon and impact assessment, then redistribute its
    # former columns to make the narrative easier to read.
    keep_columns = (1, 2, 3, 4)
    rows_ev = [[row[index] for index in keep_columns] for row in rows_ev]
    cw_ev = [1.7*cm, 4.0*cm, 10.5*cm, W-16.2*cm]
    rh = [HDR_H] + [dh]*n
    ev_t = Table(rows_ev, colWidths=cw_ev, rowHeights=rh, repeatRows=1)
    ev_t.setStyle(TableStyle(
        BASE_TS + HDR_TS + bg_ev +
        [("ALIGN",(0,0),(0,-1),"CENTER")],
    ))
    story.append(ev_t)
    return story


# ════════════════════════════════════════════════════════════════════════
# P3  未來風險預警（整頁）
#   欄寬：廠區1.5 | 指標2.8 | 現況4.2 | 目標2.3 | 趨勢2.0 | 風險10.9 | 等級3.0 = 26.7✓
#   最長表頭："本週現況"(4字,1.272cm) < 3.777cm✓；"預估風險"(4字) < 10.477cm✓
# ════════════════════════════════════════════════════════════════════════
def p3_trends(d):
    story = [PageBreak()]
    story.append(sec_rule(
        "未來風險預警",
        "比較本週現況、管理目標與變化方向；偏離目標越多、趨勢持續惡化或可能影響越大，風險越高。"
    )); story.append(SP(0.12))

    trends = d.get("trend_warnings", [])
    n = len(trends)
    dh = _rh(n, ovh=OVH_RULE)
    cw_tw = auto_col_widths(
        ["廠區", "指標", "近期與前段", "比較基準", "趨勢", "預估風險", "等級"],
        [1.3*cm, 3.2*cm, 5.0*cm, 2.8*cm, 2.0*cm, 9.6*cm, W-23.9*cm],
    )
    rows_tw = [[Ph("廠區"), Ph("指標"), Ph("本週現況"), Ph("目標值"),
                Ph("趨勢"), Ph("預估風險"), Ph("等級")]]
    bg_tw = []
    for t in trends:
        trend_s = TREND_ST.get(t.get("trend",""), "body")
        rows_tw.append([
            P(t["plant"],"bodyc"), P(t["metric"],"bodyc"),
            P(t["current"],"bodyc"), P(t["target"],"bodyc"),
            P(t["trend"], trend_s), P(t["forecast"]),
            dot(t["level"]),
        ])
        bg_tw.append(("BACKGROUND", (0,len(rows_tw)-1), (-1,len(rows_tw)-1),
                      LEVEL_BG.get(t["level"].upper(), C["white"])))
    rh = [HDR_H] + [dh]*n
    tw_t = Table(rows_tw, colWidths=cw_tw, rowHeights=rh, repeatRows=1)
    tw_t.setStyle(TableStyle(
        BASE_TS + HDR_TS + bg_tw +
        [("ALIGN",(0,0),(3,-1),"CENTER"),
         ("ALIGN",(4,0),(4,-1),"CENTER")] + dot_style(6),
    ))
    story.append(tw_t)
    return story


# ════════════════════════════════════════════════════════════════════════
# P4  各廠區健康度評分（整頁）
#   overhead = OVH_P4 = 1.52cm（含說明文字列）
#   欄寬：排名1.2 | 廠區1.5 | 近期優勢10.2 | 近期弱項10.8 | 狀態3.0 = 26.7✓
#   表頭驗算："排名"(0.636cm) < 可用 0.777cm✓；"主要優勢"(1.272cm) < 10.577cm✓
# ════════════════════════════════════════════════════════════════════════
def p4_health(d):
    story = [PageBreak()]
    story.append(sec_rule(
        "各廠區趨勢表現排序（本週）",
        "依主要優勢改善幅度由優至劣排序。"
    ))
    story.append(SP(0.1))

    health = d.get("trend_rankings", [])
    n = len(health)
    dh = _rh(n, ovh=OVH_RULE)
    cw_r = auto_col_widths(
        ["排名", "廠區", "主要優勢"],
        [1.2*cm, 1.5*cm, W-2.7*cm],
    )
    rows_r = [[Ph("排名"), Ph("廠區"), Ph("主要優勢")]]
    bg_r = []
    for s in health:
        bg = LEVEL_BG.get(str(s.get("status", "WARNING")).upper(), C["white"])
        rows_r.append([
            P(str(s["rank"]), "bodyc"), P(s["plant"], "bodyc"),
            P(s["advantage"]),
        ])
        bg_r.append(("BACKGROUND", (0,len(rows_r)-1), (-1,len(rows_r)-1), bg))
    rh = [HDR_H] + [dh]*n
    r_t = Table(rows_r, colWidths=cw_r, rowHeights=rh, repeatRows=1)
    r_t.setStyle(TableStyle(
        BASE_TS + HDR_TS + bg_r +
         [("ALIGN",(0,0),(1,-1),"CENTER")],
    ))
    story.append(r_t)
    return story


# ════════════════════════════════════════════════════════════════════════
# P5  各指標最佳表現（整頁）
#   欄寬：指標5.2 | 最佳廠區2.0 | 資料點14.5 | 數值5.0 = 26.7✓
#   表頭驗算："最佳廠區"(4字,1.272cm) < 可用 1.577cm✓
# ════════════════════════════════════════════════════════════════════════
def p5_best(d):
    story = [PageBreak()]
    story.append(sec_rule(
        "各指標最佳表現",
        "比較近期三日與前段四日的走勢；改善幅度最大或最穩定者列為各主題最佳表現。"
    )); story.append(SP(0.12))

    best = d.get("best_performers", [])
    n = len(best)
    dh = _rh(n, ovh=OVH_RULE)
    cw_p = auto_col_widths(
        ["指標", "最佳廠區", "資料點", "近期與前段比較"],
        [4.5*cm, 2.0*cm, 11.7*cm, W-18.2*cm],
    )
    rows_p = [[Ph("指標"), Ph("最佳廠區"), Ph("資料點"), Ph("近期與前段比較")]]
    for bp in best:
        rows_p.append([
            P(bp["metric"]),
            P(bp["plant"], "bodyc"),
            P(bp["data_point"], "sm"),
            P(bp["value"], "norm_c"),
        ])
    rh = [HDR_H] + [dh]*n
    bt = Table(rows_p, colWidths=cw_p, rowHeights=rh, repeatRows=1)
    bt.setStyle(TableStyle(BASE_TS + [
        ("BACKGROUND",    (0,0), (-1,0), C["normal"]),
        ("FONTNAME",      (0,0), (-1,0), F),
        ("TOPPADDING",    (0,0), (-1,0), CP),
        ("BOTTOMPADDING", (0,0), (-1,0), CP),
        ("ROWBACKGROUNDS",(0,1), (-1,-1), [C["bg_norm"], C["white"]]),
        ("ALIGN",         (1,0), (1,-1), "CENTER"),
        ("ALIGN",         (3,0), (3,-1), "CENTER"),
    ]))
    story.append(bt)
    return story


# ════════════════════════════════════════════════════════════════════════
# P6  各指標待加強方向（整頁）
#   欄寬：指標5.2 | 待加強廠區2.2 | 資料點14.3 | 現況數值5.0 = 26.7✓
#   表頭驗算："待加強廠區"(5字,1.590cm) < 可用 1.777cm✓
# ════════════════════════════════════════════════════════════════════════
def p6_worst(d):
    story = [PageBreak()]
    story.append(sec_rule(
        "各指標待加強方向",
        "比較近期三日與前段四日的走勢；近期相對自身以往水準惡化幅度最大者列為待加強。"
    )); story.append(SP(0.12))

    worst = d.get("worst_areas", [])
    n = len(worst)
    dh = _rh(n, ovh=OVH_RULE)
    cw_w = auto_col_widths(
        ["指標", "待改善廠區", "資料點", "近期與前段比較"],
        [4.5*cm, 2.2*cm, 11.5*cm, W-18.2*cm],
    )
    rows_w = [[Ph("指標"), Ph("待加強廠區"), Ph("資料點"), Ph("近期與前段比較")]]
    for wa in worst:
        rows_w.append([
            P(wa["metric"]),
            P(wa["plant"], "bodyc"),
            P(wa["data_point"], "sm"),
            P(wa["value"], "crit_c"),
        ])
    rh = [HDR_H] + [dh]*n
    wt = Table(rows_w, colWidths=cw_w, rowHeights=rh, repeatRows=1)
    wt.setStyle(TableStyle(BASE_TS + [
        ("BACKGROUND",    (0,0), (-1,0), C["critical"]),
        ("FONTNAME",      (0,0), (-1,0), F),
        ("TOPPADDING",    (0,0), (-1,0), CP),
        ("BOTTOMPADDING", (0,0), (-1,0), CP),
        ("ROWBACKGROUNDS",(0,1), (-1,-1), [C["bg_crit"], C["white"]]),
        ("ALIGN",         (1,0), (1,-1), "CENTER"),
        ("ALIGN",         (3,0), (3,-1), "CENTER"),
    ]))
    story.append(wt)
    return story


# ════════════════════════════════════════════════════════════════════════
# P7  管理建議與追蹤事項（整頁，底部保留頁尾）
#   overhead P7 = OVH_P7 = 1.89cm（含 sec+SP前 + SP後+HR+footer）
#   欄寬：優先1.2 | 廠區1.5 | 項目3.0 | 建議行動18.0 | 等級3.0 = 26.7✓
#   表頭驗算："建議行動"(4字,1.272cm) < 可用 17.577cm✓
# ════════════════════════════════════════════════════════════════════════
def p7_actions(d):
    story = [PageBreak()]
    story.append(sec_rule(
        "管理建議與追蹤事項",
        "依 Critical、Major、Warning 及影響程度排列；順位越前面越需先處理。"
    )); story.append(SP(0.15))

    actions = d.get("actions", [])
    n = len(actions)
    dh = _rh(n, ovh=OVH_P7 + (OVH_RULE - OVH_STD))
    c_pri = 1.2*cm; c_plt = 1.5*cm; c_itm = 4.0*cm; c_lv = 3.0*cm
    cw_a = auto_col_widths(
        ["優先", "廠區", "項目", "改善行動", "等級"],
        [c_pri, c_plt, c_itm, W-c_pri-c_plt-c_itm-c_lv, c_lv],
    )
    rows_a = [[Ph("優先"), Ph("廠區"), Ph("項目"), Ph("建議行動"), Ph("等級")]]
    bg_a = []
    for a in actions:
        rows_a.append([
            P(a["priority"], "bodyc"), P(a["plant"], "bodyc"),
            P(a["item"]), P(a["action"]),
            dot(a["level"]),
        ])
        bg_a.append(("BACKGROUND", (0,len(rows_a)-1), (-1,len(rows_a)-1),
                     LEVEL_BG.get(a["level"].upper(), C["white"])))
    rh = [HDR_H] + [dh]*n
    at = Table(rows_a, colWidths=cw_a, rowHeights=rh, repeatRows=1)
    at.setStyle(TableStyle(
        BASE_TS + HDR_TS + bg_a +
        [("ALIGN",(0,0),(1,-1),"CENTER")] + dot_style(4),
    ))
    story.append(at)
    story.append(SP(0.4))
    story.append(HRFlowable(width="100%", thickness=0.4,
                             color=C["gray"], spaceAfter=4))
    story.append(Paragraph(
        "本報告由 EFplant 平台週報自動分析　｜　"
        "資料來源：https://fmcsfree120.github.io/EFplant/　｜　"
        "如有問題請聯絡　環廠處數位整合部",
        ST["foot"],
    ))
    return story


# ════════════════════════════════════════════════════════════════════════
# 主程式
# ════════════════════════════════════════════════════════════════════════
def p8_alarm_risk(d):
    story = [PageBreak(), sec_rule(
        "各廠區警報風險評比",
        "由上至下依未復歸 HIHI/LOLO、Critical 次數及異常警報次數由多至少排序。"
    ), SP(0.12)]
    items = d.get("alarm_risk", [])
    rows = [[Ph("廠區"), Ph("異常警報"), Ph("Critical"),
             Ph("未復歸 HIHI/LOLO"), Ph("最高風險項目"), Ph("等級")]]
    bg = []
    for item in items:
        rows.append([
            P(item.get("plant", ""), "bodyc"),
            P(item.get("abnormal", 0), "bodyc"), P(item.get("critical", 0), "bodyc"),
            P(item.get("active_high", 0), "bodyc"),
            P(item.get("top_risk", ""), "sm"), dot(item.get("status", "WARNING")),
        ])
        bg.append(("BACKGROUND", (0, len(rows)-1), (-1, len(rows)-1),
                   LEVEL_BG.get(str(item.get("status", "WARNING")).upper(), C["white"])))
    widths = auto_col_widths(
        ["廠區", "異常警報", "Critical", "未復歸 HIHI/LOLO", "最高風險項目", "等級"],
        [1.5*cm, 2.2*cm, 2.0*cm, 3.2*cm, 14.8*cm, W-23.7*cm],
    )
    table = Table(rows, colWidths=widths,
                  rowHeights=[HDR_H] + [_rh(len(items), ovh=OVH_RULE)] * len(items), repeatRows=1)
    table.setStyle(TableStyle(BASE_TS + HDR_TS + bg +
                              [("ALIGN", (0,0), (3,-1), "CENTER")] + dot_style(5)))
    story.append(table)
    return story


def p9_data_quality(d):
    story = [PageBreak(), sec_rule(
        "資料品質與儀表通訊健康",
        "逐點找曲線空窗、連續歸零及48小時振幅0%的 Keep Last；中斷越久、異常越持續，等級越高。"
    ), SP(0.12)]
    all_items = d.get("data_quality", [])
    items = []
    for kind in ("曲線空窗", "降為 0", "持續為 0", "Keep Last"):
        items.extend([row for row in all_items if row.get("type") == kind][:3])
    for row in all_items:
        if row not in items and len(items) < 14:
            items.append(row)
    level_order = {"CRITICAL": 4, "MAJOR": 3, "WARNING": 2, "NORMAL": 1}
    def duration_hours(row):
        import re
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*小時", str(row.get("duration", "")))
        return float(m.group(1)) if m else 0.0
    items.sort(key=lambda row: (-level_order.get(str(row.get("level", "NORMAL")).upper(), 0), -duration_hours(row)))
    rows = [[Ph("廠區"), Ph("異常類型"), Ph("設備／Tag"), Ph("異常時段"),
             Ph("持續時間"), Ph("判定內容"), Ph("等級")]]
    bg = []
    for item in items:
        rows.append([
            P(item.get("plant", ""), "bodyc"), P(item.get("type", "")),
            P(item.get("point", ""), "sm"), P(item.get("period", ""), "sm"),
            P(item.get("duration", ""), "bodyc"), P(item.get("detail", ""), "sm"),
            dot(item.get("level", "WARNING")),
        ])
        bg.append(("BACKGROUND", (0, len(rows)-1), (-1, len(rows)-1),
                   LEVEL_BG.get(str(item.get("level", "WARNING")).upper(), C["white"])))
    display_count = len(items)
    if not items:
        rows.append([P("-", "bodyc"), P("未發現異常"), P("-"), P("-"), P("-"),
                     P("本週未偵測到曲線空窗、歸零或超過48小時 Keep Last。"), dot("NORMAL")])
        display_count = 1
    widths = auto_col_widths(
        ["廠區", "異常類型", "設備／Tag", "異常時段", "持續時間", "判定內容", "等級"],
        [1.4*cm, 2.3*cm, 5.4*cm, 4.8*cm, 2.2*cm, 7.6*cm, 3.0*cm],
    )
    table = Table(rows, colWidths=widths,
                  rowHeights=[HDR_H] + [_rh(display_count, ovh=OVH_RULE)] * display_count, repeatRows=1)
    table.setStyle(TableStyle(BASE_TS + HDR_TS + bg +
                              [("ALIGN", (0,0), (0,-1), "CENTER"),
                               ("ALIGN", (4,0), (4,-1), "CENTER")] + dot_style(6)))
    story.append(table)
    return story


def main():
    parser = argparse.ArgumentParser(description="Build EFplant management weekly report PDF.")
    parser.add_argument("--local-output", action="store_true",
                        help="Write a temporary review PDF to the project directory.")
    args = parser.parse_args()
    try:
        ensure_analysis_current()
    except (OSError, RuntimeError) as exc:
        print(f"❌ {exc}")
        sys.exit(1)
    if not JSON_PATH.exists():
        print(f"❌ 找不到 {JSON_PATH}，請先執行分析程序。")
        sys.exit(1)

    d  = json.loads(JSON_PATH.read_text("utf-8"))
    wk = d["meta"]["week_num"]
    dr = f"{d['meta']['data_from']} ～ {d['meta']['data_to']}"
    rd = datetime.now().strftime("%Y/%m/%d %H:%M")
    ds = datetime.now().strftime("%Y%m%d")

    OUT_DIR = BASE_DIR if args.local_output else Path(r"\\192.168.120.33\SCADA Runtime Status\AutoWeekly")
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"[WARN] 無法存取網路路徑，改存本機：{e}")
        OUT_DIR = BASE_DIR
    suffix = "_TEST" if args.local_output else ""
    out = OUT_DIR / f"{ds}_FMCS_weekly{wk}{suffix}.pdf"

    # 列印各頁資料列數（自我驗證用）
    n_ev = len(d.get("events",[]))
    n_tw = len(d.get("trend_warnings",[]))
    n_hs = len(d.get("trend_rankings",[]))
    n_bp = len(d.get("best_performers",[]))
    n_wa = len(d.get("worst_areas",[]))
    n_ac = len(d.get("actions",[]))
    n_t3 = len(d.get("top3",[]))
    print(f"\n產生中：W{wk}  資料期間：{dr}")
    print(f"  P1重點={n_t3}列(列高{_rh(n_t3,OVH_P1)/cm:.2f}cm)")
    print(f"  P2事件={n_ev}列(列高{_rh(n_ev)/cm:.2f}cm)  "
          f"P3趨勢={n_tw}列(列高{_rh(n_tw)/cm:.2f}cm)  "
          f"P4健康={n_hs}列(列高{_rh(n_hs,OVH_P4)/cm:.2f}cm)")
    print(f"  P5最佳={n_bp}列(列高{_rh(n_bp)/cm:.2f}cm)  "
          f"P6待強={n_wa}列(列高{_rh(n_wa)/cm:.2f}cm)  "
          f"P7建議={n_ac}列(列高{_rh(n_ac,OVH_P7)/cm:.2f}cm)")

    doc   = build_doc(out, wk, rd, dr)
    story = []
    story += p1_summary(d)
    if d.get("events"):
        story += p2_events(d)
    story += p3_trends(d)
    story += p4_health(d)
    story += p5_best(d)
    story += p6_worst(d)
    story += p8_alarm_risk(d)
    story += p9_data_quality(d)
    story += p7_actions(d)
    doc.build(story)
    save_report_copy_to_localcontext(out, d)
    print(f"[OK] 完成（9 頁）：{out}\n")


if __name__ == "__main__":
    main()
