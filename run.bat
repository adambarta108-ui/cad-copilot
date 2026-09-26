@echo off
rem Double-click to run CAD Copilot with SolidWorks.
cd /d "%~dp0"
".venv\Scripts\python.exe" agent.py
pause
