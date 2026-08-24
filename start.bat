@echo off
title AI Altyazi Studyosu Baslatiliyor...
cd /d "%~dp0"

:: Siyah ekran arkada kalmasin diye 'start' ve 'pythonw' kullaniyoruz.
start "" ".venv310\Scripts\pythonw.exe" "run.py"

exit
