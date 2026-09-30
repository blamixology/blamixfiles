@echo off
REM Builds a portable app folder: dist\BlamixFiles\BlamixFiles.exe + its files.
REM Your servers/vault are stored in dist\BlamixFiles\data\ next to the exe.
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Creating virtual environment...
  py -3 -m venv .venv || python -m venv .venv || (echo Python 3.10+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
)
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller || (pause & exit /b 1)

REM keep existing data if you rebuild over an old build
if exist dist\BlamixFiles\data (
  echo Preserving existing data folder...
  rmdir /s /q _data_backup 2>nul
  move dist\BlamixFiles\data _data_backup >nul
)

.venv\Scripts\pyinstaller.exe --noconfirm --clean --onedir --windowed --name BlamixFiles ^
  --icon blamixfiles\assets\app.ico ^
  --add-data "blamixfiles\assets;blamixfiles\assets" ^
  --collect-submodules pygments.lexers --collect-submodules pygments.styles ^
  --exclude-module tkinter --exclude-module PySide6.Qt3DCore --exclude-module PySide6.QtCharts ^
  --exclude-module PySide6.QtDataVisualization --exclude-module PySide6.QtMultimedia ^
  --exclude-module PySide6.QtQuick3D --exclude-module PySide6.QtDesigner ^
  --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtWebEngineWidgets ^
  run.py || (pause & exit /b 1)

.venv\Scripts\python.exe packaging\prune_qt.py dist\BlamixFiles
if exist _data_backup move _data_backup dist\BlamixFiles\data >nul
rmdir /s /q build 2>nul
del BlamixFiles.spec 2>nul
echo.
echo ============================================================
echo  Done:  %~dp0dist\BlamixFiles\BlamixFiles.exe
echo  Copy the whole dist\BlamixFiles folder anywhere (USB stick too).
echo  Your data is kept in the "data" folder beside the exe.
echo ============================================================
explorer dist\BlamixFiles
pause
