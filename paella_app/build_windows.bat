@echo off
REM Build PaellaApp on Windows — works with OR without conda
cd /d "%~dp0"
echo ========================================
echo  Paella Windows Build
echo  Folder: %CD%
echo ========================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install one of:
    echo   - Miniconda: https://docs.anaconda.com/miniconda/
    echo   - Python 3.12: https://www.python.org/downloads/
    echo     ^(check "Add python.exe to PATH"^)
    pause
    exit /b 1
)

echo Python found:
python --version
echo.

set USE_CONDA=0
where conda >nul 2>&1
if not errorlevel 1 (
    echo Trying conda env 'paella'...
    call conda activate paella
    if not errorlevel 1 set USE_CONDA=1
)

if "%USE_CONDA%"=="0" (
    echo Conda not available — using pip only.
    echo Installing packages ^(may take several minutes^)...
    python -m pip install --upgrade pip
    python -m pip install -r requirements-windows-build.txt
) else (
    echo Using conda env paella.
    python -m pip install pyinstaller
    python -m pip install -r requirements-remote.txt
)

echo.
echo Building PaellaApp folder...
python helper_functions\package_paella.py

if errorlevel 1 (
    echo.
    echo BUILD FAILED — see errors above.
    pause
    exit /b 1
)

echo.
echo SUCCESS. Copy the PaellaApp folder to lab PCs.
echo It should be here: %~dp0..\PaellaApp
echo.
pause
