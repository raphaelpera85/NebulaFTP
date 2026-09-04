@echo off
setlocal enabledelayedexpansion

:: ==============================================
:: MulletaFlix Build Script
:: Builds both MulletaFlix and Original NebulaFTP executables
:: ==============================================

cd /d "%~dp0"

echo ==============================================
echo  MulletaFlix Build System
echo ==============================================
echo.

:: Check for Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found in PATH
    pause
    exit /b 1
)

:: Check for PyInstaller
python -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    pip install pyinstaller
    if errorlevel 1 (
        echo ERROR: Failed to install PyInstaller
        pause
        exit /b 1
    )
)

:: Check for UPX (optional but recommended)
where upx >nul 2>&1
if errorlevel 1 (
    echo WARNING: UPX not found. Executable will be larger.
    echo Download from https://upx.github.io/ and add to PATH
    echo.
)

:: Install dependencies
echo Installing dependencies...
pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: Failed to install dependencies
    pause
    exit /b 1
)

:: Install supabase for sync functionality
pip install supabase >nul 2>&1

:: Clean previous builds
echo Cleaning previous builds...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"

:: ==============================================
:: BUILD MULLETAFLIX
:: ==============================================
echo.
echo ==============================================
echo  Building MulletaFlix...
echo ==============================================
echo.

set BUILD_VARIANT=mulletaflix
python -m PyInstaller MulletaFlix.spec --clean --noconfirm
if errorlevel 1 (
    echo ERROR: MulletaFlix build failed
    pause
    exit /b 1
)

echo.
echo MulletaFlix built successfully!
echo Output: dist\MulletaFlix.exe
echo.

:: ==============================================
:: BUILD ORIGINAL NEBULA (optional)
:: ==============================================
echo.
set /p BUILD_ORIGINAL="Build Original NebulaFTP as well? (y/N): "
if /i "!BUILD_ORIGINAL!"=="y" (
    echo.
    echo ==============================================
    echo  Building Original NebulaFTP...
    echo ==============================================
    echo.

    :: Create a temporary spec for original
    set BUILD_VARIANT=original
    python -m PyInstaller MulletaFlix.spec --clean --noconfirm
    if errorlevel 1 (
        echo ERROR: Original NebulaFTP build failed
        pause
        exit /b 1
    )

    :: Rename the output
    if exist "dist\MulletaFlix.exe" (
        move /y "dist\MulletaFlix.exe" "dist\NebulaFTP.exe"
        echo Original NebulaFTP built successfully!
        echo Output: dist\NebulaFTP.exe
    )
)

:: ==============================================
:: CREATE INSTALLER (using Inno Setup if available)
:: ==============================================
echo.
where iscc >nul 2>&1
if not errorlevel 1 (
    echo.
    echo ==============================================
    echo  Creating Inno Setup Installer...
    echo ==============================================
    echo.

    if exist "MulletaFlix.iss" (
        iscc MulletaFlix.iss
        if not errorlevel 1 (
            echo Installer created in Output\
        ) else (
            echo WARNING: Inno Setup compilation failed
        )
    ) else (
        echo WARNING: MulletaFlix.iss not found, skipping installer creation
    )
) else (
    echo.
    echo Inno Setup not found. Skipping installer creation.
    echo Install from https://jrsoftware.org/isinfo.php to create installers.
)

:: ==============================================
:: SUMMARY
:: ==============================================
echo.
echo ==============================================
echo  BUILD COMPLETE
echo ==============================================
echo.
echo Output files:
if exist "dist\MulletaFlix.exe" (
    echo   - dist\MulletaFlix.exe (MulletaFlix)
)
if exist "dist\NebulaFTP.exe" (
    echo   - dist\NebulaFTP.exe (Original NebulaFTP)
)
if exist "Output\MulletaFlix_Setup.exe" (
    echo   - Output\MulletaFlix_Setup.exe (Installer)
)
echo.
echo To test MulletaFlix:
echo   cd dist
echo   copy ..\.env.mulletaflix .env
echo   MulletaFlix.exe
echo.
pause