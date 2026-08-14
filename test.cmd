@echo off
rem Run the FeetBrowser test suite on Windows. The counterpart of test.sh, and
rem it runs the same suites in the same order.
rem
rem The renderer draws into its own framebuffer, so nothing here needs a
rem display or a toolkit. The JavaScript engine is the Rust extension
rem feetbrowser_engine, so the suite runs out of the local venv maturin builds
rem it into, which needs a Rust toolchain installed. Two suites step outside
rem all that: test_win32.py opens real windows here (and test_cocoa.py skips,
rem as it does everywhere but macOS), and test_nav.py and smoke.py reach the
rem network.
setlocal
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
  echo Python 3 was not found on PATH. Install it from python.org, or run
  echo this script's commands with "py -3" in place of "python".
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv || exit /b 1
)
set PY=.venv\Scripts\python.exe

rem Ensure the Rust JS engine (feetbrowser_engine) is built in the local venv.
"%PY%" -c "import feetbrowser_engine" >nul 2>&1
if errorlevel 1 (
  "%PY%" -m pip install -q maturin || exit /b 1
  ".venv\Scripts\maturin.exe" develop --release --manifest-path rust/Cargo.toml || exit /b 1
)

"%PY%" -c "import pyflakes" >nul 2>&1
if errorlevel 1 (
  "%PY%" -m pip install -q pyflakes || exit /b 1
)

"%PY%" -m pyflakes feetbrowser tests || exit /b 1
"%PY%" tests\test_render.py || exit /b 1
rem Opens real windows here; skips everywhere else, and test_cocoa.py is the
rem other way round.
"%PY%" tests\test_win32.py || exit /b 1
"%PY%" tests\test_cocoa.py || exit /b 1
"%PY%" tests\test_units.py || exit /b 1
"%PY%" tests\test_js.py || exit /b 1
"%PY%" tests\test_shoes.py || exit /b 1
"%PY%" tests\test_nav.py || exit /b 1
"%PY%" tests\test_toes.py || exit /b 1
"%PY%" tests\test_gh_scroll.py || exit /b 1
"%PY%" tests\smoke.py || exit /b 1
