@echo off
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Нужны права администратора для входящего порта 8088.
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
netsh advfirewall firewall delete rule name="TG-Assistant worker 8088" >nul 2>&1
netsh advfirewall firewall add rule name="TG-Assistant worker 8088" dir=in action=allow protocol=tcp localport=8088 profile=private,domain,public
if errorlevel 1 (
  echo Не удалось открыть порт.
  pause
  exit /b 1
)
echo Входящий TCP 8088 разрешён. Теперь запустите start.bat и не закрывайте окно.
pause
