@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Запускаю бота Новогодней Жабилизации... (чтобы остановить — закрой это окно)
".venv\Scripts\python.exe" bot.py
pause
