# Migration contract for an agent on the old runtime

## Who this is for

Assume you are reading this from an existing customized Hermes runtime. That
runtime may contain source patches, copied MCP servers, compatibility plugins,
extra memory databases, custom service wrappers, or old comments that describe
problems native Hermes now solves.

Your job is to move the user to current official Hermes with the smallest
possible local layer. Do not port the old implementation forward just because
it exists.

Repository maintainers: these instructions describe a target user's runtime.
Do not run migration commands against the maintainer workstation while merely
editing or reviewing this repository.

## Non-negotiable outcome

- Official `NousResearch/hermes-agent` is the runtime code owner.
- The user's identity, conversations, memory, projects, skills, configuration,
  messaging identity, and credentials survive unchanged unless the user
  explicitly requests a change.
- Native Hermes owns sessions, `state.db`, compaction, memory/search,
  `MEMORY.md`, `USER.md`, per-session task tracking, persistent goals, gateway,
  cron, and service installation.
- Optional browser, Composio, and MCP capabilities are cold until needed. The
  current official Windows installer provisions browser dependencies and
  Chromium, but the capability must remain inactive until it has a consumer.
- The old runtime remains intact and runnable until native verification and a
  rollback rehearsal both pass.

## Do not carry these defaults forward

Do not recreate or reactivate:

- source overlay/apply engines;
- LCM or a second context engine;
- Anamnesis or a second recall database;
- AutoDream/nightly-dream services;
- a separate Telegram transcript database for context recall;
- automatic broad transcript replay;
- permanent browser daemons without a current consumer;
- all MCP servers hot;
- placeholder/no-op compatibility plugins;
- custom durable-runtime or session-resume implementations already native;
- broad GitHub credentials inside the client runtime.

A real client-local capability may be retained only when you identify its live
consumer, owner, data boundary, health proof, and independent rollback.

## Execution sequence

### 1. Inventory without mutation

Record:

- operating system, user, and effective `HERMES_HOME`;
- `command -v hermes` / `Get-Command hermes` and the exact code checkout/SHA;
- dirty/ahead/behind state of the old checkout;
- the process and service definition that launch the live gateway;
- current gateway health, messaging identity, active turns, and restart count;
- hashes of `config.yaml`, service definition, launcher, and environment file;
- on Windows, the relevant User `PATH` and `HERMES_HOME` values;
- paths for `state.db`, `MEMORY.md`, `USER.md`, skills, workspace/projects,
  media, and client-local data;
- legacy plugins, MCPs, databases, cron jobs, daemons, and hooks;
- credential locations by path/name only. Never print secret values.

If the active code route, service owner, runtime home, or credential boundary is
ambiguous, stop. Resolve ownership before changing anything.

### 2. Build a local immutable rollback

Create a timestamped backup outside the code checkout. Preserve, with local-only
permissions:

- the old code SHA and dirty diff/bundle;
- allowlisted config and environment files;
- service definition and launcher or Windows command-route values;
- a SQLite-consistent `state.db` snapshot created with the SQLite backup API,
  plus its integrity result, schema version, and restore procedure;
- state/data manifest and hashes;
- identity/context files, skills, and projects;
- the exact stop/switch/start commands.

Backups may contain credentials only on the same trusted machine. Receipts must
contain hashes and paths, never secret contents. Prove the rollback command can
find every required artifact before proceeding. A raw copy of a live
`state.db` without its WAL state is not a valid backup.

### 3. Install native Hermes in an isolated staging home

Use the official installer, not code from this repository.

Do not run an installer against the live `HERMES_HOME`. The official installers
write config scaffolding, synchronize skills, change permissions, and may set
persistent environment/PATH values in addition to installing code. Create a
separate staging home on the same trusted machine; keep it until the migration
is accepted because it owns the new code and managed dependencies.

The installers can also change which runtime the user-facing `hermes` command
resolves to even when an explicit installation directory is supplied.
Before invoking one, record the current resolved command. On POSIX, preserve
the launcher's path, contents or link target, mode, and hash, and give the
installer a staging-only `HOME` so its launcher, Node shims, and shell-profile
writes cannot reach the live user's route. On Windows, preserve the User and
process `PATH`, `HERMES_HOME`, and `HERMES_GIT_BASH_PATH` values. Immediately
after the installer exits, restore those route inputs and prove `command -v
hermes` / `Get-Command hermes` still reaches the old runtime. Use only the new
runtime's absolute executable path until the controlled switch. Stop if the
old route cannot be restored exactly.

POSIX headless example:

```bash
live_home="${HERMES_HOME:-$HOME/.hermes}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
staging_home="${live_home%/}.native-staging-$stamp"
staging_user_home="$staging_home/user-home"
new_runtime="$staging_home/hermes-agent"
old_hermes="$(command -v hermes)"
old_hermes_target="$(readlink "$old_hermes" 2>/dev/null || printf '%s\n' "$old_hermes")"
old_hermes_mode="$(stat -f '%Lp' "$old_hermes" 2>/dev/null || stat -c '%a' "$old_hermes")"
old_hermes_hash="$(git hash-object "$old_hermes")"
mkdir -p "$staging_user_home"
curl -fsSL https://hermes-agent.nousresearch.com/install.sh \
  | HOME="$staging_user_home" HERMES_HOME="$staging_home" bash -s -- \
      --skip-setup --skip-browser \
      --hermes-home "$staging_home" --dir "$new_runtime"
test "$(command -v hermes)" = "$old_hermes"
test "$(readlink "$old_hermes" 2>/dev/null || printf '%s\n' "$old_hermes")" = "$old_hermes_target"
test "$(stat -f '%Lp' "$old_hermes" 2>/dev/null || stat -c '%a' "$old_hermes")" = "$old_hermes_mode"
test "$(git hash-object "$old_hermes")" = "$old_hermes_hash"
git -C "$new_runtime" rev-parse HEAD
HERMES_HOME="$staging_home" "$new_runtime/venv/bin/hermes" doctor
```

Windows PowerShell example:

```powershell
$LiveHome = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { "$env:LOCALAPPDATA\hermes" }
$Stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$StagingHome = "${LiveHome}.native-staging-$Stamp"
$NewRuntime = Join-Path $StagingHome "hermes-agent"
$Installer = Join-Path $env:TEMP "hermes-install.ps1"
$OldHermes = (Get-Command hermes -ErrorAction Stop).Source
$OldHermesHash = (Get-FileHash $OldHermes -Algorithm SHA256).Hash
$OldUserPath = [Environment]::GetEnvironmentVariable("Path", "User")
$OldUserHermesHome = [Environment]::GetEnvironmentVariable("HERMES_HOME", "User")
$OldUserGitBash = [Environment]::GetEnvironmentVariable("HERMES_GIT_BASH_PATH", "User")
$OldProcessPath = $env:Path
$OldProcessHermesHome = $env:HERMES_HOME
$OldProcessGitBash = $env:HERMES_GIT_BASH_PATH
Invoke-WebRequest https://hermes-agent.nousresearch.com/install.ps1 -OutFile $Installer
try {
    & $Installer -SkipSetup -HermesHome $StagingHome -InstallDir $NewRuntime
} finally {
    [Environment]::SetEnvironmentVariable("Path", $OldUserPath, "User")
    [Environment]::SetEnvironmentVariable("HERMES_HOME", $OldUserHermesHome, "User")
    [Environment]::SetEnvironmentVariable("HERMES_GIT_BASH_PATH", $OldUserGitBash, "User")
    $env:Path = $OldProcessPath
    if ($null -eq $OldProcessHermesHome) { Remove-Item Env:HERMES_HOME -ErrorAction SilentlyContinue } else { $env:HERMES_HOME = $OldProcessHermesHome }
    if ($null -eq $OldProcessGitBash) { Remove-Item Env:HERMES_GIT_BASH_PATH -ErrorAction SilentlyContinue } else { $env:HERMES_GIT_BASH_PATH = $OldProcessGitBash }
}
$RestoredHermes = (Get-Command hermes -ErrorAction Stop).Source
if ($RestoredHermes -ne $OldHermes) { throw "Hermes command route changed: $RestoredHermes" }
if ((Get-FileHash $RestoredHermes -Algorithm SHA256).Hash -ne $OldHermesHash) { throw "Hermes launcher changed" }
if ([Environment]::GetEnvironmentVariable("Path", "User") -ne $OldUserPath) { throw "User PATH was not restored" }
if ([Environment]::GetEnvironmentVariable("HERMES_HOME", "User") -ne $OldUserHermesHome) { throw "User HERMES_HOME was not restored" }
if ([Environment]::GetEnvironmentVariable("HERMES_GIT_BASH_PATH", "User") -ne $OldUserGitBash) { throw "User HERMES_GIT_BASH_PATH was not restored" }
git -C $NewRuntime rev-parse HEAD
try {
    $env:HERMES_HOME = $StagingHome
    & "$NewRuntime\venv\Scripts\hermes.exe" doctor
} finally {
    if ($null -eq $OldProcessHermesHome) { Remove-Item Env:HERMES_HOME -ErrorAction SilentlyContinue } else { $env:HERMES_HOME = $OldProcessHermesHome }
}
```

Record the exact installed SHA. Do not claim “latest” without that proof.
On Windows, the current official installer has no browser-skip option and
installs browser npm dependencies and Playwright Chromium. Treat this as a
packaging exception: do not start or configure browser automation until a real
consumer requires it.

### 4. Subtract before adding

Start with native config and the user's existing identity/data. Prefer, in
order: native Hermes, configuration, an official extension point, then a small
client-local addition. Never begin by copying the old overlay.

Use [DEFAULTS.md](DEFAULTS.md) as the target posture. Classify every old item as
`native`, `keep-client-local`, `cold-optional`, `operator-only`, or `retire`.

### 5. Switch only after route proof

Before the live switch:

- prove zero active turns;
- prove the new `hermes` command imports from the new checkout;
- prove the exact old service definition and rollback command are available;
- preserve the old code and data untouched.

Restore the live runtime's original command/PATH and `HERMES_HOME` immediately
after staging. Invoke the new executable by absolute path with `HERMES_HOME`
explicitly set to the staging home until the final live-data snapshot is
sealed. Only during the controlled switch may the new service be bound to the
live data home.

After draining and stopping the old gateway, create and validate a final
SQLite-consistent `state.db` snapshot. Record the exact commands that stop both
runtimes, move aside the database and its `-wal`/`-shm` sidecars, restore the
snapshot, run an integrity check, and restart the old runtime. Native startup
may migrate the shared database, so a code-only switch is not a rollback.

Drain the old gateway, reinstall the gateway service using the **new** Hermes
command while preserving the prior user/system scope and profile, deliberately
replace the user-facing command route with the new absolute command, then start
it. Do not edit a dirty old checkout in place.

### 6. Immediate verification

Fail and roll back on any failed check:

- `hermes doctor` is clean or has only explicitly accepted optional warnings;
- gateway process imports from the new checkout;
- messaging identity and allowlist are unchanged;
- private inbound/outbound canary succeeds;
- restart and resume succeed;
- two simultaneous topics/sessions do not leak context;
- a low-signal continuation clarifies rather than borrowing another task;
- `MEMORY.md`, `USER.md`, native session search, and `state.db` are readable;
- the built-in task tool works, persistent goals resume, and scheduled work
  survives restart;
- one declared optional tool cold-starts successfully, then stops;
- no retired legacy daemon or second memory/transcript database is active;
- credential/config hashes are unchanged unless explicitly planned;
- rollback rehearsal returns to the old healthy runtime and restore returns to
  native health.

For the rollback rehearsal, first stop native Hermes and take a validated
SQLite-consistent snapshot of its database. Move the native database and
sidecars aside, restore the final pre-switch snapshot, and prove the old runtime
healthy. Stop the old runtime before restoring the native snapshot and service.
Never open the same database from both runtimes or restore over a live gateway.

### 7. Delete only after proof

After the user accepts native health, delete only obsolete **code and service
bindings**. Do not delete identity, memory, sessions, projects, skills, media,
credentials, or historical backups. Keep one sealed rollback until the user's
retention window expires.

## Required completion receipt

Return:

- old and new exact SHAs;
- platform, runtime home, service scope, and active route;
- backup path and manifest hash;
- pre-switch and native `state.db` snapshot hashes, schema versions, and
  integrity results;
- command-route restoration and controlled-cutover results;
- preserved/retired/kept-exception counts;
- every verification result;
- credential/data-change booleans;
- rollback command and rehearsal result;
- any remaining client-local exception with its owner.

“Installed,” “updated,” or “looks healthy” is not completion without this
evidence.
