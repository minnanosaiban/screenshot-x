@echo off
rem このファイルをダブルクリックするとアプリが起動する。
rem pythonw を使うので黒いコンソール画面は出ない。
cd /d "%~dp0"
start "" pythonw screenshot_x_app.py
