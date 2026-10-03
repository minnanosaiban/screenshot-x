@echo off
setlocal

rem ============================================================
rem  screenshot-x push (GitHub only, source distribution)
rem  Double-click after changes to push updates to GitHub.
rem
rem  First-time setup (already done for this folder):
rem    git init
rem    git branch -M main
rem    git remote add origin https://github.com/minnanosaiban/screenshot-x.git
rem
rem  chrome_profile/ ・ settings.json ・ __pycache__/ ・ ターミナル.txt は
rem  .gitignore で除外済み（ログイン情報・実アカウント名・実パスが入るため）。
rem ============================================================

echo === Push screenshot-x to GitHub ===
cd /d "%~dp0"
echo Current: %CD%

echo === Commit ^& Push to GitHub (main) ===
git add .
echo === Files to be committed ===
git status --short
echo (chrome_profile / settings.json must NOT appear above. Close this window to cancel.)
pause
git commit -m "Update screenshot-x" || echo No changes to commit
git push -u origin main
if %errorlevel% neq 0 (
    echo [ERROR] Git push failed. Check remote/auth settings.
    pause
    exit /b 1
)

echo === Done ===
pause
