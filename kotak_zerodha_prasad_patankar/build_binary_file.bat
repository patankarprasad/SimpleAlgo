@echo off

REM Get the directory where this BAT file resides
set SCRIPT_DIR=%~dp0

wsl bash -ic "cd \"$(wslpath '%SCRIPT_DIR%')\" && pip install flask && pyinstaller --onefile --collect-all flask main.py && mv dist/main IOB.bin && rm -rf build dist main.spec"

pause