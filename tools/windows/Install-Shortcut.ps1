# Optional, per-user shortcuts to the portable package; no administrator needed.
$ErrorActionPreference = 'Stop'
$packageRoot = $PSScriptRoot
$runtimeExe = Join-Path $packageRoot 'runtime\pythonw.exe'
$launcher = Join-Path $packageRoot 'launch.pyw'
if (-not (Test-Path -LiteralPath $runtimeExe) -or -not (Test-Path -LiteralPath $launcher)) {
    throw 'Extract the complete package before creating a shortcut.'
}
$shellObject = New-Object -ComObject WScript.Shell
$shortcutPath = Join-Path ([Environment]::GetFolderPath('Desktop')) 'ShiHui Recovery.lnk'
if (Test-Path -LiteralPath $shortcutPath) { throw 'Desktop shortcut already exists; nothing was overwritten.' }
$shortcut = $shellObject.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $runtimeExe
$shortcut.Arguments = '-B "' + $launcher + '"'
$shortcut.WorkingDirectory = $packageRoot
$shortcut.Description = 'Local NTFS file recovery'
$shortcut.Save()
Write-Host 'Shortcut created. Keep the extracted package in its current location.'
