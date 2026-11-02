@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Нужен Python 3.11 или новее. Установите с python.org с опцией Add to PATH.
  pause
  exit /b 1
)
where node >nul 2>nul
if errorlevel 1 (
  echo Нужен Node.js 20 или новее. Установите с nodejs.org.
  pause
  exit /b 1
)
call npm install --no-audit --no-fund
if errorlevel 1 goto failed
call npx playwright install chromium
if errorlevel 1 goto failed
python -c "import json,shutil; open('runtime.json','w',encoding='utf-8').write(json.dumps({'node':shutil.which('node')}))"
python -c "import sys,pathlib; p=pathlib.Path(sys.executable); w=p.with_name('pythonw.exe'); pathlib.Path('python-path.txt').write_text(str(w if w.exists() else p),encoding='ascii')"
echo Готово. Запустите файл Запустить.vbs.
pause
exit /b 0
:failed
echo Установка не завершена. Проверьте интернет и сообщение об ошибке выше.
pause
exit /b 1
