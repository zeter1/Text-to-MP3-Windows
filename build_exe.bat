@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"

rem ============================================================================
rem Text-to-MP3-Windows - ready Windows EXE builder
rem
rem Double-click build_exe.bat. It automatically:
rem   1. Finds tested Python 3.13 or installs it for the current user via winget.
rem   2. Creates an isolated .build-venv.
rem   3. Installs pywin32, imageio-ffmpeg and pinned PyInstaller tooling.
rem   4. Verifies Windows SAPI/COM and the actual FFmpeg binary.
rem   5. Runs repository tests before packaging.
rem   6. Builds a reliable ONEDIR Windows GUI distribution.
rem   7. Runs --self-test on the packaged EXE before reporting success.
rem
rem Result:
rem   dist\Text-to-MP3-Windows\Text-to-MP3-Windows.exe
rem   + its runtime folder containing Python, pywin32 and FFmpeg.
rem Copy the WHOLE Text-to-MP3-Windows folder to another PC.
rem Python and a separate FFmpeg installation are NOT required to run it.
rem
rem CI/Codex: build_exe.bat --ci
rem Full guide: BUILD_EXE.md
rem ============================================================================

set "NO_PAUSE="
if /i "%~1"=="--ci" set "NO_PAUSE=1"
if /i "%~1"=="--no-pause" set "NO_PAUSE=1"

call :find_python313
if not defined BASE_PY call :install_python
if not defined BASE_PY goto :fail

set "BUILD_VENV=%CD%\.build-venv"
set "BUILD_PY=%BUILD_VENV%\Scripts\python.exe"

if exist "%BUILD_PY%" (
    "%BUILD_PY%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)" >nul 2>nul
    if errorlevel 1 (
        echo [SETUP] Existing build environment uses another Python version. Recreating it...
        rmdir /s /q "%BUILD_VENV%" || goto :fail
    )
)

if not exist "%BUILD_PY%" (
    echo [1/7] Creating isolated Python 3.13 build environment...
    "%BASE_PY%" -m venv "%BUILD_VENV%" || goto :fail
) else (
    echo [1/7] Reusing isolated Python 3.13 build environment...
)

echo [2/7] Installing application and packaging dependencies...
"%BUILD_PY%" -m pip install --disable-pip-version-check --no-input --timeout 60 --retries 2 -r requirements.txt || goto :fail
"%BUILD_PY%" -m pip install --disable-pip-version-check --no-input --timeout 60 --retries 2 "pyinstaller==6.22.3" "pyinstaller-hooks-contrib>=2026.6" || goto :fail
"%BUILD_PY%" -m pip check || goto :fail

echo [3/7] Verifying Windows SAPI, COM and bundled FFmpeg source dependency...
"%BUILD_PY%" -m py_compile text_to_mp3.py build_support\package_runtime.py || goto :fail
"%BUILD_PY%" -c "import sys; sys.argv=['package_runtime','--self-test']; import build_support.package_runtime" || goto :fail

echo [4/7] Running offline repository tests...
"%BUILD_PY%" -m unittest discover -s tests -v || goto :fail

echo [5/7] Cleaning previous build...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist "Text-to-MP3-Windows.spec" del /q "Text-to-MP3-Windows.spec"

echo [6/7] Building ready Windows distribution...
"%BUILD_PY%" -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onedir ^
  --contents-directory "runtime" ^
  --windowed ^
  --name "Text-to-MP3-Windows" ^
  --runtime-hook "build_support\package_runtime.py" ^
  --collect-all imageio_ffmpeg ^
  --hidden-import win32com.client ^
  --hidden-import win32com.client.dynamic ^
  --hidden-import pythoncom ^
  --hidden-import pywintypes ^
  text_to_mp3.py || goto :fail

set "DIST_DIR=%CD%\dist\Text-to-MP3-Windows"
set "DIST_EXE=%DIST_DIR%\Text-to-MP3-Windows.exe"
if not exist "%DIST_EXE%" (
    echo [ERROR] Expected packaged EXE was not created.
    goto :fail
)

echo [7/7] Running packaged runtime self-test...
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$p=Start-Process -FilePath '%DIST_EXE%' -ArgumentList '--self-test' -PassThru -Wait; exit $p.ExitCode" || goto :fail

echo.
echo [OK] READY BUILD CREATED AND VERIFIED.
echo Folder: %DIST_DIR%
echo EXE   : %DIST_EXE%
echo.
echo Copy the whole folder above. It already contains Python runtime and FFmpeg.
goto :success

:find_python313
set "BASE_PY="
if defined pythonLocation if exist "%pythonLocation%\python.exe" (
    "%pythonLocation%\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)" >nul 2>nul
    if not errorlevel 1 set "BASE_PY=%pythonLocation%\python.exe"
)
if defined BASE_PY exit /b 0
where py >nul 2>nul && for /f "delims=" %%P in ('py -3.13 -c "import sys; print(sys.executable)" 2^>nul') do set "BASE_PY=%%P"
if defined BASE_PY exit /b 0
for %%P in ("%LOCALAPPDATA%\Programs\Python\Python313\python.exe" "%ProgramFiles%\Python313\python.exe") do if exist "%%~P" set "BASE_PY=%%~P"
if defined BASE_PY exit /b 0
where python >nul 2>nul && python -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)" >nul 2>nul && for /f "delims=" %%P in ('python -c "import sys; print(sys.executable)"') do set "BASE_PY=%%P"
exit /b 0

:install_python
echo [SETUP] Tested Python 3.13 was not found. Trying automatic per-user installation...
where winget >nul 2>nul || (
    echo [ERROR] Python 3.13 is missing and Windows Package Manager ^(winget^) is unavailable.
    echo Install Python 3.13 from python.org, then run this file again.
    exit /b 1
)
winget install --id Python.Python.3.13 -e --scope user --silent --accept-source-agreements --accept-package-agreements
if errorlevel 1 (
    echo [ERROR] Automatic Python 3.13 installation failed.
    exit /b 1
)
call :find_python313
exit /b 0

:fail
echo.
echo [ERROR] EXE build failed. Read the first error above.
echo No incomplete package is reported as ready.
if not defined NO_PAUSE pause
exit /b 1

:success
if not defined NO_PAUSE pause
exit /b 0
