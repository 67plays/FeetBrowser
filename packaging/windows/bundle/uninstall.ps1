<#
.SYNOPSIS
    Undo install.ps1.

.DESCRIPTION
    Removes the Start Menu shortcut, the Add/Remove Programs entry and the
    installed folder. Leaves the two files the browser writes into the user's
    profile alone unless -Purge is given, because losing your saved settings
    is not what "uninstall" should mean by default.

.PARAMETER Quiet
    No prompts, no output beyond errors. This is what the Settings app runs.

.PARAMETER Purge
    Also delete ~/.feetbrowser_shoes.json, the saved theme.
#>
[CmdletBinding()]
param(
    [switch]$Quiet,
    [switch]$Purge
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$AppName = 'FeetBrowser'
$RegistryKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$AppName"

function Say($message) { if (-not $Quiet) { Write-Host $message } }

# Where the install actually is, according to the registry, falling back to
# wherever this script is sitting. The registry is asked first because the
# Settings app can run this script from a copy.
$location = $PSScriptRoot
if (Test-Path $RegistryKey) {
    $recorded = (Get-ItemProperty -Path $RegistryKey -Name InstallLocation -ErrorAction SilentlyContinue).InstallLocation
    if ($recorded -and (Test-Path $recorded)) { $location = $recorded }
}

Say "Uninstalling $AppName from $location"

$link = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$AppName.lnk"
if (Test-Path $link) {
    Remove-Item -Force $link
    Say "  removed the Start Menu shortcut"
}

if (Test-Path $RegistryKey) {
    Remove-Item -Path $RegistryKey -Recurse -Force
    Say "  removed the Add/Remove Programs entry"
}

if ($Purge) {
    $shoes = Join-Path $HOME '.feetbrowser_shoes.json'
    if (Test-Path $shoes) { Remove-Item -Force $shoes; Say "  removed $shoes" }
}

if (Test-Path $location) {
    # This script is inside the directory it is about to delete. PowerShell
    # reads a script into memory before running it and does not hold the file
    # open, so the only thing that can block the delete is the working
    # directory -- which is why we leave first.
    Set-Location $env:TEMP
    try {
        Remove-Item -Recurse -Force $location
        Say "  removed $location"
    } catch {
        # A browser still running out of the folder is the one case that
        # genuinely cannot be deleted, and "access denied" is not a useful
        # thing to say about it.
        throw ("could not remove $location -- close FeetBrowser and try again.`n" +
               $_.Exception.Message)
    }
}

Say ""
Say "$AppName is gone."
