@echo off
rem Windows : double-clic pour lancer Trip Compta (installe ce quil faut au premier lancement).
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
if not exist ".venv\Scripts\python.exe" (
  echo Premier lancement : installation (1 a 2 minutes)...
  %PY% -m venv .venv || (echo Python 3 est necessaire : https://www.python.org/downloads/ & pause & exit /b 1)
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || (echo Echec de linstallation & pause & exit /b 1)
)
".venv\Scripts\python.exe" run.py %*
pause
