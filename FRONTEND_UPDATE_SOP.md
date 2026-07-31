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
  - `known_equipment.json`
- Project source files such as `generate_dashboard.py` and Markdown files are ignored by `.gitignore`.

## Root Cause Found On 2026-07-18

The GitHub Pages site for `fmcsfree120/EFplant` was already updated online:

- `https://fmcsfree120.github.io/EFplant/` contained `th-classify-20260718-2`.
- `https://fmcsfree120.github.io/EFplant/service-worker.js` contained `efplant-sw-v19-th-classify-force-refresh`.

The alternate URL `https://cruz6739.github.io/forsevice-netlify/` did not contain the new version token and is a different deployment target. If a user opens that URL, pushing `fmcsfree120/EFplant` will not change the visible frontend.

A second structural issue is that `.gitignore` ignores `*.py`, so source fixes in `generate_dashboard.py` are local-only unless explicitly force-added or separately documented. Generated static files can still be pushed, but the rule change itself is not preserved in Git by default.

## Standard Update Procedure

Every completed frontend source modification must run this procedure
immediately. Do not leave a completed change only in source code or local
generated files: regenerate, verify, commit, push, and verify the canonical
Pages URL before reporting completion.

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
- HF, HJ1, HJ2, LC2, LC3, PCB, S2, S2A, S3, and T2A use their matching
  `ALM_DB.dbo.ALM_*` tables and the separate
  `latest_alarm_history_other_backup.csv`.
- Every plant uses the same alarm-risk calculations and presentation, but the
  source frame must be filtered by `PLANT` before aggregation.
- Plants without a connected alarm table retain the standard pending section;
  missing source coverage must not be interpreted as zero alarms.
- HF remains a required equipment navigation tab. Until equipment status is
  available, its equipment section uses the standard construction placeholder
  while its connected alarm-risk section remains visible below it.

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
