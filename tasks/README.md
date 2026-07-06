# tasks/ — the scheduled/periodic layer

The bundle's per-platform task suite: the timers and triggers that run alongside the gateway.
Templates are per-OS because the mechanism differs (systemd `--user` timers on Linux, launchd
plists on macOS, Scheduled-Tasks-as-SYSTEM on Windows), but the *jobs* are the same.

## The one that makes long work feel alive: 10-minute progress updates

The flagship: on a long-running task, every ~10 minutes the agent sends the client a short,
human-voiced update on what it's actually working on — never a robotic "tool_call 14" status.
Two parts:

1. **The composer** — [`../bin/progress-compose.py`](../bin/progress-compose.py) (runnable). Given
   the current goal + a summary of recent activity + elapsed minutes, it asks a cheap model to
   write 2-4 natural lines in an operator-to-boss voice, with all machinery (tools, logs, job ids,
   internal names) stripped. Falls back to a templated line if the model is down, so the client
   always gets *something* human.

2. **The trigger** — a workload watchdog or periodic beat that first proves a task is in flight,
   then calls the composer and delivers its output back into the active client thread. This is not
   a blind cron announcement; it is tied to the live workload record for the turn being worked.
   Wire it however your platform schedules recurring work:

   - **Linux** (`linux/`): a systemd `--user` timer on a 10-minute `OnUnitActiveSec`, or a
     `*/10 * * * *` crontab line, gated so it only fires while a task is actually running.
   - **macOS** (`mac/`): a launchd plist with `StartInterval = 600`.
   - **Windows** (`windows/`): a Scheduled Task (run as SYSTEM, absolute python path) on a
     10-minute repeat.

   The gate matters: compose+send should no-op when nothing is running, so an idle agent stays
   quiet (that's the whole notification philosophy — speak on real activity, not on a clock).

## Example (macOS launchd, 10-minute beat)

```xml
<!-- ~/Library/LaunchAgents/<you>.progress-updates.plist -->
<key>ProgramArguments</key>
<array>
  <string>/path/to/python3</string>
  <string>/path/to/bundle/bin/progress-compose.py</string>
  <string>--goal</string>   <string>$(current task goal)</string>
  <string>--elapsed</string><string>$(minutes since task start)</string>
</array>
<key>StartInterval</key><integer>600</integer>
```

Wrap it in a small sender that pulls the live goal/elapsed plus chat/thread identity from your
runtime's task state and pipes the composed text to your chat platform. For Telegram, pass the
thread id through as `message_thread_id` (and `reply_to_message_id` when you have the originating
message), so the update lands inline in the same work thread instead of the group root. Keep the
send idempotent so a client never gets two identical updates back to back.

## Everything else that lives here

Health probes on their own cadence (see `../health-probes/` — self-heal heartbeat, auth self-heal,
canary reconciler), plus any nightly reflection/capture jobs. All quiet-by-default: they act, they
don't narrate.
