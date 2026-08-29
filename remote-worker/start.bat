@echo off
cd /d "%~dp0"
echo === TG-Assistant worker ===
echo Python ne nuzhen. Pervyj zapusk skachaet llama-server s GitHub.
echo.

netsh advfirewall firewall show rule name="TG-Assistant worker 8088" >nul 2>&1
if errorlevel 1 (
  echo Otkryvaju port 8088...
  netsh advfirewall firewall add rule name="TG-Assistant worker 8088" dir=in action=allow protocol=tcp localport=8088 profile=private,domain,public >nul 2>&1
)

if "%~1"=="" (set "MODEL=%~dp0model.gguf") else set "MODEL=%~f1"
if not exist "%MODEL%" (
  echo Net fajla modeli.
  echo Polozhite model.gguf rjadom so start.bat
  echo.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0fetch-runtime.ps1"
if errorlevel 1 (
  echo.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-server.ps1" "%MODEL%" "%~2"
echo.
echo ----------------------------------------
pause
