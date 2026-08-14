@echo off
rem Launch FeetBrowser on Windows. The counterpart of run.sh, and it does the
rem same three things: find a Python, make sure the Rust JavaScript engine is
rem built, and run the browser out of wherever the engine ended up.
rem
rem No GUI toolkit is needed -- rendering is our own and the window is plain
rem user32 -- but the JavaScript engine is a compiled extension, so a Rust
rem toolchain has to be installed the first time. See docs/usage.md.
setlocal
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
  echo Python 3 was not found on PATH. Install it from python.org, or run
  echo this script's commands with "py -3" in place of "python".
  exit /b 1
)

rem Already importable, because someone installed it into this Python? Run.
python -c "import feetbrowser_engine" >nul 2>&1
if not errorlevel 1 (
  python -m feetbrowser %*
  exit /b %errorlevel%
)

rem Unseal a venv made before the line below grew --system-site-packages. A
rem default venv cannot see the system's site-packages, which is where the
rem optional image decoders live, and a venv is only ever created once -- so
rem without this the fix reaches fresh checkouts and nobody who already hit
rem the bug. Re-running venv over an existing directory rewrites pyvenv.cfg
rem and leaves everything installed in it exactly where it was.
findstr /i /c:"include-system-site-packages = false" ".venv\pyvenv.cfg" >nul 2>&1
if not errorlevel 1 python -m venv --system-site-packages .venv

rem Otherwise the venv is what runs the browser, so ask the venv -- and not
rem the system python -- whether the engine is there.
if not exist ".venv\Scripts\python.exe" goto build
".venv\Scripts\python.exe" -c "import feetbrowser_engine" >nul 2>&1
if not errorlevel 1 goto run

:build
python -m venv --system-site-packages .venv || exit /b 1
".venv\Scripts\python.exe" -m pip install -q maturin || exit /b 1
".venv\Scripts\maturin.exe" develop --release --manifest-path rust/Cargo.toml || exit /b 1

:run
".venv\Scripts\python.exe" -m feetbrowser %*
exit /b %errorlevel%
