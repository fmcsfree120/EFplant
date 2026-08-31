# EFplant Frontend Update SOP

## Purpose

This procedure prevents repeated "frontend is still old" work by separating three different problems:

1. The correct GitHub Pages site was not checked.
2. Browser or service worker cache is still serving old files.
3. The repository only tracks generated frontend files, while Python and Markdown sources are ignored.

## Current Deployment Facts

- Canonical Git remote: `https://github.com/fmcsfree120/EFplant.git`
- Canonical GitHub Pages URL: `https://fmcsfree120.github.io/EFplant/`
- The previously checked URL `https://cruz6739.github.io/forsevice-netlify/` is not this repository's Pages deployment.
- The tracked frontend files are:
  - `index.html`
  - `data.enc`
  - `health.json`
  - `service-worker.js`
- Project source files such as `generate_dashboard.py` and Markdown files are ignored by `.gitignore`.

## Root Cause Found On 2026-07-18

The GitHub Pages site for `fmcsfree120/EFplant` was already updated online:

- `https://fmcsfree120.github.io/EFplant/` contained `th-classify-20260718-2`.
- `https://fmcsfree120.github.io/EFplant/service-worker.js` contained `efplant-sw-v19-th-classify-force-refresh`.

The alternate URL `https://cruz6739.github.io/forsevice-netlify/` did not contain the new version token and is a different deployment target. If a user opens that URL, pushing `fmcsfree120/EFplant` will not change the visible frontend.

A second structural issue is that `.gitignore` ignores `*.py`, so source fixes in `generate_dashboard.py` are local-only unless explicitly force-added or separately documented. Generated static files can still be pushed, but the rule change itself is not preserved in Git by default.

## GitHub Pages Build Recovery (2026-08-06)

### Symptom and evidence

`git push origin main` can succeed while the canonical Pages URL still serves an
older `service-worker.js`. This is a deployment failure, not automatically a
browser-cache problem. A release is complete only after the canonical Pages URL
and its service worker expose the new version tokens.

On 2026-08-06 the legacy Pages builder entered `errored` status even for an
automatic update that changed only `data.enc` and `health.json`. The last
successful public version consequently remained online. Use the authenticated
GitHub API to inspect Pages configuration, legacy builds, and Actions runs:

```text
GET /repos/fmcsfree120/EFplant/pages
GET /repos/fmcsfree120/EFplant/pages/builds?per_page=5
GET /repos/fmcsfree120/EFplant/actions/workflows/deploy-pages.yml/runs?per_page=3
```

Do not print or persist the Git credential token while making these checks.

### Confirmed release discipline (2026-08-06)

The standard publishing source is the existing `main` branch at repository
root (`main /`). Do not create a temporary Pages branch, switch the Pages
source, add a replacement workflow, deactivate deployments, delete the Pages
site, or retry multiple deployment paths as a first response to a delay.

For a normal release, regenerate the approved public frontend files, stage only
those files, commit and push `main`, then request or wait for the Pages build.
Keep the currently serving site in place while the replacement is processed.

The Pages build API may retain an `errored` legacy-build status even when the
canonical URL is serving the newly pushed static files. Therefore a release is
not judged from Git push or build status alone: verify the canonical URL,
service-worker version token, and SHA-256 hashes of `data.enc` and
`health.json` against the local committed files.

If the canonical files do not match after a single `main` build request, stop
configuration churn and preserve the live site. Escalate through the
organization's GitHub administrator or support entitlement rather than opening
branches or changing publishing mechanisms.

## 自動更新架構與 2026-08-07 停更事故 SOP

### Windows 排程與 Python 各自負責什麼

EFplant 採用兩層排程，兩者用途不同：

1. Windows 工作排程器的 `EFplant AutoUpdate` 是開機啟動器與程序監督者。
   它在 Windows 開機時直接執行 `.venv\Scripts\python.exe main.py`，並讓沒有
   登入桌面的情況也能啟動服務。
2. `main.py` 才是資料更新排程器。啟動時先更新一次，之後於每小時 `:00`
   和 `:30` 抓取資料，另每 2 分鐘檢查一次待發布的 GitHub commit。

資料流程如下：

```text
Windows 開機
  -> EFplant AutoUpdate
  -> python main.py（常駐）
  -> MSSQL / 警報資料 -> Dashboard -> Git commit/push -> GitHub Pages
```

工作排程器顯示 `0x41301` 代表工作「目前正在執行」，不是錯誤碼。正常常駐時
狀態應為 `Running`；若狀態是 `Ready`，表示 Python 已經不在執行。

Python 可以自行完成每 30 分鐘排程，但已停止的 Python 無法自行重新啟動，
也無法在 Windows 重開機後憑空啟動。因此不建議完全移除 Windows 排程。
若不使用工作排程器，必須用 Windows Service、NSSM、容器 restart policy 等
另一個作業系統層的 supervisor 取代；只把 Python 放進無限迴圈並不等價。

`main.py` 會在每輪排程工作完全結束後檢查自身檔案版本。偵測到新版時，服務會
自行重載新版程式，因此日常程式更新不需要人工停止或透過 Windows 工作排程器換版。
Windows 工作排程器只保留為開機啟動與程序異常死亡後的監督者；它不能由已停止的
Python 程式取代。

### 事故：前台全部停在 8/6 23:00

事故證據：

- 前台設備時間停在 `8/6 23:00`。
- 本機 `health.json` 最後更新為 `2026-08-06T23:32:55`。
- 最後自動更新 commit 時間約為 `23:30`。
- `EFplant AutoUpdate` 最後結果為 `1`，狀態回到 `Ready`。
- EFplant 單一實例鎖 TCP `127.0.0.1:47312` 沒有程序占用。

直接原因是常駐 Python 已異常退出，所以後續所有半小時更新都沒有被觸發。
原版本的 `flush_pending_release()` 在例外保護外先執行 `git fetch`；短暫網路、
GitHub 或 Git 錯誤會穿透 `schedule.run_pending()`，終止整個主程序。同時舊版
沒有持久化 runtime log，因此事故當下的最後一個例外已無法事後還原。

2026-08-07 已完成以下永久修正：

- 所有排程工作經過 `run_resilient_job()`，單一工作失敗不再終止主迴圈。
- Git fetch/push 失敗只記錄警告，保留 commit 並於稍後重試。
- 背景輸出寫入 `efplant_autoupdate.log`，保留未來事故證據。
- `Setup_Service.ps1` 改為讓工作排程器直接監督 Python，而不是啟動後立即退出的
  中介程序。

### 停更時的標準診斷

在專案目錄執行；查詢工作排程可能需要系統管理員 PowerShell：

```powershell
Get-Content health.json
git log -5 --date=iso --pretty=format:'%h %ad %s'
schtasks /Query /TN "EFplant AutoUpdate" /V /FO LIST
Get-NetTCPConnection -LocalPort 47312 -ErrorAction SilentlyContinue
Get-Content -Encoding UTF8 efplant_autoupdate.log -Tail 100
```

判讀順序：

1. `health.json` 是否超過一個更新週期未前進。
2. 工作是否為 `Running`。`Ready` 加上最後結果非 `0` 代表程序已失敗。
3. `47312` 是否為 `Bound`。它是 EFplant 專用程序鎖；沒有占用代表服務未執行。
4. 從 `efplant_autoupdate.log` 最後一個 traceback 判斷是 MSSQL、Git、檔案權限
   或產生器錯誤。
5. 不可直接結束所有 `python.exe`；其他專案也使用 Python。若必須停止，只能先由
   `47312` 的 `OwningProcess` 確認 EFplant PID。

### 標準恢復程序

1. 先確認修復版可編譯：

```powershell
.\.venv\Scripts\python.exe -m py_compile main.py generate_dashboard.py github_pages_release_gate.py
```

2. 若前台資料已過期，執行一次性更新。此命令完成後會重新抓取資料、生成前台並
   嘗試發布，但不會留下另一個常駐實例：

```powershell
.\.venv\Scripts\python.exe -c "import main; main.fetch_data_and_update()"
```

3. 若輸出出現 `.git/index.lock: Permission denied`，確認沒有其他 Git 程序後，使用
   具有 repository 寫入權限的終端執行：

```powershell
git add index.html data.enc health.json service-worker.js
git commit -m "Recover EFplant frontend update"
git push origin main
```

4. 啟動既有常駐工作：

```powershell
schtasks /Run /TN "EFplant AutoUpdate"
```

5. 驗證它不是只完成一次更新後退出：

```powershell
schtasks /Query /TN "EFplant AutoUpdate" /V /FO LIST
Get-NetTCPConnection -LocalPort 47312 -ErrorAction SilentlyContinue
Get-Content -Encoding UTF8 efplant_autoupdate.log -Tail 50
```

預期工作為 `Running`、結果可顯示 `0x41301`，且 `47312` 為 `Bound`。接著必須等到
下一個 `:00` 或 `:30`，確認日誌再次出現「排程作業啟動」。

6. 驗證正式站點，而不只驗證 Git push：

```powershell
Invoke-WebRequest -Uri 'https://fmcsfree120.github.io/EFplant/health.json' `
  -UseBasicParsing -TimeoutSec 30 | Select-Object StatusCode,Content
```

正式 `health.json` 必須與本機一致；高風險發布另需比對正式 `data.enc` 與本機檔案的
SHA-256。右上角 `TIMESTAMP` 是整點資料槽，因此 `13:30` 完成更新後仍顯示
`13:00` 屬正常行為；應以 `health.json` 和 runtime log 判斷半點更新是否發生。

### 工作排程遺失或動作錯誤時

只有在工作不存在、執行路徑錯誤或無法開機啟動時，才以系統管理員 PowerShell
執行 `Setup_Service.ps1` 重建。這會建立最高權限的開機常駐工作，屬持久化系統
變更，執行前必須取得系統管理員／系統擁有者明確授權。正常停更只需診斷並啟動
既有工作，不應先刪除重建。

## Standard Update Procedure

Every completed frontend source modification must run this procedure
immediately. Do not leave a completed change only in source code or local
generated files: regenerate, verify, commit, push, and verify the canonical
Pages URL before reporting completion.

### Single deployment queue

GitHub Pages cancels an older deployment when a newer dynamic Pages request is
queued for the same `main` branch. All releases must therefore use the
repository-local release gate:

- `.githooks/pre-push` calls `github_pages_release_gate.py` before every push.
- If a `pages build and deployment` run is `queued` or `in_progress`, the push
  is deferred with exit code 75; do not use **Re-run jobs** or manually request
  another Pages build.
- `main.py` retains its generated commit locally and retries pending
  `origin/main..HEAD` commits every two minutes after the active deployment
  finishes.
- Manual releases must treat an exit-75 push as queued work: wait for the
  automatic retry or run the same normal `git push origin main` once the gate
  reports no active deployment. Never bypass the hook with `--no-verify`.

This serializes the half-hour auto-update and human frontend releases without
creating branches or changing the Pages publishing source.

1. Confirm the intended frontend URL before doing any cache work.

```powershell
git remote -v
```

Expected canonical URL:

```text
https://fmcsfree120.github.io/EFplant/
```

2. Bump all frontend cache version tokens.

Update these values together:

- `generate_dashboard.py`: `CACHE_EPOCH`
- `generate_dashboard.py`: `service-worker.js?v=...`
- `service-worker.js`: `SW_VER`

Use a unique version, for example:

```text
th-classify-YYYYMMDD-N
efplant-sw-vNN-purpose
```

3. Regenerate frontend output.

```powershell
python -m py_compile generate_dashboard.py
.\.venv\Scripts\python.exe generate_dashboard.py
```

4. Verify generated files locally.

```powershell
Select-String -LiteralPath 'index.html','service-worker.js' -Pattern 'th-classify|efplant-sw'
```

5. Stage only frontend deployment files.

```powershell
git add index.html data.enc health.json service-worker.js
git diff --cached --stat
```

Do not accidentally stage local secrets or deployment helpers, especially:

- `AnthropicKey.txt`
- `openaiKEY.txt`
- `accounts.json`
- unreviewed `.ps1`, `.bat`, or local task scripts

As of 2026-08-01, `AnthropicKey.txt` and all `*.ps1` files are covered by
`.gitignore`, so a plain `git add .` will no longer stage them. See
"Secret and Deployment File Protection" below for the standing rule.

6. Commit and push.

```powershell
git commit -m "Force refresh frontend cache"
git push origin main
```

7. Verify GitHub Pages online, not only Git.

```powershell
Invoke-WebRequest -Uri 'https://fmcsfree120.github.io/EFplant/' -UseBasicParsing -TimeoutSec 30 |
  Select-Object StatusCode,@{Name='CacheEpoch';Expression={ if($_.Content -match "th-classify-[0-9-]+"){$matches[0]}else{'NOT_FOUND'} }}

Invoke-WebRequest -Uri 'https://fmcsfree120.github.io/EFplant/service-worker.js' -UseBasicParsing -TimeoutSec 30 |
  Select-Object StatusCode,@{Name='SW';Expression={ if($_.Content -match "efplant-sw-v[0-9a-zA-Z\-]+"){$matches[0]}else{'NOT_FOUND'} }}
```

8. If online is new but the browser is old, the problem is client-side or wrong URL.

Required checks:

- Confirm the browser address is exactly `https://fmcsfree120.github.io/EFplant/`.
- Do not use `https://cruz6739.github.io/forsevice-netlify/` for this project.
- Press the in-page `RESET SYSTEM CACHE & RELOAD` button.
- If still stale, use DevTools Application tab:
  - Unregister all service workers for the site.
  - Clear site storage.
  - Reload with cache disabled.

## Secret and Deployment File Protection

`EFplant`'s remote (`fmcsfree120/EFplant`) is a **public** repository that
also publishes to GitHub Pages. Any file that reaches a commit can become
publicly visible, so local secrets and internal deployment helpers must stay
outside Git entirely rather than relying on manual review at commit time.

### Standing `.gitignore` Rules (since 2026-08-01)

- `AnthropicKey.txt` is explicitly listed next to `openaiKEY.txt` in the
  "帳密與敏感設定" block of `.gitignore`.
- `*.ps1` is ignored the same way `*.bat` and `*.vbs` already are, so all
  local deployment/task scripts (e.g. `send_weekly_report.ps1`,
  `deploy_eshplantform.ps1`, `Setup_Weekly_*.ps1`, `Deploy_Weekly_*.ps1`) are
  excluded by default. They contain internal IPs, SMTP hosts, and recipient
  addresses that must not reach the public repo.

### Rule for New Secret or Internal-Only Files

Whenever a new API key file, credential file, or internal deployment script
is added to this project:

1. Add its exact filename (for a one-off secret file) or its extension
   pattern (for a whole class of local-only scripts) to `.gitignore` in the
   same commit that introduces the file — do not defer this.
2. Never rely on remembering to exclude it manually with `git add <specific files>`;
   pattern-based `.gitignore` rules are the actual safety net.
3. Verify with `git status --short` that the new file does not appear as
   untracked (`??`) before it is left in place.
4. If a secret or internal script was ever force-added (`git add -f`) or
   committed before an ignore rule existed, adding the ignore rule alone is
   not enough — treat the exposed value as compromised and rotate it.

## When Source Logic Changes

If a fix changes `generate_dashboard.py`, remember that `.gitignore` ignores `*.py`. The generated frontend can be updated, but the Python rule change is not tracked unless you intentionally force-add it:

```powershell
git add -f generate_dashboard.py
```

Only force-add source files after confirming they do not contain secrets or local-only paths that should stay private.

## Completion Criteria

Frontend update is complete only when all are true:

- `git push origin main` succeeds.
- `git ls-remote origin refs/heads/main` matches local `git rev-parse HEAD`.
- The canonical Pages URL returns the expected `CACHE_EPOCH`.
- The canonical `service-worker.js` returns the expected `SW_VER`.
- Any report of "still old" includes the exact URL being viewed.

## Permanent Rollback Protection

The half-hour updater must never retain a one-time import of the dashboard
generator. `main.py` reloads `generate_dashboard.py` from disk for every
dashboard rebuild, so frontend source edits take effect without restarting the
resident scheduler.

The reloaded generator must also reload `alarm_filter.py` at the same boundary.
Otherwise a resident process can retain the old filter module while loading a
new generator that expects newly added shared exclusion helpers, causing data
fetch to succeed but dashboard generation and `health.json` publication to stop.

Two release guards are mandatory:

- `main.py` compares the generator `CACHE_EPOCH` with the generated
  `index.html` before staging or pushing.
- Git uses `.githooks/pre-commit` through `core.hooksPath=.githooks`. If the
  staged `index.html` differs from the generator version, the hook rebuilds
  with the latest generator, stages the repaired deployment files, and only
  allows the commit after the versions match. If repair fails, it rejects and
  restores the generated deployment files.

Verify the repository-local hook configuration with:

```powershell
git config --local --get core.hooksPath
```

Expected value:

```text
.githooks
```

## Plant Classification Before Publishing

All source rows whose `PLANT` value is `KF` must be normalized to `KF1`
before equipment grouping, known-equipment registration, KPI calculation,
trend compilation, or frontend navigation generation.

- `KF` must never be published as a standalone plant tab.
- Equipment status, quality trends, run-rate trends, and alarm history share
  the canonical `KF1` label.
- New ingestion paths must call the shared plant-label normalization before
  grouping or deduplication.
- Release verification must confirm that generated navigation contains `KF1`
  and does not contain a `data-plant="KF"` button.

### Trend Classification Rules

- Chemical level tags using `_LT_PV`, `_LS_PV`, or `_WL_PV` are valid level
  measurements and must enter the bulk-chemical trend classification.
- Equipment or tags beginning with `WDUST` are authoritative wet-dust
  identifiers and must be classified as `濕式集塵靜壓` before any generic
  `DUST` dry-dust rule is evaluated.

### Weekly Report Plant Coverage

- The weekly management report must canonicalize `KF` to `KF1` before any
  grouping or scoring.
- Its plant coverage is the union of the frontend equipment, quality, and
  run-rate CSV files; a plant present in any source must appear in the weekly
  health assessment.
- Static plant order controls display order only and must never act as a
  filtering whitelist.

### Alarm Risk Plant Isolation

- KF1 keeps its established `ALM_KF` ingestion path and dedicated
  `latest_alarm_history_backup.csv`; other plants must never be relabeled or
  merged into that file.
- HF, HJ1, HJ2, LC2, LC3, PCB, S2, S2A, S3, T2A, and TH use their matching
  `ALM_DB.dbo.ALM_*` tables and the separate
  `latest_alarm_history_other_backup.csv`.
- All connected alarm paths, including KF1, use the same gap recovery rule:
  normal updates read the next 24 hours after that plant's own CSV anchor. If
  that interval is empty but the source table has later data, the updater
  backfills the retained 14-day interval, de-duplicates it, and logs the gap.
  A missing interval must never permanently stall a plant's anchor.
- Every plant uses the same alarm-risk calculations and presentation, but the
  source frame must be filtered by `PLANT` before aggregation.
- Alarm panels use the alarm CSV file modification time as the last successful
  synchronization time. The daily trend always shows the seven completed
  calendar days immediately before the synchronization date (today - 7 days
  through today - 1 day), producing exactly seven comparable bars. The 24-hour
  heatmap plus both TOP 10 panels use the actual preceding 24 hours; they must
  never move their window backward to end at the last alarm event.
- Display synchronization time and last valid alarm-event time as separate
  fields. A successful sync with no valid events shows an explicit empty state.
  If synchronization is more than two hours old, show a source-interruption
  warning and suppress zero-alarm and risk-ranking interpretations.
- The empty state for the TOP 10 high-risk-equipment panel is worded
  `近 24 小時無高風險警報`; do not describe it as an absence of all valid alarms.
- The TOP 10 alarm-recovery panel is shared by every connected plant: display
  only individual recoveries lasting at least 30 minutes, rank by duration,
  and show up to 10 records without padding short-duration entries.
- In the TOP 10 alarm-recovery panel, the `VALUE` column displays `ALM_VALUE`
  from the non-OK alarm-entry record. The following OK record closes the
  recovery duration but its value must never replace the alarm-entry value.
- The mobile recovery table has six visible columns. Its mobile CSS must assign
  widths to columns 1 through 6 totaling 100%; the sixth `最長單次耗時` column
  must remain visible without horizontal clipping.
- In the TOP 10 high-risk-equipment panel, each bar width is its alarm-event
  count divided by the total event count of the currently ranked records; rows
  are sorted by event count descending, then risk score descending.
- Plants without a connected alarm table retain the standard pending section;
  missing source coverage must not be interpreted as zero alarms.
- HF remains a required equipment navigation tab. Until equipment status is
  available, its equipment section uses the standard construction placeholder
  while its connected alarm-risk section remains visible below it.
- HF quality-history rows are included in the shared trend plant list even
  while HF equipment status and run-rate sources remain unavailable. HF must
  not receive mock equipment or run-rate data.
- HF chemical `EQNAME` suffixes `_1` and `_2` identify separate tanks of the
  same chemical and appear as `HF 槽1` / `HF 槽2` series in the existing
  cross-plant chemical charts. `CUSO4`, `FECL3`, and `MM6800` are supported
  chemical names. HF `WT...PH` data joins the shared `出口pH` chart.

### KF1 Chiller Running State

- KF1 `CHU*` equipment whose source tag contains `_LOAD` uses a low-range
  load signal and must not use the standard analog `>=10` running threshold.
- For this specific source, absolute load `>0.1` is `RUN`; `<=0.1` is `STOP`.
  The deadband prevents sensor zero drift from being reported as operation.
- All other plants and equipment retain the standard running-state rules.

### Explicit Motor-Current Running State

- Equipment whose description explicitly contains `電流`, or whose tag uses
  `_VFD_A` or `_PM_I_AVG`, is treated as an ampere-based motor-current source.
- For these explicit current signals, absolute current `>1 A` is `RUN`;
  `<=1 A` is `STOP`.
- This rule runs before generic integer `0/1` handling. Unconfirmed `VFD_FB`,
  `VFD_PV`, voltage, pressure, and differential-pressure signals are excluded.

## Account Page Classification Prototype

- Local `account.csv` is the sole login and page-classification source;
  `accunt.txt` and `accounts.json` are retired and must not be used as fallback.
  `account.csv` must remain ignored
  by Git because it contains plaintext-equivalent passwords. Password values use
  the `text:` prefix so spreadsheet software cannot discard leading zeroes; the
  generator removes only that first prefix before PBKDF2 derivation.
- Supported roles are `viewer` and `submitter`. A viewer receives the existing
  dashboard navigation only. A submitter with `allowed_pages=inspection`
  receives one plant-scoped navigation label, formatted as
  `<PLANT>風險巡檢人工填報`, and no dashboard navigation.
- Each account profile is encrypted with that password-derived key inside
  `data.enc`. Do not publish usernames, roles, plant permissions, or passwords
  as plaintext static assets.
- The inspection form is currently a page-classification and mobile-layout
  prototype. Its submit action must not claim that CSV persistence succeeded
  until a separately approved HTTPS write path and idempotent CSV merge process
  exist.
- This prototype controls normal page presentation; it does not yet create
  cryptographically separate dashboard and inspection payloads. If page data
  confidentiality becomes a requirement, stop and split the encrypted payloads
  before treating role-based hiding as a security boundary.
