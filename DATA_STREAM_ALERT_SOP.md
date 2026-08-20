# EFplant 資料串流中斷與恢復通知 SOP

## 1. 適用範圍

本 SOP 只監控 EFplant 的兩類前台資料：

- 設備運轉：依廠區及設備種類分流。
- 品質趨勢：依廠區及既有趨勢圖分類分流。

廠區風險警報資料（`latest_alarm_history_backup.csv`、`latest_alarm_history_other_backup.csv`）不屬於本 SOP，不得納入中斷通知。

## 2. 判定方式

1. 每次 `main.py` 在整點或半點執行資料更新時，同步執行檢查。
2. 以 MSSQL 原始資料時間判定，不使用前台為顯示而對齊過的整點時間。
3. 每個「廠區＋看板＋趨勢圖／設備種類」各自保存最後資料時間。
4. 最後資料時間嚴格超過 6 小時才判定中斷；剛好 6 小時不告警。
5. MSSQL 查詢整體失敗或本輪缺少某資料流時，沿用狀態檔的最後成功時間繼續判定，不得把查詢失敗視為恢復。

## 3. 通知與去重

- 通知目標固定為 Synology Chat 的 `DID異常告警` 頻道。
- `py/synology_chat_notify.py` 優先讀取 `DIDAlertWebhook.txt`；尚未建立專用檔時沿用既有 `ChatWebhook.txt`。管理者必須確認該 Webhook 綁定 `DID異常告警`。
- Webhook URL 是秘密資料，不得輸出、提交或寫入文件。
- 目前 Synology NAS 以內網 IP 提供 HTTPS，憑證名稱與 IP 不一致；專用發送器比照既有 Chat 通道停用該次請求的憑證名稱驗證。此例外只限指定的本機 Webhook 設定，不得擴大套用到其他網路請求。
- 同一資料流進入中斷時只通知一次；持續中斷不重複發送。
- 傳送失敗不寫成已通知，下一個半小時週期重試。
- 中斷中的資料流重新取得 6 小時內新資料時，只發送一次恢復／回補通知。

中斷訊息格式：

```text
廠區:xxx，品質趨勢(趨勢圖名稱)及設備運轉(設備種類)資料中斷，請檢查資料庫串流是否正常
```

恢復訊息格式：

```text
廠區:xxx，品質趨勢(趨勢圖名稱)及設備運轉(設備種類)資料已回補或恢復抓到新資料
```

同一廠區同一輪有多個異常時合併成一則訊息，避免洗版。

## 4. 狀態與程式位置

- 判定器：`EFplant/data_stream_monitor.py`
- 排程入口：`EFplant/main.py`
- Chat 發送器：`YPput/py/synology_chat_notify.py`
- 本機狀態：`EFplant/data_stream_alert_state.json`

狀態檔可能包含廠區與資料流健康資訊，必須保持在 `.gitignore`，不得推送到公開 GitHub Pages。

## 5. 驗證與維運

修改後至少執行：

```powershell
cd C:\Users\U01572\Documents\EFplant
.\.venv\Scripts\python.exe -m py_compile main.py data_stream_monitor.py test_data_stream_monitor.py
.\.venv\Scripts\python.exe -m unittest -v test_data_stream_monitor.py

cd C:\Users\U01572\Documents\YPput\py
.\myenv\Scripts\python.exe -m py_compile synology_chat_notify.py
```

測試一律注入假 sender，不得向正式頻道發送測試訊息。正式啟用後，從 `efplant_autoupdate.log` 確認每半小時檢查沒有例外；不得為套用此功能而全域終止 `python.exe`。

若要更換通知頻道，先在 Synology Chat 建立該頻道的 Incoming Webhook，將 URL 以 UTF-8 寫入 `DIDAlertWebhook.txt`，並確認檔案不受版本控制追蹤；不得把 URL 寫死於 Python。
