param(
  [Alias("HermesRoot")]
  [string]$HermesHome = $(if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $HOME ".hermes" }),
  [string]$Profile = "root",
  [switch]$EnableTelegramDepth
)

$ErrorActionPreference = "SilentlyContinue"
function Read-Json { param($path) if (-not (Test-Path $path)) { return $null }; try { return Get-Content -Raw -Path $path | ConvertFrom-Json } catch { return $null } }
function Resolve-ProfileDir {
  param($hermesRoot, $profile)
  if ($profile -and $profile -ne "root" -and $profile -ne "enoch") {
    if ($profile -notmatch '^[A-Za-z0-9_-]+$') { throw "invalid_profile" }
    # A named profile must never borrow another profile's healthy state.
    return (Join-Path $hermesRoot ("profiles\" + $profile))
  }
  return $hermesRoot
}


$profileDir = Resolve-ProfileDir $HermesHome $Profile
$state = Read-Json (Join-Path $profileDir "gateway_state.json")
if (-not $state) {
  Write-Output "missing"; exit 1
}
try {
  $stateAge = ([DateTime]::UtcNow - [DateTimeOffset]::Parse([string]$state.updated_at).UtcDateTime).TotalSeconds
  if ($stateAge -lt 0 -or $stateAge -gt 600) { Write-Output "gateway_state_stale"; exit 1 }
} catch { Write-Output "gateway_state_timestamp_invalid"; exit 1 }
if ([string]$state.gateway_state -ne "running") { Write-Output ("state=" + $state.gateway_state); exit 1 }
$telegram = $state.platforms.telegram
if ($telegram -and [string]$telegram.state -ne "connected") { Write-Output ("telegram=" + $telegram.state); exit 1 }
if ($telegram -and $telegram.ingress.stalled -eq $true) { Write-Output "telegram_ingress_stalled"; exit 1 }
$gatewayPid = $state.pid
if (-not $gatewayPid -or -not (Get-Process -Id ([int]$gatewayPid) -ErrorAction SilentlyContinue)) { Write-Output ("pid_dead=" + $gatewayPid); exit 1 }
try {
  $cw = (netstat -ano | Select-String "CLOSE_WAIT" | Where-Object { $_.Line -match ("\s" + [regex]::Escape([string]$gatewayPid) + "\s*$") }).Count
  if ($cw -gt 5) { Write-Output ("close_wait=" + $cw); exit 1 }
} catch {}

$manifest = Read-Json (Join-Path $profileDir "config\canary_manifest.json")
$probeEnabled = $EnableTelegramDepth.IsPresent
if ($manifest -and $manifest.telegram -and $manifest.telegram.depth_probe_enabled -eq $true) { $probeEnabled = $true }
if ($manifest -and $manifest.disable -and $manifest.disable.enabled -eq $true) { $probeEnabled = $false }
if ($probeEnabled) {
  $probe = Join-Path $HermesHome "bin\telegram-watchdog-depth-probe.ps1"
  if (-not (Test-Path $probe)) { Write-Output "telegram_depth=missing"; exit 1 }
  & powershell -NoProfile -ExecutionPolicy Bypass -File $probe -HermesHome $HermesHome -Profile $Profile | Out-Null
  if ($LASTEXITCODE -ne 0) { Write-Output "telegram_depth=failed"; exit 1 }
}
Write-Output "ok"
exit 0
