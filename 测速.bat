@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
echo 正在测速，大约 1-3 分钟，请稍等...
".venv\Scripts\python.exe" -m app.speedtest
echo.
echo 测速完成，结果已保存到 data\speedtest.txt，可以关闭这个窗口。
pause
