# Install or update the Bot Doctor Hermes runtime

## Outcome

Move this machine to the exact release in `release.json` without losing the
user's identity, conversations, memory, projects, skills, configuration,
messaging identity, credentials, or unfinished work.

This repository is a public, source-visible release bundle:

- NousResearch Hermes owns the base runtime.
- `upstream.lock` pins the exact compatible upstream commit.
- `runtime-payload-source-manifest.json` names every public runtime-payload
  file by Git blob identity.
- `patches/registry.yaml` explains why each source patch exists and when it can
  be removed.
- `bin/verify-release.py` and `bin/assemble-runtime.py` reproduce the runtime
  that passed the fleet release.

Do not silently substitute upstream `main`, an older public bundle, a private
fleet checkout, or a dirty live runtime.

## First classify the machine

Choose one path from evidence:

- **Fresh:** no live Hermes gateway and no user-owned Hermes data.
- **Existing native/public:** a working Hermes installation already owns the
  gateway and user data.
- **Legacy customized:** the live runtime contains old overlays, copied MCPs,
  duplicate memory/context systems, or ambiguous service wiring.

Never infer the live runtime from a folder name. Record the process, imported
source root, service definition, command route, effective `HERMES_HOME`, config
path, and exact Git state.

## Release preflight

From this repository:

```bash
python3 bin/verify-release.py
```

The command must verify the source blobs, upstream and Golden SHAs, component
digests, and deployment digest. Stop if it fails.

## Fresh POSIX installation

Choose a user-owned profile and an immutable candidate path:

```bash
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
export HERMES_CODEX_401_CIRCUIT_STATE="$HERMES_HOME/state/codex-401-circuit.json"
release_id="$(python3 -c 'import json; print(json.load(open("release.json"))["release"])')"
candidate="$HERMES_HOME/state/runtime-candidates/$release_id"

python3 bin/assemble-runtime.py \
  --output "$candidate" \
  --prepare-home "$HERMES_HOME"
```

`--prepare-home` runs the pinned upstream dependency installer with a separate
installer `HOME`, creates the candidate venv and native profile scaffolding,
then applies and verifies the public Golden payload. It does not install or
restart a gateway.

Run native setup with the candidate:

```bash
HERMES_HOME="$HERMES_HOME" "$candidate/venv/bin/python" -m hermes_cli.main setup
"$candidate/venv/bin/python" bin/install-profile.py \
  --hermes-home "$HERMES_HOME" \
  --runtime-dir "$candidate"
HERMES_HOME="$HERMES_HOME" "$candidate/venv/bin/hermes" doctor
```

Review the doctor output before installing the gateway service. Then use the
candidate's native service command, preserving the intended user/system scope.
Persist both `HERMES_HOME` and `HERMES_CODEX_401_CIRCUIT_STATE` in the native
service definition before accepting it; the shell export alone is not the
service binding:

```bash
HERMES_HOME="$HERMES_HOME" "$candidate/venv/bin/hermes" gateway install --no-start-now
```

On Linux, resolve the installed unit through the pinned runtime. This example
is for the native user scope; use `--scope system`, the runtime's
`get_systemd_unit_path(system=True)`, and the same authority used for native
installation when the proven owner is system scope:

```bash
unit="$(HERMES_HOME="$HERMES_HOME" "$candidate/venv/bin/python" -c \
  'from hermes_cli.gateway import get_systemd_unit_path; print(get_systemd_unit_path())')"
"$candidate/venv/bin/python" bin/bind-service-circuit.py \
  --hermes-home "$HERMES_HOME" \
  --runtime-dir "$candidate" \
  --kind systemd \
  --scope user \
  --definition "$unit"
systemctl --user daemon-reload
systemctl --user start "$(basename "$unit")"
systemctl --user show "$(basename "$unit")" -p Environment
HERMES_HOME="$HERMES_HOME" "$candidate/venv/bin/hermes" gateway status
```

On macOS, update the exact native plist, reload that plist directly so the
native refresh path cannot discard the added environment value, and inspect
the loaded definition:

```bash
plist="$(HERMES_HOME="$HERMES_HOME" "$candidate/venv/bin/python" -c \
  'from hermes_cli.gateway import get_launchd_plist_path; print(get_launchd_plist_path())')"
label="$(HERMES_HOME="$HERMES_HOME" "$candidate/venv/bin/python" -c \
  'from hermes_cli.gateway import get_launchd_label; print(get_launchd_label())')"
"$candidate/venv/bin/python" bin/bind-service-circuit.py \
  --hermes-home "$HERMES_HOME" \
  --runtime-dir "$candidate" \
  --kind launchd \
  --definition "$plist"
launchctl bootout "gui/$UID/$label"
launchctl bootstrap "gui/$UID" "$plist"
launchctl print "gui/$UID/$label"
HERMES_HOME="$HERMES_HOME" "$candidate/venv/bin/hermes" gateway status
```

The binding helper prints a backup path. Re-run it after any native gateway
reinstall. Roll it back with `--hermes-home "$HERMES_HOME" --restore-backup
/exact/backup/path`, then reload the same service definition.

Do not configure Telegram, a provider, or a connected service with credentials
the user has not supplied.

## Fresh or staged Windows installation

Use the pinned upstream PowerShell installer to create an isolated clean
candidate first. Preserve the prior User and process `PATH`, `HERMES_HOME`, and
`HERMES_GIT_BASH_PATH` when this is an upgrade. Use a clean release checkout
created with `git -c core.autocrlf=false clone <release-repository-url>` so
payload files retain their canonical LF bytes. Verification accepts a normal
Git-for-Windows CRLF checkout only when LF normalization reproduces every
declared identity.

Set `$InstallMode`, `$ProvenHermesHome`, and `$ProvenServiceOwner` from the
evidence collected in **First classify the machine**. For an existing install,
the home and service owner must come from the live process and service
definition. For a fresh install, prove there is no live gateway or Hermes user
data. Stop on missing, contradictory, or ambiguous evidence.

```powershell
$InstallMode = "<fresh-or-existing>"
$ProvenHermesHome = "<absolute-evidence-backed-HERMES_HOME>"
$ProvenServiceOwner = "<evidence-backed-user-system-or-task-owner>"
if ($InstallMode -notin @("fresh", "existing")) {
  throw "InstallMode must be explicitly fresh or existing"
}
if ($ProvenHermesHome.StartsWith("<") -or -not [IO.Path]::IsPathRooted($ProvenHermesHome)) {
  throw "ProvenHermesHome must be an evidence-backed absolute path"
}
if ($ProvenServiceOwner.StartsWith("<") -or [string]::IsNullOrWhiteSpace($ProvenServiceOwner)) {
  throw "ProvenServiceOwner must identify the inspected service scope"
}
$CurrentOperator = [Security.Principal.WindowsIdentity]::GetCurrent().Name
if ($InstallMode -eq "fresh" -and $ProvenServiceOwner -ne $CurrentOperator) {
  throw "Run fresh activation as the intended native gateway owner"
}
$Release = Get-Content .\release.json -Raw | ConvertFrom-Json
$LiveHome = [IO.Path]::GetFullPath($ProvenHermesHome)
$Candidate = Join-Path $LiveHome ("state\runtime-candidates\" + $Release.release)
$StagingHome = Join-Path $env:TEMP ("botdoctor-hermes-staging-" + $Release.release)
$ProfileHome = if ($InstallMode -eq "existing") { $StagingHome } else { $LiveHome }
if ($InstallMode -eq "fresh" -and (Test-Path (Join-Path $LiveHome "config.yaml"))) {
  throw "Fresh classification conflicts with existing Hermes data"
}
if ($InstallMode -eq "existing" -and -not (Test-Path (Join-Path $LiveHome "config.yaml"))) {
  throw "Existing classification conflicts with the proven profile"
}
$Installer = Join-Path $env:TEMP "hermes-install.ps1"
$InstallerUrl = "https://raw.githubusercontent.com/NousResearch/hermes-agent/3ef6bbd201263d354fd83ec55b3c306ded2eb72a/scripts/install.ps1"
$ExpectedInstallerSha256 = "b5bdf0e959677de0168f8cfb5f9175c7b57adf5c4319a1c2fc9bec1f46fbdb6e"
$PriorProcessPath = $env:PATH
$PriorProcessHermesHome = $env:HERMES_HOME
$PriorProcessGitBashPath = $env:HERMES_GIT_BASH_PATH
$PriorProcessCodexCircuitState = $env:HERMES_CODEX_401_CIRCUIT_STATE
$PriorUserPath = [Environment]::GetEnvironmentVariable("PATH", "User")
$PriorUserHermesHome = [Environment]::GetEnvironmentVariable("HERMES_HOME", "User")
$PriorUserGitBashPath = [Environment]::GetEnvironmentVariable("HERMES_GIT_BASH_PATH", "User")

Invoke-WebRequest $InstallerUrl -OutFile $Installer
$ActualInstallerSha256 = (Get-FileHash -Algorithm SHA256 $Installer).Hash.ToLowerInvariant()
if ($ActualInstallerSha256 -ne $ExpectedInstallerSha256) {
  throw "Pinned installer digest mismatch: $ActualInstallerSha256"
}
try {
  & $Installer -SkipSetup -Commit $Release.canonical_upstream_sha `
    -HermesHome $ProfileHome -InstallDir $Candidate
} finally {
  $env:PATH = $PriorProcessPath
  $env:HERMES_HOME = $PriorProcessHermesHome
  $env:HERMES_GIT_BASH_PATH = $PriorProcessGitBashPath
  [Environment]::SetEnvironmentVariable("PATH", $PriorUserPath, "User")
  [Environment]::SetEnvironmentVariable("HERMES_HOME", $PriorUserHermesHome, "User")
  [Environment]::SetEnvironmentVariable("HERMES_GIT_BASH_PATH", $PriorUserGitBashPath, "User")
}
& "$Candidate\venv\Scripts\python.exe" .\bin\assemble-runtime.py `
  --output $Candidate --use-existing-clean-runtime
```

For a fresh machine, finish native setup and activate the native gateway:

Persist `HERMES_HOME` and `HERMES_CODEX_401_CIRCUIT_STATE` in the generated
native gateway task after installation and before it starts. Verify the task
and launcher resolve `$ProvenServiceOwner`, `$LiveHome`, and the profile-scoped
circuit path before accepting `gateway status`.

```powershell
if ($InstallMode -ne "fresh") { throw "Use the staged existing-install path instead" }
$env:HERMES_HOME = $LiveHome
$env:HERMES_CODEX_401_CIRCUIT_STATE = Join-Path $LiveHome "state\codex-401-circuit.json"
try {
  & "$Candidate\venv\Scripts\python.exe" -m hermes_cli.main setup
  & "$Candidate\venv\Scripts\python.exe" .\bin\install-profile.py `
    --hermes-home $LiveHome --runtime-dir $Candidate
  & "$Candidate\venv\Scripts\hermes.exe" doctor
  & "$Candidate\venv\Scripts\hermes.exe" gateway install --no-start-now
  $TaskName = (& "$Candidate\venv\Scripts\python.exe" -c `
    "from hermes_cli.gateway_windows import get_task_name; print(get_task_name())").Trim()
  $GatewayCmd = (& "$Candidate\venv\Scripts\python.exe" -c `
    "from hermes_cli.gateway_windows import get_task_script_path; print(get_task_script_path())").Trim()
  $GatewayVbs = [IO.Path]::ChangeExtension($GatewayCmd, ".vbs")
  & "$Candidate\venv\Scripts\python.exe" .\bin\bind-service-circuit.py `
    --hermes-home $LiveHome --runtime-dir $Candidate --kind windows `
    --cmd-launcher $GatewayCmd --vbs-launcher $GatewayVbs --task-name $TaskName
  schtasks.exe /Run /TN $TaskName
  & "$Candidate\venv\Scripts\hermes.exe" gateway status
} finally {
  $env:HERMES_HOME = $PriorProcessHermesHome
  $env:HERMES_CODEX_401_CIRCUIT_STATE = $PriorProcessCodexCircuitState
}
```

For an existing installation, prove only the isolated staged profile here:

```powershell
if ($InstallMode -ne "existing") { throw "Use the fresh-install activation path instead" }
& "$Candidate\venv\Scripts\python.exe" .\bin\install-profile.py `
  --hermes-home $StagingHome --runtime-dir $Candidate --initialize-staging
$env:HERMES_HOME = $StagingHome
$env:HERMES_CODEX_401_CIRCUIT_STATE = Join-Path $StagingHome "state\codex-401-circuit.json"
try {
  & "$Candidate\venv\Scripts\hermes.exe" doctor
} finally {
  $env:HERMES_HOME = $PriorProcessHermesHome
  $env:HERMES_CODEX_401_CIRCUIT_STATE = $PriorProcessCodexCircuitState
}
```

On an existing installation, the installer must run against an isolated staging
`HermesHome`, and its User/process environment changes must be restored before
the live cutover. Do not run `install-profile.py` against `$LiveHome` until the
controlled-cutover step after preservation and quiescence. Never let staging
change the currently resolved `hermes` command.

## Existing installation: preserve before changing

Before any live write, create an immutable local rollback outside the code
checkout:

- old code SHA plus dirty diff/bundle;
- config, environment file, service definition, launcher, and command route;
- a SQLite-consistent `state.db` snapshot made with SQLite's backup API;
- identity, context, skills, projects, and client-local data;
- hashes, schema/integrity result, and exact stop/restore/start commands.

Receipts contain paths and hashes, never credential values. A raw copy of a
live SQLite file without its WAL state is not a valid backup.

Build the candidate against a separate staging profile:

```bash
staging_home="$HERMES_HOME/state/setup-staging/$release_id"
python3 bin/assemble-runtime.py \
  --output "$candidate" \
  --prepare-home "$staging_home"
"$candidate/venv/bin/python" bin/install-profile.py \
  --hermes-home "$staging_home" \
  --runtime-dir "$candidate" \
  --initialize-staging
```

Prove the candidate's doctor, imports, tool discovery, and profile files in
staging. Do not patch a dirty live checkout in place.

## Maintenance-quiescence contract

Before switching an existing gateway, make the pending update visible to the
active agent at a safe turn/tool boundary:

> Maintenance is pending. Finish the current atomic step and avoid starting new
> delegated work. If the task cannot finish before the deadline, persist the
> exact next action and blockers.

Persist the release identity, reason, requested time, deadline, and current
phase in the runtime's local maintenance state. This is context, not a broad
behavior prompt and not permission delegated to the model.

Stop admitting new executable work while still durably accepting inbound
messages by session/topic. Inspect every operation the runtime owns:

- foreground turns and active tools;
- child, delegated, durable, and asynchronous jobs;
- cron, API, desktop, voice, and media work;
- compaction/compression leases;
- inbound queues and outbound delivery transactions;
- startup recovery and replay claims.

Switch only when each item has finished or has an exact durable, replayable
checkpoint. Require the same ready result twice across a stable interval. If
state is stale, contradictory, unidentified, or not replayable, leave this
client on the old runtime. Never kill uncertain work to keep an update moving.

## Controlled cutover

At readiness:

1. Take the final SQLite-consistent snapshot and verify rollback.
2. Run `bin/install-profile.py` against the live `HERMES_HOME`; it creates a
   local per-file rollback and does not switch the service.
3. Stop the old gateway through its actual service owner.
4. Bind that same service scope/profile to the candidate's absolute Python and
   source root, with `HERMES_HOME` set to the proven live profile and
   `HERMES_CODEX_401_CIRCUIT_STATE` set to
   `$HERMES_HOME/state/codex-401-circuit.json` (or the equivalent Windows
   path). Run `bin/bind-service-circuit.py` against the exact native-generated
   systemd unit, launchd plist, or Windows CMD/VBS launchers before start, then
   reload that definition through its actual service owner.
5. Start the candidate and prove the old generation is absent.
6. Restore durable checkpoints and replay accepted messages exactly once before
   reopening admission.

Do not open the same database from two generations.

The profile receipt prints its backup path. Its bounded rollback is:

```bash
python3 bin/install-profile.py \
  --hermes-home "$HERMES_HOME" \
  --restore-backup /exact/path/from/the/profile-receipt
```

This restores only files and configuration owned by that profile-install run;
service and database rollback remain the separately captured cutover rollback.
The service-binding helper prints its own backup path. Restore it with:

```bash
python3 bin/bind-service-circuit.py \
  --hermes-home "$HERMES_HOME" \
  --restore-backup /exact/service-binding-backup/path
```

Reload the same systemd unit, launchd plist, or Windows task after restoring.

## Acceptance proof

Fail and roll back on any failed required check:

- `bin/verify-release.py --runtime-dir <candidate>` passes;
- `hermes doctor` has no unaccepted required failure;
- process imports resolve only inside the candidate root;
- messaging identity and allowlist are unchanged;
- a private inbound/outbound turn succeeds;
- restart and continuation succeed without duplicates;
- two chats/topics do not leak context;
- native memory/search, built-in tasks, goals, and cron remain readable;
- native Tool Search discovers a cold approved capability;
- the capability router is hot while optional backends remain cold;
- queued inbound and unfinished delivery records replay exactly once;
- retired duplicate memory/context daemons are absent;
- rollback returns to the old healthy runtime and restore returns to the new
  healthy runtime.

## What not to carry forward

Do not recreate LCM, Anamnesis, AutoDream/nightly dream, a second transcript
recall database, automatic broad transcript replay, all MCPs hot, permanent
browser daemons, placeholder plugins, copied upstream source, or broad client
GitHub credentials. Retain a local capability only when its consumer, owner,
data boundary, health proof, and rollback are known.

## Completion receipt

Return the old and new release identities, platform, live/staging homes, service
scope, active route, backup and manifest hashes, database integrity results,
quiescence observations, verification results, rollback rehearsal, and every
remaining local exception.

“Installed,” “updated,” or “looks healthy” is not completion evidence.
