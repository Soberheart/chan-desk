@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
echo 正在做网络诊断，大约 1-2 分钟...
".venv\Scripts\python.exe" -m app.nettest
echo.
echo 诊断完成，结果已保存到 data\nettest.txt，可以关闭这个窗口。
pause
