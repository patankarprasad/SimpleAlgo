@echo off
setlocal

echo [1/4] Installing PyInstaller...
pip install --upgrade pyinstaller
if errorlevel 1 ( echo ERROR: pip install failed & pause & exit /b 1 )

echo [2/4] Cleaning previous build...
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist

echo [3/4] Building EXE...
pyinstaller SimpleAlgo.spec
if errorlevel 1 ( echo ERROR: PyInstaller build failed & pause & exit /b 1 )

echo [4/4] Copying external files to dist\...
copy /y config.py dist\config.py
if exist .env.example copy /y .env.example dist\.env.example

for %%f in (
    kite_token.json
    angel_token.json
    positions_state.json
    paper_positions_state.json
    strategy_config.json
    contract_pin.json
) do (
    if exist %%f copy /y %%f dist\%%f
)

echo.
echo ============================================================
echo  Build complete!  Output: dist\
echo ============================================================
echo.
echo   SimpleAlgo.exe    -- run this
echo   config.py         -- EDIT: instruments, strategy params
echo   .env.example      -- copy to .env and fill credentials
echo   .env              -- create this from .env.example
echo.
pause
