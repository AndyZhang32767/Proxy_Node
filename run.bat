@echo off
title ProxyNet - Cross-site proxy networking

cd /d "%~dp0"

:: Prefer Python 3.12 (user-installed)
set PYTHON=C:\Users\AndyZhang123\AppData\Local\Programs\Python\Python312\python.exe
if exist "%PYTHON%" goto :run

:: Fallback: search PATH
for %%p in (python3.12 python3.11 python python3) do (
    where %%p >nul 2>&1 && set PYTHON=%%p && goto :run
)

:: Last resort: try common paths
for %%d in (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
) do (
    if exist %%d (set PYTHON=%%d && goto :run)
)

echo [ERROR] Python 3.10+ not found.
pause
exit /b 1

:run
echo [INFO] Using Python: %PYTHON%
"%PYTHON%" main.py %*
pause
