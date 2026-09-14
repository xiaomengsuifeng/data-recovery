"""Exercise the fixture's script writer without executing disk or recycle APIs."""
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.name == "nt", "Windows PowerShell 5.1 script encoding")
class WindowsFixtureScriptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.generator = Path(__file__).resolve().parents[1] / "tools/windows/New-RecoveryFixture.ps1"

    def run_writer(self, body, function="Write-FixtureDiskpartScript"):
        quote = lambda text: "'" + str(text).replace("'", "''") + "'"
        program = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(GENERATOR, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Generator contains parse errors.' }
# Load only the tested function. Do not dot-source the generator.
$writer = @($ast.FindAll({ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq FUNCTION
}, $true))
if ($writer.Count -ne 1) { throw 'Expected one matching function.' }
. ([ScriptBlock]::Create($writer[0].Extent.Text))
$testRoot = ROOT
BODY
""".replace("GENERATOR", quote(self.generator)).replace("FUNCTION", quote(function)).replace("ROOT", quote(self.root)).replace("BODY", body)
        executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        encoded = base64.b64encode(program.encode("utf-16le")).decode("ascii")
        result = subprocess.run([str(executable), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                                capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        return json.loads(result.stdout.decode("utf-8-sig"))

    def test_native_script_has_no_bom_nuls_and_has_complete_lines(self):
        result = self.run_writer(r"""
$path = Join-Path $testRoot 'commands.txt'
Write-FixtureDiskpartScript $path @('list vdisk')
$bytes = [IO.File]::ReadAllBytes($path)
@{ text = [Text.Encoding]::Default.GetString($bytes); bytes = @($bytes) } | ConvertTo-Json
""")
        self.assertEqual(result["text"], "list vdisk\r\nexit\r\n")
        self.assertEqual(bytes(result["bytes"]), b"list vdisk\r\nexit\r\n")

    def test_random_samples_preserve_sizes_and_refuse_overwriting(self):
        result = self.run_writer(r"""
$sizes = @(0, 1, 511, 4097, 1MB)
foreach ($size in $sizes) { Write-FixtureRandomFile (Join-Path $testRoot "$size.bin") $size }
$path = Join-Path $testRoot 'letters.txt'
Write-FixtureRandomFile $path 255 -AsciiText
$before = [IO.File]::ReadAllText($path)
$rejected = $false
try { Write-FixtureRandomFile $path 255 } catch { $rejected = $true }
$invalid = 0
foreach ($size in @(-1, (1MB + 1))) {
    try { Write-FixtureRandomFile (Join-Path $testRoot 'invalid.bin') $size } catch { $invalid++ }
}
@{ sizes = @($sizes | ForEach-Object { (Get-Item -LiteralPath (Join-Path $testRoot "$_.bin")).Length })
    letters = $before; rejected = $rejected; unchanged = $before -ceq [IO.File]::ReadAllText($path)
    invalid = $invalid; invalid_exists = [IO.File]::Exists((Join-Path $testRoot 'invalid.bin')) } | ConvertTo-Json
""", function="Write-FixtureRandomFile")
        self.assertEqual(result["sizes"], [0, 1, 511, 4097, 1024 ** 2])
        self.assertRegex(result["letters"], r"^[A-Z]{255}$")
        self.assertTrue(result["rejected"])
        self.assertTrue(result["unchanged"])
        self.assertEqual(result["invalid"], 2)
        self.assertFalse(result["invalid_exists"])

    def test_path_round_trips_or_fails_before_writing(self):
        result = self.run_writer(r"""
$text = 'rem C:\' + [char]0x4E2D + [char]0x6587 + '\fixture.vhd'
$path = Join-Path $testRoot 'unicode.txt'
try {
    Write-FixtureDiskpartScript $path @($text)
    $result = @{ written = $true; text = [Text.Encoding]::Default.GetString([IO.File]::ReadAllBytes($path)); original = $text }
} catch {
    if (Test-Path -LiteralPath $path) { throw 'Encoding failure left a script file.' }
    $result = @{ written = $false; error = $_.Exception.Message }
}
$result | ConvertTo-Json
""")
        if result["written"]:
            self.assertEqual(result["text"], result["original"] + "\r\nexit\r\n")
        else:
            self.assertIn("ANSI code page", result["error"])

    def test_invalid_surrogate_is_rejected_without_creating_script(self):
        result = self.run_writer(r"""
$path = Join-Path $testRoot 'invalid.txt'
try { Write-FixtureDiskpartScript $path @('rem ' + [char]0xD800); throw 'Expected encoding rejection.' }
catch { $message = $_.Exception.Message }
@{ exists = [IO.File]::Exists($path); error = $message } | ConvertTo-Json
""")
        self.assertFalse(result["exists"])
        self.assertIn("ANSI code page", result["error"])

    def test_existing_script_is_not_overwritten(self):
        result = self.run_writer(r"""
$path = Join-Path $testRoot 'existing.txt'
[IO.File]::WriteAllText($path, 'previous evidence')
$rejected = $false
try { Write-FixtureDiskpartScript $path @('list vdisk') }
catch { $rejected = $true }
@{ rejected = $rejected; text = [IO.File]::ReadAllText($path) } | ConvertTo-Json
""")
        self.assertTrue(result["rejected"])
        self.assertEqual(result["text"], "previous evidence")

    def test_owned_disk_uses_numeric_cim_bus_type_and_keeps_identity_guards(self):
        result = self.run_writer(r"""
$fixtureVhd = Join-Path $testRoot 'not-mounted.vhd'
$fixtureBytes = 128MB
$fixtureDiskNumber = 7
$fixtureDiskId = 'owned-id'
$script:disk = [pscustomobject]@{
    Number = 7; UniqueId = 'owned-id'; IsBoot = $false; IsSystem = $false
    Size = 128MB; LogicalSectorSize = 512; BusType = 'File Backed Virtual'
    CimInstanceProperties = @{ BusType = [pscustomobject]@{ Value = [uint16]15 } }
}
# Stub discovery only; these tests do not create or attach a VHD.
function Assert-OwnedVhd {}
function Get-DiskImage { param($ImagePath) [pscustomobject]@{ Attached = $true } }
function Get-Disk { $script:disk }
$accepted = (Get-OwnedDisk).UniqueId -eq 'owned-id'
$rejected = @()
foreach ($case in @('physical', 'boot', 'wrong-size', 'wrong-id')) {
    $script:disk.CimInstanceProperties.BusType.Value = if ($case -eq 'physical') { [uint16]17 } else { [uint16]15 }
    $script:disk.IsBoot = $case -eq 'boot'
    $script:disk.Size = if ($case -eq 'wrong-size') { 256MB } else { 128MB }
    $script:disk.UniqueId = if ($case -eq 'wrong-id') { 'other-id' } else { 'owned-id' }
    try { $null = Get-OwnedDisk; $rejected += $false }
    catch { $rejected += $true }
}
@{ accepted = $accepted; rejected = $rejected } | ConvertTo-Json
""", function="Get-OwnedDisk")
        self.assertTrue(result["accepted"])
        self.assertEqual(result["rejected"], [True] * 4)

    def test_sample_path_walks_plain_directory_parents_and_rejects_outside(self):
        result = self.run_writer(r"""
$fixtureRoot = $testRoot + '\'
function Assert-OwnedVolume { param([switch]$RequireMarker) }
$folder = Join-Path $testRoot 'Samples\nested\folder'
$null = [IO.Directory]::CreateDirectory($folder)
$file = Join-Path $folder 'sample.txt'
[IO.File]::WriteAllText($file, 'synthetic sample')
Assert-SamplePath $file
Assert-SamplePath $folder
$outside = Join-Path $testRoot 'outside.txt'
[IO.File]::WriteAllText($outside, 'outside sample tree')
$rejected = $false
try { Assert-SamplePath $outside }
catch { $rejected = $true }
@{ accepted = $true; outside_rejected = $rejected } | ConvertTo-Json
""", function="Assert-SamplePath")
        self.assertTrue(result["accepted"])
        self.assertTrue(result["outside_rejected"])


if __name__ == "__main__":
    unittest.main()
