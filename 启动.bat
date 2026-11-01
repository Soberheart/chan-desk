@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "ROOT=%~dp0"
set "VPY=%ROOT%.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf-8"

if exist "%VPY%" goto deps
echo ==== Finding Python 3.11+ ====
set "PY="
py -3.12 --version >nul 2>&1
if not errorlevel 1 set "PY=py -3.12"
if not defined PY (
  python --version >nul 2>&1
  if not errorlevel 1 set "PY=python"
)
for %%K in (HKCU HKLM) do (
  if not defined PY for /f "tokens=2,*" %%a in ('reg query "%%K\Software\Python\PythonCore\3.12\InstallPath" /v ExecutablePath 2^>nul ^| findstr REG_SZ') do if exist "%%b" set "PY="%%b""
)
for %%P in ("%LOCALAPPDATA%\Programs\Python\Python312" "%ProgramFiles%\Python312" "C:\Python312" "D:\Python312" "D:\Python\Python312" "D:\Program Files\Python312") do (
  if not defined PY if exist "%%~P\python.exe" set "PY="%%~P\python.exe""
)
if not defined PY (
  echo Python not found. Please install Python 3.12 from python.org.
  pause & exit /b 1
)
echo Using Python: %PY%
%PY% -m venv .venv

:deps
if not exist ".venv\installed.ok" (
  echo ==== Installing packages (first run, 1-2 min) ====
  "%VPY%" -m pip install --upgrade pip
  "%VPY%" -m pip install -r requirements.txt
  if errorlevel 1 ( echo pip install failed. & pause & exit /b 1 )
  echo ok> ".venv\installed.ok"
)

echo ==== Chan Desk running at http://127.0.0.1:8765 ====
echo First time: open the Data page and click the update button. Close this window to stop.
start "" cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:8765/"
"%VPY%" -m app.server
pause
