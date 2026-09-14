#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
Tests direct-volume recovery using a read-only copy of a synthetic fixture.
.DESCRIPTION
Accepts a completed New-RecoveryFixture bundle, never an existing disk number
or drive letter. Copies a verified stage to a NEW fixed VHD, mounts only that
copy ReadOnly, and detaches it in finally. No formatting/deletion is performed.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $Fixture,
    [Parameter(Mandatory = $true)][string] $OutputParent,
    [ValidateSet('after-direct-delete', 'after-empty-recycle-bin')]
    [string] $Stage = 'after-direct-delete',
    [string] $Python,
    [string] $TskBin
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
if (-not [Environment]::Is64BitProcess) { throw 'Use 64-bit Windows PowerShell.' }
$bundlePython = Join-Path $PSScriptRoot '..\runtime\python.exe'
if (-not $Python) {
    if (Test-Path -LiteralPath $bundlePython -PathType Leaf) {
        $Python = $bundlePython
        if (-not $TskBin) { $TskBin = Join-Path $PSScriptRoot '..\vendor\tsk\bin' }
    } else { $Python = Join-Path $PSScriptRoot '..\..\.venv\Scripts\python.exe' }
}
$Python = (Resolve-Path -LiteralPath $Python).ProviderPath
if (-not $TskBin) { throw 'Specify TskBin when running from a source checkout.' }
$TskBin = (Resolve-Path -LiteralPath $TskBin).ProviderPath
$helper = Join-Path $PSScriptRoot 'live_volume_validation.py'
if (-not (Test-Path -LiteralPath $helper)) { $helper = Join-Path $PSScriptRoot '..\live_volume_validation.py' }
$helper = (Resolve-Path -LiteralPath $helper).ProviderPath
$Fixture = (Resolve-Path -LiteralPath $Fixture).ProviderPath
$parent = (Resolve-Path -LiteralPath $OutputParent).ProviderPath
if ($parent -notmatch '^[A-Za-z]:\\' -or $parent -match '["\r\n]') { throw 'OutputParent must be a local directory.' }
$ancestor = Get-Item -LiteralPath $parent -Force
if (-not $ancestor.PSIsContainer) { throw 'OutputParent must be a directory.' }
while ($null -ne $ancestor) {
    if ($ancestor.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Output ancestors must not be reparse points.' }
    $ancestor = $ancestor.Parent
}
$run = Join-Path $parent ('live-validation-' + [Guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $run
$material = Join-Path $run 'material'
$utf8 = New-Object Text.UTF8Encoding($false)
function Save-Result([string] $Name, $Value) {
    [IO.File]::WriteAllText((Join-Path $run $Name), ($Value | ConvertTo-Json -Depth 10), $utf8)
}
function Invoke-CheckedPython([string] $Name, [string[]] $Arguments) {
    # PS 5.1 turns redirected native stderr into ErrorRecords. Preserve the
    # complete diagnostics before inspecting the exit code, even under Stop.
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $Python @Arguments 2>&1 | ForEach-Object { $_.ToString() } |
            Out-File -LiteralPath (Join-Path $run ($Name + '.log')) -Encoding UTF8
        $commandExit = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousPreference }
    if ($commandExit -ne 0) { throw "Python check $Name exited with $commandExit; see $Name.log." }
}
function Disk-Inventory { @(Get-Disk | Sort-Object Number | Select-Object Number,UniqueId,Size,IsBoot,IsSystem) }
function Partition-Inventory { @(Get-Partition | Sort-Object DiskNumber,PartitionNumber |
    Select-Object DiskNumber,PartitionNumber,Offset,Size,DriveLetter,GptType,MbrType) }
$before = @(Disk-Inventory)
$partsBefore = @(Partition-Inventory)
Save-Result 'disks-before.json' $before
Save-Result 'partitions-before.json' $partsBefore
$workflow = [ordered]@{ schema_version=1; status='failed'; stage=$Stage; scope='read_only_synthetic_vhd'; results=$material; exit_code=2 }
$copy = $null
$plan = $null
$checked = $false
$detached = $false
try {
    Invoke-CheckedPython 'startup' @('-X','utf8','-B','-m','recovery_core','doctor','--tsk-bin',$TskBin)
    Invoke-CheckedPython 'qt-import' @('-X','utf8','-B','-c','from PySide6.QtWidgets import QApplication')
    Invoke-CheckedPython 'prepare' @('-X','utf8','-B',$helper,'prepare','--fixture',$Fixture,'--output',$material,'--stage',$Stage)
    $plan = Get-Content -LiteralPath (Join-Path $material 'plan.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $candidate = [IO.Path]::GetFullPath($plan.copy.path)
    if ($candidate -ne (Join-Path $material 'readonly-copy.vhd') -or
        (Get-Item -LiteralPath $candidate -Force).Attributes -band [IO.FileAttributes]::ReparsePoint -or
        (Get-Item -LiteralPath $candidate).Length -ne 128MB + 512 -or
        (Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash -ne $plan.copy.sha256) { throw 'Prepared copy is invalid.' }
    $copy = $candidate
    if ((Get-DiskImage -ImagePath $copy).Attached) { throw 'New copy was unexpectedly already attached.' }
    $null = Mount-DiskImage -ImagePath $copy -Access ReadOnly -NoDriveLetter -PassThru
    $disks = @(Get-DiskImage -ImagePath $copy | Get-Disk)
    if ($disks.Count -ne 1) { throw 'Copy did not resolve to exactly one disk.' }
    $disk = $disks[0]
    if ($disk.IsBoot -or $disk.IsSystem -or -not $disk.IsReadOnly -or $disk.Size -ne 128MB -or
        $disk.LogicalSectorSize -ne 512 -or [int]$disk.CimInstanceProperties['BusType'].Value -ne 15 -or
        $before.Number -contains $disk.Number -or $before.UniqueId -contains $disk.UniqueId) {
        throw 'Attached disk is not the new read-only synthetic copy.'
    }
    $parts = @(Get-Partition -DiskNumber $disk.Number)
    if ($parts.Count -ne 1 -or $parts[0].Offset -ne [long]$plan.offset_sectors * 512) { throw 'Unexpected fixture partition.' }
    if ([string]$parts[0].DriveLetter -notmatch '^[A-Za-z]$') {
        $letter = @('Z','Y','X','W','V','U','T','S','R','Q','P','O','N','M','L','K','J','I','H','G','F','E','D' |
            Where-Object { -not (Get-PSDrive -Name $_ -ErrorAction SilentlyContinue) -and -not [IO.Directory]::Exists($_ + ':\') })[0]
        if (-not $letter) { throw 'No free D-Z drive letter.' }
        $null = $parts[0] | Add-PartitionAccessPath -AccessPath ($letter + ':\')
    }
    $part = Get-Partition -DiskNumber $disk.Number
    if ([string]$part.DriveLetter -notmatch '^[D-Z]$') { throw 'Test drive letter was not assigned.' }
    $volume = $part | Get-Volume
    $mount = [string]$part.DriveLetter + ':\'
    $disk = Get-DiskImage -ImagePath $copy | Get-Disk
    if (-not $disk.IsReadOnly -or $volume.FileSystem -ne 'NTFS' -or $volume.FileSystemLabel -ne $plan.label -or
        [IO.File]::ReadAllText((Join-Path $mount $plan.marker)) -ne $plan.fixture_id) { throw 'Read-only fixture marker or identity differs.' }
    $source = @{ kind='windows_volume'; mount=$mount; path=('\\.\' + $mount.Substring(0,2))
        guid=[string]$volume.UniqueId; label=[string]$volume.FileSystemLabel; size=[long]$volume.Size
        disk_numbers=@([int]$disk.Number); disk_ids=@([string]$disk.UniqueId); sector_size=512; system=$false }
    [IO.File]::WriteAllText((Join-Path $material 'mount.json'),
        (@{ schema_version=1; read_only=$true; copy=$copy; source=$source } | ConvertTo-Json -Depth 8), $utf8)
    $env:QT_QPA_PLATFORM = 'windows'
    Invoke-CheckedPython 'mounted' @('-X','utf8','-B',$helper,'mounted','--output',$material,'--tsk-bin',$TskBin)
    $checked = $true
} catch {
    $workflow.error = $_.Exception.Message
} finally {
    if ($copy) {
        try {
            if ((Get-DiskImage -ImagePath $copy).Attached) { $null = Dismount-DiskImage -ImagePath $copy }
            $detached = -not (Get-DiskImage -ImagePath $copy).Attached
        } catch { $workflow.detach_error = $_.Exception.Message }
    }
    $after = @(Disk-Inventory)
    $partsAfter = @(Partition-Inventory)
    Save-Result 'disks-after.json' $after
    Save-Result 'partitions-after.json' $partsAfter
    $workflow.detached = $detached
    $workflow.disk_inventory_unchanged = ($before | ConvertTo-Json -Depth 5 -Compress) -ceq ($after | ConvertTo-Json -Depth 5 -Compress)
    $workflow.partition_inventory_unchanged = ($partsBefore | ConvertTo-Json -Depth 5 -Compress) -ceq ($partsAfter | ConvertTo-Json -Depth 5 -Compress)
    if ($plan) {
        $workflow.source_checks = @($plan.source_image, $plan.original_vhd, $plan.copy | ForEach-Object {
            $entry = $_
            try { @{ path=$entry.path; unchanged=((Get-FileHash -LiteralPath $entry.path -Algorithm SHA256).Hash -eq $entry.sha256) } }
            catch { @{ path=$entry.path; unchanged=$false; error=$_.Exception.Message } }
        })
    }
    Save-Result 'workflow.json' $workflow
}
if ($checked -and $detached -and $workflow.disk_inventory_unchanged -and $workflow.partition_inventory_unchanged -and
    @($workflow.source_checks | Where-Object { -not $_.unchanged }).Count -eq 0) {
    try {
        Invoke-CheckedPython 'detached' @('-X','utf8','-B',$helper,'detached','--output',$material,'--tsk-bin',$TskBin)
        $workflow.status = 'passed'; $workflow.exit_code = 0
    } catch { $workflow.error = $_.Exception.Message }
}
Save-Result 'workflow.json' $workflow
Write-Host "Direct-volume validation evidence: $run"
exit $workflow.exit_code
