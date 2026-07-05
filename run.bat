@echo off
setlocal enabledelayedexpansion

echo ===================================================
echo               Starting Framecycler
echo ===================================================

REM 1. Check for Python installation
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found in your PATH.
    echo Please install Python 3.12+ and add it to your PATH.
    pause
    exit /b 1
)

REM 2. Check for virtual environment
if not exist ".venv" (
    echo [INFO] Virtual environment not found. Creating one in .venv...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
)

REM 3. Activate virtual environment
echo [INFO] Activating virtual environment...
call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo [ERROR] Failed to activate virtual environment.
    pause
    exit /b 1
)

REM 4. Check/Install Python dependencies
python -c "import PySide6, OpenGL, numpy, av, OpenColorIO, cv2" 2>nul
if errorlevel 1 (
    echo [INFO] Dependencies missing or incomplete. Installing from requirements.txt...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Failed to install dependencies.
        pause
        exit /b 1
    )
) else (
    echo [INFO] Python dependencies are up-to-date.
)

REM 5. Check if native C++ engine is compiled
set ENGINE_FOUND=0
for %%f in (src\framecycler\framecycler_engine*.pyd) do (
    set ENGINE_FOUND=1
)

if !ENGINE_FOUND! equ 0 (
    echo [INFO] Native engine binary not found. Compiling extension...
    python build.py
    if errorlevel 1 (
        echo [ERROR] Failed to compile C++ engine.
        pause
        exit /b 1
    )
) else (
    echo [INFO] Native engine binary found.
)

REM 6. Run Framecycler
echo [INFO] Running Framecycler...
python -m src.framecycler %*
if %ERRORLEVEL% neq 0 (
    echo [WARNING] Framecycler exited with code %ERRORLEVEL%.
    pause
    exit /b %ERRORLEVEL%
)

endlocal
