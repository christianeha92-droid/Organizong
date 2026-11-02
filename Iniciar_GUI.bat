@echo off
setlocal
cd /d "%~dp0"
where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw -3 gui.py
) else (
    py -3 gui.py
)
endlocal
