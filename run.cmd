@echo off
rem Launch FeetBrowser on Windows. The counterpart of run.sh, and it does the
rem same three things: find a Python, make sure the Rust JavaScript engine is
rem built, and run the browser out of wherever the engine ended up.
rem
rem No GUI toolkit is needed -- rendering is our own and the window is plain
rem user32 -- but the JavaScript engine is a compiled extension, so the first
rem run needs a Rust toolchain and, on Windows only, a C++ linker to go with
rem it. See docs/usage.md, and the messages at the bottom of this file.
setlocal
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
  echo Python 3 was not found on PATH. Install it from python.org, or run
  echo this script's commands with "py -3" in place of "python".
  exit /b 1
)

rem Already importable, because someone installed it into this Python? Run --
rem but both halves have to be there. Before feetplayer was its own repository
rem the media stack was these files, so the engine was the whole test; now it
rem is a package, and a Python with the engine but no feetplayer would give
rem you a browser that plays no video. The venv below has both.
python -c "import feetbrowser_engine, feetplayer" >nul 2>&1
if not errorlevel 1 (
  call :warmfortran python
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
rem Ask before spending several minutes on it. Someone who typed run.cmd
rem expecting a browser and got a wall of cargo output has every reason to
rem think they are in the wrong repository.
where /q cargo
if errorlevel 1 goto norust
echo FeetBrowser: building the JavaScript engine before the first start.
echo.
echo It is a Rust extension rather than Python, so it has to be compiled.
echo maturin does it, into a virtualenv in this directory. Expect a minute or
echo two, longer on a slow machine or a cold cargo cache -- and only this
echo once. Every later start skips straight past this.
echo.
python -m venv --system-site-packages .venv || exit /b 1
".venv\Scripts\python.exe" -m pip install -q maturin || exit /b 1
".venv\Scripts\maturin.exe" develop --release --manifest-path rust/Cargo.toml
if errorlevel 1 goto nolinker
echo.
echo FeetBrowser: engine built. Starting.

:run
call :ensureplayer
call :warmfortran ".venv\Scripts\python.exe"
".venv\Scripts\python.exe" -m feetbrowser %*
exit /b %errorlevel%

rem feetplayer is the media stack -- the container readers, the audio output
rem and the Fortran decoders -- pinned to a commit in requirements.txt. pip
rem compiles the Fortran during the install, so this is the minute the
rem decoders cost, and it is paid once per pin rather than once per start:
rem "pip freeze" prints a VCS install as the requirement line that produced
rem it, so the pin installed and the pin asked for compare as plain strings.
rem Failing to install it is not fatal -- the browser then reports H.264 and
rem AAC as codecs it does not have, and everything else still works.
:ensureplayer
for /f "usebackq delims=" %%L in (`findstr /v /r /c:"^ *#" /c:"^ *$" requirements.txt`) do set "WANT=%%L"
".venv\Scripts\python.exe" -m pip freeze 2>nul | findstr /x /c:"%WANT%" >nul
if not errorlevel 1 goto :eof
echo FeetBrowser: installing feetplayer, the media stack. This compiles its
echo Fortran decoders, so expect a minute -- and only when the pin moves.
echo.
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 call :say_noplayer 1>&2
goto :eof

rem Loading the decoders here rather than on the first <video> costs nothing
rem when they are already built and saves a stall in the middle of a page.
rem Everything about it is optional: no feetplayer, or a library that will not
rem load, means the browser reports H.264 and AAC as codecs it does not have,
rem which is what it did before any of this existed. So nothing here is
rem allowed to change the exit status.
:warmfortran
%1 -c "import feetplayer.h264 as v, feetplayer.aac as a; v.available(); a.available()" >nul 2>&1
goto :eof

:say_noplayer
echo.
echo FeetBrowser: feetplayer did not install, so this run has no video and no
echo sound. Everything else works. The install needs git and gfortran; see
echo requirements.txt.
echo.
goto :eof

rem The two failures worth explaining, both written to stderr by redirecting
rem the whole subroutine rather than every line inside it.
:norust
call :say_norust 1>&2
exit /b 1

:nolinker
call :say_nolinker 1>&2
exit /b 1

:say_norust
echo FeetBrowser needs a Rust toolchain, and there is not one on this machine.
echo.
echo The JavaScript engine is a Rust extension -- see rust\ -- rather than
echo Python, so it has to be compiled before the browser can start. There is
echo no pure-Python fallback.
echo.
echo Install Rust with rustup: download and run rustup-init.exe from
echo https://rustup.rs. It needs no administrator rights.
echo.
echo Windows then needs one thing the other platforms do not, and it is the
echo step people miss. See below; rustup will say the same when it runs.
echo.
goto :say_linker

:say_nolinker
echo.
echo FeetBrowser: the JavaScript engine did not build.
echo.
echo The compiler's own output is above and says what went wrong. If it
echo mentions link.exe or dlltool.exe, or a missing linker, that is the system
echo side of a Rust install rather than anything about this repository.
echo.

:say_linker
echo Rust compiles the code but does not link it, and Windows ships no linker
echo for it to use. That is a separate download from rustup's:
echo.
echo   Build Tools for Visual Studio, from
echo   https://visualstudio.microsoft.com/downloads/
echo.
echo In its installer, tick the "Desktop development with C++" workload.
echo Ticking it is the part that gets missed: the Build Tools installed
echo without that workload leave you with no link.exe at all, and the error
echo is word for word the one you get having never installed them.
echo.
echo Open a new command prompt afterwards so the linker is on your PATH, then
echo run this script again.
echo.
echo There is a second route, if a multi-gigabyte Visual Studio download is
echo not what you want. rustup's GNU toolchain links with MinGW instead:
echo.
echo   rustup toolchain install stable-x86_64-pc-windows-gnu
echo   rustup default stable-x86_64-pc-windows-gnu
echo.
echo That one has a prerequisite rustup does not install either, so it trades
echo one download for another rather than avoiding one. If the build gets
echo further and then stops at
echo.
echo   error calling dlltool 'dlltool.exe': program not found
echo.
echo it is MinGW-w64's binutils that are missing. Install MinGW-w64 -- MSYS2,
echo at https://www.msys2.org, is one way -- and put its bin directory on your
echo PATH. The dlltool.exe that rustup installs alongside the toolchain does
echo not stand in for them: it needs the assembler from that same package
echo sitting next to it, and fails differently without it.
echo.
echo Of the two routes, Visual Studio is the one this project builds against.
goto :eof
