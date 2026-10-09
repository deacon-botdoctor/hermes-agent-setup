"""Let a reader process boot when its PM install-state directory is on a read-only mount.

Root cause (4score-reports on Spark, 2026-10-07): systemd ProtectSystem=strict /
ProtectHome=read-only makes ~/.hermes/installs/<id>/ read-only. ``activate_dependencies``
only READS the committed selection, but ``runtime_lock`` opens ``.install.lock`` with
O_CREAT|O_RDWR, which raises OSError(EROFS). EROFS is not a PermissionError, so
hermes_bootstrap prints "run `hermes pm repair`" and exits 1. ``lease_directory`` (lease
file O_CREAT|O_EXCL) and ``_claim_recovery_lock`` (.recovery.lock) fail the same way.

Change: on EROFS only, readers proceed lock-free and lease-free (identical to the existing
"lock wait expired" reader path) and log once; writers (timeout=None) still raise. Nothing
is ever written on the read-only path. No ReadWritePaths / unit change is needed.
"""
from __future__ import annotations

from pathlib import Path

MARKER = "HERMES_PM_READ_ONLY_INSTALL_STATE_v1"


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"{MARKER}: anchor drift in {label} (found {count})")
    return source.replace(old, new, 1)


RS_IMPORT_OLD = "import base64\n"
RS_IMPORT_NEW = "import base64\nimport errno\n"

RS_HELPER_ANCHOR = "@contextmanager\ndef runtime_lock("
RS_HELPER = '''# [HERMES_PM_READ_ONLY_INSTALL_STATE_v1] A reader whose install state sits on a read-only
# mount (systemd ProtectSystem=strict / ProtectHome=read-only) cannot create lock or lease
# files. It only reads the committed selection, so it proceeds exactly like a reader whose
# lock wait expired, and never writes. Writers (timeout=None) still fail loudly.
_READ_ONLY_WARNED: set[str] = set()


def _read_only_state(exc: OSError, path: Path) -> bool:
    if exc.errno != errno.EROFS:
        return False
    key = str(path)
    if key not in _READ_ONLY_WARNED:
        _READ_ONLY_WARNED.add(key)
        LOG.warning("install state is on a read-only filesystem; continuing without lock/lease (%s)", path)
    return True


'''

RS_LOCK_OLD = '''    state = install_state_dir(project)
    state.mkdir(parents=True, exist_ok=True)
    fd = os.open(state / ".install.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
'''
RS_LOCK_NEW = '''    state = install_state_dir(project)
    try:
        state.mkdir(parents=True, exist_ok=True)
        fd = os.open(state / ".install.lock", os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:  # HERMES_PM_READ_ONLY_INSTALL_STATE_v1
        if timeout is None or not _read_only_state(exc, state):
            raise
        yield False
        return
    try:
'''

RS_LEASE_OLD = '''    leases = generation / ".leases"
    leases.mkdir(exist_ok=True)
    _prune_unlocked_leases(leases)
    while True:
        lease = leases / uuid.uuid4().hex
        fd = os.open(lease, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
'''
RS_LEASE_NEW = '''    leases = generation / ".leases"
    try:
        leases.mkdir(exist_ok=True)
    except OSError as exc:  # HERMES_PM_READ_ONLY_INSTALL_STATE_v1
        if not _read_only_state(exc, generation):
            raise
        return lambda: None
    _prune_unlocked_leases(leases)
    while True:
        lease = leases / uuid.uuid4().hex
        try:
            fd = os.open(lease, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except OSError as exc:  # HERMES_PM_READ_ONLY_INSTALL_STATE_v1
            if not _read_only_state(exc, generation):
                raise
            return lambda: None
'''

ER_OLD = '''    state = install_state_dir(root)
    state.mkdir(parents=True, exist_ok=True)
    fd = os.open(state / ".recovery.lock", os.O_CREAT | os.O_RDWR, 0o600)
'''
ER_NEW = '''    state = install_state_dir(root)
    try:
        state.mkdir(parents=True, exist_ok=True)
        fd = os.open(state / ".recovery.lock", os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:  # HERMES_PM_READ_ONLY_INSTALL_STATE_v1
        import errno as _errno
        if exc.errno == _errno.EROFS:
            return None  # a read-only install cannot be repaired from here; skip, never write
        raise
'''


def patch_pm_read_only_install_state_v1(hermes_dir: Path) -> bool:
    root = Path(hermes_dir)
    rs = root / "hermes_cli/runtime_state.py"
    er = root / "hermes_cli/_early_recovery.py"
    rs_src, er_src = rs.read_text(), er.read_text()
    if MARKER in rs_src and MARKER in er_src:
        return False
    rs_src = _replace_once(rs_src, RS_IMPORT_OLD, RS_IMPORT_NEW, "runtime_state import")
    rs_src = _replace_once(rs_src, RS_HELPER_ANCHOR, RS_HELPER + RS_HELPER_ANCHOR, "runtime_state helper")
    rs_src = _replace_once(rs_src, RS_LOCK_OLD, RS_LOCK_NEW, "runtime_lock")
    rs_src = _replace_once(rs_src, RS_LEASE_OLD, RS_LEASE_NEW, "lease_directory")
    er_src = _replace_once(er_src, ER_OLD, ER_NEW, "_claim_recovery_lock")
    compile(rs_src, str(rs), "exec")
    compile(er_src, str(er), "exec")
    rs.write_text(rs_src)
    er.write_text(er_src)
    return True
