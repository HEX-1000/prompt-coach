@echo off
rem Starts the prompt coach TUI in the current console. install.ps1 must have run once.
chcp 65001 >nul
set PYTHONUTF8=1
"%~dp0.venv\Scripts\python.exe" -B "%~dp0coach_tui.py"
