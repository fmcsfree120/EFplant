import pymssql
import pandas as pd
import schedule
import time
import subprocess
import os
import sys
import json
import socket
from datetime import datetime
from generate_dashboard import create_status_dashboard
import warnings

warnings.filterwarnings('ignore', '.*pandas only supports SQLAlchemy.*')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

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
        subprocess.run(
            ["git", "add", "index.html", "health.json", "known_equipment.json", "chart.html"],
            cwd=SCRIPT_DIR, check=True
        )
        result = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=SCRIPT_DIR, capture_output=True
        )
        if result.returncode == 0:
            subprocess.run(["git", "push"], cwd=SCRIPT_DIR, check=True)
            print("✅ 成功推送到 GitHub！")
        else:
            print("ℹ️ 內容無變動，略過推送。")
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
        create_status_dashboard(backup_df, "index.html")
        push_to_github(f"Account sync ({reason}): {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        print("[OK] 已用備份資料重建密鑰庫並推送。")
    except Exception as e:
        print(f"[ERROR] 備份重建失敗: {e}")


# ── 主排程任務 ────────────────────────────────────────────────────────────────

def fetch_data_and_update():
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"\n{'='*52}")
    print(f"  [{now_str}] 排程作業啟動")
    print(f"{'='*52}")

    # Step 1：比對帳號是否異動
    accounts_changed = sync_accounts()

    try:
        # Step 2：連線 MSSQL 抓取最新資料
        print("連線 MSSQL 抓取資料...")
        cfg  = load_config()['mssql']
        conn = pymssql.connect(
            cfg['server'], cfg['user'], cfg['password'],
            cfg['database'], charset=cfg.get('charset', 'utf8')
        )
        query = """
        SELECT * FROM dbo.EQSTS_DB
        WHERE TIMESTAMP >= DATEADD(hour, -1, (SELECT MAX(TIMESTAMP) FROM dbo.EQSTS_DB))
        ORDER BY TIMESTAMP DESC
        """
        df = pd.read_sql(query, conn)
        conn.close()

        if df.empty:
            print("[WARN] 資料庫無資料。")
            if accounts_changed:
                regen_from_backup("no new data")
            return

        print(f"[OK] 取得 {len(df)} 筆資料。")

        # Step 3：備份資料
        df.to_csv(os.path.join(SCRIPT_DIR, "latest_data_backup.csv"),
                  index=False, encoding='utf-8')

        # Step 4：重建儀表板（使用最新 accounts.json）
        create_status_dashboard(df, "index.html")

        # Step 5：推送到 GitHub
        msg = f"Auto-update: {now_str}"
        if accounts_changed:
            msg += " [accounts updated]"
        push_to_github(msg)

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

    # 啟動時立即執行一次
    fetch_data_and_update()

    # 排程：每小時整點（:00）執行
    schedule.every().hour.at(":00").do(fetch_data_and_update)

    next_run = schedule.next_run()
    print(f"\n排程已設定：每小時整點自動更新。")
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
