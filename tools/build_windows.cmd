@echo off
rem Build the Windows release zip with PyInstaller (folder build, not onefile:
rem starts faster, nothing is unpacked to %%TEMP%% on each launch, and it trips
rem antivirus heuristics far less often).
rem Run on Windows with Python 3.12+ from python.org (tkinter included):
rem     tools\build_windows.cmd
rem Output: dist\Top-Spin-4-Launcher-Windows-v<version>-x64.zip (+ .sha256)
rem         containing TopSpin4Launcher.exe, _internal\ (Python, art, Xenia Edge)
rem         and licenses\.
setlocal
cd /d "%~dp0\.."

for /f "delims=" %%v in ('python topspin4_launcher.py --version') do set VER=%%v
if "%VER%"=="" (echo Could not read VERSION from topspin4_launcher.py & exit /b 1)
set NAME=Top-Spin-4-Launcher-Windows-v%VER%-x64
set APP=TopSpin4Launcher

rem Bundle Xenia Edge for Windows: put xenia_edge.exe (and any files that ship
rem with it) in TopSpin4Launcher\xenia\windows\ before building.
if not exist "xenia\windows\xenia_edge.exe" (echo Missing xenia\windows\xenia_edge.exe & exit /b 1)

python -m pip install --upgrade pyinstaller || exit /b 1
if exist "build\windows" rmdir /s /q "build\windows"
if exist "dist\%NAME%" rmdir /s /q "dist\%NAME%"
if not exist dist mkdir dist
rem Absolute paths: PyInstaller resolves --icon/--add-data relative to --specpath.
python -m PyInstaller --noconfirm --clean --onedir --windowed ^
  --name %APP% --icon "%CD%\assets\icon.ico" ^
  --add-data "%CD%\assets\icon.png;assets" --add-data "%CD%\assets\header.png;assets" ^
  --add-data "%CD%\assets\steam_grid.png;assets" --add-data "%CD%\assets\steam_wide.png;assets" ^
  --add-data "%CD%\assets\steam_hero.png;assets" ^
  --add-data "%CD%\xenia\windows;xenia" --add-data "%CD%\licenses;licenses" ^
  --distpath build\windows\dist --workpath build\windows\work --specpath build\windows ^
  topspin4_launcher.py || exit /b 1

rem Release folder: the PyInstaller output plus the licenses at the top level.
move "build\windows\dist\%APP%" "dist\%NAME%" >nul || exit /b 1
xcopy /e /i /q licenses "dist\%NAME%\licenses" >nul || exit /b 1

rem Headless check: a windowed exe has no console, so rely on the exit code.
start "" /wait "dist\%NAME%\%APP%.exe" --self-test
if errorlevel 1 (echo Self-test FAILED & exit /b 1)

if exist "dist\%NAME%.zip" del "dist\%NAME%.zip"
rem Python's zipfile writes standard "/" entry names (Compress-Archive in
rem Windows PowerShell 5 writes "\", which breaks unzip on other systems).
python -c "import shutil; shutil.make_archive(r'dist\%NAME%', 'zip', 'dist', '%NAME%')" || exit /b 1
powershell -NoProfile -Command "$h=(Get-FileHash 'dist\%NAME%.zip' -Algorithm SHA256).Hash.ToLower(); Set-Content -NoNewline -Path 'dist\%NAME%.zip.sha256' -Value ($h + '  %NAME%.zip' + [char]10)" || exit /b 1

echo Built dist\%NAME%.zip
type "dist\%NAME%.zip.sha256"
