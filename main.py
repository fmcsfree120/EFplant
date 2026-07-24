import pymssql
import pandas as pd
import schedule
import time
import subprocess
import os
import sys
import json
import socket
import threading
import csv
import importlib.util
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
import warnings
from urllib.parse import urlparse, parse_qs

# ── 強制 stdout/stderr 以 UTF-8 輸出 ────────────────────────────────
# Windows 主控台預設為 cp950(Big5)，print 中文或 emoji 時可能拋出
# UnicodeEncodeError 導致常駐排程程序中斷。統一改為 UTF-8 並容錯。
# 經 run_background.vbs 隱藏視窗執行時 stdout 仍存在，故以 try 包覆容錯。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 全域的對照表緩存與線程安全鎖
_INDEX_ID_MAP = {}
_INDEX_MAP_LOCK = threading.Lock()
_SIGNLOG_LOCK = threading.Lock()
LOGIN_AUDIT_ENABLED = False

SIGNLOG_HEADERS = ["Timestamp", "Action", "User_Name", "Device_ID", "Session_ID", "Duration", "Client_IP"]

def ensure_signlog_schema(log_path):
    """Create or migrate signlog.csv to the current login audit schema."""
    if not os.path.exists(log_path) or os.path.getsize(log_path) == 0:
        with open(log_path, 'w', encoding='utf-8-sig', newline='') as f:
            csv.writer(f).writerow(SIGNLOG_HEADERS)
        return

    with open(log_path, 'r', encoding='utf-8-sig', newline='') as f:
        rows = list(csv.reader(f))

    if not rows:
        with open(log_path, 'w', encoding='utf-8-sig', newline='') as f:
            csv.writer(f).writerow(SIGNLOG_HEADERS)
        return

    old_headers = rows[0]
    if old_headers == SIGNLOG_HEADERS:
        return

    migrated = [SIGNLOG_HEADERS]
    for row in rows[1:]:
        record = {old_headers[i]: row[i] if i < len(row) else "" for i in range(len(old_headers))}
        migrated.append([
            record.get("Timestamp", ""),
            record.get("Action", ""),
            record.get("User_Name", record.get("Account", "")),
            record.get("Device_ID", ""),
            record.get("Session_ID", ""),
            record.get("Duration", ""),
            record.get("Client_IP", ""),
        ])

    with open(log_path, 'w', encoding='utf-8-sig', newline='') as f:
        csv.writer(f).writerows(migrated)

def write_signlog(data, client_ip):
    if not LOGIN_AUDIT_ENABLED:
        print("[LOGGER] Login audit disabled; request ignored.")
        return

    action = data.get("action", "").upper()
    success = data.get("success", True)
    if isinstance(success, str):
        success = success.lower() not in ("0", "false", "no", "failed")
    index_id = data.get("index_id", "")
    session_id = data.get("session_id", "")
    device_id = data.get("device_id", "")
    duration = data.get("duration", "")

    name = "Unknown"
    if index_id:
        with _INDEX_MAP_LOCK:
            name = _INDEX_ID_MAP.get(index_id, "Unknown")

    if name == "Unknown" and index_id:
        name = f"Guest_{index_id[:8]}"

    if action == "LOGIN":
        action_str = "LOGIN_SUCCESS" if success else "LOGIN_FAILED"
    elif action == "LOGOUT":
        action_str = "LOGOUT"
    else:
        action_str = action

    timestamp_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_path = os.path.join(SCRIPT_DIR, "signlog.csv")

    with _SIGNLOG_LOCK:
        ensure_signlog_schema(log_path)
        with open(log_path, 'a', encoding='utf-8-sig', newline='') as f:
            csv.writer(f).writerow([timestamp_str, action_str, name, device_id, session_id, duration, client_ip])

    print(f"[LOGGER] 記錄成功: {timestamp_str} | {action_str} | {name} | {device_id} | {client_ip}")

def get_salt(script_dir):
    salt_file = os.path.join(script_dir, "salt.bin")
    if os.path.exists(salt_file):
        try:
            with open(salt_file, "rb") as f:
                s = f.read()
                if len(s) == 16:
                    return s
        except Exception:
            pass
    return None

def rebuild_index_id_map():
    global _INDEX_ID_MAP
    print("正在編譯本機帳號 INDEX_ID 匹配快取...")
    
    salt = get_salt(SCRIPT_DIR)
    if not salt:
        # Fallback: 嘗試從 data.enc 中讀取 salt
        data_path = os.path.join(SCRIPT_DIR, "data.enc")
        if os.path.exists(data_path):
            try:
                with open(data_path, "r", encoding="utf-8") as f:
                    ex = json.load(f)
                    s_hex = ex.get("salt", "")
                    if len(s_hex) == 32:
                        salt = bytes.fromhex(s_hex)
            except Exception:
                pass
                
    if not salt:
        print("[WARN] 無法獲取 salt，暫時無法編譯 INDEX_ID 匹配快取。")
        return
        
    accunt_path = os.path.join(SCRIPT_DIR, "accunt.txt")
    if not os.path.exists(accunt_path):
        print("[WARN] 找不到 accunt.txt，無法編譯對照表。")
        return
        
    new_map = {}
    from Crypto.Protocol.KDF import PBKDF2
    from Crypto.Hash import SHA256
    
    try:
        with open(accunt_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or ',' not in line:
                    continue
                parts = line.split(',', 1)
                name = parts[0].strip()
                pwd = parts[1].strip()
                if name and pwd:
                    # 計算與前端完全相同的 PBKDF2
                    dk = PBKDF2(pwd.encode('utf-8') if isinstance(pwd, str) else pwd, salt, dkLen=32, count=100000, hmac_hash_module=SHA256)
                    iid = dk[:16].hex()
                    new_map[iid] = name
                    
        with _INDEX_MAP_LOCK:
            _INDEX_ID_MAP = new_map
        print(f"[OK] INDEX_ID 匹配快取編譯成功，共 {len(new_map)} 筆對照。")
    except Exception as e:
        print(f"[ERROR] 編譯對照表失敗: {e}")


class LoggerAPIHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # 靜默 HTTP 請求日誌，避免干擾主排程輸出
        return

    def send_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Allow-Private-Network', 'true')

    def do_OPTIONS(self):
        # 處理 CORS 預檢請求
        self.send_response(200)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/api/log':
            try:
                query = parse_qs(parsed.query)
                data = {k: v[0] if v else "" for k, v in query.items()}
                write_signlog(data, self.client_address[0])
                self.send_response(204)
                self.send_cors_headers()
                self.end_headers()
                return
            except Exception as e:
                print(f"[LOGGER ERROR] GET 記錄發生錯誤: {e}")

        if parsed.path == '/api/ping':
            self.send_response(200)
            self.send_cors_headers()
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode('utf-8'))
            return

        self.send_response(404)
        self.send_cors_headers()
        self.end_headers()

    def do_POST(self):
        if self.path == '/api/log':
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            
            try:
                data = json.loads(post_data.decode('utf-8'))
                write_signlog(data, self.client_address[0])
                
                # 回應客戶端 200 OK
                self.send_response(200)
                self.send_cors_headers()
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "success"}).encode('utf-8'))
                return
            except Exception as e:
                print(f"[LOGGER ERROR] 寫入日誌發生錯誤: {e}")
                
        # 其他路徑返回 404
        self.send_response(404)
        self.send_cors_headers()
        self.end_headers()


def start_logger_api_server():
    server_address = ('0.0.0.0', 47313)
    try:
        httpd = HTTPServer(server_address, LoggerAPIHandler)
        print(f"[API] 異步日誌伺服器已啟動，監聽 Port 47313...")
        httpd.serve_forever()
    except Exception as e:
        print(f"[API ERROR] 日誌伺機器啟動失敗: {e}")

warnings.filterwarnings('ignore', '.*pandas only supports SQLAlchemy.*')



SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def create_status_dashboard_fresh(df, output_path):
    """每次重建都重新載入磁碟上的產生器，避免常駐程序沿用舊版程式碼。"""
    generator_path = os.path.join(SCRIPT_DIR, "generate_dashboard.py")
    module_name = f"efplant_dashboard_{time.time_ns()}"
    spec = importlib.util.spec_from_file_location(module_name, generator_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"無法載入前台產生器：{generator_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.create_status_dashboard(df, output_path)


def assert_frontend_version_current():
    """推送前確認輸出版本與磁碟上的產生器一致，禁止前台版次倒退。"""
    generator_path = os.path.join(SCRIPT_DIR, "generate_dashboard.py")
    index_path = os.path.join(SCRIPT_DIR, "index.html")
    with open(generator_path, "r", encoding="utf-8") as f:
        generator_text = f.read()
    with open(index_path, "r", encoding="utf-8") as f:
        index_text = f.read()
    pattern = r"var CACHE_EPOCH = ['\"]([^'\"]+)['\"]"
    source_match = re.search(pattern, generator_text)
    output_match = re.search(pattern, index_text)
    if not source_match or not output_match:
        raise RuntimeError("找不到前台 CACHE_EPOCH，已拒絕推送")
    source_version = source_match.group(1)
    output_version = output_match.group(1)
    if source_version != output_version:
        raise RuntimeError(
            f"前台版號不一致，已拒絕推送：產生器={source_version}，輸出={output_version}"
        )
    print(f"[OK] 前台版號防倒退檢查通過：{output_version}")

# ── 防止重複啟動（使用 Port 佔用作為 mutex）──────────────────────────────────
_LOCK_SOCKET = None

def acquire_single_instance_lock():
    """
    嘗試綁定一個本機 port 作為程序鎖。
    若已有另一個 EFplant 在執行，此 port 已被佔用，直接結束。
    """
    global _LOCK_SOCKET
    _LOCK_SOCKET = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _LOCK_SOCKET.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        _LOCK_SOCKET.bind(('127.0.0.1', 47312))   # EFplant 專用 port（不對外開放）
    except OSError:
        print("=" * 52)
        print("  [WARN] EFplant 服務已在執行中，不重複啟動。")
        print("  若要重啟，請先在工作管理員結束 python.exe。")
        print("=" * 52)
        sys.exit(0)


def load_config():
    """讀取 config.json（含 MSSQL 帳密）— 此檔案已在 .gitignore，不會推送至 GitHub。"""
    path = os.path.join(SCRIPT_DIR, "config.json")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"\n[ERROR] 找不到 {path}\n"
            "請在專案資料夾建立 config.json，格式如下：\n"
            '{\n  "mssql": {\n    "server": "IP",\n    "user": "帳號",\n'
            '    "password": "密碼",\n    "database": "資料庫名稱",\n    "charset": "utf8"\n  }\n}'
        )
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


# ── 帳號同步 ─────────────────────────────────────────────────────────────────

def load_accunt_txt():
    """
    讀取 accunt.txt（格式：姓名,密碼 每行一筆）
    回傳密碼清單，讀取失敗回傳 None
    """
    path = os.path.join(SCRIPT_DIR, "accunt.txt")
    if not os.path.exists(path):
        print("[WARN] 找不到 accunt.txt，跳過帳號同步。")
        return None
    passwords = []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if ',' in line:
                    pwd = line.split(',', 1)[1].strip()
                    if pwd:
                        passwords.append(pwd)
    except Exception as e:
        print(f"[WARN] accunt.txt 讀取錯誤: {e}")
        return None
    return passwords


def sync_accounts():
    """
    比對 accunt.txt 與 accounts.json 是否一致。
    若有異動（新增/刪除人員或密碼變更），自動更新 accounts.json。
    回傳 True 表示有異動（需重建密鑰庫），False 表示無異動。
    """
    accounts_path = os.path.join(SCRIPT_DIR, "accounts.json")

    new_passwords = load_accunt_txt()
    if new_passwords is None:
        return False

    # 讀取現有 accounts.json
    try:
        with open(accounts_path, 'r', encoding='utf-8') as f:
            current = json.load(f)
        current_passwords = current.get("passwords", [])
    except (FileNotFoundError, json.JSONDecodeError):
        current_passwords = []

    # 比對（排序後比較，不因順序不同誤判）
    if sorted(new_passwords) == sorted(current_passwords):
        print(f"[OK] 帳號清單無異動（共 {len(new_passwords)} 組）。")
        return False

    # 計算差異（只輸出數量，不輸出實際密碼）
    new_set     = set(new_passwords)
    old_set     = set(current_passwords)
    added_cnt   = len(new_set - old_set)
    removed_cnt = len(old_set - new_set)

    # 寫入更新後的 accounts.json
    with open(accounts_path, 'w', encoding='utf-8') as f:
        json.dump({"passwords": new_passwords}, f, indent=4, ensure_ascii=False)

    print(f"[SYNC] 帳號異動！ 新增 {added_cnt} 組 / 移除 {removed_cnt} 組 → 合計 {len(new_passwords)} 組")
    return True


# ── GitHub 推送 ───────────────────────────────────────────────────────────────

def push_to_github(commit_msg):
    """將更新的檔案 commit 並 push 到 GitHub Pages。"""
    try:
        assert_frontend_version_current()
        subprocess.run(
            ["git", "add", "index.html", "data.enc", "health.json",
             "known_equipment.json", "service-worker.js"],
            cwd=SCRIPT_DIR, check=True
        )
        result = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=SCRIPT_DIR, capture_output=True
        )
        if result.returncode == 0:
            subprocess.run(["git", "push"], cwd=SCRIPT_DIR, check=True)
            print("[OK] 成功推送到 GitHub！")
        else:
            print("[INFO] 內容無變動，略過推送。")
    except Exception as e:
        print(f"[ERROR] GitHub 推送失敗: {e}")


# ── 備份資料重建（MSSQL 失敗但帳號異動時使用）────────────────────────────────

def regen_from_backup(reason="account sync"):
    """當 MSSQL 連線失敗但帳號有異動時，用備份 CSV 重建密鑰庫。"""
    backup_path = os.path.join(SCRIPT_DIR, "latest_data_backup.csv")
    if not os.path.exists(backup_path):
        print("[WARN] 無備份資料，密鑰庫暫時無法更新。")
        return
    try:
        backup_df = pd.read_csv(backup_path)
        create_status_dashboard_fresh(backup_df, "index.html")
        push_to_github(f"Account sync ({reason}): {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        print("[OK] 已用備份資料重建密鑰庫並推送。")
    except Exception as e:
        print(f"[ERROR] 備份重建失敗: {e}")


# ── 主排程任務 ────────────────────────────────────────────────────────────────

def update_alarm_history():
    """隨設備狀態週期增量更新 KF1 警報 CSV，並只保留最新資料時間往前 7 天。"""
    alarm_path = os.path.join(SCRIPT_DIR, "latest_alarm_history_backup.csv")
    temp_path = alarm_path + ".tmp"
    source_columns = [
        'ALM_NATIVETIMELAST', 'ALM_TAGNAME', 'ALM_VALUE', 'ALM_DESCR',
        'ALM_ALMSTATUS', 'ALM_ALMPRIORITY', 'ALM_DATEIN', 'ALM_TIMEIN',
        'ALM_DATELAST', 'ALM_TIMELAST'
    ]
    text_columns = [c for c in source_columns if c != 'ALM_NATIVETIMELAST']
    conn = None
    try:
        existing = pd.DataFrame()
        anchor = None
        if os.path.exists(alarm_path):
            existing = pd.read_csv(alarm_path, encoding='utf-8-sig')
            if 'ALM_NATIVETIMELAST' in existing.columns:
                existing['ALM_NATIVETIMELAST'] = pd.to_datetime(
                    existing['ALM_NATIVETIMELAST'], errors='coerce')
                anchor = existing['ALM_NATIVETIMELAST'].max()

        cfg = load_config()['mssql']
        conn = pymssql.connect(
            cfg['server'], cfg['user'], cfg['password'],
            cfg['database'], charset=cfg.get('charset', 'cp950')
        )

        if pd.notna(anchor):
            # 從 CSV 最新時間之後補下一個 24 小時區段；每 30 分鐘執行可即時取得新增資料。
            query = """
            SELECT * FROM [ALM_DB].[dbo].[ALM_KF]
            WHERE ALM_NATIVETIMELAST > %s
              AND ALM_NATIVETIMELAST <= DATEADD(hour, 24, %s)
            ORDER BY ALM_NATIVETIMELAST ASC
            """
            incoming = pd.read_sql(query, conn, params=(anchor, anchor))
            print(f"[ALARM] 增量錨點 {anchor}，取得後續 24 小時新資料 {len(incoming)} 筆。")
        else:
            query = """
            SELECT * FROM [ALM_DB].[dbo].[ALM_KF]
            WHERE ALM_NATIVETIMELAST >= DATEADD(hour, -24, (
                SELECT MAX(ALM_NATIVETIMELAST) FROM [ALM_DB].[dbo].[ALM_KF]
            ))
            ORDER BY ALM_NATIVETIMELAST ASC
            """
            incoming = pd.read_sql(query, conn)
            print(f"[ALARM] 無既有 CSV，以 SQL 最新時間回抓 24 小時，共 {len(incoming)} 筆。")

        incoming['PLANT'] = 'KF1'
        if not existing.empty:
            existing['PLANT'] = 'KF1'
        combined = pd.concat([existing, incoming], ignore_index=True, sort=False)
        combined['ALM_NATIVETIMELAST'] = pd.to_datetime(
            combined['ALM_NATIVETIMELAST'], errors='coerce')
        combined = combined.dropna(subset=['ALM_NATIVETIMELAST'])
        for col in text_columns:
            if col in combined.columns:
                combined[col] = combined[col].fillna('').astype(str).str.strip()

        dedup_columns = [c for c in source_columns if c in combined.columns]
        combined = (combined
                    .drop_duplicates(subset=dedup_columns, keep='last')
                    .sort_values('ALM_NATIVETIMELAST'))
        if not combined.empty:
            latest_time = combined['ALM_NATIVETIMELAST'].max()
            cutoff = latest_time - pd.Timedelta(days=7)
            before_cleanup = len(combined)
            combined = combined[combined['ALM_NATIVETIMELAST'] >= cutoff]
            print(f"[ALARM] 七天裁剪移除 {before_cleanup - len(combined)} 筆，保留 {len(combined)} 筆。")

        ordered = ['PLANT'] + [c for c in source_columns if c in combined.columns]
        remaining = [c for c in combined.columns if c not in ordered]
        combined = combined[ordered + remaining]
        combined.to_csv(temp_path, index=False, encoding='utf-8-sig', date_format='%Y-%m-%d %H:%M:%S.%f')
        os.replace(temp_path, alarm_path)
        print(f"[OK] KF1 警報 CSV 已隨設備週期更新：{alarm_path}")
        return True
    except Exception as alarm_err:
        print(f"[WARN] KF1 警報增量更新失敗，沿用既有 CSV: {alarm_err}")
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass
        return False
    finally:
        if conn is not None:
            conn.close()


def fetch_data_and_update():
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    # 以發動抓取 SQL 的當下整點為統一時間基準
    trigger_hour = datetime.now().replace(minute=0, second=0, microsecond=0)
    trigger_ts   = pd.Timestamp(trigger_hour)
    print(f"\n{'='*52}")
    print(f"  [{now_str}] 排程作業啟動 (觸發整點: {trigger_hour.strftime('%Y-%m-%d %H:00')})")
    print(f"{'='*52}")

    # Step 1：比對帳號是否異動
    accounts_changed = sync_accounts()
    if accounts_changed:
        rebuild_index_id_map()

    # Step 2：風險總覽與設備狀態共用同一個 30 分鐘更新／發布週期。
    # 警報來源失敗時 update_alarm_history() 會保留既有 CSV，設備更新仍可繼續。
    update_alarm_history()

    try:
        # Step 3：連線 MSSQL 抓取最新資料
        print("連線 MSSQL 抓取資料...")
        cfg  = load_config()['mssql']
        conn = pymssql.connect(
            cfg['server'], cfg['user'], cfg['password'],
            cfg['database'], charset=cfg.get('charset', 'cp950')
        )
        
        # 1) 抓取設備狀態 (最多保留 3 小時以實現自動刪除機制)
        query = """
        SELECT * FROM dbo.EQSTS_DB
        WHERE TIMESTAMP >= DATEADD(hour, -3, (SELECT MAX(TIMESTAMP) FROM dbo.EQSTS_DB))
        ORDER BY TIMESTAMP DESC
        """
        df = pd.read_sql(query, conn)
        
        # 2) 抓取品質/能效歷史趨勢 (7天，168小時)
        print("同步抓取 dbo.EQQT_DB (能效/品質歷史趨勢)...")
        query_quality = """
        SELECT * FROM dbo.EQQT_DB
        WHERE TIMESTAMP >= DATEADD(day, -7, (SELECT MAX(TIMESTAMP) FROM dbo.EQQT_DB))
        ORDER BY TIMESTAMP ASC
        """
        try:
            df_quality = pd.read_sql(query_quality, conn)
            if 'PLANT' in df_quality.columns:
                df_quality['PLANT'] = (
                    df_quality['PLANT'].astype(str).str.strip().str.upper()
                    .replace({'KF': 'KF1'})
                )
            print(f"[OK] 取得品質數據 {len(df_quality)} 筆。")
            
            # ── 資料存儲生命週期管理 (品質/能效趨勢數據) ─────────────────────
            # 確保趨勢圖數據持續累積到第 169 筆 (>7 天 / 168 小時) 的舊數據自動刪除，避免資料無限膨脹
            if not df_quality.empty:
                # 1. 統一對齊到整點
                df_quality['TIMESTAMP'] = pd.to_datetime(df_quality['TIMESTAMP']).dt.ceil('h')
                # 2. 各 (PLANT, TAGNAME) 組的最新一批資料強制對齊至觸發整點
                #    條件：該標籤最後一筆的整點時間在 trigger_ts 前 24 小時內（排除長期失聯的標籤）
                recent_cutoff = trigger_ts - pd.Timedelta(hours=24)
                max_per_tag = df_quality.groupby(['PLANT', 'TAGNAME'])['TIMESTAMP'].transform('max')
                is_latest_row  = (df_quality['TIMESTAMP'] == max_per_tag)
                is_recent_tag  = (max_per_tag >= recent_cutoff)
                df_quality.loc[is_latest_row & is_recent_tag, 'TIMESTAMP'] = trigger_ts
                print(f"[OK] 品質趨勢最新一批已對齊觸發整點 {trigger_hour.strftime('%H:00')}。")

                # 3. 去重排重，以 PLANT + TAGNAME + TIMESTAMP 為鍵，保留同整點內最晚的一筆 (keep last)
                df_quality = df_quality.drop_duplicates(subset=['PLANT', 'TAGNAME', 'TIMESTAMP'], keep='last')
                
                max_q_time = df_quality['TIMESTAMP'].max()
                if pd.notna(max_q_time):
                    # 3. 刪除大於 7 天 (168小時) 的舊數據
                    df_quality = df_quality[df_quality['TIMESTAMP'] >= max_q_time - pd.Timedelta(days=7)]
                    
                    # 4. 如果不同的整點時間戳個數大於 168 個，保留最新 168 個整點的數據，自動刪除更舊的
                    unique_hours = sorted(df_quality['TIMESTAMP'].unique(), reverse=True)
                    if len(unique_hours) > 168:
                        keep_hours = unique_hours[:168]
                        df_quality = df_quality[df_quality['TIMESTAMP'].isin(keep_hours)]
                    print(f"[CLEANUP] 品質趨勢快取已對齊整點並裁剪保留最新 168 個整點內數據 (剩餘 {len(df_quality)} 筆)。")
            # ── 針對大宗化學品 (CHEM/SUP_.*_TANK) 強制覆寫 EQNAME ────────────────────
            if not df_quality.empty:
                chem_mask = df_quality['TAGNAME'].str.contains('CHEM|SUP_.*_TANK', case=False, na=False)
                df_quality.loc[chem_mask, 'EQNAME'] = '大宗化學品'
            # ───────────────────────────────────────────────────────────────
            
            df_quality.to_csv(os.path.join(SCRIPT_DIR, "latest_quality_backup.csv"),
                              index=False, encoding='utf-8-sig')
        except Exception as q_err:
            print(f"[WARN] 抓取品質趨勢 table 失敗 (EQQT_DB 讀取失敗): {q_err}")

        conn.close()

        if df.empty:
            print("[WARN] 資料庫無資料。")
            if accounts_changed:
                regen_from_backup("no new data")
            return

        print(f"[OK] 取得 {len(df)} 筆資料。")

        # ── 資料存儲生命週期管理 (設備運轉狀態快取) ─────────────────────
        # 確保設備運轉的數據累積到第 3 小時 (>3 小時) 的舊數據自動刪除，避免資料無限膨脹
        if not df.empty:
            # 設備狀態以觸發整點為 TIMESTAMP（所有當次抓取的資料一律對齊 trigger_ts）
            df['TIMESTAMP'] = trigger_ts
            print(f"[CLEANUP] 設備運轉快取已統一對齊觸發整點 {trigger_hour.strftime('%H:00')} (共 {len(df)} 筆)。")
        # ───────────────────────────────────────────────────────────────────

        # Step 4：備份資料
        if 'PLANT' in df.columns:
            df['PLANT'] = (
                df['PLANT'].astype(str).str.strip().str.upper()
                .replace({'KF': 'KF1'})
            )
        df.to_csv(os.path.join(SCRIPT_DIR, "latest_data_backup.csv"),
                  index=False, encoding='utf-8-sig')

        # Step 5：重建儀表板（使用最新 accounts.json 與警報 CSV）
        create_status_dashboard_fresh(df, "index.html")

        # Step 6：推送到 GitHub
        msg = f"Auto-update: {now_str}"
        if accounts_changed:
            msg += " [accounts updated]"
        push_to_github(msg)

        # Step 7：重新整理 RAM (釋放記憶體與 Standby cache)
        try:
            import gc
            import ctypes
            gc.collect()
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ctypes.windll.psapi.EmptyWorkingSet(handle)
            print("[OK] 記憶體 (RAM) 已重新整理並釋放。")
        except Exception as ram_err:
            pass

    except Exception as e:
        print(f"[ERROR] MSSQL 錯誤: {e}")
        if accounts_changed:
            print("[INFO] 帳號有異動，嘗試用備份資料更新密鑰庫...")
            regen_from_backup("MSSQL error")


# ── 程式進入點 ────────────────────────────────────────────────────────────────

def main():
    acquire_single_instance_lock()   # 確保只有一個實例在執行
    print("=" * 52)
    print("  EFplant 自動化排程服務 啟動")
    print("=" * 52)

    # 預先編譯 INDEX_ID 對照快取
    rebuild_index_id_map()
    
    # 啟動異步日誌伺服器
    api_thread = threading.Thread(target=start_logger_api_server, daemon=True)
    api_thread.start()

    # 啟動時立即執行一次
    fetch_data_and_update()

    # 排程：每小時整點（:00）與每半點（:30）各執行一次。
    # fetch_data_and_update 會先更新警報，再重建並發布設備狀態與風險總覽。
    # 註：趨勢資料時間戳仍以 floor('h') 對齊整點，故運轉率等趨勢圖 X 軸維持每小時、不受影響。
    schedule.every().hour.at(":00").do(fetch_data_and_update)
    schedule.every().hour.at(":30").do(fetch_data_and_update)

    next_run = schedule.next_run()
    print(f"\n排程已設定：設備狀態與 KF1 廠區運行風險總覽每 30 分鐘同步更新。")
    print(f"下次執行時間: {next_run.strftime('%Y-%m-%d %H:%M:%S')}")
    print("背景運行中... (Ctrl+C 停止)\n")

    try:
        while True:
            schedule.run_pending()
            time.sleep(20)  # 每 20 秒檢查一次，確保整點準時觸發
    except KeyboardInterrupt:
        print("\n服務已停止。")


if __name__ == "__main__":
    main()
