@echo off
cd /d "C:\Python\Python codes\Running Systems\kotakproject"

pyinstaller --onefile main.py

move dist\main.exe IOB.exe
rmdir /s /q build
rmdir /s /q dist
del main.spec

pause