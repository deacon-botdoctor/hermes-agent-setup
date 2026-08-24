# Host Health

<!-- HERMES_HOST_HEALTH_v1:START -->

Treat the machine as a finite working environment. A healthy agent plans work against observed disk and memory headroom instead of discovering limits after a job fails.

## Before heavy work

For work expected to create, download, copy, render, unpack, or transform more than `machine_profile.limits.large_job_estimate_bytes` (5 GiB by default), or to sustain high disk, CPU, or memory use:

1. Read `state/local-selfcheck-latest.json` under the active Hermes home.
2. If it is missing or more than 30 minutes old, run the installed `bin/hermes-local-selfcheck.py` first.
3. Read `machine_profile.storage`, `machine_profile.memory`, `machine_profile.pressure`, and `machine_profile.limits`. Estimate input, output, temporary, and rollback space before starting.
4. Treat `max_concurrent_large_jobs` as a planning recommendation, not a hard gate. Explain the headroom and execution plan when exceeding it.

## Pressure behavior

- Machine-health evidence never automatically blocks a requested job. If disk, capacity, or pressure warns/fails, explain the current headroom, likely risk, and checkpoint strategy, then continue unless the user directs otherwise or execution becomes impossible.
- Treat `disk_warn_new_payload_limit_bytes` as the point where extra inspection and explanation are required, not as a prohibition.
- If `machine_profile.pressure.large_job_posture` is `inspect`, review `top_memory_processes` and `oldest_processes`, inspect bounded task-owned storage and retention receipts, and tell the user what is consuming resources. Allocated swap and active swap churn are distinct signals; neither authorizes claiming current thrashing without activity evidence.
- A `swap_pressure_status` of `fail` requires immediate Doc attention; `warn` requires a Doc investigation packet. Doc may repair confirmed task-owned leaks or policy-covered storage while the client agent continues the requested job with checkpoints.
- Prefer bounded cleanup already covered by retention policy. Never delete unique client data, active runtimes, rollback targets, or protected dependencies.
- Never reboot the host or stop unknown, user-owned, gateway, or interactive processes solely to clear swap. Doc may investigate and use only an already authorized, policy-safe repair lane.
- A machine-specific client overlay may set stricter limits and exact archive destinations. The stricter rule wins.

## Storage topology

Golden never assumes an external disk exists. Use external or archival storage only when a client-specific overlay declares the exact target and, where available, its stable volume identity. A path alone is not proof that the intended volume is mounted.

## Ownership

The local agent owns safe behavior. Doc independently verifies that the self-check is installed, scheduled, fresh, and structurally valid, then escalates persistent failures. Neither role gains authority to perform destructive cleanup or move client data merely because pressure is observed.

<!-- HERMES_HOST_HEALTH_v1:END -->
