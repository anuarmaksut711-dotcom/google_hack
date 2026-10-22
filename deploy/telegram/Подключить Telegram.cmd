@echo off
setlocal
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0configure-telegram.ps1" -FileOnly
if errorlevel 1 (
  echo.
  echo Setup failed. The token file was kept. Read the message above.
  pause
  exit /b 1
)
echo.
echo Setup complete. Connect your Telegram account in the website settings.
pause
