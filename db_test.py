import pymssql
import pandas as pd
import json
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def load_config():
    """讀取 config.json（MSSQL 帳密）— 不推送至 GitHub。"""
    path = os.path.join(SCRIPT_DIR, "config.json")
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def test_connection():
    cfg = load_config()['mssql']

    try:
        print(f"嘗試連線至 {cfg['server']} ...")
        conn = pymssql.connect(
            cfg['server'], cfg['user'], cfg['password'],
            cfg['database'], charset=cfg.get('charset', 'utf8')
        )
        print("連線成功！")

        query = "SELECT TOP 50 * FROM dbo.EQSTS_DB ORDER BY TIMESTAMP DESC"
        print(f"執行查詢: {query}")
        df = pd.read_sql(query, conn)

        print(f"\n成功抓取 {len(df)} 筆資料。")

        csv_filename = os.path.join(SCRIPT_DIR, "test_data.csv")
        df.to_csv(csv_filename, index=False, encoding='utf-8')
        print(f"資料已儲存為 test_data.csv（此檔案已被 .gitignore 保護）。")

        print("\n=== 資料預覽 ===")
        try:
            print(df.head())
        except Exception:
            print("終端機編碼無法顯示所有字元，但資料已成功儲存。")

        conn.close()
    except Exception as e:
        print(f"發生錯誤: {e}")


if __name__ == "__main__":
    test_connection()
