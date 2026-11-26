"""Optional ComputerUseProvider; dormant unless computer_use.backend is wayvnc."""
from tools.computer_use.backend import ComputerUseProvider

from .backend import WayVncBackend, Settings
from tools.bot_desktop.external import ExternalScreenError


class WayVncProvider(ComputerUseProvider):
    name = "wayvnc"
    @property
    def display_name(self):
        return "WayVNC (foreground RFB desktop)"

    def create_backend(self, *, permission_mode: str):
        return WayVncBackend(Settings.load(), permission_mode=permission_mode)

    def is_available(self):
        # Metadata/filesystem only. A true result is not connectivity/readiness proof.
        try:
            Settings.load().validate()
            return True
        except (OSError, ValueError, TypeError, KeyError, ExternalScreenError):
            return False

    def doctor(self):
        import subprocess
        from pathlib import Path
        try:
            settings = Settings.load()
            settings.validate()
            result = subprocess.run(
                [settings.python, "-I", "-B", str(Path(__file__).with_name("rfb_worker.py")), "--check"],
                timeout=5, capture_output=True, text=True, check=False,
            )
            print(result.stdout.strip() or result.stderr.strip())
            print("Filesystem/dependency check only; no RFB connection or input was attempted.")
            return result.returncode
        except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired, ExternalScreenError) as exc:
            print(f"WayVNC unavailable: {exc}")
            return 1


def register(ctx):
    ctx.register_computer_use_provider(WayVncProvider())
