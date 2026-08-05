param(
  [string]$HermesHome = $(if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:USERPROFILE ".hermes" }),
  [string]$Profile = "root"
)

$ErrorActionPreference = "Stop"
$env:HERMES_HOME = $HermesHome
if (-not $env:TERMINAL_CWD) { $env:TERMINAL_CWD = $HermesHome }
$LogDir = Join-Path $HermesHome "logs"
$Log = Join-Path $LogDir "start-hermes.log"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }
function Write-Log { param($Msg) Add-Content -Path $Log -Value ("{0} [start-hermes] {1}" -f ([DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")), $Msg) -Encoding UTF8 }

$EnvPath = Join-Path $HermesHome ".env"
if (Test-Path $EnvPath) {
  Get-Content -Path $EnvPath | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) { return }
    $key, $value = $line.Split("=", 2)
    if ($key -and $key -ne "ANTHROPIC_TOKEN") {
      [Environment]::SetEnvironmentVariable($key, $value, "Process")
    }
  }
}

$VenvPython = Join-Path $HermesHome "hermes-agent\venv\Scripts\python.exe"
$HermesExe = Join-Path $HermesHome "hermes-agent\venv\Scripts\hermes.exe"
if (-not (Test-Path $VenvPython)) { throw "venv python missing at $VenvPython" }
Write-Log "Starting Hermes gateway profile=$Profile"
Set-Location $HermesHome
if (Test-Path $HermesExe) {
  if ($Profile -and $Profile -ne "root") { & $HermesExe gateway run --profile $Profile --replace } else { & $HermesExe gateway run --replace }
} else {
  if ($Profile -and $Profile -ne "root") { & $VenvPython -m hermes_cli.main gateway run --profile $Profile --replace } else { & $VenvPython -m hermes_cli.main gateway run --replace }
}
exit $LASTEXITCODE
