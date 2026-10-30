@echo off
rem Build the Windows launcher .exe with PyInstaller.
rem Run on Windows with Python 3.12+ from python.org (tkinter included):
rem     tools\build_windows.cmd
rem Output: dist\Top-Spin-4-Launcher-Windows-v<version>-x64.exe (+ .sha256)
setlocal
cd /d "%~dp0\.."

for /f "delims=" %%v in ('python topspin4_launcher.py --version') do set VER=%%v
if "%VER%"=="" (echo Could not read VERSION from topspin4_launcher.py & exit /b 1)
set NAME=Top-Spin-4-Launcher-Windows-v%VER%-x64
rem Absolute paths: PyInstaller resolves --icon/--add-data relative to --specpath.

rem Bundle Xenia Edge for Windows: put xenia_edge.exe (and any files that ship
rem with it) in TopSpin4Launcher\xenia\windows\ before building.
if not exist "xenia\windows\xenia_edge.exe" (echo Missing xenia\windows\xenia_edge.exe & exit /b 1)

python -m pip install --upgrade pyinstaller || exit /b 1
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name %NAME% --icon "%CD%\assets\icon.ico" ^
  --add-data "%CD%\assets\icon.png;assets" --add-data "%CD%\assets\header.png;assets" ^
  --add-data "%CD%\assets\steam_grid.png;assets" --add-data "%CD%\assets\steam_wide.png;assets" ^
  --add-data "%CD%\assets\steam_hero.png;assets" ^
  --add-data "%CD%\xenia\windows;xenia" --add-data "%CD%\licenses;licenses" ^
  --distpath dist --workpath build\windows --specpath build\windows ^
  topspin4_launcher.py || exit /b 1

rem Headless check: a windowed exe has no console, so rely on the exit code.
start "" /wait "dist\%NAME%.exe" --self-test
if errorlevel 1 (echo Self-test FAILED & exit /b 1)

for /f "tokens=* delims=" %%h in ('certutil -hashfile "dist\%NAME%.exe" SHA256 ^| findstr /v ":"') do (
  > "dist\%NAME%.exe.sha256" echo %%h  %NAME%.exe
  goto :hashed
)
:hashed
echo Built dist\%NAME%.exe
type "dist\%NAME%.exe.sha256"
