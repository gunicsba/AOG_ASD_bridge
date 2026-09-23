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
python -m PyInstaller --onefile --console --name "AOG-ASD-Sniffer" %ICON_FLAG% "%~dp0asd_sniffer.py"
python -m PyInstaller --onefile --console --name "AOG-ASD-Probe" %ICON_FLAG% "%~dp0asd_probe.py"
if exist "%~dp0dist\AOG-ASD.exe" (
    copy "%~dp0dist\AOG-ASD.exe" "%~dp0AOG-ASD.exe" >nul
    echo.
    echo Built: AOG-ASD.exe
)
if exist "%~dp0dist\AOG-ASD-Sniffer.exe" (
    copy "%~dp0dist\AOG-ASD-Sniffer.exe" "%~dp0AOG-ASD-Sniffer.exe" >nul
    echo Built: AOG-ASD-Sniffer.exe
)
if exist "%~dp0dist\AOG-ASD-Probe.exe" (
    copy "%~dp0dist\AOG-ASD-Probe.exe" "%~dp0AOG-ASD-Probe.exe" >nul
    echo Built: AOG-ASD-Probe.exe
)
pause
