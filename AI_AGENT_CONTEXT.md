# EFplant — AI Agent 交接文件
> 最後更新：2026-05-21｜版本：v2.0
> 任何 AI Agent 讀取此文件後，應能完整接手本專案的維護與開發。

---

## 1. 專案總覽

**EFplant** 是一套工廠廠務設備 SCADA 監控儀表板系統，特點如下：

- 從 **MSSQL 資料庫**（內網）每小時整點自動抓取設備運轉資料
- 將資料加密成**單一靜態 HTML 檔**（`index.html`），推送至 **GitHub Pages** 公開托管
- 使用者透過密碼在瀏覽器端解密，**完全無後端**（100% static）
- 以 **AES-256-CBC + PBKDF2** 實作多帳號密碼保護（類似 1Password 的靜態加密架構）
- 主要使用者（90%+）為手機用戶，UI 以手機優先設計

**技術棧**：Python 3 / pandas / pymssql / pycryptodome / schedule / CryptoJS 4.1.1 / GitHub Pages

---

## 2. 檔案結構

```
EFplant/
├── 🔒 config.json              ← MSSQL 帳密（gitignored，僅本機）
├── 🔒 accounts.json            ← 使用者密碼清單（gitignored，僅本機）
├── 🔒 accunt.txt               ← 員工名冊（gitignored，僅本機，格式：姓名,密碼）
├── 🔒 *.csv                    ← 所有 CSV 資料備份（gitignored）
├── 🔒 .venv/                   ← Python 虛擬環境（gitignored）
│
├── 📤 index.html               ← 產出物：AES 加密登入頁＋儀表板（推送 GitHub）
├── 📤 health.json              ← 產出物：最後更新時間戳（推送 GitHub）
├── 📤 chart.html               ← 產出物：趨勢圖（施工中佔位頁，推送 GitHub）
├── 📤 known_equipment.json     ← 設備清單歷史（用於 NEW 標籤偵測，推送 GitHub）
│
├── ⚙️  main.py                 ← 主排程引擎（每小時整點執行）
├── ⚙️  generate_dashboard.py   ← 儀表板生成＋AES 加密核心
├── ⚙️  generate_chart.py       ← 趨勢圖生成（目前未整合入主流程）
├── ⚙️  db_test.py              ← MSSQL 連線測試工具
│
├── 🛠️  Start_Auto_Update.bat   ← 啟動 main.py（顯示視窗）
├── 🛠️  run_background.vbs      ← 靜默啟動 main.py（無視窗）
├── 🛠️  Push_to_GitHub.bat      ← 手動推送 index.html 到 GitHub
├── 📄  .gitignore              ← 保護敏感檔案
└── 📄  AI_AGENT_CONTEXT.md     ← 本文件
```

### 🔒 絕對不能推送到 GitHub 的檔案
| 檔案 | 原因 |
|------|------|
| `config.json` | 含 MSSQL 明文帳密 |
| `accounts.json` | 含使用者明文密碼 |
| `accunt.txt` | 含員工姓名＋密碼（PII）|
| `*.csv` | 含真實生產設備狀態資料 |
| `.venv/` | Python 虛擬環境（體積龐大，無需版控）|

---

## 3. 核心架構：AES 多帳號靜態加密

### 加密流程（Python 後端，generate_dashboard.py）

```
生成儀表板 HTML (full_html)
    ↓
隨機產生 32-byte MASTER_KEY
    ↓
AES-256-CBC 加密 full_html → ENCRYPTED_PAYLOAD
    ↓
讀取 accounts.json 中的每個密碼：
    PBKDF2-HMAC-SHA256（100,000 次迭代）→ 32-byte 衍生金鑰
    前 16 bytes → INDEX_ID（用於查找）
    後 16 bytes → ENC_KEY（用於加密）
    AES-128-CBC 用 ENC_KEY 加密 MASTER_KEY → enc_master
    存入 KEY_SAFES[INDEX_ID] = {iv, enc_master}
    ↓
輸出 index.html（登入外殼 + 嵌入 ENCRYPTED_PAYLOAD + KEY_SAFES）
```

### 解密流程（JavaScript 前端，CryptoJS）

```
使用者輸入密碼
    ↓
PBKDF2（相同參數）→ 32-byte 衍生金鑰
    ↓
前 16 bytes → INDEX_ID → 查找 KEY_SAFES[INDEX_ID]
後 16 bytes → ENC_KEY → 解密 enc_master → MASTER_KEY
    ↓
MASTER_KEY 解密 ENCRYPTED_PAYLOAD → full_html
    ↓
document.write(full_html)（完整替換頁面，URL 不變）
```

### 30 分鐘 Session 機制

- 登入成功後，衍生金鑰（hex）儲存於 `localStorage`（key: `ef_sess_v1`），有效期 30 分鐘（滾動計時）
- 頁面載入時先檢查 `localStorage`，若 session 有效則直接解密，無需重新輸入密碼
- 每小時資料更新後（新 `GLOBAL_SALT`），舊 session 自動失效，使用者需重新登入
- **安全評估**：中低風險，適用於工廠內網操作環境（非金融/個資資料）

### 重要加密參數（勿隨意修改）

| 參數 | 值 |
|------|---|
| Payload 加密 | AES-256-CBC |
| Key Safe 加密 | AES-128-CBC |
| PBKDF2 演算法 | HMAC-SHA256 |
| PBKDF2 迭代次數 | 100,000 |
| 衍生金鑰長度 | 32 bytes |
| CryptoJS 版本 | 4.1.1（CDN）|

> ⚠️ 修改 Python 加密參數時，必須同步修改 `wrapper_html` 中對應的 JavaScript 邏輯。

---

## 4. 資料流程

### 自動更新流程（每小時整點）

```
main.py 排程觸發（整點 :00）
    ↓
Step 1：sync_accounts()
  比對 accunt.txt vs accounts.json
  若有差異 → 更新 accounts.json（新增/刪除人員）
    ↓
Step 2：連線 MSSQL（config.json 讀取帳密）
  Server: 192.168.120.32
  Database: EQSTS
  Table: dbo.EQSTS_DB
  Query: 最近1小時資料（依 MAX(TIMESTAMP) - 1hr）
    ↓
Step 3：備份資料至 latest_data_backup.csv（gitignored）
    ↓
Step 4：create_status_dashboard(df, "index.html")
  → 生成儀表板 HTML → AES 加密 → 寫出 index.html
  → 同步寫出 health.json（含 generation_time_iso）
  → 若 chart.html 不存在則建立施工中版本
  → 更新 known_equipment.json（新設備偵測）
    ↓
Step 5：git add / commit / push（index.html, health.json, known_equipment.json, chart.html）
```

### MSSQL 資料欄位

| 欄位 | 說明 |
|------|------|
| `PLANT` | 廠區代號（T2A, S2A, PCB, S2, S3, HJ1, HJ2, LC2, LC3）|
| `EQNO` | 設備編號（用於分類）|
| `TIMESTAMP` | 資料時間戳 |
| `VALUE` | 數值（>= 10.0 = 運轉中，< 10.0 = 停機）|
| `TAGNAME` | SCADA 標籤名稱 |
| `DESCRIPTION` | 設備描述 |

### MSSQL 連線設定（config.json，勿推送）

```json
{
    "mssql": {
        "server": "192.168.120.32",
        "user": "user",
        "password": "（請查閱本機 config.json）",
        "database": "EQSTS",
        "charset": "utf8"
    }
}
```

---

## 5. 設備分類系統

### 靜態分類（CATEGORIES，generate_dashboard.py 頂部）

| 中文名稱 | 識別字串 | 圖示 | 英文 |
|---------|---------|------|------|
| 冰機 | CHU | ❄️ | Chiller |
| 空壓 | CDA | 💨 | Compressed Air |
| 外氣空調箱 | MAU | 🌀 | MAU |
| 酸排氣 | ASCR | 🧪 | Acid Scrubber |
| 鹼排氣 | BSCR | ⚗️ | Base Scrubber |
| 有機排氣 | VSCR | 🍃 | VOC Scrubber |
| 熱排氣 | HSCR | 🔥 | Hot Scrubber |
| 乾式集塵 | DUST（排除 WDUST）| 🧹 | Dry Dust |
| 溼式集塵 | WDUST | 💧 | Wet Dust |
| 製程冷卻水 | PCW | 🌊 | PCW |
| 其他設備 | （其餘）| ⚙️ | — |

### 動態自動分群（其他設備）

落入「其他設備」的設備不再混雜顯示，系統會自動偵測 EQNO 前綴字母並分群：

```python
# EQNO 前綴提取規則（正規表示式）
re.match(r'^([A-Za-z]+(?:_[A-Za-z]+)*)', eqno)

# 範例：
# PUMP001 → 群組 "PUMP"
# FAN_A01 → 群組 "FAN"
# HEX101  → 群組 "HEX"
```

**新增已知設備類別的方法**：
1. 在 `CATEGORIES` 清單加入新條目（維持在「其他設備」之前）
2. 在 `classify_equipment()` 函數加入對應的 `if "XXX" in u: return "中文名稱"`
3. 重新執行 `generate_dashboard.py` 並推送

---

## 6. 帳號管理

### accunt.txt 格式（gitignored，勿推送）

```
姓名,密碼
王小明,01234
李大華,A5678
...（每行一筆，共 N 筆）
```

### 自動同步機制（main.py: sync_accounts()）

每次整點排程執行時，**第一步**就是比對帳號：

```
讀取 accunt.txt 解析密碼清單
    ↓
與 accounts.json 比對（sorted 比較，不因順序誤判）
    ├── 無差異 → 繼續正常流程
    └── 有差異（新增/刪除人員）
            → 更新 accounts.json
            → 本次儀表板重建時使用新帳號（密鑰庫自動更新）
            → 若 MSSQL 失敗，用備份 CSV 重建
```

**日常人員異動操作**：只需修改 `accunt.txt`，下一個整點自動生效，無需手動操作。

### 目前帳號數量

19 組（詳見本機 `accunt.txt`）

---

## 7. 廠區顯示規則

### 目前廠區狀態

| 廠區 | 狀態 |
|------|------|
| T2A | ✅ 真實 MSSQL 資料 |
| S2A, PCB, S2, S3, HJ1, HJ2, LC2, LC3 | 🚧 施工中（MSSQL 尚未接通）|

### 廠區自動上線機制

```python
# generate_dashboard.py 判斷邏輯
if plant in all_plants:      # MSSQL 有此廠區資料
    is_mock = False          # 顯示真實設備卡片
else:
    is_mock = True           # 顯示「施工中」畫面
```

**當 MSSQL 出現新廠區資料時，下一個整點自動上線，不需修改任何程式碼。**

---

## 8. 儀表板功能說明

### 🧠 大腦狀態指示燈（header 左側）

依「距上次 index.html 產生時間」判斷：

| 顏色 | 條件 | 意義 |
|------|------|------|
| 🟢 綠色強發光 | < 75 分鐘 | 資料正常，更新機制運作中 |
| 🟡 黃色發光 | 75–135 分鐘 | 更新延遲，可能 MSSQL 或網路問題 |
| 🔴 紅色閃爍 | > 135 分鐘 | 數據鏈中斷，請確認 main.py 是否執行 |

發光效果：4 層 `drop-shadow` 疊加（4px / 10px / 22px / 40px），灰白色底（`grayscale(1) brightness(2.2)`）。

### 🔄 自動偵測新資料（health.json + JS polling）

- 每次產生 `index.html` 時同步寫出 `health.json`：`{"updated": "2026-05-21T17:00:00"}`
- 瀏覽器每 60 秒 fetch `health.json`，若伺服器版本比本地新 → 顯示「NEW DATA AVAILABLE」橫幅
- 使用者點 `REFRESH NOW` → 重新整理頁面 → 重新輸入密碼 → 看到最新資料

### 🆕 新設備 NEW 標籤

- `known_equipment.json` 記錄每個廠區的歷史設備 EQNO 清單
- 每次更新時比對，新出現的 EQNO 卡片右上角顯示橘色 `NEW` 閃爍標籤
- 首次建立 `known_equipment.json` 時不標記任何設備（避免全部顯示 NEW）
- 下次更新後 NEW 標籤消失（已記錄為已知設備）

### 📱 版面設計原則

- **手機優先**（90%+ 使用者為手機）
- 廠區切換列：橫向觸控捲動，`flex-wrap: nowrap`
- KPI 卡片：手機 2×2 格，桌機 4×1
- 設備卡片：手機 **3 欄**，桌機 auto-fill（minmax 140px）
- 所有文字元素 `white-space: nowrap`，不換行
- Header、KPI 區、廠區選單固定頂部，只有設備列表捲動
- `height: 100dvh`（適應手機瀏覽器工具列）

---

## 9. 排程與自動化

### 啟動方式

| 方式 | 說明 |
|------|------|
| 雙擊 `run_background.vbs` | 靜默背景執行，無視窗（推薦）|
| 雙擊 `Start_Auto_Update.bat` | 顯示 CMD 視窗，可看到執行 log |
| Windows 工作排程器 | 開機自動啟動（需手動設定一次）|

### 防重複啟動機制

`main.py` 啟動時嘗試綁定 Port 47312（本機僅限）：
- 成功 → 正常啟動
- 失敗（Port 已被佔用）→ 偵測到已有實例在運行 → 自動退出並提示

> 若發現工作管理員有多個 `python.exe`，請全部結束後重新啟動一個。

### 排程設定

```python
schedule.every().hour.at(":00").do(fetch_data_and_update)
time.sleep(20)  # 每 20 秒檢查，確保整點準時
```

啟動時會立即執行一次（不等整點），之後固定每小時整點執行。

### Windows 工作排程器設定方式

1. `Win + R` → `taskschd.msc`
2. 建立基本工作 → 名稱：`EFplant AutoUpdate`
3. 觸發程序：**當我登入時**
4. 動作：啟動程式 → `wscript.exe`
5. 引數：`"C:\Users\U01572\Documents\EFplant\run_background.vbs"`
6. 勾選「以最高權限執行」

---

## 10. 手動推送（Push_to_GitHub.bat）

```
功能：將 index.html + health.json + known_equipment.json + chart.html 手動推送至 GitHub
檢查項目：
  1. 偵測是否有實際變更（無變更則跳出，不產生空 commit）
  2. 每步驟錯誤偵測（git add / commit / push 任一失敗立即顯示錯誤）
  3. 明確顯示 [SUCCESS] / [ERROR]
注意：bat 檔全為 ASCII，避免中文字元在 Windows CMD 造成編碼問題
```

---

## 11. 安全性設計

### 已實施的保護措施

| 措施 | 說明 |
|------|------|
| `.gitignore` | 保護 config.json / accounts.json / accunt.txt / *.csv / .venv/ |
| AES-256-CBC | 整份儀表板 HTML 加密 |
| PBKDF2 10萬次 | 密碼暴力破解難度高 |
| 每次更新換 SALT | 舊 session 自動失效 |
| Session localStorage | 30分鐘滾動計時，不儲存明文密碼 |
| 防重複啟動 | Port mutex 避免多個排程同時推送 |
| git history 清除 | 已從歷史紀錄移除舊版含帳密的 commit（2026-05-21 執行）|

### 風險提醒

- MSSQL 密碼曾短暫出現在 GitHub 歷史（已清除），**建議通知資訊部門更換 MSSQL `user` 帳號密碼**
- Session 儲存在 localStorage，DevTools 可讀出衍生金鑰（對工廠環境屬可接受風險）

---

## 12. 新增設備類別的 SOP

1. 在 `generate_dashboard.py` 頂部 `CATEGORIES` 清單，於「其他設備」**之前**新增：
   ```python
   {"name": "中文類別名", "pattern": "EQNO前綴", "icon": "圖示", "desc": "英文縮寫"},
   ```
2. 在 `classify_equipment()` 函數新增對應判斷（放在 `return "其他設備"` 之前）：
   ```python
   if "EQNO前綴" in u: return "中文類別名"
   ```
3. 執行 `generate_dashboard.py`（或等下一個整點 `main.py` 自動執行）
4. 推送至 GitHub

---

## 13. 新增使用者的 SOP

1. 開啟本機 `accunt.txt`
2. 新增一行：`姓名,密碼`
3. 儲存檔案
4. **等下一個整點**，`main.py` 會自動偵測差異並更新密鑰庫
5. 或立即執行 `Push_to_GitHub.bat`（需先手動更新 `accounts.json`）

---

## 14. 對 AI Agent 的重要規則

1. **不可修改加密參數**：`iterations=100000`、CBC 模式、金鑰長度，若修改 Python 端必須同步修改 `wrapper_html` 中的 JS 端
2. **不可暴露明文資料**：新功能/資料必須在 `full_html`（加密前）加入，不能放在 `wrapper_html`（登入外殼）
3. **不可引入後端**：本系統設計為 100% 靜態 GitHub Pages，禁止引入 Flask / Node.js / ngrok 等後端服務
4. **不可推送敏感檔案**：每次 git 操作前確認 `.gitignore` 保護範圍；推送清單僅包含 `index.html`、`health.json`、`known_equipment.json`、`chart.html`
5. **bat 檔必須純 ASCII**：Windows CMD 以 Big5 讀取 bat 檔，中文字元會造成命令解析錯誤
6. **pycryptodome 使用方式**：`from Crypto.Cipher import AES`（非 `Cryptodome`），套件已安裝在 `.venv`
7. **本機測試**：`python generate_dashboard.py` 使用 `test_data.csv`（若存在）或 `latest_data_backup.csv`；直接在瀏覽器開啟 `index.html`，輸入 `accounts.json` 中任一密碼測試

---

## 15. 環境資訊

| 項目 | 值 |
|------|---|
| 專案路徑 | `C:\Users\U01572\Documents\EFplant\` |
| Python 虛擬環境 | `.venv\Scripts\python.exe` |
| GitHub Remote | `https://github.com/fmcsfree120/EFplant.git` |
| GitHub Branch | `main` |
| GitHub Pages URL | `https://fmcsfree120.github.io/EFplant/` |
| MSSQL Server | `192.168.120.32`（內網，外部無法存取）|
| 排程執行者 | `U01572`（Windows 使用者）|
| 防重複啟動 Port | `127.0.0.1:47312` |
