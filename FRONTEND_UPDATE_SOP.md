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
- Git uses `.githooks/pre-commit` through `core.hooksPath=.githooks`. The hook
  rejects and restores generated deployment files if the staged `index.html`
  version differs from the generator version.

Verify the repository-local hook configuration with:

```powershell
git config --local --get core.hooksPath
```

Expected value:

```text
.githooks
```
