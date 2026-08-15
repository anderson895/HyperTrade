@echo off
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" (
    echo Virtual environment missing. Create it first:
    echo   "C:\Program Files\Python313\python.exe" -m venv venv
    echo   venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)
"venv\Scripts\python.exe" -m src.main %*
pause
