<#
.SYNOPSIS
    Prove an unpacked FeetBrowser bundle actually works.

.DESCRIPTION
    Run against an unzipped bundle on a machine that has no FeetBrowser
    development environment -- that is the point. The CI job that runs this
    downloads the artifact and nothing else: no checkout, no Python, no
    engine, PATH stripped back to system32, and PYTHONHOME and PYTHONPATH set
    to a directory that does not exist. Everything below therefore either
    comes out of the bundle or does not happen.

    It is deliberately not a unit test suite. It is the list of things that
    are true if and only if somebody can double-click the .exe and browse:

      1  the folder has the files it claims to
      2  the .exe is a GUI-subsystem PE with version information, so no
         console flashes up and Explorer has something to show
      3  --version and --help answer
      4  the interpreter inside is sealed off from the machine around it
      5  the compiled engine loads
      6  https works, OpenSSL and the Windows certificate store included
      7  a page fetched over a socket renders to the right pixels
      8  launching it the way Explorer does opens a real window
      9  it can decode H.264 and AAC, on a machine with no gfortran

.PARAMETER Root
    The unpacked bundle -- the directory with FeetBrowser.exe in it.

.PARAMETER Evidence
    Where to leave the rendered PNGs. Created if missing.

.PARAMETER SkipNetwork
    Skip the checks that leave the machine (6, and the live render in 7).
    The local-fixture render still runs.

.PARAMETER H264Vector
.PARAMETER H264Truth
    An H.264 stream and the I420 picture a reference decoder produced from
    it. They travel beside this script -- the CI job that runs it has no
    checkout to take them from. Without them check 9 still proves the
    decoder loads; with them it proves what it decodes.

.PARAMETER AacVector
.PARAMETER AacTruth
    The same pair for sound: an ADTS stream and the float32 samples a
    reference decoder produced from it.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Root,
    [string]$Evidence,
    [switch]$SkipNetwork,
    [string]$H264Vector = (Join-Path $PSScriptRoot 'h264\mb1.264'),
    [string]$H264Truth  = (Join-Path $PSScriptRoot 'h264\mb1.i420.z'),
    [string]$AacVector  = (Join-Path $PSScriptRoot 'aac\lowrate.aac'),
    [string]$AacTruth   = (Join-Path $PSScriptRoot 'aac\lowrate.f32.z')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = (Resolve-Path $Root).Path
if (-not $Evidence) { $Evidence = Join-Path ([IO.Path]::GetTempPath()) 'feetbrowser-evidence' }
New-Item -ItemType Directory -Force -Path $Evidence | Out-Null
$Evidence = (Resolve-Path $Evidence).Path

# Scratch space with no spaces and no accents in the path, on purpose: $Root
# is the thing under test and may well have both, but the fixtures and the
# temporary PNGs should not add a second variable to a failure.
$Work = Join-Path ([IO.Path]::GetTempPath()) ('fb-verify-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $Work | Out-Null

$script:Failures = @()
$script:Checks = 0
$script:port = 0
$script:server = $null

# Poison, deliberately, for everything this script starts. If the ._pth
# mechanism ever stopped working, PYTHONHOME pointing at a directory that does
# not exist would stop the interpreter dead -- which is the loudest possible
# way for the isolation claim to fail, and much better than it quietly
# borrowing a Python from elsewhere on the machine. The CI job strips PATH as
# well; this is here so the script proves the same thing when a human runs it
# on their own desktop.
$env:PYTHONPATH = 'C:\feetbrowser-must-ignore-this\lib'
$env:PYTHONHOME = 'C:\feetbrowser-must-ignore-this'

function Check($name, [scriptblock]$body) {
    $script:Checks++
    Write-Host ""
    Write-Host "-- $name" -ForegroundColor Cyan
    try {
        & $body
        Write-Host "   ok" -ForegroundColor Green
    } catch {
        Write-Host "   FAILED: $($_.Exception.Message)" -ForegroundColor Red
        $script:Failures += "$name : $($_.Exception.Message)"
    }
}

# Start-Process joins -ArgumentList with spaces and adds no quoting of its
# own, which is a trap the moment a path has a space in it. Quoting every
# argument is harmless for the ones that do not need it -- Windows strips the
# quotes again in CommandLineToArgvW, which is what both CPython and the Rust
# launcher parse their command line with.
function Invoke-Exe {
    param(
        [string]$Path,
        [string[]]$Arguments = @(),
        [int]$TimeoutSeconds = 180
    )
    $out = Join-Path $Work ([Guid]::NewGuid().ToString('N') + '.out')
    $err = Join-Path $Work ([Guid]::NewGuid().ToString('N') + '.err')
    $quoted = @($Arguments | ForEach-Object { '"' + $_ + '"' })
    $p = if ($quoted.Count) {
        Start-Process -FilePath $Path -ArgumentList $quoted -PassThru -WindowStyle Hidden `
                      -RedirectStandardOutput $out -RedirectStandardError $err
    } else {
        Start-Process -FilePath $Path -PassThru -WindowStyle Hidden `
                      -RedirectStandardOutput $out -RedirectStandardError $err
    }
    if (-not $p.WaitForExit($TimeoutSeconds * 1000)) {
        try { $p.Kill() } catch { }   # it may have exited between the two lines
        throw "$([IO.Path]::GetFileName($Path)) $($Arguments -join ' ') did not exit within $TimeoutSeconds s"
    }
    # Get-Content -Raw gives $null for an empty file, and $null.Trim() under
    # StrictMode is an error in every caller. Strings, always.
    $stdout = [string](Get-Content -Raw -ErrorAction SilentlyContinue $out)
    $stderr = [string](Get-Content -Raw -ErrorAction SilentlyContinue $err)
    [pscustomobject]@{ ExitCode = $p.ExitCode; Out = $stdout; Err = $stderr }
}

function Assert-ExitZero($result, $what) {
    if ($result.ExitCode -ne 0) {
        throw "$what exited $($result.ExitCode)`nstdout: $($result.Out)`nstderr: $($result.Err)"
    }
}

$Exe = Join-Path $Root 'FeetBrowser.exe'
$Python = Join-Path $Root 'python.exe'

Write-Host "verifying $Root"
Write-Host "evidence  $Evidence"

# ---------------------------------------------------------------------------
Check "the folder is complete" {
    # doormat\win32.py is named for the same reason feetplayer\ is: both are
    # pip dependencies rather than files in this repository, so a pip step
    # that silently did nothing leaves a bundle that starts, renders and
    # screenshots. Without doormat it then opens no window at all, which the
    # Explorer-launch check below proves the hard way, sixty seconds and a
    # whole browser start later, and which this one says in a filename.
    foreach ($needed in @('FeetBrowser.exe', 'FeetBrowser._pth', 'feetbrowser\__main__.py',
                          'feetbrowser\ua.css', '_ssl.pyd',
                          'libssl-3.dll', 'libcrypto-3.dll', '_socket.pyd', '_ctypes.pyd',
                          'libffi-8.dll', 'vcruntime140.dll', 'README-FIRST.txt',
                          'install.ps1', 'uninstall.ps1',
                          'feetplayer\__init__.py', 'feetplayer\mediacodec.py',
                          'doormat\__init__.py', 'doormat\win32.py')) {
        if (-not (Test-Path (Join-Path $Root $needed))) { throw "missing $needed" }
    }
    # -match rather than -Filter: in a Windows wildcard '?' also matches zero
    # characters at the end of a name, so 'python3??.dll' would match the
    # python3.dll forwarder as well and the count would never be one.
    $dll = @(Get-ChildItem -Path $Root -File | Where-Object { $_.Name -match '^python3\d+\.dll$' })
    if ($dll.Count -ne 1) { throw "expected one python3NN.dll, found $($dll.Count)" }
    # -Recurse because whether the engine sits at the top of the folder or in a
    # package directory of its own is maturin's business, not ours. What matters
    # is that there is exactly one of it and that importing it works, which the
    # next checks do for real.
    $pyd = @(Get-ChildItem -Path $Root -Recurse -File -Filter 'feetbrowser_engine*.pyd')
    if ($pyd.Count -ne 1) { throw "expected one feetbrowser_engine .pyd, found $($pyd.Count)" }
    Write-Host "   $($dll[0].Name), $($pyd[0].Name)"
}

# ---------------------------------------------------------------------------
Check "FeetBrowser.exe is a GUI-subsystem binary with version information" {
    # Straight out of the PE header, because "no console window flashes up" is
    # a property of this one 16-bit field and nothing else. e_lfanew at 0x3c
    # points at the PE signature; the COFF header is 20 bytes; Subsystem is at
    # offset 68 of the optional header. 2 is IMAGE_SUBSYSTEM_WINDOWS_GUI, 3 is
    # WINDOWS_CUI.
    $bytes = [IO.File]::ReadAllBytes($Exe)
    $peOffset = [BitConverter]::ToInt32($bytes, 0x3c)
    if ([Text.Encoding]::ASCII.GetString($bytes, $peOffset, 4).TrimEnd([char]0) -ne 'PE') {
        throw "no PE signature at 0x$($peOffset.ToString('x'))"
    }
    $subsystem = [BitConverter]::ToUInt16($bytes, $peOffset + 24 + 68)
    if ($subsystem -ne 2) { throw "subsystem is $subsystem, expected 2 (WINDOWS_GUI)" }

    $info = (Get-Item $Exe).VersionInfo
    if ($info.ProductName -ne 'FeetBrowser') { throw "ProductName is '$($info.ProductName)'" }
    if (-not $info.FileVersion) { throw "no FileVersion; the resource script did not get linked in" }
    Write-Host "   subsystem 2 (GUI), version $($info.FileVersion), $([int]((Get-Item $Exe).Length / 1KB)) KB"
}

# ---------------------------------------------------------------------------
$version = (Select-String -Path (Join-Path $Root 'feetbrowser\__init__.py') `
                          -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value

Check "--version" {
    $r = Invoke-Exe $Exe @('--version') 60
    Assert-ExitZero $r '--version'
    if ($r.Out.Trim() -ne "FeetBrowser $version") {
        throw "said '$($r.Out.Trim())', expected 'FeetBrowser $version'"
    }
    Write-Host "   $($r.Out.Trim())"
}

# ---------------------------------------------------------------------------
# The check this file was extended for. feetplayer\h264.py and
# feetplayer\aac.py fall back to compiling their Fortran with gfortran, which
# every developer has and no user does, so a bundle that shipped no decoder
# passes every other check here, installs, starts, renders, and only admits it
# to whoever opens a video -- by which time it is a download. PATH is already
# cut back to the system directories in CI, so nothing on this machine can
# supply a compiler or a MinGW runtime DLL: what answers is what shipped.
#
# Now that the media stack is a pip dependency there is a second way to get
# this wrong -- shipping no feetplayer at all -- and it fails here too, in the
# folder check above and in the missing library below.
#
# Both decoders, one check each. A bundle carrying the video library and not
# the sound one plays pictures in silence, and it passed this file until the
# second Check below was added.
# ---------------------------------------------------------------------------
function Check-Decoder {
    param($What, $Glob, $Module, $Flag, $Vector, $Truth)
    $lib = @(Get-ChildItem -Path (Join-Path $Root 'feetplayer') -File -Filter $Glob)
    if ($lib.Count -ne 1) { throw "expected one prebuilt $What decoder in feetplayer\, found $($lib.Count)" }
    if (-not (Test-Path (Join-Path $Root 'feetplayer\fortran'))) {
        throw "no fortran\ inside feetplayer\; $Module cannot match the decoder against its sources"
    }
    $checkArgs = @($Flag)
    if ((Test-Path $Vector) -and (Test-Path $Truth)) {
        $checkArgs += @($Vector, $Truth)
    } else {
        Write-Host "   no $What test vector beside this script: checking that the decoder loads, not what it decodes"
    }
    $r = Invoke-Exe $Exe $checkArgs 120
    Assert-ExitZero $r $Flag
    Write-Host ("   " + ($r.Out.Trim() -replace "`r?`n", "`n   "))
    Write-Host "   $($lib[0].Name), $([int]($lib[0].Length / 1KB)) KB"
}

Check "it can decode H.264" {
    Check-Decoder 'H.264' '_h264_*.dll' 'feetplayer\h264.py' '--check-video' $H264Vector $H264Truth
}

Check "it can decode AAC" {
    Check-Decoder 'AAC' '_aac_*.dll' 'feetplayer\aac.py' '--check-audio' $AacVector $AacTruth
}

Check "--help" {
    $r = Invoke-Exe $Exe @('--help') 60
    Assert-ExitZero $r '--help'
    foreach ($phrase in @('usage: python3 -m feetbrowser', '--screenshot', '--toes')) {
        if ($r.Out -notlike "*$phrase*") { throw "--help never mentions '$phrase'" }
    }
}

# ---------------------------------------------------------------------------
# Everything the bundle has to be able to say about itself, asked from inside
# it. Written out here rather than passed with -c so that quoting is not part
# of the test.
# ---------------------------------------------------------------------------
$selfcheck = @'
"""Run by verify-bundle.ps1, inside the bundle's own interpreter."""
import os
import sys

root = os.path.normcase(os.path.abspath(sys.argv[1]))
network = sys.argv[2] == "network"
failures = []


def check(name, fn):
    try:
        detail = fn()
    except Exception as e:                                  # noqa: BLE001
        failures.append("%s: %s: %s" % (name, type(e).__name__, e))
        print("   FAIL %s: %s: %s" % (name, type(e).__name__, e))
    else:
        print("   ok   %s%s" % (name, (" -- " + detail) if detail else ""))


def inside(path):
    return os.path.normcase(os.path.abspath(path)).startswith(root)


def interpreter_is_ours():
    assert inside(sys.executable), sys.executable
    assert inside(sys.prefix), sys.prefix
    return sys.version.split()[0]


def sys_path_is_sealed():
    # The whole isolation claim, in one assertion. The job that runs this sets
    # PYTHONPATH and PYTHONHOME to a poison directory; if the ._pth were not
    # doing its job, the poison would be on sys.path (or the interpreter would
    # not have started at all, which is the other way this fails).
    poison = os.environ.get("PYTHONPATH", "")
    home = os.environ.get("PYTHONHOME", "")
    assert poison, "the caller was supposed to set PYTHONPATH to something bogus"
    stray = [p for p in sys.path if p and not inside(p)]
    assert not stray, "sys.path leaves the bundle: %r" % (stray,)
    assert not any(poison.lower() in p.lower() for p in sys.path), sys.path
    assert not any("site-packages" in p.lower() for p in sys.path), sys.path
    return "PYTHONPATH=%s and PYTHONHOME=%s both ignored; %d entries, all inside" % (
        poison, home, len(sys.path))


def no_optional_extras():
    # curl_cffi is the one module the browser still reaches for lazily, in
    # net.py's request_impersonated(). It cannot be in a bundle that ships no
    # pip, so this asserts it really is absent -- the point being that the
    # check further down exercises the fallback path rather than the installed
    # one. The other two are the usual suspects a Python image pipeline picks
    # up by accident; the engine decodes PNG, GIF, JPEG and Netpbm itself and
    # neither should ever appear.
    missing = []
    for name in ("curl_cffi", "PIL", "cairosvg"):
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
        else:
            raise AssertionError("%s is importable; this is not a clean bundle" % name)
    return "absent as expected: " + ", ".join(missing)


def engine_loads():
    import feetbrowser_engine
    assert inside(feetbrowser_engine.__file__), feetbrowser_engine.__file__
    from feetbrowser import raster, fontengine, imagecodec, cssparser  # noqa: F401
    return os.path.basename(feetbrowser_engine.__file__)


def openssl_and_the_certificate_store():
    import ssl
    ctx = ssl.create_default_context()
    stats = ctx.cert_store_stats()
    # load_default_certs on Windows pulls from the system certificate store
    # through _ssl.enum_certificates. Zero here means https would fail
    # verification against every site on the internet.
    assert stats["x509_ca"] > 0, stats
    return "%s, %d CA certificates from the Windows store" % (
        ssl.OPENSSL_VERSION, stats["x509_ca"])


def https_really_works():
    from feetbrowser.net import URL
    _headers, body, _ctype = URL("https://example.com/").request()
    assert "Example Domain" in body, body[:400]
    return "fetched %d bytes from https://example.com/" % len(body)


def impersonation_degrades_instead_of_exploding():
    # net.py's request_impersonated() imports curl_cffi lazily and falls back
    # to the ordinary socket path when it is not installed. In a bundle it is
    # never installed, so this is the path every user is on, and an unhandled
    # ImportError here would be a crash on first navigation to a site that
    # asks for it.
    from feetbrowser.net import URL
    _headers, body, _ctype = URL("https://example.com/").request_impersonated()
    assert "Example Domain" in body, body[:400]
    return "fell back to the socket transport, %d bytes" % len(body)


check("the interpreter is the one in the folder", interpreter_is_ours)
check("sys.path never leaves the folder", sys_path_is_sealed)
check("no optional third-party modules are present", no_optional_extras)
check("the compiled engine loads", engine_loads)
check("OpenSSL and the certificate store", openssl_and_the_certificate_store)
if network:
    check("https through the browser's own transport", https_really_works)
    check("request_impersonated falls back without curl_cffi", impersonation_degrades_instead_of_exploding)

sys.exit(1 if failures else 0)
'@

$selfcheckPath = Join-Path $Work 'selfcheck.py'
[IO.File]::WriteAllText($selfcheckPath, $selfcheck, [Text.UTF8Encoding]::new($false))

Check "the interpreter, the engine and TLS, from inside the bundle" {
    $mode = if ($SkipNetwork) { 'offline' } else { 'network' }
    $r = Invoke-Exe $Python @($selfcheckPath, $Root, $mode) 240
    Write-Host ($r.Out.TrimEnd())
    if ($r.Err) { Write-Host $r.Err.TrimEnd() }
    Assert-ExitZero $r 'selfcheck.py'
}

# ---------------------------------------------------------------------------
# A page, over a socket, into pixels.
# ---------------------------------------------------------------------------
$fixtureDir = Join-Path $Work 'fixture'
New-Item -ItemType Directory -Force -Path $fixtureDir | Out-Null
$fixture = @'
<!doctype html>
<html><head><title>FeetBrowser packaging fixture</title>
<style>
body { background: #ffffff; }
h1 { color: #ff0000; font-size: 64px; }
div.swatch { background: #1e90ff; width: 640px; height: 320px; }
</style></head>
<body>
<h1>PACKAGED</h1>
<div class="swatch"></div>
<p>Served over a socket to the packaged browser.</p>
</body></html>
'@
[IO.File]::WriteAllText((Join-Path $fixtureDir 'index.html'), $fixture, [Text.UTF8Encoding]::new($false))

# The counting half of the render assertion, again inside the bundle: it is
# the bundled engine's own PNG decoder reading the bundled renderer's output.
$countPath = Join-Path $Work 'countpixels.py'
[IO.File]::WriteAllText($countPath, @'
import sys
from feetbrowser.imagecodec import decode

path, want_blue, want_red = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
with open(path, "rb") as f:
    w, h, rgba = decode(f.read())
blue = red = 0
for i in range(0, len(rgba), 4):
    r, g, b = rgba[i], rgba[i + 1], rgba[i + 2]
    if abs(r - 0x1E) < 12 and abs(g - 0x90) < 12 and abs(b - 0xFF) < 12:
        blue += 1
    elif r > 180 and g < 80 and b < 80:
        red += 1
print("%dx%d: %d dodger-blue pixels, %d red pixels" % (w, h, blue, red))
if blue < want_blue:
    sys.exit("the swatch did not render: %d blue pixels, wanted %d" % (blue, want_blue))
if red < want_red:
    sys.exit("the heading did not render: %d red pixels, wanted %d" % (red, want_red))
'@, [Text.UTF8Encoding]::new($false))

Check "a page fetched over a socket renders to the right pixels" {
    # Served by the bundle's own python.exe, which is also a check that the
    # standard library really is complete in that zip.
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $script:port = ([Net.IPEndPoint]$listener.LocalEndpoint).Port
    $listener.Stop()

    $script:server = Start-Process -FilePath $Python -WindowStyle Hidden -PassThru `
        -ArgumentList @('"-m"', '"http.server"', "`"$script:port`"", '"--bind"', '"127.0.0.1"',
                        '"--directory"', "`"$fixtureDir`"")

    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        try {
            $probe = [Net.Sockets.TcpClient]::new('127.0.0.1', $script:port)
            $probe.Close()
            break
        } catch { Start-Sleep -Milliseconds 200 }
    }

    $url = "http://127.0.0.1:$script:port/index.html"
    $shot = Join-Path $Work 'fixture.png'
    $r = Invoke-Exe $Exe @('--screenshot', $url, $shot) 240
    Assert-ExitZero $r '--screenshot against the fixture'
    if (-not (Test-Path $shot)) { throw "--screenshot exited 0 but wrote no file" }

    $counted = Invoke-Exe $Python @($countPath, $shot, '100000', '1000') 120
    Write-Host "   $($counted.Out.Trim())"
    Assert-ExitZero $counted 'the pixel count'
    Copy-Item $shot (Join-Path $Evidence 'fixture-over-http.png') -Force
}

# ---------------------------------------------------------------------------
Check "--screenshot writes to a path with a space and an accent in it" {
    # Built from character codes rather than typed literally so that nothing
    # about this test depends on the encoding this file was saved in.
    $awkward = Join-Path $Work ("out " + [char]0xE9 + "vidence")
    New-Item -ItemType Directory -Force -Path $awkward | Out-Null
    $shot = Join-Path $awkward ('r' + [char]0xE9 + 'ndu.png')
    $r = Invoke-Exe $Exe @('--screenshot', 'about:blank', $shot) 120
    Assert-ExitZero $r '--screenshot to an awkward path'
    if (-not (Test-Path -LiteralPath $shot)) { throw "no file at $shot" }
    Write-Host "   wrote $shot"
}

# ---------------------------------------------------------------------------
Check "launching it the way Explorer does opens a real window" {
    # No redirected handles and no arguments beyond the URL: as close to a
    # double-click as a script can get. What is being proved is that the
    # process reaches doormat/win32.py and gets an HWND out of it -- a
    # bundle that could render headlessly but could not open a window would
    # pass every check above and still be useless.
    $url = "http://127.0.0.1:$script:port/index.html"
    $p = Start-Process -FilePath $Exe -ArgumentList "`"$url`"" -PassThru
    try {
        $deadline = (Get-Date).AddSeconds(60)
        $handle = 0
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Milliseconds 500
            $p.Refresh()
            if ($p.HasExited) { throw "it exited with $($p.ExitCode) instead of opening a window" }
            if ($p.MainWindowHandle -ne 0) { $handle = $p.MainWindowHandle; break }
        }
        if ($handle -eq 0) { throw "no window appeared within 60 s" }
        Start-Sleep -Seconds 3   # let it finish the first paint
        $p.Refresh()
        Write-Host ("   HWND 0x{0:x}, title '{1}', one process (pid {2})" -f `
                    [int64]$p.MainWindowHandle, $p.MainWindowTitle, $p.Id)
    } finally {
        # Best effort: a browser that has already gone is the good outcome,
        # and failing the check because the cleanup raced would be silly.
        try { if (-not $p.HasExited) { $p.Kill() } } catch { }
    }
}

if ($script:server) { try { $script:server.Kill() } catch { } }   # best effort

# ---------------------------------------------------------------------------
if (-not $SkipNetwork) {
    Check "a real page off the real web renders" {
        $shot = Join-Path $Work 'example.png'
        $ok = $false
        # example.com is somebody else's server having somebody else's
        # afternoon, so this gets three goes before it is our bug.
        foreach ($attempt in 1..3) {
            $r = Invoke-Exe $Exe @('--screenshot', 'https://example.com/', $shot) 240
            if ($r.ExitCode -eq 0 -and (Test-Path $shot)) { $ok = $true; break }
            Write-Host "   attempt $attempt failed ($($r.ExitCode)): $($r.Err)"
            Start-Sleep -Seconds 5
        }
        if (-not $ok) { throw "could not render https://example.com/ in three attempts" }
        $size = (Get-Item $shot).Length
        if ($size -lt 2000) { throw "the render is only $size bytes; that is a blank page" }
        Copy-Item $shot (Join-Path $Evidence 'example.com-over-https.png') -Force
        Write-Host "   $size bytes"
    }
}

# ---------------------------------------------------------------------------
Write-Host ""
Write-Host ("{0} checks, {1} failed" -f $script:Checks, $script:Failures.Count)
if ($script:Failures.Count) {
    foreach ($f in $script:Failures) { Write-Host "  $f" -ForegroundColor Red }
    exit 1
}
Write-Host "the bundle is self-contained and works." -ForegroundColor Green
