<#
.SYNOPSIS
    Assemble the portable Windows build of FeetBrowser.

.DESCRIPTION
    Three things go in and one directory comes out:

      * python.org's Windows embeddable package -- plain CPython, no
        installer, redistributable, pinned to a version and a SHA-256 below;
      * the compiled engine, as the .pyd out of a maturin wheel;
      * FeetBrowser.exe, the launcher crate in launcher/.

    Plus feetbrowser/ itself, which is pure Python and just gets copied.

    Nothing is frozen, bytecode-scanned or dependency-analysed. The layout is
    the one the embeddable package was designed for: everything in one
    directory, and a `._pth` file next to the DLL that says what sys.path is.
    That is the whole mechanism, and it is why this script is short.

.PARAMETER Wheel
    The feetbrowser_engine wheel to take the .pyd from. Defaults to whatever
    single .whl is sitting in dist/ at the repository root. Build one with:

        maturin build --release --manifest-path rust/Cargo.toml --out dist

    from an interpreter whose minor version matches PythonVersion below --
    the extension is not abi3 (see the comment at the top of wheels.yml), so
    a cp312 wheel will not import into a 3.13 runtime.

.PARAMETER OutDir
    Where to build. Defaults to build/windows at the repository root.

.PARAMETER SkipLauncher
    Assemble everything except FeetBrowser.exe. Only useful for poking at the
    layout on a machine with no Rust toolchain; the result is not shippable.

.PARAMETER NoZip
    Leave the staged directory and do not produce the .zip.
#>
[CmdletBinding()]
param(
    [string]$Wheel,
    [string]$OutDir,
    [string]$CacheDir,
    [switch]$SkipLauncher,
    [switch]$NoZip
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# What we ship an interpreter of.
#
# Pinned, not "latest": the .pyd is built for one CPython minor version and a
# bundle that silently moved to the next one would ship an extension the
# interpreter cannot load. The hash is checked on every build, so a mirror
# serving something else is a failed build rather than a surprise in a
# stranger's download.
#
# To move to a new CPython: change both lines, and check the new sum against
# https://www.python.org/downloads/windows/ rather than against whatever this
# script just downloaded.
# ---------------------------------------------------------------------------
$PythonVersion = '3.13.15'
$PythonSha256  = 'D1F04D990AEE1253D8569E8E5104E30FA9F5FA830899F14843448872D936A2CF'

$MinorTag = 'cp' + ($PythonVersion -split '\.')[0] + ($PythonVersion -split '\.')[1]   # cp313

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if (-not $OutDir)   { $OutDir   = Join-Path $RepoRoot 'build\windows' }
if (-not $CacheDir) { $CacheDir = Join-Path $PSScriptRoot '.cache' }

function Step($message) { Write-Host "==> $message" -ForegroundColor Cyan }
function Fail($message) { throw $message }

# ---------------------------------------------------------------------------
# 1. The interpreter.
# ---------------------------------------------------------------------------
$embedZip = Join-Path $CacheDir "python-$PythonVersion-embed-amd64.zip"
New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null

if (Test-Path $embedZip) {
    Step "using the cached CPython $PythonVersion embeddable package"
} else {
    $url = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
    Step "downloading $url"
    $previous = $ProgressPreference
    $ProgressPreference = 'SilentlyContinue'   # or Invoke-WebRequest crawls
    try {
        Invoke-WebRequest -Uri $url -OutFile $embedZip -UseBasicParsing
    } finally {
        $ProgressPreference = $previous
    }
}

$actual = (Get-FileHash -Algorithm SHA256 -Path $embedZip).Hash
if ($actual -ne $PythonSha256) {
    Remove-Item -Force $embedZip
    Fail "the embeddable package does not match its pinned hash.`n  expected $PythonSha256`n  got      $actual"
}
Step "CPython $PythonVersion checksum ok"

# ---------------------------------------------------------------------------
# 2. The engine.
# ---------------------------------------------------------------------------
if (-not $Wheel) {
    $found = @(Get-ChildItem -Path (Join-Path $RepoRoot 'dist') -Filter '*.whl' -ErrorAction SilentlyContinue)
    if ($found.Count -ne 1) {
        Fail ("expected exactly one wheel in dist/, found $($found.Count). Build one with:`n" +
              "  maturin build --release --manifest-path rust/Cargo.toml --out dist`n" +
              "or pass -Wheel <path>.")
    }
    $Wheel = $found[0].FullName
}
$Wheel = (Resolve-Path $Wheel).Path
$wheelName = Split-Path -Leaf $Wheel

# The one mistake this script exists to make impossible: a wheel for the wrong
# interpreter. It installs fine, imports nowhere, and the failure lands on the
# user as "DLL load failed while importing feetbrowser_engine".
if ($wheelName -notmatch [regex]::Escape($MinorTag)) {
    Fail "$wheelName is not a $MinorTag wheel; the bundle ships CPython $PythonVersion and needs one."
}
if ($wheelName -notmatch 'win_amd64') {
    Fail "$wheelName is not a win_amd64 wheel."
}
Step "engine wheel: $wheelName"

# ---------------------------------------------------------------------------
# 3. Stage.
# ---------------------------------------------------------------------------
$stage = Join-Path $OutDir 'FeetBrowser'
if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
New-Item -ItemType Directory -Force -Path $stage | Out-Null

Step "expanding the embeddable package"
Expand-Archive -Path $embedZip -DestinationPath $stage -Force

# python.cat is a signature catalogue for the CPython files as python.org
# shipped them. Keeping it means anyone can verify that half of the bundle
# against Microsoft's own tooling, which is worth more than the 500 KB.
# -match, not -Filter: Windows wildcards let '?' match *zero* characters at the
# end of a name, so 'python3??.dll' happily matches python3.dll -- the stable
# ABI forwarder, which the embeddable package also ships -- as well as
# python313.dll, and the count below would always be two.
$pythonDll = @(Get-ChildItem -Path $stage -File | Where-Object { $_.Name -match '^python3\d+\.dll$' })
if ($pythonDll.Count -ne 1) { Fail "expected one python3NN.dll in the embeddable package, found $($pythonDll.Count)" }
$stdlibZip = [IO.Path]::GetFileNameWithoutExtension($pythonDll[0].Name) + '.zip'
if (-not (Test-Path (Join-Path $stage $stdlibZip))) { Fail "no $stdlibZip in the embeddable package" }

Step "unpacking the engine"
$wheelTmp = Join-Path $OutDir '_wheel'
if (Test-Path $wheelTmp) { Remove-Item -Recurse -Force $wheelTmp }
# Expand-Archive insists on the extension, and a .whl is a .zip.
$wheelCopy = Join-Path $OutDir ($wheelName + '.zip')
Copy-Item $Wheel $wheelCopy -Force
Expand-Archive -Path $wheelCopy -DestinationPath $wheelTmp -Force
Remove-Item -Force $wheelCopy

# Everything at the top of the wheel that is not packaging metadata: the .pyd
# and, if a future engine ever grows one, a DLL beside it.
$payload = @(Get-ChildItem -Path $wheelTmp -File)
if (-not ($payload | Where-Object { $_.Extension -eq '.pyd' })) {
    Fail "$wheelName contains no .pyd at its root"
}
foreach ($file in $payload) { Copy-Item $file.FullName $stage -Force }
Remove-Item -Recurse -Force $wheelTmp

# Copy first and clean after: Copy-Item's -Exclude only reaches the top level
# of a -Recurse copy, which is exactly where the __pycache__ directories are
# not.
Step "copying feetbrowser/"
Copy-Item -Recurse -Force -Path (Join-Path $RepoRoot 'feetbrowser') -Destination $stage
$package = Join-Path $stage 'feetbrowser'
Get-ChildItem -Path $package -Recurse -Force -Directory |
    Where-Object { $_.Name -eq '__pycache__' } |
    Remove-Item -Recurse -Force
Get-ChildItem -Path $package -Recurse -Force -File |
    Where-Object { $_.Extension -in @('.pyc', '.pyo') } |
    Remove-Item -Force

# toes/ has to exist for the toe engine to have somewhere to install into;
# feetbrowser/toes.py looks for it one directory above the package, which in
# this layout is the bundle root.
New-Item -ItemType Directory -Force -Path (Join-Path $stage 'toes') | Out-Null
Copy-Item (Join-Path $RepoRoot 'toes\README.md') (Join-Path $stage 'toes') -Force

foreach ($doc in @('LICENSE', 'README.md')) {
    $src = Join-Path $RepoRoot $doc
    if (Test-Path $src) { Copy-Item $src $stage -Force }
}
Copy-Item (Join-Path $PSScriptRoot 'bundle\README-FIRST.txt') $stage -Force
Copy-Item (Join-Path $PSScriptRoot 'bundle\install.ps1')      $stage -Force
Copy-Item (Join-Path $PSScriptRoot 'bundle\uninstall.ps1')    $stage -Force

# ---------------------------------------------------------------------------
# 4. sys.path.
#
# The embeddable package already ships pythonNNN._pth saying exactly what we
# want -- the stdlib zip, then the directory the file is in, which is where
# feetbrowser/ and feetbrowser_engine.pyd now are. It is left alone.
#
# This adds a second copy under the executable's name. CPython looks for a
# `._pth` beside the DLL *and* beside the running executable, and which of
# the two it settles on has moved between releases; with both present the
# answer is the same either way. The file is also the isolation switch: its
# mere existence makes the interpreter ignore PYTHONPATH, PYTHONHOME, the
# registry and site-packages, which is what stops a Python installed
# elsewhere on the machine reaching into this one.
# ---------------------------------------------------------------------------
Step "writing FeetBrowser._pth"
$pth = @(
    "# FeetBrowser runs its own private copy of CPython.",
    "#",
    "# This file is that privacy: with a ._pth beside it, the interpreter takes",
    "# sys.path from here and from nowhere else -- no PYTHONPATH, no PYTHONHOME,",
    "# no registry, no site-packages, no user site directory. Deleting it does",
    "# not make the browser more configurable, it makes it borrow whatever",
    "# Python happens to be installed on the machine, which is the one thing a",
    "# self-contained application must not do.",
    "#",
    "# The two lines are the standard library (a zip) and this directory, which",
    "# is where feetbrowser/ and feetbrowser_engine live.",
    "",
    $stdlibZip,
    "."
)
# ASCII, no BOM: CPython reads a ._pth line by line and a BOM would become
# part of the first path.
[IO.File]::WriteAllLines((Join-Path $stage 'FeetBrowser._pth'), $pth, [Text.UTF8Encoding]::new($false))

# ---------------------------------------------------------------------------
# 5. The launcher.
# ---------------------------------------------------------------------------
if ($SkipLauncher) {
    Write-Warning "skipping FeetBrowser.exe; this bundle is not shippable"
} else {
    Step "building FeetBrowser.exe"
    $launcher = Join-Path $PSScriptRoot 'launcher'
    $target = Join-Path $launcher 'target'
    # No build-machine paths in the shipped binary. There are no dependencies
    # to leak a home directory, but the crate's own path would otherwise be
    # embedded in panic locations.
    $savedRustflags = $env:RUSTFLAGS
    $env:RUSTFLAGS = "--remap-path-prefix=$launcher=feetbrowser-launcher"
    # Turns "no rc.exe, shipping without an icon" from a warning into an error.
    $env:FEETBROWSER_REQUIRE_RESOURCES = '1'
    try {
        & cargo build --release --locked `
            --manifest-path (Join-Path $launcher 'Cargo.toml') `
            --target x86_64-pc-windows-msvc `
            --target-dir $target
        if ($LASTEXITCODE -ne 0) { Fail "cargo build failed ($LASTEXITCODE)" }
    } finally {
        if ($null -eq $savedRustflags) {
            Remove-Item Env:RUSTFLAGS -ErrorAction SilentlyContinue
        } else {
            $env:RUSTFLAGS = $savedRustflags
        }
        Remove-Item Env:FEETBROWSER_REQUIRE_RESOURCES -ErrorAction SilentlyContinue
    }
    Copy-Item (Join-Path $target 'x86_64-pc-windows-msvc\release\FeetBrowser.exe') $stage -Force
}

# ---------------------------------------------------------------------------
# 6. Does it run?
#
# Not the real verification -- verify-bundle.ps1 is, and it runs on a machine
# with the project's Python taken away -- but a build that produces something
# that cannot print its own version should stop here rather than upload.
# ---------------------------------------------------------------------------
if (-not $SkipLauncher) {
    Step "smoke test"
    $expected = (Select-String -Path (Join-Path $RepoRoot 'feetbrowser\__init__.py') `
                               -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
    $out = Join-Path $OutDir 'version.txt'
    $p = Start-Process -FilePath (Join-Path $stage 'FeetBrowser.exe') -ArgumentList '--version' `
                       -Wait -PassThru -NoNewWindow -RedirectStandardOutput $out
    # [string] because Get-Content -Raw hands back $null for an empty file and
    # $null.Trim() under StrictMode would replace a useful error with a
    # confusing one.
    $said = ([string](Get-Content -Raw -ErrorAction SilentlyContinue $out)).Trim()
    Remove-Item -Force -ErrorAction SilentlyContinue $out
    if ($p.ExitCode -ne 0) { Fail "FeetBrowser.exe --version exited $($p.ExitCode)" }
    if ($said -ne "FeetBrowser $expected") { Fail "FeetBrowser.exe --version said '$said', expected 'FeetBrowser $expected'" }
    Write-Host "    $said"
}

# ---------------------------------------------------------------------------
# 7. The zip.
# ---------------------------------------------------------------------------
$bytes = (Get-ChildItem -Recurse -File $stage | Measure-Object -Property Length -Sum).Sum
$count = (Get-ChildItem -Recurse -File $stage).Count
Write-Host ""
Write-Host ("bundle: {0}" -f $stage)
Write-Host ("        {0} files, {1:N1} MB unpacked" -f $count, ($bytes / 1MB))

if (-not $NoZip) {
    Step "zipping"
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = Join-Path $OutDir 'FeetBrowser-windows-x64.zip'
    if (Test-Path $zip) { Remove-Item -Force $zip }
    # CreateFromDirectory rather than Compress-Archive: Compress-Archive is
    # minutes on a tree this size and mangles paths over 260 characters.
    [IO.Compression.ZipFile]::CreateFromDirectory(
        $stage, $zip, [IO.Compression.CompressionLevel]::Optimal, $true)
    Write-Host ("zip:    {0}" -f $zip)
    Write-Host ("        {0:N1} MB" -f ((Get-Item $zip).Length / 1MB))
}
