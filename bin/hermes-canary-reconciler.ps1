$ErrorActionPreference = 'Continue'
$Bin = Split-Path -Parent $MyInvocation.MyCommand.Path
$Hermes = Split-Path -Parent $Bin
$HomeDir = Split-Path -Parent $Hermes
if ((Split-Path -Leaf $HomeDir) -ieq 'profiles') {
  $profileParent = Split-Path -Parent $HomeDir
  if ((Split-Path -Leaf $profileParent) -ieq '.hermes') {
    $HomeDir = Split-Path -Parent $profileParent
  }
}
$State = Join-Path $Hermes 'state'
$Logs = Join-Path $Hermes 'logs'
New-Item -ItemType Directory -Force -Path $State,$Logs | Out-Null

function NowIso { (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') }
function ReadJson($Path) {
  if (Test-Path -LiteralPath $Path) {
    try { [IO.File]::ReadAllText($Path) | ConvertFrom-Json } catch { $null }
  }
}
function WriteJsonAtomic($Path, $Obj) {
  $parent = Split-Path -Parent $Path
  New-Item -ItemType Directory -Force -Path $parent | Out-Null
  $tmp = Join-Path $parent ('.' + (Split-Path -Leaf $Path) + '.tmp-' + $PID + '-' + [Guid]::NewGuid().ToString('N'))
  try {
    $json = $Obj | ConvertTo-Json -Depth 16
    [IO.File]::WriteAllText($tmp, $json + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $tmp -Destination $Path -Force
  } finally {
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
  }
}
function PsLiteral($Value) { "'" + ([string]$Value).Replace("'", "''") + "'" }
function IsoDurationMinutes($Minutes) { 'PT' + [int]$Minutes + 'M' }

function ResolveGatewayPython {
  # The scheduled gateway is the live runtime authority.  A stale
  # runtime-binding.json must never keep a health task on an older candidate.
  try {
    $gateway = Get-ScheduledTask -TaskName 'HermesGateway' -ErrorAction SilentlyContinue
    $actions = @($gateway.Actions)
    if ($actions.Count -ne 1) { return $null }
    $arguments = [string]$actions[0].Arguments
    $match = [regex]::Match($arguments, '-File\s+"?([^"\r\n]*start-hermes-golden-[^"\r\n]+\.ps1)"?', [Text.RegularExpressions.RegexOptions]::IgnoreCase)
    if (-not $match.Success) { return $null }
    $wrapper = [string]$match.Groups[1].Value
    if (-not (Test-Path -LiteralPath $wrapper -PathType Leaf)) { return $null }
    $wrapperText = [IO.File]::ReadAllText($wrapper)
    $venvMatch = [regex]::Match($wrapperText, '\$env:VIRTUAL_ENV\s*=\s*''([^'']+)''', [Text.RegularExpressions.RegexOptions]::IgnoreCase)
    if (-not $venvMatch.Success) { return $null }
    $python = Join-Path ([string]$venvMatch.Groups[1].Value) 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { return $null }
    $candidateRoot = [IO.Path]::GetFullPath((Split-Path -Parent (Split-Path -Parent $python))).TrimEnd('\')
    $expectedRoot = [IO.Path]::GetFullPath((Join-Path $Hermes 'state\runtime-candidates')).TrimEnd('\') + '\'
    if (-not $candidateRoot.StartsWith($expectedRoot, [StringComparison]::OrdinalIgnoreCase)) { return $null }
    return $python
  } catch { return $null }
}

function ResolvePython {
  $gatewayPython = ResolveGatewayPython
  if ($gatewayPython) { return $gatewayPython }
  $binding = ReadJson (Join-Path $State 'runtime-binding.json')
  if ($binding -and $binding.runtime_python -and (Test-Path -LiteralPath ([string]$binding.runtime_python))) {
    return [string]$binding.runtime_python
  }
  foreach ($candidate in @('python.exe','py.exe')) {
    if (Get-Command $candidate -ErrorAction SilentlyContinue) { return $candidate }
  }
  return 'python.exe'
}

function NewBoundedSettings($ExecutionMinutes) {
  # Windows 10's ScheduledTasks cmdlet enum omits StopExisting even though the
  # Task Scheduler XML schema supports it. Register with a temporary supported
  # value, then atomically replace the final task definition from XML.
  New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes $ExecutionMinutes) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable
}

function TestTaskContract($Task, $Execute, $Arguments, $Script, $Minutes, $ExecutionMinutes) {
  if (-not $Task) { return $false }
  try {
    $actions = @($Task.Actions)
    $interval = [string](@($Task.Triggers)[0].Repetition.Interval)
    $xml = [xml](Export-ScheduledTask -TaskName $Task.TaskName -ErrorAction Stop)
    $policy = [string]$xml.Task.Settings.MultipleInstancesPolicy
    $limit = [System.Xml.XmlConvert]::ToTimeSpan([string]$xml.Task.Settings.ExecutionTimeLimit)
    return [bool](
      $Task.Principal.UserId -eq 'SYSTEM' -and
      $actions.Count -eq 1 -and
      [string]$actions[0].Execute -ieq $Execute -and
      [string]$actions[0].Arguments -eq $Arguments -and
      [string]$actions[0].Arguments.IndexOf($Script, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
      $interval -eq (IsoDurationMinutes $Minutes) -and
      $policy -eq 'StopExisting' -and
      $limit.TotalMinutes -eq $ExecutionMinutes -and
      $Task.Settings.DisallowStartIfOnBatteries -eq $false -and
      $Task.Settings.StopIfGoingOnBatteries -eq $false -and
      $Task.Settings.StartWhenAvailable -eq $true
    )
  } catch { return $false }
}

function EnsureTask($Name, $Execute, $Arguments, $Script, $Minutes, $ExecutionMinutes) {
  if (-not (Test-Path -LiteralPath $Script)) { return 'script_missing' }
  try {
    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if (TestTaskContract $existing $Execute $Arguments $Script $Minutes $ExecutionMinutes) { return 'already_compliant' }
    $action = New-ScheduledTaskAction -Execute $Execute -Argument $Arguments
    $trigger = New-ScheduledTaskTrigger `
      -Once `
      -At (Get-Date).AddMinutes(1) `
      -RepetitionInterval (New-TimeSpan -Minutes $Minutes) `
      -RepetitionDuration (New-TimeSpan -Days 3650)
    $settings = NewBoundedSettings $ExecutionMinutes
    Register-ScheduledTask `
      -TaskName $Name `
      -Action $action `
      -Trigger $trigger `
      -Settings $settings `
      -User 'SYSTEM' `
      -RunLevel Highest `
      -Force | Out-Null
    $xml = [xml](Export-ScheduledTask -TaskName $Name -ErrorAction Stop)
    if ($null -eq $xml.Task.Settings.MultipleInstancesPolicy) { throw 'scheduled task XML has no MultipleInstancesPolicy' }
    $xml.Task.Settings.MultipleInstancesPolicy = 'StopExisting'
    Register-ScheduledTask -TaskName $Name -Xml $xml.OuterXml -User 'SYSTEM' -Force | Out-Null
    return 'ensured'
  } catch { return ('failed: ' + $_.Exception.Message) }
}

function PythonArguments($Python, $Script, $ExtraArgs) {
  $command = '$env:HERMES_HOME=' + (PsLiteral $Hermes) + '; & ' + (PsLiteral $Python) + ' ' + (PsLiteral $Script)
  if ($ExtraArgs) { $command += ' ' + $ExtraArgs }
  '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "' + $command.Replace('"','`"') + '"'
}

function TaskEvidence($Name, $IntervalMinutes, $ExecutionMinutes) {
  $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
  if (-not $task) {
    return [ordered]@{name=$Name;present=$false;interval_minutes=$IntervalMinutes;verdict='missing'}
  }
  $info = Get-ScheduledTaskInfo -TaskName $Name -ErrorAction SilentlyContinue
  $lastRun = if ($info -and $info.LastRunTime -and $info.LastRunTime.Year -gt 1900) { $info.LastRunTime.ToUniversalTime().ToString('o') } else { $null }
  $runningSeconds = $null
  if ([string]$task.State -eq 'Running' -and $info -and $info.LastRunTime -and $info.LastRunTime.Year -gt 1900) {
    $runningSeconds = [int][Math]::Max(0, ((Get-Date) - $info.LastRunTime).TotalSeconds)
  }
  $lastResult = if ($info) { [int64]$info.LastTaskResult } else { $null }
  $limit = $null
  $instancePolicy = $null
  try {
    $xml = [xml](Export-ScheduledTask -TaskName $Name -ErrorAction Stop)
    $instancePolicy = [string]$xml.Task.Settings.MultipleInstancesPolicy
    $limit = [int]([System.Xml.XmlConvert]::ToTimeSpan([string]$xml.Task.Settings.ExecutionTimeLimit).TotalSeconds)
  } catch {}
  $verdict = 'pass'
  if ([string]$task.State -eq 'Disabled') { $verdict = 'disabled' }
  elseif ($null -ne $runningSeconds -and $runningSeconds -gt ($IntervalMinutes * 120)) { $verdict = 'running_too_long' }
  elseif ($null -ne $lastResult -and $lastResult -ne 0 -and [string]$task.State -ne 'Running') { $verdict = 'last_result_failed' }
  elseif ($instancePolicy -ne 'StopExisting') { $verdict = 'instance_policy_unsafe' }
  elseif ($null -eq $limit -or $limit -ne ($ExecutionMinutes * 60)) { $verdict = 'execution_limit_unsafe' }
  [ordered]@{
    name=$Name
    present=$true
    state=[string]$task.State
    enabled=([string]$task.State -ne 'Disabled')
    interval_minutes=$IntervalMinutes
    last_run_time=$lastRun
    last_result=$lastResult
    running_seconds=$runningSeconds
    execution_time_limit_seconds=$limit
    multiple_instances=$instancePolicy
    verdict=$verdict
  }
}

$python = ResolvePython
$config = Join-Path $Hermes 'config.yaml'
$configText = if (Test-Path -LiteralPath $config) { ([IO.File]::ReadAllText($config)).ToLowerInvariant() } else { '' }
$capabilities = @()
$actions = @()
$missing = @()
$failedActions = @()

$selfScript = Join-Path $Bin 'hermes-local-selfcheck.py'
$selfState = Join-Path $State 'local-selfcheck-latest.json'
$selfStatus = EnsureTask 'HermesLocalSelfCheck' 'powershell.exe' (PythonArguments $python $selfScript '') $selfScript 15 3
$capabilities += [ordered]@{id='hermes_core';title='Hermes runtime core';detected=$true;reasons=@('config_or_runtime_home');canary=[ordered]@{id='local_selfcheck';script=$selfScript;state=$selfState;status=$(if(Test-Path -LiteralPath $selfScript){'enabled'}else{'missing'});schedule=$selfStatus}}
if (-not (Test-Path -LiteralPath $selfScript)) { $missing += [ordered]@{capability='hermes_core';canary='local_selfcheck';reason='script missing';script=$selfScript} }

$readinessScript = Join-Path $Bin 'tool-readiness-probe.py'
$readinessState = Join-Path $State 'tool-readiness-probe-latest.json'
$readinessExtra = '--output ' + (PsLiteral $readinessState)
$readinessStatus = EnsureTask 'HermesToolReadiness' 'powershell.exe' (PythonArguments $python $readinessScript $readinessExtra) $readinessScript 30 3
$capabilities += [ordered]@{id='tool_readiness';title='End-to-end tool readiness';detected=(Test-Path -LiteralPath $readinessScript);reasons=@('installed_probe');canary=[ordered]@{id='tool_readiness';script=$readinessScript;state=$readinessState;status=$(if(Test-Path -LiteralPath $readinessScript){'enabled'}else{'missing'});schedule=$readinessStatus}}
if (-not (Test-Path -LiteralPath $readinessScript)) { $missing += [ordered]@{capability='tool_readiness';canary='tool_readiness';reason='script missing';script=$readinessScript} }

$retentionScript = Join-Path $Bin 'hermes-disk-retention.py'
$retentionState = Join-Path $State 'disk-retention-last.json'
$retentionExtra = '--apply --json --clear-flags --home ' + (PsLiteral $HomeDir) + ' --hermes-home ' + (PsLiteral $Hermes)
$retentionStatus = EnsureTask 'HermesDiskRetention' 'powershell.exe' (PythonArguments $python $retentionScript $retentionExtra) $retentionScript 1440 30
$capabilities += [ordered]@{id='disk_retention';title='Runtime and snapshot retention';detected=$true;reasons=@('fleet_health_floor');canary=[ordered]@{id='disk_retention';script=$retentionScript;state=$retentionState;status=$(if(Test-Path -LiteralPath $retentionScript){'enabled'}else{'missing'});schedule=$retentionStatus}}
if (-not (Test-Path -LiteralPath $retentionScript)) { $missing += [ordered]@{capability='disk_retention';canary='disk_retention';reason='script missing';script=$retentionScript} }

if ($configText.Contains('mcp_servers') -or $configText.Contains('mcp:')) {
  $capabilities += [ordered]@{id='mcp';title='MCP tool servers';detected=$true;reasons=@('config:mcp');canary=[ordered]@{status='inventory_only';reason='no local canary registered yet'}}
}

$selfTaskArgs = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $MyInvocation.MyCommand.Path + '"'
$reconcilerStatus = EnsureTask 'HermesCanaryReconciler' 'powershell.exe' $selfTaskArgs $MyInvocation.MyCommand.Path 30 3
$taskEvidence = @(
  (TaskEvidence 'HermesLocalSelfCheck' 15 3),
  (TaskEvidence 'HermesToolReadiness' 30 3),
  (TaskEvidence 'HermesDiskRetention' 1440 30),
  (TaskEvidence 'HermesCanaryReconciler' 30 3)
)
foreach ($schedule in @($selfStatus,$readinessStatus,$retentionStatus,$reconcilerStatus)) {
  if ([string]$schedule -eq 'script_missing' -or [string]$schedule -like 'failed:*') {
    $failedActions += [ordered]@{action='ensure_task';status=[string]$schedule}
  }
}
foreach ($task in $taskEvidence) {
  if ($task.verdict -eq 'pass') { continue }
  # The local self-check owns and publishes its own failure receipt, which Doc
  # consumes directly. Mirroring that result into the reconciler creates a
  # circular latch: self-check fails because reconciler is red, while the
  # reconciler stays red because the previous self-check task failed. Keep the
  # task evidence visible, but do not make it a reconciler failure. The
  # reconciler's own task result is also from the previous cycle while this
  # cycle is still running, so it cannot veto the current result either.
  if ($task.name -in @('HermesLocalSelfCheck','HermesCanaryReconciler') -and $task.verdict -eq 'last_result_failed') {
    $actions += [ordered]@{action='observe_previous_cycle_failure';task=$task.name;status=$task.verdict}
    continue
  }
  $failedActions += [ordered]@{action='task_contract';task=$task.name;status=$task.verdict}
}

$agentId = if ($env:HERMES_AGENT_ID) { $env:HERMES_AGENT_ID } else { Split-Path -Leaf $HomeDir }
$payload = [ordered]@{
  schema_version=2
  checked_at=(NowIso)
  agent_id=$agentId
  agent_name=$agentId
  home=$HomeDir
  hermes_home=$Hermes
  capabilities=$capabilities
  scheduled_tasks=$taskEvidence
  missing_canaries=$missing
  actions=$actions
  failed_actions=$failedActions
  ok=($missing.Count -eq 0 -and $failedActions.Count -eq 0)
}
WriteJsonAtomic (Join-Path $State 'runtime-capabilities.json') $payload
WriteJsonAtomic (Join-Path $State 'canary-reconciler-latest.json') $payload
Add-Content -Path (Join-Path $Logs 'canary-reconciler.log') -Value ((NowIso) + " capabilities=$($capabilities.Count) missing=$($missing.Count) failed=$($failedActions.Count)")
$payload | ConvertTo-Json -Depth 16
if (-not $payload.ok) { exit 1 }
exit 0
