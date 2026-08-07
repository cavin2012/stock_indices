@echo off
setlocal EnableExtensions
cd /d "%~dp0"

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$p=(Resolve-Path -LiteralPath '%~dp0dashboard.html').Path; $u=([Uri]$p).AbsoluteUri+'?v='+[DateTimeOffset]::Now.ToUnixTimeMilliseconds(); Start-Process $u"
if errorlevel 1 start "" "%~dp0dashboard.html"

endlocal & exit /b 0
