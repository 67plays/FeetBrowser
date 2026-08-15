<#
.SYNOPSIS
    Put this folder somewhere permanent, with a Start Menu entry.

.DESCRIPTION
    Not an installer in the setup.exe sense -- there is no setup.exe, on
    purpose (packaging/windows/README.md says why). This is the small
    remainder of what an installer does that a portable folder cannot do for
    itself: give the thing a home, a Start Menu shortcut, and a line in
    Add/Remove Programs so it can be got rid of the way everything else is.

    Per user. No administrator rights, no service, no PATH entry, no file
    associations, nothing written outside HKCU and the user's own profile.

.PARAMETER Destination
    Where to install. Defaults to %LOCALAPPDATA%\Programs\FeetBrowser.

.PARAMETER NoShortcut
    Skip the Start Menu shortcut.

.EXAMPLE
    Right-click this file and choose "Run with PowerShell".
#>
[CmdletBinding()]
param(
    [string]$Destination,
    [switch]$NoShortcut
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$AppName = 'FeetBrowser'
$Publisher = '67plays'
$RegistryKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppName"

$source = $PSScriptRoot
if (-not (Test-Path (Join-Path $source 'FeetBrowser.exe'))) {
    throw "install.ps1 has been separated from the rest of the folder: no FeetBrowser.exe beside it."
}
if (-not $Destination) {
    $Destination = Join-Path $env:LOCALAPPDATA "Programs\$AppName"
}

$version = (Get-Item (Join-Path $source 'FeetBrowser.exe')).VersionInfo.ProductVersion
if (-not $version) { $version = '0.0.0.0' }

Write-Host "Installing $AppName $version"
Write-Host "  from $source"
Write-Host "  to   $Destination"

# Running from inside the destination is the "reinstall over myself" case, and
# copying a directory into itself is how you fill a disk.
if ((Resolve-Path $source).Path.TrimEnd('\') -ieq $Destination.TrimEnd('\')) {
    Write-Host "  already installed there; refreshing the shortcut and registry entry only."
} else {
    if (Test-Path $Destination) {
        Write-Host "  removing the previous copy"
        Remove-Item -Recurse -Force $Destination
    }
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    Copy-Item -Path (Join-Path $source '*') -Destination $Destination -Recurse -Force
}

$exe = Join-Path $Destination 'FeetBrowser.exe'

if (-not $NoShortcut) {
    $startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
    New-Item -ItemType Directory -Force -Path $startMenu | Out-Null
    $link = Join-Path $startMenu "$AppName.lnk"
    # WScript.Shell is the shortcut API Windows has always had; there is no
    # PowerShell cmdlet for .lnk files and no reason to ship a binary for it.
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($link)
    $shortcut.TargetPath = $exe
    $shortcut.WorkingDirectory = $Destination
    $shortcut.IconLocation = "$exe,0"
    $shortcut.Description = 'A web browser built from scratch'
    $shortcut.Save()
    Write-Host "  Start Menu: $link"
}

# Add/Remove Programs. UninstallString is what the Settings app runs when
# somebody clicks Uninstall, so it has to work from any working directory and
# survive a space in the path -- hence the quoting and -File.
$size = [int](((Get-ChildItem -Recurse -File $Destination |
                Measure-Object -Property Length -Sum).Sum) / 1KB)
$uninstall = Join-Path $Destination 'uninstall.ps1'

New-Item -Path $RegistryKey -Force | Out-Null
$values = @{
    DisplayName     = $AppName
    DisplayVersion  = $version
    Publisher       = $Publisher
    InstallLocation = $Destination
    DisplayIcon     = $exe
    URLInfoAbout    = 'https://github.com/JuiceyDew/FeetBrowser'
    UninstallString = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$uninstall`""
    QuietUninstallString = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$uninstall`" -Quiet"
    EstimatedSize   = $size
    NoModify        = 1
    NoRepair        = 1
}
foreach ($name in $values.Keys) {
    $kind = if ($values[$name] -is [int]) { 'DWord' } else { 'String' }
    New-ItemProperty -Path $RegistryKey -Name $name -Value $values[$name] -PropertyType $kind -Force | Out-Null
}
Write-Host "  Add/Remove Programs: $RegistryKey"

Write-Host ""
Write-Host "Done. $AppName is in the Start Menu."
Write-Host "Uninstall it from Settings > Apps, or by running:"
Write-Host "  $uninstall"
