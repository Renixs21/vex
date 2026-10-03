@echo off
cd /d "%~dp0"
start "" pythonw -m vex_ui.app --mode window %*
