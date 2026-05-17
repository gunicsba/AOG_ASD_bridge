@echo off
echo Building AOG-ASD...
pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    pip install pyinstaller
)
pip show pyserial >nul 2>&1
if errorlevel 1 (
    echo Installing pyserial...
    pip install pyserial
)
if exist "%~dp0icon.ico" (
    set ICON_FLAG=--icon="%~dp0icon.ico"
    echo Using icon: icon.ico
) else (
    set ICON_FLAG=
    echo WARNING: icon.ico not found!
)
python -m PyInstaller --onefile --console --name "AOG-ASD" %ICON_FLAG% "%~dp0AOG_ASD_bridge.py"
if exist "%~dp0dist\AOG-ASD.exe" (
    copy "%~dp0dist\AOG-ASD.exe" "%~dp0AOG-ASD.exe" >nul
    echo.
    echo Built: AOG-ASD.exe
)
pause
