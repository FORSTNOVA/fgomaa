@echo off
chcp 65001 >nul
title FGO MAA 自动化助手 - 启动器

echo =======================================================
echo          正在启动 FGO MAA 现代化客户端...
echo =======================================================

if exist "%~dp0dist\FGO-MAA\FGO-MAA.exe" (
    start "" "%~dp0dist\FGO-MAA\FGO-MAA.exe"
    exit /b 0
)

if exist "%~dp0FGO-MAA.exe" (
    start "" "%~dp0FGO-MAA.exe"
    exit /b 0
)

if exist "%~dp0.venv\Scripts\python.exe" (
    start "" "%~dp0.venv\Scripts\python.exe" "%~dp0app.py"
    exit /b 0
)

echo [错误] 未找到 FGO-MAA.exe 客户端程序，请检查文件是否完整！
pause
