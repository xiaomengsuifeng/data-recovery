#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
Generates a new isolated fixture, then validates every stage against originals.
.DESCRIPTION
Creates a unique child directory under OutputParent. The existing fixture
generator owns all VHD operations; this wrapper never accepts an existing disk.
An existing fixture can instead be tested without elevation using the Python
validate-fixture command. Keep all generated files for diagnosis.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $OutputParent,
    [string] $Python,
    [string] $TskBin,
    [ValidateSet('basic', 'expanded')]
    [string] $Profile = 'basic',
    [ValidateRange(0, 64)]
    [int] $WritePressureMiB = 0,
    [switch] $DeepPng,
    [switch] $DeepLog
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not [Environment]::Is64BitProcess) { throw 'Use 64-bit Windows PowerShell.' }

$bundlePython = Join-Path $PSScriptRoot '..\runtime\python.exe'
if (-not $Python) {
    if (Test-Path -LiteralPath $bundlePython -PathType Leaf) {
        $Python = $bundlePython
        if (-not $TskBin) { $TskBin = Join-Path $PSScriptRoot '..\vendor\tsk\bin' }
    } else {
        $Python = Join-Path $PSScriptRoot '..\..\.venv\Scripts\python.exe'
    }
}
$Python = (Resolve-Path -LiteralPath $Python -ErrorAction Stop).ProviderPath
$tskArguments = @()
if ($TskBin) {
    $TskBin = (Resolve-Path -LiteralPath $TskBin -ErrorAction Stop).ProviderPath
    $tskArguments = @('--tsk-bin', $TskBin)
}
$scanArguments = @()
if ($DeepPng) { $scanArguments += '--deep-png' }
if ($DeepLog) { $scanArguments += '--deep-log' }
# Fail before creating a VHD if an older package/runtime lacks the new command.
& $Python -X utf8 -B -m recovery_core validate-fixture --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'This Python runtime does not provide validate-fixture.' }
& $Python -X utf8 -B -m recovery_core doctor @tskArguments
if ($LASTEXITCODE -ne 0) { throw 'TSK startup check failed; no fixture was created.' }

$validationParent = (Resolve-Path -LiteralPath $OutputParent).ProviderPath
if ($validationParent -notmatch '^[A-Za-z]:\\' -or $validationParent -match '["\r\n]') {
    throw 'OutputParent must be an existing local directory with no quote/newline.'
}
$ancestor = Get-Item -LiteralPath $validationParent -Force
if (-not $ancestor.PSIsContainer) { throw 'OutputParent must be a directory.' }
while ($null -ne $ancestor) {
    if ($ancestor.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'OutputParent and its ancestors must not be reparse points.'
    }
    $ancestor = $ancestor.Parent
}
$validationWork = Join-Path $validationParent ('recovery-validation-' + [Guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $validationWork -ErrorAction Stop
$materialParent = Join-Path $validationWork 'fixtures'
$null = New-Item -ItemType Directory -Path $materialParent -ErrorAction Stop
$resultsPath = Join-Path $validationWork 'results'
$workflow = [ordered]@{
    schema_version = 1; status = 'failed'; fixture = $null; results = $resultsPath
    started_utc = [DateTime]::UtcNow.ToString('o'); exit_code = 2
    profile = $Profile; write_pressure_mib = $WritePressureMiB
    deep_png = $DeepPng.IsPresent; deep_log = $DeepLog.IsPresent
}

try {
    $generator = Join-Path $PSScriptRoot 'New-RecoveryFixture.ps1'
    $windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    & $windowsPowerShell -NoProfile -NonInteractive -STA -File $generator -OutputParent $materialParent -Profile $Profile -WritePressureMiB $WritePressureMiB
    if ($LASTEXITCODE -ne 0) { throw 'Fixture generation failed. Inspect the preserved fixture logs.' }
    # This parent was just created for this run; never pick a fixture from a
    # shared directory or guess a disk/volume identifier after a failure.
    $materials = @(Get-ChildItem -LiteralPath $materialParent -Directory -Force)
    if ($materials.Count -ne 1) { throw 'Expected exactly one generated fixture directory.' }
    $workflow.fixture = $materials[0].FullName
    & $Python -X utf8 -B -m recovery_core validate-fixture $workflow.fixture --output $resultsPath @tskArguments @scanArguments
    $validationExit = $LASTEXITCODE
    if ($validationExit -notin @(0, 1, 2, 130)) { throw "Unexpected validation exit: $validationExit" }
    $states = @{ 0 = 'passed'; 1 = 'incomplete'; 2 = 'failed'; 130 = 'cancelled' }
    $workflow.status = $states[$validationExit]
    $workflow.exit_code = $validationExit
} catch {
    $workflow.error = $_.Exception.Message
    Write-Warning $_.Exception.Message
} finally {
    $workflow.finished_utc = [DateTime]::UtcNow.ToString('o')
    [IO.File]::WriteAllText((Join-Path $validationWork 'workflow.json'),
        ($workflow | ConvertTo-Json -Depth 6), (New-Object Text.UTF8Encoding($false)))
    Write-Host "Validation evidence: $validationWork"
}
exit $workflow.exit_code
