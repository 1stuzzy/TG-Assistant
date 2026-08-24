@echo off
cd /d "%~dp0"
echo === TG-Assistant worker ===
py -m pip install -r requirements.txt
if not exist "model.gguf" if "%~1"=="" (
  echo.
  echo Положите файл модели рядом с start.bat и назовите его model.gguf
  echo или запустите: start.bat C:\path\to\model.gguf
  echo.
  pause
  exit /b 1
)
if "%~1"=="" (
  py server.py --model model.gguf --host 0.0.0.0 --port 8088 --gpu
) else (
  py server.py --model "%~1" --host 0.0.0.0 --port 8088 --gpu
)
pause
