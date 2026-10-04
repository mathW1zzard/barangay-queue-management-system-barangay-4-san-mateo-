@echo off
REM Builds the desktop app. Run from the project folder with your virtual environment active.
REM Needs:  pip install pyinstaller
echo Cleaning old build...
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul

echo Building...
pyinstaller --noconfirm --clean BarangayQueueManagement.spec
if errorlevel 1 (
    echo.
    echo BUILD FAILED - read the messages above.
    pause
    exit /b 1
)

echo.
echo Done. Run:  dist\BarangayQueue\BarangayQueueManagement.exe
pause
