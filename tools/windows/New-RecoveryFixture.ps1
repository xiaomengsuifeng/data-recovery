#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
Creates synthetic NTFS deletion fixtures inside a NEW, isolated fixed VHD.
.DESCRIPTION
Windows 11 only. No existing disk/volume/VHD can be supplied as a target.
The output parent must exist; this script always creates a unique child directory.
The only formatted disk is the newly created VHD, resolved via Get-DiskImage.
Recycle-bin APIs are always scoped to that verified volume; never all drives.
Exercised on Windows 11. Read docs/windows-testing.md for partial acceptance results.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $OutputParent,
    [ValidateSet('basic', 'expanded')]
    [string] $Profile = 'basic',
    [ValidateRange(0, 64)]
    [int] $WritePressureMiB = 0
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'Run this script on Windows 11, not on the Mac development machine.'
}
if (-not [Environment]::Is64BitProcess) { throw 'Use 64-bit Windows PowerShell.' }
if ([Threading.Thread]::CurrentThread.ApartmentState -ne 'STA') {
    throw 'Start Windows PowerShell with -STA.'
}

# Refuse UNC/device paths, command injection and redirection through reparse points.
$fixtureParent = (Resolve-Path -LiteralPath $OutputParent).ProviderPath
if ($fixtureParent -notmatch '^[A-Za-z]:\\' -or $fixtureParent -match '["\r\n]') {
    throw 'OutputParent must be an existing local drive directory with no quote/newline.'
}
$ancestor = Get-Item -LiteralPath $fixtureParent -Force
if (-not $ancestor.PSIsContainer) { throw 'OutputParent must be a directory.' }
while ($null -ne $ancestor) {
    if ($ancestor.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'OutputParent and its ancestors must not be reparse points.'
    }
    $ancestor = $ancestor.Parent
}

Import-Module Storage -ErrorAction Stop
foreach ($command in @('Get-DiskImage', 'Mount-DiskImage', 'Dismount-DiskImage',
        'Get-Disk', 'Get-Partition', 'Get-Volume', 'Add-PartitionAccessPath')) {
    $null = Get-Command $command -ErrorAction Stop
}
$fixtureId = [Guid]::NewGuid().ToString('N')
$fixtureWork = Join-Path $fixtureParent ('ntfs-fixture-' + $fixtureId)
$null = New-Item -ItemType Directory -Path $fixtureWork -ErrorAction Stop
$fixtureVhd = Join-Path $fixtureWork 'fixture.vhd'
$fixtureLabel = 'RECOV_' + $fixtureId.Substring(0, 12)
$fixtureMarker = '.recovery-fixture-' + $fixtureId
$fixtureBytes = [long](128 * 1MB)
$fixtureDiskNumber = $null
$fixtureDiskId = $null
$fixtureRoot = $null
$fixtureOffset = $null
$fixtureLetter = $null
$fixtureCreated = $false
$fixtureLastDiskpart = [DateTime]::MinValue
$fixtureSnapshots = New-Object 'System.Collections.Generic.List[object]'
$fixtureEntries = New-Object 'System.Collections.Generic.List[object]'
$fixtureExistingDisks = @(Get-Disk | ForEach-Object { [int]$_.Number })
$fixtureUtf8 = New-Object Text.UTF8Encoding($false)

function Write-FixtureJson([string] $Path, $Value) {
    [IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 12), $fixtureUtf8)
}
function Write-FixtureEvent([string] $Action, $Data) {
    $line = [ordered]@{ utc = [DateTime]::UtcNow.ToString('o'); action = $Action; data = $Data }
    [IO.File]::AppendAllText((Join-Path $fixtureWork 'events.jsonl'),
        (($line | ConvertTo-Json -Compress -Depth 10) + "`n"), $fixtureUtf8)
    Write-Host $Action
}
function Assert-OwnedVhd {
    if (-not $fixtureCreated -or -not [IO.File]::Exists($fixtureVhd)) {
        throw 'The script has not created this VHD.'
    }
    $item = Get-Item -LiteralPath $fixtureVhd -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'VHD is a reparse point.' }
    if ($item.DirectoryName -ne $fixtureWork) { throw 'VHD is outside the unique work directory.' }
}
function Write-FixtureDiskpartScript([string] $Path, [string[]] $Lines) {
    # DiskPart /s ignores UTF-16 scripts on the tested Windows 11 build even
    # when its exit code is zero. Use the Windows ANSI code page without a BOM.
    # Reject unrepresentable paths instead of silently replacing characters.
    $encoding = [Text.Encoding]::GetEncoding([Text.Encoding]::Default.CodePage,
        [Text.EncoderFallback]::ExceptionFallback, [Text.DecoderFallback]::ExceptionFallback)
    $text = (($Lines + 'exit') -join "`r`n") + "`r`n"
    try { $bytes = $encoding.GetBytes($text) }
    catch [Text.EncoderFallbackException] {
        throw 'DiskPart path cannot be encoded in the Windows ANSI code page. Use an ASCII output parent.'
    }
    $stream = [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $stream.Write($bytes, 0, $bytes.Length) }
    finally { $stream.Dispose() }
}
function Invoke-FixtureDiskpart([string] $Name, [string[]] $Lines) {
    # Microsoft requires at least 15 seconds between successive diskpart scripts.
    $remaining = 15 - ([DateTime]::UtcNow - $script:fixtureLastDiskpart).TotalSeconds
    if ($remaining -gt 0) { Start-Sleep -Milliseconds ([int][Math]::Ceiling($remaining * 1000)) }
    if ($Name -eq 'format') { $null = Get-OwnedDisk }
    $scriptPath = Join-Path $fixtureWork ($Name + '.diskpart.txt')
    Write-FixtureDiskpartScript $scriptPath $Lines
    $diskpartOutput = & "$env:SystemRoot\System32\diskpart.exe" /s $scriptPath 2>&1
    $diskpartExit = $LASTEXITCODE
    $script:fixtureLastDiskpart = [DateTime]::UtcNow
    [IO.File]::WriteAllLines((Join-Path $fixtureWork ($Name + '.diskpart.log')),
        [string[]]$diskpartOutput, $fixtureUtf8)
    if ($diskpartExit -ne 0) { throw "diskpart $Name failed (exit $diskpartExit). See its log." }
}
function Get-OwnedDisk {
    Assert-OwnedVhd
    $image = Get-DiskImage -ImagePath $fixtureVhd -ErrorAction Stop
    if (-not $image.Attached) { throw 'Owned VHD is not attached.' }
    $disks = @($image | Get-Disk -ErrorAction Stop)
    if ($disks.Count -ne 1) { throw 'Owned VHD did not resolve to exactly one disk.' }
    $disk = $disks[0]
    # Get-Disk exposes BusType as a display string. Read the underlying CIM
    # UInt16 so the file-backed virtual-disk guard does not depend on labels.
    if ($disk.IsBoot -or $disk.IsSystem -or [long]$disk.Size -ne $fixtureBytes -or
        [int]$disk.LogicalSectorSize -ne 512 -or [int]$disk.CimInstanceProperties['BusType'].Value -ne 15) {
        throw 'Attached disk is not the expected 128 MiB, 512-sector file-backed virtual disk.'
    }
    if ($null -ne $fixtureDiskNumber -and [int]$disk.Number -ne $fixtureDiskNumber) {
        throw 'Virtual disk number changed unexpectedly.'
    }
    if ($null -ne $fixtureDiskId -and [string]$disk.UniqueId -ne $fixtureDiskId) {
        throw 'Virtual disk identity changed unexpectedly.'
    }
    return $disk
}
function Assert-OwnedVolume([switch] $RequireMarker) {
    $disk = Get-OwnedDisk
    $parts = @(Get-Partition -DiskNumber $disk.Number -ErrorAction Stop)
    if ($parts.Count -ne 1) { throw 'Expected exactly one partition on the owned VHD.' }
    $part = $parts[0]
    if ([string]$part.DriveLetter -ne $fixtureLetter -or
        [long]$part.Offset -ne [long]$fixtureOffset) { throw 'Test partition/drive mapping changed.' }
    $volume = $part | Get-Volume -ErrorAction Stop
    if ($volume.FileSystem -ne 'NTFS' -or $volume.FileSystemLabel -ne $fixtureLabel) {
        throw 'Test volume filesystem or label does not match.'
    }
    $viaLetter = @(Get-Partition -DriveLetter $fixtureLetter -ErrorAction Stop)
    if ($viaLetter.Count -ne 1 -or $viaLetter[0].DiskNumber -ne $disk.Number) {
        throw 'Drive letter does not map back to the owned VHD.'
    }
    if ($RequireMarker) {
        $markerPath = Join-Path $fixtureRoot $fixtureMarker
        if (-not [IO.File]::Exists($markerPath) -or [IO.File]::ReadAllText($markerPath) -ne $fixtureId) {
            throw 'Owned volume marker missing or changed.'
        }
    }
}
function Assert-SamplePath([string] $Path) {
    Assert-OwnedVolume -RequireMarker
    $prefix = Join-Path $fixtureRoot 'Samples\'
    $full = [IO.Path]::GetFullPath($Path)
    if (-not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Operation is outside the synthetic Samples directory.'
    }
    $node = Get-Item -LiteralPath $full -Force
    while ($node.FullName -ne $fixtureRoot.TrimEnd('\')) {
        if ($node.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Synthetic sample path contains a reparse point.'
        }
        # Parent/Directory return plain DirectoryInfo objects without the
        # PSIsContainer note property added by Get-Item.
        if ($node -is [IO.DirectoryInfo]) { $node = $node.Parent } else { $node = $node.Directory }
        if ($null -eq $node) { break }
    }
}
function Write-Manifest([string] $Name, [object[]] $Entries, [string] $Stage) {
    Write-FixtureJson (Join-Path $fixtureWork ($Name + '.manifest.json')) ([ordered]@{
        schema_version = 1; fixture_id = $fixtureId; stage = $Stage
        sector_size = 512; offset_sectors = [long]($fixtureOffset / 512)
        files = @($Entries)
    })
}

# P/Invoke types use explicit Unicode paths and a double-NUL source buffer.
# EmptyOnly() rejects empty/rootless strings even if a caller bypasses PowerShell guards.
Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class RecoveryFixtureNative {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct FileOperation {
        public IntPtr hwnd; public uint wFunc; public IntPtr pFrom; public IntPtr pTo;
        public ushort fFlags;
        [MarshalAs(UnmanagedType.Bool)] public bool fAnyOperationsAborted;
        public IntPtr hNameMappings; public IntPtr lpszProgressTitle;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct BinInfo { public uint cbSize; public long bytes; public long items; }
    [DllImport("shell32.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
    private static extern int SHFileOperationW(ref FileOperation op);
    [DllImport("shell32.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
    private static extern int SHEmptyRecycleBinW(IntPtr hwnd, string root, uint flags);
    [DllImport("shell32.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
    private static extern int SHQueryRecycleBinW(string root, ref BinInfo info);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
    private static extern SafeFileHandle CreateFileW(string path, uint access, uint share,
        IntPtr security, uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool FlushFileBuffers(SafeFileHandle handle);
    private static void CheckRoot(string root) {
        if (String.IsNullOrEmpty(root) || root.Length != 3 || root[1] != ':' || root[2] != '\\'
            || root[0] < 'D' || root[0] > 'Z') throw new ArgumentException("Explicit D:\\-Z:\\ root required");
    }
    public static long Count(string root) {
        CheckRoot(root);
        BinInfo info = new BinInfo(); info.cbSize = (uint)Marshal.SizeOf(typeof(BinInfo));
        int hr = SHQueryRecycleBinW(root, ref info);
        if (hr != 0) throw new COMException("Cannot query test-volume Recycle Bin", hr);
        return info.items;
    }
    public static void Recycle(string root, string path) {
        CheckRoot(root);
        if (!Path.GetFullPath(path).StartsWith(root + "Samples\\", StringComparison.OrdinalIgnoreCase)
            || path.IndexOfAny(new char[] {'*', '?', '\0'}) >= 0)
            throw new ArgumentException("Synthetic absolute sample path required");
        IntPtr source = Marshal.StringToHGlobalUni(path + "\0\0");
        try {
            FileOperation op = new FileOperation(); op.wFunc = 3; op.pFrom = source;
            op.fFlags = 0x0040 | 0x0010 | 0x0004 | 0x0400; // ALLOWUNDO, NOCONFIRMATION, SILENT, NOERRORUI
            int result = SHFileOperationW(ref op);
            if (result != 0 || op.fAnyOperationsAborted)
                throw new IOException("Shell recycle failed/aborted; code " + result);
        } finally { Marshal.FreeHGlobal(source); }
    }
    public static void EmptyOnly(string root) {
        CheckRoot(root);
        int hr = SHEmptyRecycleBinW(IntPtr.Zero, root, 0x1 | 0x2 | 0x4);
        if (hr != 0) throw new COMException("Cannot empty test-volume Recycle Bin", hr);
    }
    public static void FlushVolume(string root) {
        CheckRoot(root);
        using (SafeFileHandle h = CreateFileW("\\\\.\\" + root.Substring(0, 2),
            0xC0000000, 3, IntPtr.Zero, 3, 0, IntPtr.Zero)) {
            if (h.IsInvalid || !FlushFileBuffers(h)) throw new Win32Exception(Marshal.GetLastWin32Error());
        }
    }
    private static ulong BE(byte[] data, int start, int count) {
        ulong v = 0; for (int i=0; i<count; i++) v = (v << 8) | data[start+i]; return v;
    }
    public static void ExportFixedVhd(string input, string output, long expectedSize) {
        using (FileStream src = new FileStream(input, FileMode.Open, FileAccess.Read, FileShare.None)) {
            if (src.Length != expectedSize + 512) throw new IOException("Unexpected fixed VHD length");
            byte[] footer = new byte[512]; src.Position = src.Length - 512;
            int n = 0; while (n < footer.Length) {
                int r = src.Read(footer, n, footer.Length-n); if (r == 0) throw new EndOfStreamException(); n += r;
            }
            if (System.Text.Encoding.ASCII.GetString(footer,0,8) != "conectix" ||
                BE(footer,60,4) != 2 || BE(footer,48,8) != (ulong)expectedSize ||
                BE(footer,12,4) != 0x00010000 || BE(footer,16,8) != UInt64.MaxValue)
                throw new IOException("Not the expected fixed VHD: refuse raw conversion");
            uint sum=0; for(int i=0;i<512;i++) if(i<64 || i>67) sum += footer[i];
            if (BE(footer,64,4) != (uint)~sum) throw new IOException("VHD footer checksum mismatch");
            // An empty output validates the newly-created VHD without writing a copy.
            if (String.IsNullOrEmpty(output)) return;
            src.Position=0;
            using (FileStream dst = new FileStream(output, FileMode.CreateNew, FileAccess.Write, FileShare.None)) {
                byte[] buffer=new byte[1024*1024]; long remaining=expectedSize;
                while(remaining>0) {
                    int r=src.Read(buffer,0,(int)Math.Min(buffer.Length,remaining));
                    if(r==0) throw new EndOfStreamException(); dst.Write(buffer,0,r); remaining-=r;
                }
                dst.Flush(true);
            }
        }
    }
}
'@

function Mount-OwnedFixture {
    Assert-OwnedVhd
    $null = Mount-DiskImage -ImagePath $fixtureVhd -Access ReadWrite -PassThru -ErrorAction Stop
    # Disk numbers may change after a deliberate detach. Resolve only from our image path.
    $script:fixtureDiskNumber = $null
    $disk = Get-OwnedDisk
    $script:fixtureDiskNumber = [int]$disk.Number
    $parts = @(Get-Partition -DiskNumber $disk.Number)
    if ($parts.Count -ne 1) { throw 'Unexpected partition count after reattach.' }
    # Storage can return a NUL Char for an unassigned letter; its string is
    # nonempty. Check for an actual drive letter before skipping assignment.
    if ([string]$parts[0].DriveLetter -notmatch '^[A-Za-z]$') {
        if (Get-PSDrive -Name $fixtureLetter -ErrorAction SilentlyContinue) { throw 'Test drive letter was taken.' }
        $null = $parts[0] | Add-PartitionAccessPath -AccessPath $fixtureRoot -ErrorAction Stop
    }
    Assert-OwnedVolume -RequireMarker
}
function Export-Stage([string] $Name, [object[]] $DeletedEntries) {
    Assert-OwnedVolume -RequireMarker
    [RecoveryFixtureNative]::FlushVolume($fixtureRoot)
    $null = Dismount-DiskImage -ImagePath $fixtureVhd -ErrorAction Stop
    if ((Get-DiskImage -ImagePath $fixtureVhd).Attached) { throw 'VHD stayed attached; no snapshot taken.' }
    $raw = Join-Path $fixtureWork ($Name + '.img')
    [RecoveryFixtureNative]::ExportFixedVhd($fixtureVhd, $raw, $fixtureBytes)
    $snapshot = [ordered]@{
        stage = $Name; image = $Name + '.img'; bytes = $fixtureBytes
        sha256 = (Get-FileHash -LiteralPath $raw -Algorithm SHA256).Hash.ToLowerInvariant()
        sector_size = 512; offset_sectors = [long]($fixtureOffset / 512)
        manifest = $Name + '.manifest.json'; utc = [DateTime]::UtcNow.ToString('o')
        deleted_target_count = @($DeletedEntries).Count
    }
    Write-Manifest $Name $DeletedEntries $Name
    $fixtureSnapshots.Add($snapshot)
    Write-FixtureJson (Join-Path $fixtureWork 'stages.json') @($fixtureSnapshots.ToArray())
    Write-FixtureEvent 'snapshot-exported' $snapshot
}
function Add-SyntheticSample([string] $Relative, [string] $Scenario, [string] $Kind, [int] $Seed) {
    Assert-OwnedVolume -RequireMarker
    $path = Join-Path $fixtureRoot $Relative
    $null = [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($path))
    if ($Kind -eq 'png') {
        Add-Type -AssemblyName System.Drawing
        $bitmap = New-Object Drawing.Bitmap(160, 100)
        try {
            for ($y = 0; $y -lt 100; $y++) {
                for ($x = 0; $x -lt 160; $x++) {
                    $bitmap.SetPixel($x, $y, [Drawing.Color]::FromArgb(
                        (($x * 3 + $Seed) % 256), (($y * 5 + $Seed * 7) % 256), (($x + $y + $Seed * 11) % 256)))
                }
            }
            $bitmap.Save($path, [Drawing.Imaging.ImageFormat]::Png)
        } finally { $bitmap.Dispose() }
    } else {
        $text = "Synthetic recovery fixture $fixtureId / $Scenario / $Seed`r`n"
        if ($Kind -eq 'large-text') { $text = $text * 200 }
        [IO.File]::WriteAllText($path, $text, $fixtureUtf8)
    }
    $original = Join-Path (Join-Path $fixtureWork 'originals') $Relative
    $null = [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($original))
    [IO.File]::Copy($path, $original, $false)
    $fixtureEntries.Add([pscustomobject][ordered]@{
        original_path = $path; relative_path = $Relative.Replace('\', '/')
        size = [long](Get-Item -LiteralPath $path).Length
        sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        scenario = $Scenario; synthetic = $true
    })
}
function Assert-ExactSample($Entry) {
    Assert-SamplePath $Entry.original_path
    if ((Get-Item -LiteralPath $Entry.original_path).Length -ne $Entry.size -or
        (Get-FileHash -LiteralPath $Entry.original_path -Algorithm SHA256).Hash -ne $Entry.sha256) {
        throw 'Synthetic sample changed; deletion refused.'
    }
}
function Write-FixtureRandomFile([string] $Path, [int] $Size, [switch] $AsciiText) {
    if ($Size -lt 0 -or $Size -gt 1MB) { throw 'Random sample size is outside the supported range.' }
    $bytes = New-Object byte[] $Size
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $random.GetBytes($bytes) } finally { $random.Dispose() }
    if ($AsciiText) {
        for ($i = 0; $i -lt $bytes.Length; $i++) { $bytes[$i] = 65 + ($bytes[$i] % 26) }
    }
    $stream = [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) }
    finally { $stream.Dispose() }
}
function Add-RandomSample([string] $Relative, [string] $Scenario, [int] $Size, [string] $Edit = 'none') {
    Assert-OwnedVolume -RequireMarker
    if ($Relative -notmatch '^Samples\\' -or $Relative.Contains('..') -or $Relative.Contains(':')) {
        throw 'Random sample must be under the synthetic Samples directory.'
    }
    $path = Join-Path $fixtureRoot $Relative
    $null = [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($path))
    $initialSize = if ($Edit -eq 'shrink') { $Size + 101 } else { $Size }
    Write-FixtureRandomFile $path $initialSize -AsciiText:($path.EndsWith('.txt'))
    if ($Edit -ne 'none') {
        Assert-SamplePath $path
        [RecoveryFixtureNative]::FlushVolume($fixtureRoot)
        if ($Edit -eq 'shrink') {
            $stream = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::None)
            try { $stream.SetLength($Size); $stream.Flush($true) } finally { $stream.Dispose() }
        } elseif ($Edit -eq 'rewrite') {
            $replacement = New-Object byte[] $Size
            $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
            try { $rng.GetBytes($replacement) } finally { $rng.Dispose() }
            for ($i = 0; $i -lt $replacement.Length; $i++) { $replacement[$i] = 97 + ($replacement[$i] % 26) }
            $stream = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::None)
            try { $stream.Write($replacement, 0, $replacement.Length); $stream.Flush($true) } finally { $stream.Dispose() }
        } elseif ($Edit -eq 'rename') {
            $renamed = Join-Path ([IO.Path]::GetDirectoryName($path)) ('renamed-' + ([string][char]0x6587 * 48) + '-' + [IO.Path]::GetFileName($path))
            $renamed = [IO.Path]::GetFullPath($renamed)
            if (-not $renamed.StartsWith((Join-Path $fixtureRoot 'Samples\'), [StringComparison]::OrdinalIgnoreCase) -or
                $renamed.Length -gt 240 -or [IO.File]::Exists($renamed)) { throw 'Invalid synthetic rename destination.' }
            [IO.File]::Move($path, $renamed)
            $path = $renamed
            $Relative = $path.Substring($fixtureRoot.Length)
        } else { throw 'Unsupported synthetic file edit.' }
        Write-FixtureEvent 'synthetic-file-edited' @{ path = $path; edit = $Edit; final_size = $Size }
    }
    $original = Join-Path (Join-Path $fixtureWork 'originals') $Relative
    $null = [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($original))
    [IO.File]::Copy($path, $original, $false)
    $fixtureEntries.Add([pscustomobject][ordered]@{
        original_path = $path; relative_path = $Relative.Replace('\', '/')
        size = [long](Get-Item -LiteralPath $path).Length
        sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        scenario = $Scenario; synthetic = $true; content_pattern = 'cryptographic-random'; edit = $Edit
    })
}
function Add-WritePressure {
    Assert-OwnedVolume -RequireMarker
    $folder = Join-Path $fixtureRoot 'Samples\additional-writes'
    if (Test-Path -LiteralPath $folder) { throw 'Write pressure requires a new synthetic directory.' }
    $null = [IO.Directory]::CreateDirectory($folder)
    Assert-SamplePath $folder
    $files = New-Object 'System.Collections.Generic.List[object]'
    # Create small records as well as bulk data. Their bytes and allocation
    # outcomes are evidence, not a promise of which deleted clusters are reused.
    for ($i = 0; $i -lt 64 + $WritePressureMiB; $i++) {
        Assert-OwnedVolume -RequireMarker
        $size = if ($i -lt 64) { 257 } else { 1MB }
        $path = Join-Path $folder ('write-{0:D3}.bin' -f $i)
        Write-FixtureRandomFile $path $size
        $files.Add(@{ relative_path = $path.Substring($fixtureRoot.Length).Replace('\', '/'); size = $size
            sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() })
    }
    Write-FixtureJson (Join-Path $fixtureWork 'write-pressure.json') @{
        schema_version = 1; fixture_id = $fixtureId; stage = 'after-additional-writes'
        bulk_mib = $WritePressureMiB; small_file_count = 64; files = @($files.ToArray())
    }
    Write-FixtureEvent 'additional-writes-complete' @{ bulk_mib = $WritePressureMiB; small_files = 64 }
}
function Get-RecycleEvidence([object[]] $Expected) {
    Assert-OwnedVolume -RequireMarker
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $binPath = Join-Path (Join-Path $fixtureRoot '$Recycle.Bin') $sid
    $indexes = @(Get-ChildItem -LiteralPath $binPath -Force -File -ErrorAction Stop |
        Where-Object { $_.Name.StartsWith('$I', [StringComparison]::Ordinal) })
    if ($indexes.Count -ne $Expected.Count -or [RecoveryFixtureNative]::Count($fixtureRoot) -ne $Expected.Count) {
        throw 'Recycle Bin count does not match only the synthetic test files.'
    }
    $seen = @{}
    $evidence = New-Object 'System.Collections.Generic.List[object]'
    foreach ($index in $indexes) {
        $bytes = [IO.File]::ReadAllBytes($index.FullName)
        if ($bytes.Length -lt 26) { throw 'Truncated $I record.' }
        $version = [BitConverter]::ToInt64($bytes, 0)
        $length = [BitConverter]::ToInt64($bytes, 8)
        if ($version -eq 2) {
            if ($bytes.Length -lt 28) { throw 'Truncated version-2 $I record.' }
            $chars = [BitConverter]::ToUInt32($bytes, 24)
            if ($chars -lt 1 -or $chars -gt 32768 -or 28 + 2 * [long]$chars -gt $bytes.Length) {
                throw 'Invalid version-2 $I path length.'
            }
            $originalPath = [Text.Encoding]::Unicode.GetString($bytes, 28, [int]($chars * 2)).TrimEnd([char]0)
        } elseif ($version -eq 1) {
            $originalPath = [Text.Encoding]::Unicode.GetString($bytes, 24, $bytes.Length - 24).TrimEnd([char]0)
        } else { throw 'Unrecognized $I format; refuse to continue.' }
        $match = @($Expected | Where-Object { $_.original_path -ceq $originalPath })
        if ($match.Count -ne 1 -or $seen.ContainsKey($originalPath) -or $length -ne $match[0].size) {
            throw 'Recycle index does not belong uniquely to an expected synthetic sample.'
        }
        $content = Join-Path $binPath ('$R' + $index.Name.Substring(2))
        if (-not [IO.File]::Exists($content) -or
            (Get-FileHash -LiteralPath $content -Algorithm SHA256).Hash -ne $match[0].sha256) {
            throw 'Recycled content does not match its known original; no empty operation permitted.'
        }
        $seen[$originalPath] = $true
        $evidence.Add([ordered]@{
            original_path = $originalPath; index_path = $index.FullName; content_path = $content
            index_version = $version; size = $length; sha256 = $match[0].sha256
        })
    }
    # The drive-scoped empty operation must not encounter unaccounted-for bin contents.
    $allowed = @{}
    foreach ($record in $evidence) {
        $allowed[$record.index_path] = $true
        $allowed[$record.content_path] = $true
    }
    $binRoot = Join-Path $fixtureRoot '$Recycle.Bin'
    foreach ($item in @(Get-ChildItem -LiteralPath $binRoot -Force -Recurse -ErrorAction Stop)) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Unexpected reparse point in test bin.' }
        if ($item.PSIsContainer) {
            if ($item.FullName -ne $binPath) { throw 'Unexpected directory/user in test-volume bin.' }
        } elseif (-not $allowed.ContainsKey($item.FullName)) {
            if ($item.Name -ne 'desktop.ini' -or $item.Length -gt 4096) {
                throw 'Unaccounted-for file in test-volume bin; no empty operation permitted.'
            }
        }
    }
    return $evidence.ToArray()
}

try {
    Write-FixtureJson (Join-Path $fixtureWork 'environment.json') ([ordered]@{
        fixture_id = $fixtureId; created_utc = [DateTime]::UtcNow.ToString('o')
        os = [Environment]::OSVersion.VersionString; powershell = $PSVersionTable.PSVersion.ToString()
        process_architecture = $env:PROCESSOR_ARCHITECTURE; vhd_bytes = $fixtureBytes
        diskpart_code_page = [Text.Encoding]::Default.CodePage
        profile = $Profile; write_pressure_mib = $WritePressureMiB
        script_sha256 = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()
        note = 'Synthetic logical NTFS test; does not simulate physical SSD TRIM or hardware failure.'
    })
    Write-FixtureEvent 'creating-new-fixed-vhd' @{ path = $fixtureVhd; bytes = $fixtureBytes }
    if (Test-Path -LiteralPath $fixtureVhd) { throw 'Refusing an existing VHD.' }
    Invoke-FixtureDiskpart 'create' @("create vdisk file=`"$fixtureVhd`" maximum=128 type=fixed")
    if (-not [IO.File]::Exists($fixtureVhd)) {
        throw 'DiskPart returned without creating the VHD. Inspect create.diskpart.log; no disk was attached.'
    }
    $fixtureCreated = $true
    [RecoveryFixtureNative]::ExportFixedVhd($fixtureVhd, $null, $fixtureBytes)
    $null = Mount-DiskImage -ImagePath $fixtureVhd -Access ReadWrite -NoDriveLetter -PassThru
    $disk = Get-OwnedDisk
    if ($fixtureExistingDisks -contains [int]$disk.Number -or $disk.PartitionStyle -ne 'RAW' -or
        @(Get-Partition -DiskNumber $disk.Number -ErrorAction SilentlyContinue).Count -ne 0) {
        throw 'Only a newly appeared, uninitialized, empty VHD may be formatted.'
    }
    $fixtureDiskNumber = [int]$disk.Number
    $lettersInUse = @([IO.Directory]::GetLogicalDrives() | ForEach-Object { $_.Substring(0, 1) })
    $fixtureLetter = @('Z','Y','X','W','V','U','T','S','R','Q','P','O','N','M','L','K','J','I','H','G','F','E','D' |
        Where-Object { $lettersInUse -notcontains $_ -and -not (Get-PSDrive -Name $_ -ErrorAction SilentlyContinue) })[0]
    if (-not $fixtureLetter) { throw 'No unused D-Z drive letter is available.' }
    $fixtureRoot = $fixtureLetter + ':\'
    # No clean, noerr, existing-disk parameter, or fallback formatting path exists.
    # The disk number below was resolved from only our freshly-created VHD and rechecked above execution.
    Invoke-FixtureDiskpart 'format' @(
        "select vdisk file=`"$fixtureVhd`"", "select disk $fixtureDiskNumber", 'convert mbr',
        'create partition primary', "format fs=ntfs quick label=`"$fixtureLabel`"",
        "assign letter=$fixtureLetter"
    )
    $disk = Get-OwnedDisk
    $fixtureDiskId = [string]$disk.UniqueId
    $parts = @(Get-Partition -DiskNumber $disk.Number)
    if ($parts.Count -ne 1 -or $parts[0].Offset % 512 -ne 0) { throw 'Unexpected partition geometry.' }
    $fixtureOffset = [long]$parts[0].Offset
    Assert-OwnedVolume
    [IO.File]::WriteAllText((Join-Path $fixtureRoot $fixtureMarker), $fixtureId, $fixtureUtf8)
    Write-FixtureEvent 'owned-volume-verified' @{
        drive = $fixtureRoot; disk = $fixtureDiskNumber; label = $fixtureLabel
        offset_sectors = [long]($fixtureOffset / 512)
    }
    $initialBin = Join-Path $fixtureRoot '$Recycle.Bin'
    if ((Test-Path -LiteralPath $initialBin) -and [RecoveryFixtureNative]::Count($fixtureRoot) -ne 0) {
        throw 'New test volume has unexpected recycled items.'
    }
    # Unicode constructed from codepoints so Windows PowerShell 5.1 can read
    # this ASCII script. Exercise Unicode in direct deletion as well as $I.
    $unicodeName = [string][char]0x4E2D + [char]0x6587 + '.txt'
    Add-SyntheticSample ('Samples\direct\' + $unicodeName) 'direct-file' 'text' 1
    Add-SyntheticSample 'Samples\direct\large-note.txt' 'direct-file' 'large-text' 2
    Add-SyntheticSample 'Samples\deleted-folder\nested\report.txt' 'direct-directory' 'large-text' 3
    Add-SyntheticSample 'Samples\deleted-folder\photo.png' 'direct-directory' 'png' 4
    Add-SyntheticSample ('Samples\recycle-a\' + $unicodeName) 'recycle-bin' 'large-text' 5
    Add-SyntheticSample 'Samples\recycle-a\same-name.txt' 'recycle-bin' 'text' 6
    Add-SyntheticSample 'Samples\recycle-b\same-name.txt' 'recycle-bin' 'large-text' 7
    Add-SyntheticSample 'Samples\recycle-b\photo.png' 'recycle-bin' 'png' 8
    Add-SyntheticSample 'Samples\retained\control.txt' 'retained-control' 'large-text' 9
    if ($Profile -eq 'expanded') {
        $sizes = @(0, 1, 63, 127, 255, 383, 511, 639, 767, 1023, 2049, 4095, 4096, 4097, 32769)
        foreach ($size in $sizes) {
            Add-RandomSample ('Samples\direct\random-{0:D5}.txt' -f $size) 'direct-file' $size
        }
        foreach ($size in @(63, 255, 639, 4097)) {
            Add-RandomSample ('Samples\recycle-c\random-{0:D5}.bin' -f $size) 'recycle-bin' $size
        }
        foreach ($edit in @('rewrite', 'shrink', 'rename')) {
            Add-RandomSample ('Samples\direct\' + $edit + '-79.txt') 'direct-file' 79 $edit
            Add-RandomSample ('Samples\direct\' + $edit + '-511.txt') 'direct-file' 511 $edit
        }
        $longPart = ([string][char]0x4E2D + [char]0x6587) * 12
        Add-RandomSample ('Samples\direct\' + $longPart + '\' + $longPart + '\long-name-' + $longPart + '.txt') 'direct-file' 255
        Add-RandomSample 'Samples\retained\random-control.bin' 'retained-control' 4097
    }
    Write-FixtureJson (Join-Path $fixtureWork 'originals.manifest.json') @{
        schema_version = 1; fixture_id = $fixtureId; files = @($fixtureEntries.ToArray())
    }
    $direct = @($fixtureEntries | Where-Object { $_.scenario -like 'direct-*' })
    $recycled = @($fixtureEntries | Where-Object { $_.scenario -eq 'recycle-bin' })
    Export-Stage 'before-delete' @()
    Mount-OwnedFixture

    foreach ($entry in $direct) { Assert-ExactSample $entry }
    foreach ($entry in @($direct | Where-Object { $_.scenario -eq 'direct-file' })) {
        Assert-SamplePath $entry.original_path
        [IO.File]::Delete($entry.original_path) # Bypasses Recycle Bin, like Shift+Delete.
    }
    $folder = Join-Path $fixtureRoot 'Samples\deleted-folder'
    Assert-SamplePath $folder
    $tree = @(Get-ChildItem -LiteralPath $folder -Recurse -Force)
    if (@($tree | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }).Count -ne 0 -or
        @($tree | Where-Object { -not $_.PSIsContainer }).Count -ne 2) { throw 'Synthetic directory contents changed.' }
    [IO.Directory]::Delete($folder, $true)
    foreach ($entry in $direct) {
        if (Test-Path -LiteralPath $entry.original_path) { throw 'Direct delete did not complete.' }
    }
    Write-FixtureEvent 'direct-delete-complete' @{ targets = $direct.Count; method = 'System.IO delete, bypasses Recycle Bin' }
    Export-Stage 'after-direct-delete' $direct
    Mount-OwnedFixture

    foreach ($entry in $recycled) {
        Assert-ExactSample $entry
        [RecoveryFixtureNative]::Recycle($fixtureRoot, $entry.original_path)
        if (Test-Path -LiteralPath $entry.original_path) { throw 'Shell recycle left the original path present.' }
    }
    $evidence = @(Get-RecycleEvidence $recycled)
    Write-FixtureJson (Join-Path $fixtureWork 'recycle-evidence.json') $evidence
    Write-FixtureEvent 'recycle-content-and-index-verified' @{ targets = $recycled.Count }
    # Only direct-deleted files are targets here; recycled content is still allocated.
    Export-Stage 'after-move-to-recycle-bin' $direct
    Mount-OwnedFixture

    $null = Get-RecycleEvidence $recycled
    Assert-OwnedVolume -RequireMarker
    [RecoveryFixtureNative]::EmptyOnly($fixtureRoot)
    if ([RecoveryFixtureNative]::Count($fixtureRoot) -ne 0) { throw 'Test-volume Recycle Bin is not empty.' }
    foreach ($record in $evidence) {
        if (Test-Path -LiteralPath $record.index_path) { throw 'A test $I record remains allocated.' }
        if (Test-Path -LiteralPath $record.content_path) { throw 'A test $R file remains allocated.' }
    }
    Write-FixtureEvent 'test-volume-only-recycle-bin-emptied' @{ root = $fixtureRoot; targets = $recycled.Count }
    Export-Stage 'after-empty-recycle-bin' @($direct + $recycled)
    if ($WritePressureMiB -gt 0) {
        Mount-OwnedFixture
        Add-WritePressure
        foreach ($entry in @($fixtureEntries | Where-Object { $_.scenario -eq 'retained-control' })) { Assert-ExactSample $entry }
        Export-Stage 'after-additional-writes' @($direct + $recycled)
    }
    Write-FixtureJson (Join-Path $fixtureWork 'result.json') @{
        status = 'complete'; fixture_id = $fixtureId; stages = @($fixtureSnapshots.ToArray())
        note = 'Fixture creation completed; recovery quality has not been measured.'
    }
    Write-Host "Fixture complete: $fixtureWork"
    Write-Host "Raw-image NTFS offset: $([long]($fixtureOffset / 512)) sectors (512 bytes each)."
} catch {
    Write-FixtureEvent 'failed' @{ message = $_.Exception.Message }
    Write-FixtureJson (Join-Path $fixtureWork 'result.json') @{
        status = 'failed'; fixture_id = $fixtureId; error = $_.Exception.Message
        stages = @($fixtureSnapshots.ToArray())
    }
    throw
} finally {
    # Cleanup detaches only our new VHD by its path. It never deletes the output directory.
    if ($fixtureCreated -and [IO.File]::Exists($fixtureVhd)) {
        try {
            if ((Get-DiskImage -ImagePath $fixtureVhd -ErrorAction Stop).Attached) {
                $null = Dismount-DiskImage -ImagePath $fixtureVhd -ErrorAction Stop
            }
        } catch { Write-Warning "Could not detach the test VHD. Keep $fixtureVhd and inspect Disk Management." }
    }
}
