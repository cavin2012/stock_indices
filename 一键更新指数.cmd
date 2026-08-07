@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Community Sentiment Index - Daily Update

echo.
echo Updating the community sentiment index...
echo This can take several minutes. Please keep this window open.
echo.

where py.exe >nul 2>&1
if errorlevel 1 (
  python.exe "%~dp0update_index.py"
) else (
  py.exe -3 "%~dp0update_index.py"
)

set "UPDATE_EXIT=%ERRORLEVEL%"

if /I not "%INDEX_NO_OPEN%"=="1" (
  powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$p=(Resolve-Path -LiteralPath '%~dp0dashboard.html').Path; $u=([Uri]$p).AbsoluteUri+'?v='+[DateTimeOffset]::Now.ToUnixTimeMilliseconds(); Start-Process $u"
  if errorlevel 1 start "" "%~dp0dashboard.html"
)

if not "%UPDATE_EXIT%"=="0" (
  echo.
  echo The online update did not finish. The previous successful result was opened.
  echo Details are available in data\update.log.
  echo.
  if /I not "%INDEX_NO_PAUSE%"=="1" pause
)

endlocal & exit /b %UPDATE_EXIT%
