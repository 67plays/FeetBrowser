FeetBrowser for Windows
=======================

A web browser built from scratch: its own rasteriser, font engine, image
decoders, event loop, HTTP client and JavaScript engine. Nothing in the
window comes from a browser engine somebody else wrote.


RUNNING IT
----------

Double-click FeetBrowser.exe.

That is the whole installation. This folder is self-contained -- it carries
its own copy of Python, so nothing needs to be installed and nothing on the
machine is changed by running it. Move the folder anywhere you like, put it
on a USB stick, delete it when you are done.

From a command prompt it takes a URL, and a few flags:

    FeetBrowser.exe https://example.com
    FeetBrowser.exe --version
    FeetBrowser.exe --help
    FeetBrowser.exe --screenshot https://example.com page.png

The last one renders a page to a PNG without opening a window.


"WINDOWS PROTECTED YOUR PC"
---------------------------

You will probably see a blue box saying "Windows protected your PC" the first
time you run this, and Edge may warn you while downloading the zip.

That is SmartScreen, and it is telling you the truth: these binaries are not
signed. FeetBrowser is a hobby project and does not have a code-signing
certificate. Buying one costs a few hundred dollars a year and, since 2023,
also requires a hardware security module to keep the key on -- and even then
a new certificate carries no SmartScreen reputation until enough people have
run something signed with it.

Rather than pretend, here is what to check instead:

  * The zip you downloaded should have come from the project's own GitHub
    releases or from a GitHub Actions run of the repository. Nowhere else.
  * Everything in this folder that is not a FeetBrowser file is CPython,
    exactly as python.org published it -- the file name python3NN.dll says
    which version. python.cat is Microsoft's signature catalogue for those
    files and has not been touched, so they can still be verified against it.
  * The build that produced this folder is a public GitHub Actions run,
    and packaging/windows/README.md in the repository says how to reproduce
    it yourself.

To run it anyway: click "More info", then "Run anyway". If Windows refuses to
unzip the archive at all, right-click the .zip, choose Properties, and tick
"Unblock" at the bottom.


INSTALLING IT PROPERLY
----------------------

There is no setup.exe. If you would rather have a Start Menu entry and an
Add/Remove Programs entry than a folder in Downloads, right-click
install.ps1 and choose "Run with PowerShell". It copies this folder to

    %LOCALAPPDATA%\Programs\FeetBrowser

adds a Start Menu shortcut, and registers an uninstaller. It needs no
administrator rights and touches nothing outside your own user account.

uninstall.ps1 undoes all of it.


WHAT IS IN HERE
---------------

    FeetBrowser.exe         the launcher
    feetbrowser\            the browser, in Python
    feetbrowser_engine\     the rasteriser, font engine, image decoders and
                            JavaScript engine, compiled from Rust
    feetplayer\             the audio output and the H.264, AAC and MP3
                            decoders, compiled from Fortran
    doormat\                the window: the Win32 window, the input
                            translation behind it and the event loop above it
    python3NN.dll           CPython, from python.org's embeddable package
    python3NN.zip           its standard library
    python3NN._pth          what makes that Python private to this folder
    FeetBrowser._pth        the same, under the executable's name
    *.pyd, *.dll            CPython's own extension modules and their
                            libraries, including OpenSSL for https
    python.exe, pythonw.exe CPython's own interpreters, kept for debugging
    toes\                   where extensions get installed
    LICENSE                 MIT, for FeetBrowser
    LICENSE.txt             the PSF license, for CPython


REQUIREMENTS
------------

64-bit Windows 10 or later. Nothing else.
