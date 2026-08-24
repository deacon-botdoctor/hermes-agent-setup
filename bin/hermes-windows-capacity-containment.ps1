param(
  [switch]$Apply,
  [int]$MinimumAgeSeconds = 3600,
  [string]$ReceiptPath = ""
)

$ErrorActionPreference = 'Stop'
$scriptHome = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$hermesHome = if ($scriptHome -and (Test-Path -LiteralPath $scriptHome)) { $scriptHome } elseif ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:USERPROFILE '.hermes' }
if (-not $ReceiptPath) {
  $ReceiptPath = Join-Path $hermesHome 'state\windows-capacity-containment-latest.json'
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
if ($Apply -and $critical) {
  foreach ($tree in $trees) {
    foreach ($id in @($tree.member_pids | Sort-Object -Descending)) {
      try { Stop-Process -Id $id -Force -ErrorAction Stop; $terminated += $id }
      catch { $errors += "pid=$id $($_.Exception.GetType().Name)" }
    }
  }
}
$after = Capacity-Snapshot
$receipt = [ordered]@{
  schema = 'windows-capacity-containment/v1'
  generated_at = (Get-Date).ToUniversalTime().ToString('o')
  mode = if ($Apply) { 'apply' } else { 'dry_run' }
  critical = $critical
  minimum_age_seconds = $MinimumAgeSeconds
  protected_pids = @($PID, $gatewayPid | Where-Object { $_ -gt 0 })
  before = $before
  candidates = @($trees)
  terminated_pids = @($terminated | Sort-Object -Unique)
  errors = @($errors)
  after = $after
  reboot_attempted = $false
  interactive_app_restart_attempted = $false
}
Atomic-Json $ReceiptPath $receipt
$receipt | ConvertTo-Json -Depth 8
if ($errors.Count -gt 0) { exit 1 }
exit 0
