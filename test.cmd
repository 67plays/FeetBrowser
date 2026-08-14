@echo off
rem Run the FeetBrowser test suite on Windows. The counterpart of test.sh, and
rem it runs the same suites in the same order.
rem
rem The renderer draws into its own framebuffer, so nothing here needs a
rem display or a toolkit. The JavaScript engine is the Rust extension
rem feetbrowser_engine, so the suite runs out of the local venv maturin builds
rem it into, which needs a Rust toolchain installed. A few suites step outside
rem all that: test_win32.py opens real windows here (test_cocoa.py and
rem test_x11.py skip, as they do everywhere but their own platform), and
rem test_nav.py and smoke.py reach the network.
setlocal
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
  echo Python 3 was not found on PATH. Install it from python.org, or run
  echo this script's commands with "py -3" in place of "python".
  exit /b 1
)

rem Same venv run.cmd builds, and unsealed for the same reason: the optional
rem image decoders (Pillow, cairosvg) live in the system python, and tests
rem that run without them are not testing what a user runs.
if not exist ".venv\Scripts\python.exe" python -m venv --system-site-packages .venv
if not exist ".venv\Scripts\python.exe" exit /b 1

rem A venv made before that flag existed is sealed, and a venv is only ever
rem created once, so the decoders would stay invisible on every machine that
rem already has one. Re-running venv over it rewrites pyvenv.cfg and leaves
rem what is installed inside untouched.
findstr /i /c:"include-system-site-packages = false" ".venv\pyvenv.cfg" >nul 2>&1
if not errorlevel 1 python -m venv --system-site-packages .venv
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
rem test_win32.py opens real windows here; the other two skip, and it is the
rem one that skips everywhere else.
"%PY%" tests\test_cocoa.py || exit /b 1
"%PY%" tests\test_x11.py || exit /b 1
"%PY%" tests\test_win32.py || exit /b 1
"%PY%" tests\test_units.py || exit /b 1
"%PY%" tests\test_js.py || exit /b 1
"%PY%" tests\test_shoes.py || exit /b 1
"%PY%" tests\test_nav.py || exit /b 1
"%PY%" tests\test_toes.py || exit /b 1
rem No assembler here, so this checks the pure-Python fallback.
"%PY%" tests\test_asmblend.py || exit /b 1
"%PY%" tests\smoke.py || exit /b 1
