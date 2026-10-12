# Prepared runtime contract

Python 3.12. Local upstream source package cua-s1 from trycua/cua commit 9bbfa7dd3e27ca7f1861ede70aaca390174493f9, subdirectory libs/cua-s1/python. Preserve its MIT license. Weights are separate from the code and are never downloaded by this skill.

Tested dependencies: torch 2.14.0 (Linux and Windows CPU build 2.14.0+cpu), numpy 2.5.3, safetensors 0.8.0. Use the CPU backend on the prepared Linux and Windows environments. Do not distribute virtual environments between platforms.

Checkpoint SHA256: 05954c1caf51c2fb6c13ea4acbfc88a2e7653dea192252bb51dc89e76a356ddc. The adjacent cua-s1-forms.json model config must accompany it. Never load the pickle .pt file.

The release artifact contains a pinned source tar and weights/config with a SHA256 manifest. The deployment owner builds an isolated environment, installs the pinned upstream package, validates hashes, and runs the bundled tests with the candidate checkpoint before activation. No automatic installer or persistent service is introduced.

Test command: set PYTHONPATH to scripts and run python -m pytest tests/test_score.py. If the checkpoint is outside ~/models/cua-s1-forms/, set score.DEFAULT_CHECKPOINT in the test runner to that exact verified path before invoking pytest. The production CLI always supports --checkpoint.

Readiness policy: retain existing computer-control route. Activate scorer only on a platform/runtime with installation, capture, action, readback, target-protection, and rollback evidence. Windows ARM64 native and x64-emulated driver 0.22.0 passed isolated capture, fill, readback, and stale/bare/wrong-owner refusals. File-picker keyboard recovery passed on 0.22.0 and 0.28.2. These receipts qualify the tested VM configuration, not every Windows client. Exact scorer installation and runtime binding must be proved separately.

Windows isolated installation passed 13 tests with Python 3.12.10 x64 under Windows ARM64 emulation. The tested profile uses the Microsoft Visual C++ x64 runtime; a clean VM without it failed to load torch c10.dll. Use the signed Microsoft redistributable, then verify import and the bundled tests. The source package uses hatchling as its build backend; an offline install must include that build dependency. Windows native ARM64 Python/scorer execution is not established by this x64 package proof.
