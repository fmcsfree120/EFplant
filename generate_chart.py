import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime

def create_interactive_chart(df: pd.DataFrame, output_path: str = "index.html"):
    """
    根據 DataFrame 產生高互動性質感的 HTML 圖表
    """
    # 確保 TIMESTAMP 是 datetime 格式，並依時間排序 (確保折線圖不會亂跳)
    df['TIMESTAMP'] = pd.to_datetime(df['TIMESTAMP'])
    df = df.sort_values(by='TIMESTAMP')
    
    # 確保 VALUE 是數值格式
    df['VALUE'] = pd.to_numeric(df['VALUE'], errors='coerce')
    
    # 移除 VALUE 為 NaN 的無效資料
    df = df.dropna(subset=['VALUE'])

    # 如果資料是空的，產生一個友善的提示網頁
    if df.empty:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write("<h1>目前沒有可用的數值資料 (No Data Available)</h1>")
        print(f"無有效數值，已產生空資料提示於 {output_path}")
        return

    # 使用 plotly express 建立折線圖
    # X 軸為時間，Y 軸為數值，並依照 TAGNAME 分顏色
    # 若 TAGNAME 皆為空，我們嘗試使用 EQNO 作為分類
    color_col = 'TAGNAME' if df['TAGNAME'].notna().any() else 'EQNO'
    
    fig = px.line(
        df, 
        x='TIMESTAMP', 
        y='VALUE', 
        color=color_col,
        title="EFplant 遠端設備數據即時監控圖表 (MSSQL Data)",
        labels={
            "TIMESTAMP": "時間 (Timestamp)",
            "VALUE": "監測數值 (Value)",
            color_col: "標籤/設備名稱"
        },
        markers=True, # 顯示資料點
        template="plotly_dark" # 深色質感主題
    )

    # 進階樣式微調：讓圖表看起來更 premium
    fig.update_layout(
        font=dict(family="Inter, Roboto, sans-serif", size=14),
        hovermode="x unified", # 滑鼠懸浮時同時顯示所有該時間點的數值
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1
        ),
        margin=dict(l=40, r=40, t=80, b=40),
        plot_bgcolor='rgba(17,17,17,1)',
        paper_bgcolor='rgba(17,17,17,1)'
    )

    # 針對 X 軸與 Y 軸的外觀做細部美化
    fig.update_xaxes(
        showgrid=True, gridwidth=1, gridcolor='rgba(255,255,255,0.1)',
        zeroline=False
    )
    fig.update_yaxes(
        showgrid=True, gridwidth=1, gridcolor='rgba(255,255,255,0.1)',
        zeroline=True, zerolinewidth=1, zerolinecolor='rgba(255,255,255,0.2)'
    )

    # 輸出成獨立的 HTML 檔案
    # include_plotlyjs="cdn" 可以讓 HTML 檔案大幅縮小 (大約 3MB -> 幾 KB)，透過網路載入 js
    fig.write_html(output_path, include_plotlyjs="cdn", full_html=True)
    print(f"圖表已成功生成：{output_path}")

if __name__ == "__main__":
    # 測試腳本：讀取階段一產生的 test_data.csv 來測試圖表生成
    try:
        test_df = pd.read_csv("test_data.csv")
        create_interactive_chart(test_df, "index.html")
    except FileNotFoundError:
        print("找不到 test_data.csv，請先執行 db_test.py 產生測試資料。")
