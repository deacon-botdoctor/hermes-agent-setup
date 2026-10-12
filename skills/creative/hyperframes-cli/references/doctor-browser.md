# doctor, browser

Environment diagnosis and bundled-Chrome management. Run these first when a render or preview fails.

## doctor

```bash
npx hyperframes doctor
npx hyperframes doctor --json     # CI / agent output (always exit 0; gate on payload `ok`)
```

Runs independent checks and reports each as ok/warn/fail:

- **Version** — installed CLI vs latest on npm (hints upgrade when stale)
- **Node.js** — ≥ 22 required
- **CPU**, **Memory**, **Disk** — host resources
- **Environment** — env vars that affect the renderer
- **FFmpeg** / **FFprobe** — found, version, codecs
- **Chrome** — bundled or system, version, path
- **Docker** / **Docker running** — required only for `render --docker`
- **/dev/shm** — inside containers only

Run `doctor` first when:

- `render` fails with a Chrome or FFmpeg error.
- `preview` opens but the composition fails to load.
- A fresh machine has never run HyperFrames.

Common issues:

- **Missing FFmpeg** — install via `brew install ffmpeg` (macOS) or your package manager.
- **Missing bundled Chrome** — run `npx hyperframes browser ensure`.
- **Low memory** — close other Chromes, reduce `--workers`, or use `--quality draft`.
- **Chrome exits instantly inside an agent sandbox (macOS)** — seatbelt-style sandboxes
  (e.g. codex `workspace-write`) block Chromium's Mach port bootstrap
  (`MachPortRendezvous`; openai/codex#21292), so every Chrome — bundled, system, or
  headless shell — dies at startup. This is a host-level block, not a HyperFrames or
  Chrome install problem: compile checks and audio still pass, only rendering is
  unavailable. State the blocker and deliver the checked composition; render outside the
  sandbox or via `render --docker` / cloud rendering where available. **Do not build a
  substitute rasterizer** (magick/PIL/SVG frame pipelines) — on a blocked-browser host
  the deliverable IS the checked composition plus this blocker note, and rendering is
  handed to `--docker`, cloud, or the user. Write your final summary the moment the
  blocker is identified, BEFORE any optional fallback work: a later session failure must
  not erase the report of work already done.

## Linux Snap Chromium: audio effects cannot open their temporary host

If an audio-effect render fails with `audio_processing_failed` and the underlying
error is `net::ERR_FILE_NOT_FOUND` for `file:///tmp/hf-fx-host-.../audio-fx.html`,
check whether the selected Chromium is a Snap package. Its private `/tmp` can hide
HyperFrames' host-created file. Ordinary audio may still render successfully.

For this specific failure, use a fresh, nonhidden temporary directory directly under
the calling tenant's home, scoped to the render command. A hidden `~/.cache` directory
can also be inaccessible. Preserve the project's pinned CLI version or wrapper.
The example below was verified with HyperFrames 0.7.108 and Snap Chromium on Linux:

```python
import os
from pathlib import Path
import subprocess
import tempfile

# Set project and output to this render's paths before running.
with tempfile.TemporaryDirectory(prefix="hyperframes-render-", dir=Path.home()) as render_tmp:
    subprocess.run(
        ["npx", "--yes", "hyperframes@0.7.108", "render", project,
         "--strict", "--quality", "draft", "--output", output],
        env={**os.environ, "TMPDIR": render_tmp},
        check=True,
    )
```

`TemporaryDirectory` creates a private directory and removes only that directory after
the renderer exits. Keep the output outside it. Verify the resulting video and audio
with ffprobe and inspect a rendered frame. Do not globally change `TMPDIR`, disable
browser confinement, or change browser lanes to recover this error.

## browser

```bash
npx hyperframes browser ensure    # find or download the pinned Chrome
npx hyperframes browser path      # print the browser executable path (for scripting)
npx hyperframes browser clear     # remove the cached Chrome download
```

Manage the Chrome build HyperFrames uses for rendering. The pinned version exists because pixel output drifts across Chrome versions — using the bundled build keeps rendered output reproducible across machines.

Use `path` to embed the binary in scripts: `$(npx hyperframes browser path)`.
