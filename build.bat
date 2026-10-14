@echo off
REM Builds the desktop app. Run from the project folder with your virtual environment active.
REM Needs:  pip install pyinstaller
echo Cleaning old build...
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul

echo Building...
pyinstaller --noconfirm --clean Barangay_Queuing_Attendance_Management.spec
if errorlevel 1 (
    echo.
    echo BUILD FAILED - read the messages above.
    pause
    exit /b 1
)

echo.
echo Done. Run:  dist\Barangay_Queuing_Attendance_Management\Barangay_Queuing_Attendance_Management.exe
pause
