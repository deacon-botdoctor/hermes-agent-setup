param(
  [Alias("HermesRoot")]
  [string]$HermesHome = $(if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $HOME ".hermes" }),
  [string]$Profile = "root",
  [int]$MaxHeartbeatAgeSec = 120,
  [int]$MaxCloseWait = 5,
  [int]$MaxActiveAgentsAgeSec = 900,
  [int]$MaxSessionLockAgeSec = 1800,
  [int]$MaxRestartFollowupAgeSec = 900,
  [switch]$Json
)

$ErrorActionPreference = "SilentlyContinue"

function To-IsoUtc { param($dt) if (-not $dt) { return $null }; try { return ([DateTimeOffset]$dt).UtcDateTime.ToString("o") } catch { return $null } }
function Parse-Time { param($value) if (-not $value) { return $null }; try { return [DateTimeOffset]::Parse([string]$value).UtcDateTime } catch { return $null } }
function Age-Sec { param($dt) if (-not $dt) { return $null }; return [int]([DateTime]::UtcNow - $dt).TotalSeconds }
function Read-Json { param($path) if (-not (Test-Path $path)) { return $null }; try { return Get-Content -Raw -Path $path | ConvertFrom-Json } catch { return $null } }
function Write-Result { param($payload) if ($Json) { $payload | ConvertTo-Json -Depth 10 -Compress } else { [string]$payload.reason } }

function Resolve-ProfileDir {
  param($hermesRoot, $profile)
  if ($profile -and $profile -ne "root" -and $profile -ne "enoch") {
    if ($profile -notmatch '^[A-Za-z0-9_-]+$') { throw "invalid_profile" }
    $resolvedRoot = [System.IO.Path]::GetFullPath($hermesRoot).TrimEnd([char[]]"\/")
    if ((Split-Path -Leaf $resolvedRoot) -eq $profile -and
        (Split-Path -Leaf (Split-Path -Parent $resolvedRoot)) -eq "profiles") {
      return $resolvedRoot
    }
    # A named profile must never borrow another profile's healthy state.
    return (Join-Path $hermesRoot ("profiles\" + $profile))
  }
  return $hermesRoot
}
$profileDir = Resolve-ProfileDir $HermesHome $Profile
$statePath = Join-Path $profileDir "gateway_state.json"
$manifestPath = Join-Path $profileDir "config\canary_manifest.json"
$manifest = Read-Json $manifestPath
$failures = New-Object System.Collections.Generic.List[string]
$checks = [ordered]@{}

$state = Read-Json $statePath
if (-not $state) {
  $failures.Add("gateway_state_missing")
} else {
  if ([string]$state.gateway_state -ne "running") { $failures.Add("gateway_state=$($state.gateway_state)") }
  $telegram = $state.platforms.telegram
  if ($telegram -and [string]$telegram.state -ne "connected") { $failures.Add("telegram=$($telegram.state)") }
  if ($telegram -and $telegram.ingress.stalled -eq $true) { $failures.Add("telegram_ingress_stalled") }
  $gatewayPid = $state.pid
  $pidAlive = $false
  if ($gatewayPid) { $pidAlive = [bool](Get-Process -Id ([int]$gatewayPid) -ErrorAction SilentlyContinue) }
  if (-not $pidAlive) { $failures.Add("pid_dead=$gatewayPid") }
  $closeWait = 0
  if ($gatewayPid) {
    try { $closeWait = (netstat -ano | Select-String "CLOSE_WAIT" | Where-Object { $_.Line -match ("\s" + [regex]::Escape([string]$gatewayPid) + "\s*$") }).Count } catch { $closeWait = 0 }
  }
  if ($closeWait -gt $MaxCloseWait) { $failures.Add("close_wait=$closeWait") }
  $updatedAt = Parse-Time $state.updated_at
  $stateAge = Age-Sec $updatedAt
  if ($null -eq $stateAge -or $stateAge -lt 0 -or $stateAge -gt 600) { $failures.Add("gateway_state_missing_or_stale=$stateAge") }
  $checks.gateway = [ordered]@{ pid = $gatewayPid; pid_alive = $pidAlive; close_wait = $closeWait; state_age = $stateAge }

  $activeAgents = 0
  try { $activeAgents = [int]$state.active_agents } catch { $activeAgents = 0 }
  if ($activeAgents -gt 0 -and ($null -eq $stateAge -or $stateAge -gt $MaxActiveAgentsAgeSec)) { $failures.Add("stale_active_agents=$activeAgents state_age=$stateAge") }
  $checks.active_agents = [ordered]@{ active_agents = $activeAgents; state_age = $stateAge; ok = -not ($activeAgents -gt 0 -and ($null -eq $stateAge -or $stateAge -gt $MaxActiveAgentsAgeSec)) }

  $healthPath = Join-Path $profileDir "state\telegram-polling-health.json"
  $health = Read-Json $healthPath
  $heartbeatAt = $null
  $heartbeatSource = $null
  if ($health -and $health.last_poll_probe_at) { $heartbeatAt = Parse-Time $health.last_poll_probe_at; $heartbeatSource = "sidecar" }
  elseif ($telegram -and $telegram.last_successful_poll_at) { $heartbeatAt = Parse-Time $telegram.last_successful_poll_at; $heartbeatSource = "gateway_state" }
  $heartbeatAge = Age-Sec $heartbeatAt
  $pollingOk = $heartbeatAge -ne $null -and $heartbeatAge -le $MaxHeartbeatAgeSec
  if (-not $pollingOk) { $failures.Add("poll_heartbeat_missing_or_stale=$heartbeatAge") }
  $checks.polling = [ordered]@{ ok = $pollingOk; heartbeat_age = $heartbeatAge; source = $heartbeatSource; sidecar = $healthPath }
}

$locks = @()
foreach ($rel in @(".session_active.lock", "state\.session_active.lock", "state\session_active.lock")) {
  $p = Join-Path $profileDir $rel
  if (Test-Path $p) {
    $lock = Read-Json $p
    $ts = $null
    if ($lock) { foreach ($k in @("last_signal_at", "updated_at", "heartbeat_at", "started_at", "created_at")) { if ($lock.$k) { $ts = Parse-Time $lock.$k; break } } }
    $age = Age-Sec $ts
    $stale = ($null -eq $age -or $age -gt $MaxSessionLockAgeSec)
    if ($stale) { $failures.Add("stale_session_lock=$rel") }
    $locks += [ordered]@{ path = $p; age = $age; stale = $stale }
  }
}
$checks.session_locks = $locks

$restartStamps = @()
$logDir = Join-Path $profileDir "logs"
if (Test-Path $logDir) { $restartStamps = Get-ChildItem -Path $logDir -Filter "*restart*.stamp" }
$latestRestart = $null
foreach ($s in $restartStamps) { if (-not $latestRestart -or $s.LastWriteTimeUtc -gt $latestRestart.LastWriteTimeUtc) { $latestRestart = $s } }
if ($latestRestart) {
  $restartAge = [int]([DateTime]::UtcNow - $latestRestart.LastWriteTimeUtc).TotalSeconds
  $checks.restart_followup = [ordered]@{ latest = $latestRestart.FullName; age = $restartAge; ok = ($restartAge -gt $MaxRestartFollowupAgeSec) }
  if ($restartAge -le $MaxRestartFollowupAgeSec) { $failures.Add("restart_followup_requires_heartbeat") }
} else { $checks.restart_followup = [ordered]@{ ok = $true; reason = "no_recent_restart" } }

if ($manifest -and $manifest.disable -and $manifest.disable.enabled -eq $true) {
  $manifestCheck = [ordered]@{ ok = $true; disabled = $true; reason = "manifest_disabled" }
} else {
  $telegramManifest = if ($manifest) { $manifest.telegram } else { $null }
  $mode = if ($telegramManifest -and $telegramManifest.message_canary_mode) { [string]$telegramManifest.message_canary_mode } else { "heartbeat_only" }
  $issues = @()
  if ($mode -notin @("heartbeat_only", "observe_message", "send_and_observe")) { $issues += "invalid_message_canary_mode=$mode" }
  if ($mode -in @("observe_message", "send_and_observe") -and (-not $telegramManifest.scoped_test_topic -or -not $telegramManifest.scoped_test_topic.chat_id)) { $issues += "missing_scoped_test_topic.chat_id" }
  foreach ($i in $issues) { $failures.Add($i) }
  $manifestCheck = [ordered]@{ ok = ($issues.Count -eq 0); mode = $mode; issues = $issues; path = $manifestPath }
}
$checks.manifest = $manifestCheck

$classification = "healthy"
if ($failures.Count -gt 0) {
  $joined = [string]::Join(";", $failures)
  if ($joined -match "poll|telegram|gateway|close_wait|pid_dead") { $classification = "platform_or_runtime_transport" }
  elseif ($joined -match "active_agents|session_lock") { $classification = "runtime_stall" }
  elseif ($joined -match "canary|topic") { $classification = "routing_or_canary_gap" }
  else { $classification = "watchdog_depth_gap" }
}
$payload = [ordered]@{ ok = ($failures.Count -eq 0); classification = $classification; profile_dir = $profileDir; failures = @($failures); checks = $checks; reason = $(if ($failures.Count -eq 0) { "ok depth_checks=passed" } else { [string]::Join("; ", $failures) }) }
Write-Result $payload
if ($payload.ok) { exit 0 } else { exit 1 }
