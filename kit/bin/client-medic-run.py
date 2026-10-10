#!/usr/bin/env python3
"""Opt-in tenant-local Linux and macOS medic. Uses fixed probes/actions; never accepts shell commands."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from client_medic import Store, identifier, atomic_json, epoch

CONFIG = "config/client-medic.json"
PROTECTED_CONFIG_TYPES = {
    "model.provider": str,
    "model.default": str,
    "agent.gateway_notify_interval": int,
    "display.long_running_notifications": bool,
    "display.platforms.telegram.long_running_notifications": bool,
    "display.platforms.telegram.cleanup_progress": bool,
    "display.platforms.telegram.progress_on_typing": bool,
}


def validate_protected_config(expected):
    if not isinstance(expected, dict) or set(expected) - {"telegram.proxy_url"} != set(PROTECTED_CONFIG_TYPES):
        raise ValueError("invalid protected config fields")
    if any(type(expected[key]) is not kind for key, kind in PROTECTED_CONFIG_TYPES.items()):
        raise ValueError("invalid protected config value")
    if "telegram.proxy_url" in expected:
        relay_port(expected["telegram.proxy_url"])


def relay_port(url):
    from urllib.parse import urlsplit
    if not isinstance(url, str):
        raise ValueError("invalid Telegram relay")
    parsed = urlsplit(url)
    if (parsed.scheme != "socks5" or parsed.hostname != "127.0.0.1" or parsed.username is not None
            or parsed.password is not None or parsed.path or parsed.query or parsed.fragment
            or parsed.port is None or not 1 <= parsed.port <= 65535):
        raise ValueError("invalid Telegram relay")
    return parsed.port


def telegram_relay_health(url):
    """Prove the approved loopback SOCKS route and Telegram TLS, without bot traffic."""
    import socket
    import ssl
    port = relay_port(url)
    deadline = time.monotonic() + 5
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as stream:
            def read_exact(size):
                data = b""
                while len(data) < size:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("relay probe deadline")
                    stream.settimeout(remaining)
                    chunk = stream.recv(size - len(data))
                    if not chunk:
                        raise OSError("relay closed")
                    data += chunk
                return data
            stream.sendall(b"\x05\x01\x00")
            if read_exact(2) != b"\x05\x00":
                return "failed"
            host = b"api.telegram.org"
            stream.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + (443).to_bytes(2, "big"))
            header = read_exact(4)
            if header[:3] != b"\x05\x00\x00":
                return "failed"
            if header[3] == 1:
                size = 4
            elif header[3] == 4:
                size = 16
            elif header[3] == 3:
                size = read_exact(1)[0]
            else:
                return "failed"
            read_exact(size + 2)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "failed"
            stream.settimeout(remaining)
            with ssl.create_default_context().wrap_socket(stream, server_hostname="api.telegram.org"):
                return "healthy"
    except (OSError, ValueError):
        return "failed"


def protected_config_health(home, expected):
    """Compare only reviewed nonsecret settings; never learn a baseline from drift."""
    validate_protected_config(expected)
    try:
        import yaml
    except ImportError:
        return "unknown"
    try:
        path = home / "config.yaml"
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            return "unknown"
        actual = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(actual, dict):
            return "unknown"
    except (OSError, ValueError, yaml.YAMLError):
        return "unknown"
    for key, wanted in expected.items():
        value = actual
        for part in key.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if type(value) is not type(wanted) or value != wanted:
            return "failed"
    return "healthy"


def gateway_signals(home, now):
    """Observe service degradation through existing probes, without repair authority."""
    mod = gateway_module()
    if not mod.telegram_transport_expected(home):
        return {}
    state = mod.load_json(home / "gateway_state.json", {})
    stamp = mod.parse_iso(state.get("updated_at"))
    fresh = stamp is not None and 0 <= now - stamp.timestamp() <= 600
    ingress = ((state.get("platforms") or {}).get("telegram") or {}).get("ingress")
    signals = {"telegram_ingress": "unknown", "telegram_progress": "unknown"}
    if fresh and isinstance(ingress, dict) and type(ingress.get("stalled")) is bool:
        signals["telegram_ingress"] = "failed" if ingress["stalled"] else "healthy"
    if fresh and mod.pid_alive(state.get("pid")):
        health = mod.gateway_transaction_health(home)
        if health.get("pid") == state.get("pid"):
            telegram = health.get("telegram") or {}
            if telegram.get("stalled", 0) > 0 or telegram.get("recent_failures", 0) > 0:
                signals["telegram_progress"] = "failed"
            elif health.get("database_status") == "pass" and telegram.get("status") in {"pass", "unverified"}:
                signals["telegram_progress"] = "healthy"
    path = Path(__file__).with_name("probe-client-health.py")
    spec = importlib.util.spec_from_file_location("medic_transport_observer", path)
    probe_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe_module)
    transport = probe_module.collect_telegram_transport_health(
        home / "logs/gateway.log", mod.parse_iso(state.get("started_at")),
        datetime.fromtimestamp(now, timezone.utc),
    )
    signals["telegram_transport"] = {"warn": "failed", "pass": "healthy"}.get(transport["status"], "unknown")
    return signals


def read_json(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise ValueError("invalid medic input file")
    return json.loads(path.read_text())


def policy(home):
    p = home / CONFIG
    if not p.exists() and not p.is_symlink():
        return None
    if any(parent.is_symlink() for parent in (home, p.parent)):
        raise ValueError("symlink policy root")
    value = read_json(p)
    if not isinstance(value, dict) or set(value) - {"protected_config"} != {"schema", "mode", "target", "profile_root", "uid", "services"}:
        raise ValueError("invalid medic policy fields")
    if value["schema"] != "client-medic-policy/v1" or value["mode"] not in {"off", "shadow", "on"}:
        raise ValueError("invalid medic policy")
    if p.stat().st_uid != os.getuid() or p.stat().st_mode & 0o022:
        raise ValueError("policy ownership or permissions invalid")
    if value["uid"] != os.getuid() or value["profile_root"] != str(home) or home != Path.home() / ".hermes":
        raise ValueError("medic policy does not bind this tenant")
    identifier(value["target"])
    if not isinstance(value["services"], list) or not 1 <= len(value["services"]) <= 2:
        raise ValueError("invalid services")
    if "protected_config" in value:
        # Validate the fixed field/type contract without storing config values in the ledger.
        validate_protected_config(value["protected_config"])
    seen = set()
    for service in value["services"]:
        if not isinstance(service, dict) or set(service) != {"id", "unit", "desired_state"}:
            raise ValueError("invalid service fields")
        allowed = ({"gateway": "ai.hermes.gateway"} if platform.system() == "Darwin" else
                   {"gateway": "hermes-gateway.service", "test-worker": "hermes-medic-test-worker.service"})
        if service["id"] not in allowed or service["unit"] != allowed[service["id"]] or service["id"] in seen:
            raise ValueError("unregistered service")
        if service["desired_state"] not in {"running", "paused"}:
            raise ValueError("invalid desired state")
        seen.add(service["id"])
    return value


def gateway_module():
    path = Path(__file__).with_name("hermes-gateway-watchdog-client.py")
    spec = importlib.util.spec_from_file_location("medic_gateway_observer", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def probe(home, service, now, runner=subprocess.run, *, observation=None):
    if service["id"] == "gateway":
        if observation is None:
            mod = gateway_module()
            state = mod.load_json(home / "gateway_state.json", {})
            transaction = mod.gateway_transaction_health(home) if mod.pid_alive(state.get("pid")) else {}
            observation = mod.classify_gateway(
                state, now=datetime.fromtimestamp(time.time() if now is None else now, timezone.utc),
                heartbeat_max_age=600, transaction_health=transaction,
                telegram_required=mod.telegram_transport_expected(home),
            )
        if observation["health"] == "healthy":
            return "healthy"
        if observation["health"] == "outage":
            # Missing gateway state alone does not prove a stopped service.
            if platform.system() == "Darwin":
                unit = runner(
                    ["launchctl", "print", f"gui/{os.getuid()}/{service['unit']}"],
                    capture_output=True, timeout=10, text=True,
                )
                # A missing/unreadable launchd binding is not proof of a stopped service.
                if unit.returncode == 0 and any(
                    line.strip() == "state = not running" for line in unit.stdout.splitlines()
                ):
                    return "failed"
            else:
                unit = runner(
                    ["systemctl", "--user", "is-active", service["unit"]], capture_output=True, timeout=10, text=True
                )
                if unit.returncode == 3 and unit.stdout.strip() in {"inactive", "failed"}:
                    return "failed"
        return "unknown"
    result = runner(["systemctl", "--user", "is-active", service["unit"]], capture_output=True, timeout=10, text=True)
    if result.returncode == 3 and result.stdout.strip() in {"inactive", "failed"}:
        return "failed"
    if result.returncode != 0 or result.stdout.strip() != "active":
        return "unknown"
    try:
        progress = read_json(home / "state/medic-test-worker-progress.json")
        # Progress can advance during service I/O. Compare it with the read time.
        observed_now = time.time() if now is None else now
        return (
            "healthy"
            if set(progress) == {"completed_at"} and 0 <= observed_now - epoch(progress["completed_at"]) <= 600
            else "unknown"
        )
    except (OSError, ValueError, TypeError):
        return "unknown"


def binding(home):
    path = home / "state/runtime-binding.json"
    value = read_json(path)
    if value.get("kind") != "botdoctor_runtime_binding" or value.get("status") != "active":
        raise ValueError("runtime generation is unverified")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def guard_and_repair(home, config, service, store, episode, generation, now, runner=subprocess.run):
    """Hold the existing mutation lease across recheck and side effect."""
    import fcntl
    if config["mode"] != "on" or service["desired_state"] != "running":
        return "shadow" if config["mode"] == "shadow" else "paused"
    directory = home / "state/promotion/executor"
    directory.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        for name in ("runtime-mutation.lock", "active-rollout.lock"):
            path = directory / name
            if path.is_symlink():
                raise ValueError("symlink lease")
            f = stack.enter_context(path.open("a+"))
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return "busy"
            if name == "runtime-mutation.lock":
                mutation_fd = f.fileno()
        if binding(home) != generation or policy(home) != config:
            return "generation_changed"
        # A pending rollout checkpoint remains authoritative even if its process exited.
        if list((home / "state/fleet-rollouts").glob("*/rollback-transaction.pending")):
            return "rollout_pending"
        # This fence lives outside the medic backup directory. A reverted DB
        # must not create fresh repair budget after a restore.
        fence_path = directory / "client-medic-fence.json"
        cursor = store.db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
        if fence_path.exists():
            fence = read_json(fence_path)
            if (
                fence.get("generation") != store.meta("generation")
                or type(fence.get("cursor")) is not int
                or fence["cursor"] > cursor
            ):
                raise ValueError("medic restore requires reconciliation")
        current = probe(home, service, now, runner)
        if current != "failed":
            return "no_longer_failed"
        action_id = store.reserve(episode, "restart_" + service["id"], now=now)
        if action_id is None:
            return "already_reserved"
        cursor = store.db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
        atomic_json(fence_path, {"generation": store.meta("generation"), "cursor": cursor})
        environment = dict(os.environ)
        for key in (
            "HERMES_SAFE_RESTART_FORCE",
            "HERMES_SAFE_RESTART_DRY_RUN",
            "HERMES_RUNTIME_MUTATION_LEASE_HELD",
            "HERMES_RUNTIME_MUTATION_LEASE_FD",
        ):
            environment.pop(key, None)
        environment.update(HERMES_HOME=str(home), HERMES_GATEWAY_UNIT=service["unit"])
        if service["id"] == "gateway":
            helper = home / "bin/hermes-safe-restart.sh"
            if (
                helper.is_symlink()
                or not helper.is_file()
                or helper.stat().st_uid != os.getuid()
                or helper.stat().st_mode & 0o022
            ):
                store.result(action_id, "failed", now=now)
                return "unsafe_helper"
            # The established helper expects the inherited mutation lease on FD 9.
            wrapper = "import os,sys; os.dup2(int(sys.argv[1]),9,inheritable=True); os.environ.update(HERMES_RUNTIME_MUTATION_LEASE_HELD='1',HERMES_RUNTIME_MUTATION_LEASE_FD='9'); os.execvpe('bash',['bash',sys.argv[2],'gateway'],os.environ)"
            command = [sys.executable, "-c", wrapper, str(mutation_fd), str(helper)]
            kwargs = {"pass_fds": (mutation_fd,)}
        else:
            command = ["systemctl", "--user", "restart", service["unit"]]
            kwargs = {}
        try:
            result = runner(command, env=environment, capture_output=True, text=True, timeout=300, **kwargs)
        except BaseException:
            # Keep durable pending intent. A timeout or process loss is not proof of no side effect.
            raise
        outcome = "completed" if result.returncode == 0 else "failed"
        store.result(action_id, outcome, now=now)
        return outcome


def run(home, *, now=None, runner=subprocess.run, gateway_observation=None):
    home = Path(home).absolute()
    config = policy(home)
    if config is None or config["mode"] == "off":
        return {"mode": "off"}
    if platform.system() not in {"Linux", "Darwin"}:
        raise ValueError("medic activation supports Linux and macOS only")
    probe_now = now  # None keeps live probes on the current clock; explicit times are fixtures.
    now = time.time() if now is None else now
    store = Store(home / "state/medic", config["target"])
    try:
        store.cycle(complete=False, now=now)
        complete = True
        actions = []
        for service in config["services"]:
            if service["desired_state"] == "paused":
                complete = False
                continue
            status = (
                probe(home, service, probe_now, runner)
                if service["id"] != "gateway" or gateway_observation is None
                else probe(home, service, probe_now, runner, observation=gateway_observation)
            )
            complete = complete and status != "unknown"
            now = time.time() if probe_now is None else probe_now
            row = store.observe(service["id"], "availability", status, f"cycle:{now:.6f}", now, now=now)
            if status == "failed" and row and config["mode"] == "on":
                try:
                    generation = binding(home)
                    result = guard_and_repair(
                        home, config, service, store, row["id"], generation,
                        None if probe_now is None else now, runner,
                    )
                except subprocess.TimeoutExpired:
                    store.escalate(row["id"], "action_outcome_unknown", now=now)
                    result = "action_outcome_unknown"
                except (OSError, ValueError, subprocess.SubprocessError):
                    store.escalate(row["id"], "cannot_fix", now=now)
                    result = "cannot_fix"
                actions.append({"service": service["id"], "result": result})
        now = time.time() if probe_now is None else probe_now
        if any(service["id"] == "gateway" and service["desired_state"] == "running" for service in config["services"]):
            signals = gateway_signals(home, now)
            if "protected_config" in config:
                signals["protected_config"] = protected_config_health(home, config["protected_config"])
                if "telegram.proxy_url" in config["protected_config"]:
                    signals["telegram_relay"] = telegram_relay_health(config["protected_config"]["telegram.proxy_url"])
            for signal, status in signals.items():
                complete = complete and status != "unknown"
                store.observe("gateway", signal, status, f"cycle:{now:.6f}", now, now=now)
            # These are observations, never restart reasons. Existing deadline/recurrence
            # policy routes persistent degradation to Doc through the same ledger.
        store.cycle(complete=complete, now=now)
        value = store.summary(now=now)
        for condition in value["conditions"]:
            if condition["eligible"]:
                store.escalate(condition["episode_id"], condition["reason"], now=now)
        value = store.summary(now=now)
        value.update(mode=config["mode"])
        atomic_json(store.directory / "status.json", value)
        value.update(actions=actions)
        return value
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", type=Path, default=Path.home() / ".hermes")
    args = parser.parse_args()
    print(json.dumps(run(args.hermes_home), sort_keys=True))


if __name__ == "__main__":
    main()
