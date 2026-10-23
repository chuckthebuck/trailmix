@echo off
setlocal
cd /d "%~dp0"
title TrailMix Production Server
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 server.py
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python 3 is required on this server computer only.
    echo Install Python 3, then double-click this file again.
  ) else (
    python server.py
  )
)
pause
