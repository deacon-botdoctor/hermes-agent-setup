from __future__ import annotations

import base64
import json
import os
import subprocess
from typing import Any

from mcp.server.mcpserver import MCPServer as FastMCP


SERVER_NAME = "windows-runtime"
DEFAULT_TIMEOUT = int(os.environ.get("WINDOWS_RUNTIME_TIMEOUT", "30"))
DEFAULT_LOG_LINES = int(os.environ.get("WINDOWS_RUNTIME_LOG_LINES", "80"))
DEFAULT_HERMES_HOME = os.environ.get("WINDOWS_RUNTIME_HERMES_HOME", r"%USERPROFILE%\.hermes")

mcp = FastMCP(SERVER_NAME)


def _powershell_encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")


def _ssh_destination(target: str, user: str | None = None) -> str:
    target = (target or "").strip()
    if not target:
        raise ValueError("target is required")
    if user:
        return f"{user}@{target}"
    return target


def _run_ssh_powershell(
    target: str,
    script: str,
    *,
    user: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    encoded = _powershell_encoded(
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
        "$OutputEncoding = [System.Text.Encoding]::UTF8\n"
        + script
    )
    command = [
        "ssh",
        "-o",
        "ConnectTimeout=20",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        _ssh_destination(target, user),
        f"powershell -NoProfile -EncodedCommand {encoded}",
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip() or f"rc={completed.returncode}"
        raise ValueError(detail)
    return completed


def _run_json(target: str, script: str, *, user: str | None = None, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    completed = _run_ssh_powershell(target, script, user=user, timeout=timeout, check=True)
    raw = (completed.stdout or "").strip()
    if not raw:
        raise ValueError("empty response from remote PowerShell")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"failed to decode remote JSON: {raw[:400]}") from exc


def _windows_hermes_home_expr(hermes_home: str | None) -> str:
    if hermes_home:
        return json.dumps(hermes_home)
    return '(Join-Path $env:USERPROFILE ".hermes")'


@mcp.tool()
def windows_runtime_healthcheck() -> dict[str, Any]:
    return {
        "ok": True,
        "server": SERVER_NAME,
        "default_timeout": DEFAULT_TIMEOUT,
        "default_log_lines": DEFAULT_LOG_LINES,
        "default_hermes_home": DEFAULT_HERMES_HOME,
        "ssh_available": subprocess.run(
            ["which", "ssh"],
            capture_output=True,
            text=True,
            check=False,
        ).returncode
        == 0,
    }


@mcp.tool()
def windows_runtime_status(
    target: str,
    user: str | None = None,
    hermes_home: str | None = None,
) -> dict[str, Any]:
    script = f"""
$hermesHome = {_windows_hermes_home_expr(hermes_home)}
$gateway = Join-Path $hermesHome "gateway.py"
$config = Join-Path $hermesHome "config.yaml"
$logs = Join-Path $hermesHome "logs"
$obj = [ordered]@{{
  ok = $true
  target = {json.dumps(target)}
  hermes_home = $hermesHome
  user_profile = $env:USERPROFILE
  computer_name = $env:COMPUTERNAME
  hermes_home_exists = Test-Path $hermesHome
  config_exists = Test-Path $config
  logs_exists = Test-Path $logs
  python = $null
  hermes_version = $null
}}
try {{
  $obj.python = (& python --version) 2>&1 | Out-String
}} catch {{}}
try {{
  $obj.hermes_version = (& hermes --version) 2>&1 | Out-String
}} catch {{}}
$obj | ConvertTo-Json -Depth 6 -Compress
"""
    return _run_json(target, script, user=user)


@mcp.tool()
def windows_runtime_scheduled_tasks(
    target: str,
    task_name: str = "HermesGateway",
    user: str | None = None,
) -> dict[str, Any]:
    script = f"""
$name = {json.dumps(task_name)}
$tasks = @(Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue | ForEach-Object {{
  [ordered]@{{
    task_name = $_.TaskName
    state = [string]$_.State
    task_path = $_.TaskPath
    author = $_.Author
    actions = @($_.Actions | ForEach-Object {{ ($_.Execute + ' ' + $_.Arguments).Trim() }})
    triggers = @($_.Triggers | ForEach-Object {{ $_.StartBoundary }})
  }}
}})
[ordered]@{{
  ok = $true
  target = {json.dumps(target)}
  count = $tasks.Count
  tasks = $tasks
}} | ConvertTo-Json -Depth 8 -Compress
"""
    return _run_json(target, script, user=user)


@mcp.tool()
def windows_runtime_gateway_processes(target: str, user: str | None = None) -> dict[str, Any]:
    script = f"""
$rows = @(Get-CimInstance Win32_Process | Where-Object {{
  ($_.Name -match '^(python|hermes)(\\.exe)?$') -and ($_.CommandLine -match 'gateway')
}} | ForEach-Object {{
  [ordered]@{{
    process_id = $_.ProcessId
    name = $_.Name
    command_line = $_.CommandLine
  }}
}})
[ordered]@{{
  ok = $true
  target = {json.dumps(target)}
  count = $rows.Count
  processes = $rows
}} | ConvertTo-Json -Depth 6 -Compress
"""
    return _run_json(target, script, user=user)


@mcp.tool()
def windows_runtime_recent_log(
    target: str,
    path: str,
    lines: int = DEFAULT_LOG_LINES,
    user: str | None = None,
) -> dict[str, Any]:
    line_count = max(1, int(lines))
    script = f"""
$path = {json.dumps(path)}
$lines = {line_count}
if (-not (Test-Path $path)) {{
  throw \"log path missing: $path\"
}}
$content = Get-Content -Path $path -Tail $lines | Out-String
[ordered]@{{
  ok = $true
  target = {json.dumps(target)}
  path = $path
  lines = $lines
  content = $content
}} | ConvertTo-Json -Depth 4 -Compress
"""
    return _run_json(target, script, user=user, timeout=max(DEFAULT_TIMEOUT, 45))


@mcp.tool()
def windows_runtime_verify_runtime(
    target: str,
    user: str | None = None,
    hermes_home: str | None = None,
) -> dict[str, Any]:
    script = f"""
$hermesHome = {_windows_hermes_home_expr(hermes_home)}
$pipShow = $null
$mcpInstalled = $false
$hermesVersion = $null
$pythonVersion = $null
$configPath = Join-Path $hermesHome "config.yaml"
try {{
  $pythonVersion = (& python --version) 2>&1 | Out-String
}} catch {{}}
try {{
  $hermesVersion = (& hermes --version) 2>&1 | Out-String
}} catch {{}}
try {{
  $pipShow = (& python -m pip show mcp) 2>&1 | Out-String
  if ($LASTEXITCODE -eq 0 -and $pipShow) {{
    $mcpInstalled = $true
  }}
}} catch {{}}
[ordered]@{{
  ok = $true
  target = {json.dumps(target)}
  hermes_home = $hermesHome
  config_exists = Test-Path $configPath
  python_version = $pythonVersion
  hermes_version = $hermesVersion
  mcp_installed = $mcpInstalled
  pip_show_mcp = $pipShow
}} | ConvertTo-Json -Depth 6 -Compress
"""
    return _run_json(target, script, user=user, timeout=max(DEFAULT_TIMEOUT, 45))


@mcp.tool()
def windows_runtime_restart_scheduled_task(
    target: str,
    task_name: str = "HermesGateway",
    user: str | None = None,
) -> dict[str, Any]:
    script = f"""
$name = {json.dumps(task_name)}
Start-ScheduledTask -TaskName $name
Start-Sleep -Seconds 2
$task = Get-ScheduledTask -TaskName $name -ErrorAction Stop
[ordered]@{{
  ok = $true
  target = {json.dumps(target)}
  task_name = $task.TaskName
  state = [string]$task.State
}} | ConvertTo-Json -Depth 4 -Compress
"""
    return _run_json(target, script, user=user, timeout=max(DEFAULT_TIMEOUT, 45))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
