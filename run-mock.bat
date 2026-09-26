@echo off
rem Double-click to try CAD Copilot without SolidWorks.
cd /d "%~dp0"
".venv\Scripts\python.exe" agent.py --mock
pause
