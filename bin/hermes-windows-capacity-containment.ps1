param(
  [switch]$Apply,
  [int]$MinimumAgeSeconds = 3600,
  [string]$ReceiptPath = "",
  [string]$ConfigPath = ""
)

$ErrorActionPreference = 'Stop'
$scriptHome = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$hermesHome = if ($scriptHome -and (Test-Path -LiteralPath $scriptHome)) { $scriptHome } elseif ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:USERPROFILE '.hermes' }
if (-not $ReceiptPath) {
  $ReceiptPath = Join-Path $hermesHome 'state\windows-capacity-containment-latest.json'
}
if (-not $ConfigPath) {
  $ConfigPath = Join-Path $hermesHome 'config\windows-capacity-containment.json'
}

function Atomic-Json([string]$Path, $Value) {
  $parent = Split-Path -Parent $Path
  New-Item -ItemType Directory -Force -Path $parent | Out-Null
  $temporary = "$Path.tmp-$PID"
  [IO.File]::WriteAllText($temporary, (($Value | ConvertTo-Json -Depth 8) + "`n"), (New-Object Text.UTF8Encoding($false)))
  Move-Item -Force -LiteralPath $temporary -Destination $Path
}

function Capacity-Snapshot {
  $os = Get-CimInstance Win32_OperatingSystem
  $processes = @(Get-Process)
  $max = $processes | Sort-Object HandleCount -Descending | Select-Object -First 1
  return [ordered]@{
    captured_at = (Get-Date).ToUniversalTime().ToString('o')
    total_processes = $processes.Count
    virtual_total_bytes = [int64]$os.TotalVirtualMemorySize * 1024
    virtual_free_bytes = [int64]$os.FreeVirtualMemory * 1024
    max_handle_count = if ($max) { [int64]$max.HandleCount } else { 0 }
    max_handle_process = if ($max) { [string]$max.ProcessName } else { '' }
  }
}

function Is-Critical($Snapshot) {
  $freePct = if ($Snapshot.virtual_total_bytes -gt 0) { 100.0 * $Snapshot.virtual_free_bytes / $Snapshot.virtual_total_bytes } else { 100.0 }
  return ($freePct -lt 10.0 -or $Snapshot.total_processes -gt 800 -or $Snapshot.max_handle_count -gt 50000)
}

function Service-Snapshot([string]$Name) {
  $service = Get-CimInstance Win32_Service -Filter ("Name='" + $Name + "'")
  if (-not $service) { return $null }
  $process = if ($service.ProcessId) { Get-Process -Id $service.ProcessId -ErrorAction SilentlyContinue } else { $null }
  return [ordered]@{
    name = [string]$service.Name
    state = [string]$service.State
    pid = [int]$service.ProcessId
    handles = if ($process) { [int64]$process.HandleCount } else { 0 }
  }
}

function Reset-WpnUserService([int64]$Threshold) {
  $service = Get-Service | Where-Object Name -like 'WpnUserService*' | Select-Object -First 1
  if (-not $service) { throw 'WpnUserService instance not found' }
  $beforeService = Service-Snapshot $service.Name
  if ($beforeService.handles -le $Threshold) {
    return [ordered]@{kind='wpn_user_service';action='below_threshold';before=$beforeService;after=$beforeService}
  }
  Restart-Service -Name $service.Name -Force
  $deadline = (Get-Date).AddSeconds(30)
  do {
    Start-Sleep -Milliseconds 500
    $afterService = Service-Snapshot $service.Name
  } until (($afterService -and $afterService.state -eq 'Running') -or (Get-Date) -gt $deadline)
  if (-not $afterService -or $afterService.state -ne 'Running') { throw 'WpnUserService did not recover' }
  return [ordered]@{kind='wpn_user_service';action='restarted';before=$beforeService;after=$afterService}
}

function Reset-IPHelperWithTailscale([int64]$Threshold) {
  $beforeService = Service-Snapshot 'iphlpsvc'
  if (-not $beforeService) { throw 'iphlpsvc not found' }
  if ($beforeService.handles -le $Threshold) {
    return [ordered]@{kind='ip_helper_tailscale';action='below_threshold';before=$beforeService;after=$beforeService;portproxy_preserved=$true}
  }
  $beforePorts = @(& netsh interface portproxy show all | ForEach-Object { $_.TrimEnd() })
  Stop-Service -Name Tailscale -Force -ErrorAction SilentlyContinue
  try {
    Stop-Service -Name iphlpsvc -Force
    Start-Service -Name iphlpsvc
    Start-Service -Name Tailscale
    $deadline = (Get-Date).AddSeconds(60)
    do {
      Start-Sleep -Seconds 1
      $ip = Get-Service -Name iphlpsvc
      $tailscale = Get-Service -Name Tailscale
    } until (($ip.Status -eq 'Running' -and $tailscale.Status -eq 'Running') -or (Get-Date) -gt $deadline)
    if ($ip.Status -ne 'Running' -or $tailscale.Status -ne 'Running') {
      throw "services did not recover: iphlpsvc=$($ip.Status) tailscale=$($tailscale.Status)"
    }
    $afterPorts = @(& netsh interface portproxy show all | ForEach-Object { $_.TrimEnd() })
    $preserved = (@(Compare-Object $beforePorts $afterPorts).Count -eq 0)
    if (-not $preserved) { throw 'portproxy configuration changed during reset' }
    return [ordered]@{
      kind='ip_helper_tailscale'
      action='restarted'
      before=$beforeService
      after=(Service-Snapshot 'iphlpsvc')
      tailscale=[string](Get-Service -Name Tailscale).Status
      portproxy_preserved=$true
    }
  } catch {
    try { Start-Service -Name iphlpsvc -ErrorAction SilentlyContinue } catch {}
    try { Start-Service -Name Tailscale -ErrorAction SilentlyContinue } catch {}
    throw
  }
}

$before = Capacity-Snapshot
$statePath = Join-Path $hermesHome 'gateway_state.json'
$gatewayPid = 0
try { $gatewayPid = [int](Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json).pid } catch {}

# This is intentionally a bounded CIM query over shell/SSH names only. It is
# never the WMI-wide process walk that previously wedged the Posca watchdog.
$shellRows = @()
try {
  $shellRows = @(Get-CimInstance Win32_Process -Filter "Name='sshd.exe' OR Name='ssh.exe' OR Name='powershell.exe' OR Name='pwsh.exe' OR Name='cmd.exe' OR Name='conhost.exe'")
} catch {}
$byPid = @{}
$children = @{}
foreach ($row in $shellRows) {
  $id = [int]$row.ProcessId
  $parent = [int]$row.ParentProcessId
  $byPid[$id] = $row
  if (-not $children.ContainsKey($parent)) { $children[$parent] = New-Object System.Collections.ArrayList }
  [void]$children[$parent].Add($id)
}

$now = Get-Date
$trees = @()
foreach ($row in $shellRows) {
  if ([string]$row.Name -ine 'sshd.exe') { continue }
  $parent = if ($byPid.ContainsKey([int]$row.ParentProcessId)) { $byPid[[int]$row.ParentProcessId] } else { $null }
  if (-not $parent -or [string]$parent.Name -ine 'sshd.exe') { continue }
  $created = $null
  try {
    if ($row.CreationDate -is [DateTime]) { $created = [DateTime]$row.CreationDate }
    else { $created = [Management.ManagementDateTimeConverter]::ToDateTime([string]$row.CreationDate) }
  } catch {}
  $age = if ($created) { [int]($now - $created).TotalSeconds } else { 0 }
  if ($age -lt $MinimumAgeSeconds) { continue }
  $pending = New-Object System.Collections.Queue
  $pending.Enqueue([int]$row.ProcessId)
  $members = @()
  while ($pending.Count -gt 0) {
    $member = [int]$pending.Dequeue()
    if ($members -contains $member) { continue }
    $members += $member
    if ($children.ContainsKey($member)) { foreach ($child in $children[$member]) { $pending.Enqueue([int]$child) } }
  }
  if ($members -contains $PID -or ($gatewayPid -gt 0 -and $members -contains $gatewayPid)) { continue }
  $trees += [ordered]@{ root_pid=[int]$row.ProcessId; age_seconds=$age; member_pids=@($members) }
}

$critical = Is-Critical $before
$terminated = @()
$errors = @()
$serviceActions = @()
if ($Apply -and $critical) {
  foreach ($tree in $trees) {
    foreach ($id in @($tree.member_pids | Sort-Object -Descending)) {
      try { Stop-Process -Id $id -Force -ErrorAction Stop; $terminated += $id }
      catch { $errors += "pid=$id $($_.Exception.GetType().Name)" }
    }
  }
  if (Test-Path -LiteralPath $ConfigPath) {
    try {
      $config = Get-Content -Raw -LiteralPath $ConfigPath | ConvertFrom-Json
      if ([int]$config.schema_version -ne 1 -or $config.enabled -ne $true) { throw 'unsupported or disabled capacity config' }
      foreach ($rule in @($config.rules)) {
        $threshold = [int64]$rule.max_handles
        if ($threshold -lt 50000) { throw "unsafe service threshold for $($rule.kind)" }
        if ([string]$rule.kind -eq 'wpn_user_service') {
          $serviceActions += Reset-WpnUserService $threshold
        } elseif ([string]$rule.kind -eq 'ip_helper_tailscale') {
          $serviceActions += Reset-IPHelperWithTailscale $threshold
        } else {
          throw "unsupported service containment kind: $($rule.kind)"
        }
      }
    } catch {
      $errors += "service_containment $($_.Exception.GetType().Name): $($_.Exception.Message)"
    }
  }
}
$after = Capacity-Snapshot
$receipt = [ordered]@{
  schema = 'windows-capacity-containment/v2'
  generated_at = (Get-Date).ToUniversalTime().ToString('o')
  mode = if ($Apply) { 'apply' } else { 'dry_run' }
  critical = $critical
  minimum_age_seconds = $MinimumAgeSeconds
  protected_pids = @($PID, $gatewayPid | Where-Object { $_ -gt 0 })
  before = $before
  candidates = @($trees)
  terminated_pids = @($terminated | Sort-Object -Unique)
  service_actions = @($serviceActions)
  service_config_path = $ConfigPath
  errors = @($errors)
  after = $after
  reboot_attempted = $false
  interactive_app_restart_attempted = $false
}
Atomic-Json $ReceiptPath $receipt
$receipt | ConvertTo-Json -Depth 8
if ($errors.Count -gt 0) { exit 1 }
exit 0
