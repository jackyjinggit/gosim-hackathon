[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Agent,
    [string]$RequirementsDir = (Join-Path $PSScriptRoot "public-exercise\requirements"),
    [string]$TestsDir,
    [Parameter(Mandatory = $true)][string]$OutputDir,
    [string]$EnvFile,
    [string]$Image = $(if ($env:ARCBENCH_LOCAL_IMAGE) { $env:ARCBENCH_LOCAL_IMAGE } else { "arcbench-local-submit:latest" }),
    [string]$Template,
    [string]$Competition = "public-practice",
    [string]$Task = "counter",
    [string]$Memory = "2g",
    [string]$Cpus = "1",
    [switch]$RunAsRoot,
    [switch]$ShowTests,
    [switch]$PrepareOnly
)

$ErrorActionPreference = "Stop"
$PythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $PythonCommand) {
    $PythonCommand = Get-Command py -ErrorAction SilentlyContinue
}
if (-not $PythonCommand) {
    throw "Python 3 is required. Install Python 3.10+ and ensure python.exe or py.exe is on PATH."
}
$Python = $PythonCommand.Source
$PythonPrefix = @()
if ($PythonCommand.Name -eq "py.exe") { $PythonPrefix = @("-3") }
$AgentPath = (Resolve-Path -LiteralPath $Agent).Path
$RequirementsPath = (Resolve-Path -LiteralPath $RequirementsDir).Path
$OutputPath = [System.IO.Path]::GetFullPath($OutputDir)
$Arguments = @(
    (Join-Path $PSScriptRoot "local_submit.py"), "run",
    "--agent", $AgentPath,
    "--requirements-dir", $RequirementsPath,
    "--output-dir", $OutputPath,
    "--image", $Image,
    "--competition", $Competition,
    "--task", $Task,
    "--memory", $Memory,
    "--cpus", $Cpus
)

if ($TestsDir) { $Arguments += @("--tests-dir", (Resolve-Path -LiteralPath $TestsDir).Path) }
if ($EnvFile) { $Arguments += @("--env-file", (Resolve-Path -LiteralPath $EnvFile).Path) }
if ($Template) { $Arguments += @("--template", (Resolve-Path -LiteralPath $Template).Path) }
if ($RunAsRoot) { $Arguments += "--run-as-root" }
if ($ShowTests) { $Arguments += "--show-tests" }
if ($PrepareOnly) { $Arguments += "--prepare-only" }

& $Python @PythonPrefix @Arguments
exit $LASTEXITCODE
