@echo off
title EFplant 自動化排程服務
echo =========================================
echo 正在啟動 EFplant 排程，請勿關閉此視窗
echo =========================================
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python main.py
echo.
echo 發生預期外的中斷或程式錯誤！
pause
