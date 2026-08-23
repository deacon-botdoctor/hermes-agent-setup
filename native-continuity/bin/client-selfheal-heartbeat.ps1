param(
  [string]$HermesHome = ""
)

$ErrorActionPreference = 'Stop'

if (-not $HermesHome) {
  if ($env:HERMES_HOME) {
    $HermesHome = $env:HERMES_HOME
  } else {
    $HermesHome = Join-Path $env:USERPROFILE '.hermes'
  }
}

$homePath = [System.IO.Path]::GetFullPath($HermesHome)
$stateDir = Join-Path $homePath 'state'
$logDir = Join-Path $homePath 'logs'
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$stateFile = Join-Path $stateDir 'client-selfheal-heartbeat-state.json'
$logFile = Join-Path $logDir 'client-selfheal-heartbeat.log'
$lockFile = Join-Path $stateDir 'client-selfheal-heartbeat.lock'

function Write-Log([string]$msg) {
  $line = "{0} [client-selfheal-heartbeat] {1}" -f ([DateTimeOffset]::UtcNow.ToString('o')), $msg
  Add-Content -Path $logFile -Value $line -Encoding UTF8
}

function Read-State([string]$path) {
  if (-not (Test-Path $path)) { return $null }
  try { return Get-Content $path -Raw | ConvertFrom-Json } catch { return $null }
}

function Test-Fresh([string]$iso, [int]$seconds = 600) {
  if (-not $iso) { return $false }
  try {
    return (([DateTimeOffset]::UtcNow - [DateTimeOffset]::Parse($iso)).TotalSeconds -le $seconds)
  } catch {
    return $false
  }
}

function Test-RecentFile([string]$path, [int]$seconds = 600) {
  if (-not (Test-Path $path)) { return $false }
  try {
    return (([DateTimeOffset]::UtcNow - [DateTimeOffset](Get-Item $path).LastWriteTimeUtc).TotalSeconds -le $seconds)
  } catch {
    return $false
  }
}

function Test-RecentHealthyWatchdog([string]$path, [int]$seconds = 600) {
  if (-not (Test-Path $path)) { return $false }
  try {
    if ((([DateTimeOffset]::UtcNow - [DateTimeOffset](Get-Item $path).LastWriteTimeUtc).TotalSeconds) -gt $seconds) {
      return $false
    }
    $tail = Get-Content $path -Tail 40 -ErrorAction SilentlyContinue
    return ($tail | Select-String -Pattern ' healthy; ' -Quiet)
  } catch {
    return $false
  }
}

function Get-GatewayProcesses([string]$pattern) {
  @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -and
    ($_.Name -match '^(python|hermes)(\.exe)?$') -and
    ($_.CommandLine -match $pattern)
  } | Sort-Object CreationDate, ProcessId)
}

function Stop-Oldest([array]$processes, [int]$keepCount) {
  if ($processes.Count -le $keepCount) { return @() }
  $removed = @()
  $targets = @($processes | Select-Object -First ($processes.Count - $keepCount))
  foreach ($proc in $targets) {
    try {
      Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
      $removed += $proc.ProcessId
    } catch {}
  }
  return $removed
}

function Restart-Lane([string]$baseDir, [string]$taskName, [string]$pattern, [bool]$stopProcessesFirst) {
  $actions = @()
  if ($stopProcessesFirst) {
    $procs = Get-GatewayProcesses $pattern
    foreach ($proc in $procs) {
      try {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
        $actions += "stopped:$($proc.ProcessId)"
      } catch {}
    }
    Start-Sleep -Seconds 2
  }
  foreach ($rel in @('gateway.pid', 'state\gateway-launch-stamp.txt', 'gateway-launch-stamp.txt')) {
    $target = Join-Path $baseDir $rel
    if (Test-Path $target) {
      try {
        Remove-Item $target -Force -ErrorAction Stop
        $actions += "cleared:$rel"
      } catch {}
    }
  }
  try {
    Start-ScheduledTask -TaskName $taskName -ErrorAction Stop | Out-Null
    $actions += "started-task:$taskName"
  } catch {
    $actions += "start-failed:$taskName"
  }
  return $actions
}

$lockHandle = $null
try {
  $lockHandle = [System.IO.File]::Open($lockFile, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
} catch {
  exit 0
}

try {
  $dualRoot = (Test-Path (Join-Path $homePath 'profiles\sarah')) -or (Get-ScheduledTask -TaskName 'SarahGateway' -ErrorAction SilentlyContinue)
  $singleBase = $homePath
  if (-not $dualRoot) {
    $posca = Join-Path $homePath 'profiles\posca'
    if ((-not (Test-Path (Join-Path $homePath 'gateway_state.json'))) -and (Test-Path (Join-Path $posca 'gateway_state.json'))) {
      $singleBase = $posca
    }
  }
  $actions = @()
  $lanes = @()

  if ($dualRoot) {
    $alfredBase = Join-Path $homePath 'profiles\alfred'
    if (-not (Test-Path $alfredBase)) { $alfredBase = $homePath }
    $alfredTask = 'HermesGateway'
    if (Get-ScheduledTask -TaskName 'AlfredGateway' -ErrorAction SilentlyContinue) {
      $alfredTask = 'AlfredGateway'
    }
    $lanes = @(
      @{ name = 'alfred'; base = $alfredBase; task = $alfredTask; pattern = 'gateway run'; stop = $false },
      @{ name = 'sarah'; base = (Join-Path $homePath 'profiles\sarah'); task = 'SarahGateway'; pattern = 'gateway run'; stop = $false }
    )
    $all = Get-GatewayProcesses 'gateway run'
    $trimmed = Stop-Oldest $all 4
    if ($trimmed.Count -gt 0) {
      $actions += "trimmed-orphans:$($all.Count)->4"
      Write-Log "trimmed dual-root orphan gateway processes: $($trimmed -join ',')"
    }
  } else {
    $profileName = Split-Path $singleBase -Leaf
    $pattern = 'gateway run'
    if ($singleBase -match '\\profiles\\') {
      $pattern = [Regex]::Escape("gateway run --profile $profileName")
    }
    $lanes = @(
      @{ name = $profileName; base = $singleBase; task = 'HermesGateway'; pattern = $pattern; stop = $true }
    )
    $all = Get-GatewayProcesses $pattern
    $launcherCount = @($all | Where-Object { $_.Name -match '^hermes(\.exe)?$' }).Count
    $expected = if ($launcherCount -ge 1) { 3 } else { 2 }
    $trimmed = Stop-Oldest $all $expected
    if ($trimmed.Count -gt 0) {
      $actions += "trimmed-orphans:$($all.Count)->$expected"
      Write-Log "trimmed profile orphan gateway processes: $($trimmed -join ',')"
    }
  }

  $laneSummaries = @()
  foreach ($lane in $lanes) {
    $statePath = Join-Path $lane.base 'gateway_state.json'
    $state = Read-State $statePath
    $telegram = $null
    if ($state -and $state.platforms -and $state.platforms.telegram) {
      $telegram = [string]$state.platforms.telegram.state
    }
    $fresh = $false
    $updated = $null
    if ($state) {
      $updated = [string]$state.updated_at
      $fresh = Test-Fresh $updated 600
    }
    $gatewayPid = 0
    if ($state -and $state.pid) {
      try { $gatewayPid = [int]$state.pid } catch { $gatewayPid = 0 }
    }
    $liveProc = $null
    if ($gatewayPid -gt 0) {
      try { $liveProc = Get-Process -Id $gatewayPid -ErrorAction Stop } catch {}
    }
    $recentLogHealthy = $false
    foreach ($rel in @('logs\gateway.log', 'logs\agent.log', 'logs\watchdog.log')) {
      if (Test-RecentFile (Join-Path $lane.base $rel) 600) {
        $recentLogHealthy = $true
        break
      }
    }
    $recentWatchdogHealthy = Test-RecentHealthyWatchdog (Join-Path $lane.base 'logs\watchdog.log') 600
    $telegramHealthy = ($telegram -eq 'connected' -or -not $telegram)
    $stateRunning = ($state -and ([string]$state.gateway_state -eq 'running'))
    $runtimeEvidence = ($liveProc -or $recentWatchdogHealthy)
    $healthy = $stateRunning -and $telegramHealthy -and $runtimeEvidence -and ($fresh -or $recentLogHealthy -or $recentWatchdogHealthy)
    $task = Get-ScheduledTask -TaskName $lane.task -ErrorAction SilentlyContinue
    $taskState = if ($task) { [string]$task.State } else { $null }
    if (-not $healthy) {
      $laneActions = Restart-Lane $lane.base $lane.task $lane.pattern $lane.stop
      if ($laneActions.Count -gt 0) {
        $actions += ($laneActions | ForEach-Object { "$($lane.name):$_" })
        Write-Log "$($lane.name) unhealthy; actions=$($laneActions -join ',')"
      }
    }
    $laneSummaries += [ordered]@{
      name = $lane.name
      task = $lane.task
      task_state = $taskState
      gateway_state = if ($state) { [string]$state.gateway_state } else { $null }
      telegram_state = $telegram
      updated_at = $updated
      pid = $gatewayPid
      pid_alive = [bool]$liveProc
      recent_log_healthy = [bool]$recentLogHealthy
      recent_watchdog_healthy = [bool]$recentWatchdogHealthy
      healthy = [bool]$healthy
    }
  }

  $payload = [ordered]@{
    generated_at = [DateTimeOffset]::UtcNow.ToString('o')
    hermes_home = $homePath
    dual_root = [bool]$dualRoot
    actions = @($actions)
    lanes = @($laneSummaries)
  }
  [System.IO.File]::WriteAllText($stateFile, ($payload | ConvertTo-Json -Depth 6), (New-Object System.Text.UTF8Encoding($false)))
  $continuity = Join-Path $homePath 'bin\native-agent-continuity.py'
  $sessionRunner = Join-Path $homePath 'bin\native-session-runner.py'
  $baseline = Join-Path $homePath 'bin\tenant-gbrain-baseline.py'
  $contract = Join-Path $homePath 'config\native-agent-continuity-v1.json'
  $baselineReceipt = Join-Path $homePath 'state\native-agent-continuity\baseline.json'
  $continuityManifest = Join-Path $homePath 'state\native-agent-continuity\manifest.json'
  if ((Test-Path $continuity) -and (Test-Path $continuityManifest)) {
    $runtimePython = Join-Path $homePath 'hermes-agent\venv\Scripts\python.exe'
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    $python = if (Test-Path $runtimePython) {
      $runtimePython
    } elseif ($pythonCommand) {
      $pythonCommand.Source
    } else {
      $null
    }
    if ($python) {
      if (-not (Test-Path $baselineReceipt)) {
        if ((-not (Test-Path $baseline)) -or (-not (Test-Path $contract))) {
          Write-Log 'native-agent tenant GBrain baseline assets are missing'
          return
        }
        & $python $baseline apply --manifest $continuityManifest --contract $contract --json | Out-Null
        if ($LASTEXITCODE -ne 0) {
          Write-Log 'native-agent tenant GBrain baseline activation failed'
          return
        }
      }
      & $python $continuity reconcile --manifest $continuityManifest --json | Out-Null
      if ($LASTEXITCODE -ne 0) {
        Write-Log 'native-agent continuity reconcile failed'
      } elseif (Test-Path $sessionRunner) {
        & $python $sessionRunner --manifest $continuityManifest --json | Out-Null
        if ($LASTEXITCODE -ne 0) {
          Write-Log 'native-agent session projection failed'
        }
      }
    }
  }
} finally {
  if ($lockHandle) { $lockHandle.Dispose() }
}
