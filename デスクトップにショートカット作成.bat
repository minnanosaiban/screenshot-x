@echo off
chcp 65001 >nul
rem このファイルをダブルクリックすると、デスクトップに
rem 「screenshot-x」という名前でアイコン付きのショートカットができる。
rem ショートカットは start_app.bat を起動する（起動時の見た目は変わらない）。
rem 何度実行しても上書きされるだけなので安全。
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_shortcut.ps1"
if %errorlevel% neq 0 (
    echo [ERROR] shortcut creation failed.
    pause
    exit /b 1
)
pause
