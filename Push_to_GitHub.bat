@echo off
cd /d "%~dp0"

echo.
echo ==========================================
echo   EFplant - GitHub Pages Push Tool
echo ==========================================
echo.

:: Step 1 - Stage index.html + health.json + known_equipment.json
git add index.html health.json known_equipment.json chart.html
if errorlevel 1 (
    echo [ERROR] git add failed. Is git installed and in PATH?
    echo.
    pause
    exit /b 1
)

:: Step 2 - Check if there are actual staged changes
git diff --staged --quiet
if not errorlevel 1 (
    echo [INFO] index.html is unchanged since last commit.
    echo        Nothing to push.
    echo.
    pause
    exit /b 0
)

:: Step 3 - Commit
git commit -m "Manual update"
if errorlevel 1 (
    echo [ERROR] git commit failed.
    echo.
    pause
    exit /b 1
)

:: Step 4 - Push to GitHub
echo Pushing to GitHub...
echo.
git push origin main
if errorlevel 1 (
    echo.
    echo [ERROR] git push FAILED. Please check:
    echo   1. Network connection is available
    echo   2. GitHub credentials in Windows Credential Manager
    echo   3. Remote config shown below:
    git remote -v
    echo.
    pause
    exit /b 1
)

echo.
echo ==========================================
echo   [SUCCESS] Pushed to GitHub Pages!
echo ==========================================
echo.
pause
