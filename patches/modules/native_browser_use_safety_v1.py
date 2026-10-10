"""Bind native Browser Use to Golden's managed, privacy-safe CLI install."""


from __future__ import annotations


import ast
import hashlib
import json


import shutil


from pathlib import Path


TARGET = Path("tools/browser_use_cli.py")


TEST_TARGET = Path("tests/tools/test_browser_use_cli.py")


BACKUP_SUFFIX = ".bak-pre-native-browser-use-safety-v1"


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    if old not in source:
        raise RuntimeError(f"native Browser Use {label} anchor drift")
    if source.count(old) != 1:
        raise RuntimeError(f"native Browser Use {label} anchor is ambiguous")
    return source.replace(old, new, 1)


def _replace_named_node(
    source: str,
    name: str,
    replacement: str,
    label: str,
    *,
    required: bool = True,
) -> str:
    tree = ast.parse(source)
    matches = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name
    ]
    if not matches:
        if required:
            raise RuntimeError(f"native Browser Use {label} anchor drift")
        return source
    if len(matches) != 1:
        raise RuntimeError(f"native Browser Use {label} anchor is ambiguous")
    node = matches[0]
    lines = source.splitlines(keepends=True)
    start = node.lineno - 1
    end = node.end_lineno
    replacement_text = replacement.rstrip() + "\n\n"
    return "".join(lines[:start]) + replacement_text + "".join(lines[end:])


def patch_native_browser_use_safety_v1(root: Path) -> bool:
    root = Path(root)
    source = (root / TARGET).read_text(encoding="utf-8")
    if '[sys.executable, "-m", "browser_harness.run"]' in source:
        return _patch_native_harness(root)
    return _patch_d363_browser_use(root, source)


D363_MARKER = "HERMES_NATIVE_BROWSER_USE_SAFETY_v1_d363"


D363_POST_SETUP_TARGET = Path("hermes_cli/tools_config_post_setup.py")


D363_IMPORT_ANCHOR = "import contextlib\n"


D363_IMPORT_REPLACEMENT = "import contextlib\nimport hashlib\nimport tempfile\n"


D363_CONSTANT_ANCHOR = '_BACKEND_KEY = "browser-use"\nBACKEND_DISABLED = "off"\n'


RECEIPT_V2_MARKER = "HERMES_BROWSER_USE_RECEIPT_v2"


def _receipt_verifier_source() -> str:
    """Emit the installer's read-only verifier; do not maintain a second integrity policy."""
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "kit/bin/ensure-browser-use-cli.py").read_text(encoding="utf-8")
    names = {
        "RECEIPT_NAME", "HASH_CHUNK_BYTES", "RECEIPT_MAX_BYTES", "CLI_MAX_BYTES",
        "INTERPRETER_MAX_BYTES", "FILE_HASH_MAX_BYTES", "FILE_HASH_TIMEOUT_SECONDS",
        "ENVIRONMENT_HASH_MAX_BYTES", "ENVIRONMENT_HASH_MAX_ENTRIES",
        "ENVIRONMENT_HASH_MAX_PATH_BYTES", "ENVIRONMENT_HASH_TIMEOUT_SECONDS",
        "IntegrityHashLimitError", "IntegrityReadError", "_venv_python", "_venv_cli",
        "_check_hash_deadline", "_file_identity", "_consume_regular_file", "_sha256",
        "_environment_sha256", "_interpreter_integrity", "_interpreter_receipt_is_valid",
        "_artifact_sha256", "_read_receipt", "_receipt_profile_id", "_receipt_matches_install",
        "verified_command",
    }
    fragments = []
    found = set()
    for node in ast.parse(source).body:
        name = node.name if isinstance(node, (ast.FunctionDef, ast.ClassDef)) else (
            node.targets[0].id if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) else None
        )
        if name in names:
            found.add(name)
            fragments.append(ast.get_source_segment(source, node))
    if found != names:
        raise RuntimeError("Browser Use installer verifier source drift")
    contract = json.loads((repo / "kit/config/browser-use-cli-release-v1.json").read_text(encoding="utf-8"))
    return ("\n# " + RECEIPT_V2_MARKER + "\nimport platform\nimport stat\nimport time\n"
            "from typing import Any, Callable\n_MANAGED_CONTRACT = " + repr(contract) + "\n\n"
            + "\n\n".join(fragments) + "\n")


D363_CONSTANT_REPLACEMENT = '''_BACKEND_KEY = "browser-use"
BACKEND_DISABLED = "off"

# HERMES_NATIVE_BROWSER_USE_SAFETY_v1_d363: receipt-bound binary and private state.
_MANAGED_RECEIPT = "browser-use-cli-install-v1.json"
_MANAGED_PACKAGE = "browser-use==0.13.7"
_PRIVACY_ENV = {"ANONYMIZED_TELEMETRY": "false", "BH_TELEMETRY": "0",
                "BROWSER_HARNESS_TELEMETRY": "0", "BROWSER_USE_CLOUD_SYNC": "false"}

def _browser_use_state_root() -> Path:
    return Path(get_hermes_home()) / "state" / "browser-use"

def _managed_receipt_path() -> Path:
    return Path(get_hermes_home()) / "state" / _MANAGED_RECEIPT

def _verified_managed_cli() -> Optional[List[str]]:
    return verified_command(Path(get_hermes_home()), _MANAGED_CONTRACT)

def _profile_session_name(session: str) -> str:
    try:
        receipt = _managed_receipt_path().read_bytes()
    except OSError:
        receipt = b""
    prefix = "hermes_" + hashlib.sha256(receipt).hexdigest()[:16] + "_"
    suffix = session if len(session) <= 40 else session[:23] + "_" + hashlib.sha256(session.encode("utf-8")).hexdigest()[:16]
    return prefix + suffix

def _run_isolated_cli(*args, env: dict, **kwargs):
    with tempfile.TemporaryDirectory(prefix="hermes-browser-use-pycache-") as pycache:
        isolated = dict(env)
        isolated.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PYTHONPYCACHEPREFIX": pycache})
        return subprocess.run(*args, env=isolated, **kwargs)
'''


D363_CONSTANT_REPLACEMENT += _receipt_verifier_source()


D363_ENV_REPLACEMENT = '''def _base_subprocess_env() -> dict:
    from tools.browser_tool import _build_browser_env
    env = _build_browser_env()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PATH"] = _floor_subprocess_path(env.get("PATH", ""))
    state = _browser_use_state_root()
    profile = state / "browser-profile"
    env.update(_PRIVACY_ENV)
    env.update({"BH_HOME": str(state), "BH_CONFIG_DIR": str(state / "config"),
                "BH_RUNTIME_DIR": str(state / "runtime"), "BH_TMP_DIR": str(state / "tmp"),
                "BH_AGENT_WORKSPACE": str(state / "workspace"), "HOME": str(profile),
                "USERPROFILE": str(profile), "XDG_CONFIG_HOME": str(profile / ".config"),
                "XDG_CACHE_HOME": str(profile / ".cache"),
                "BROWSER_USE_CONFIG_DIR": str(profile / ".config" / "browser-use")})
    return env'''


D363_FIND_REPLACEMENT = '''def _find_cli() -> Optional[List[str]]:
    """Resolve only the receipt-bound Hermes-managed Browser Use executable."""
    return _verified_managed_cli()'''


D363_INSTALL_REPLACEMENT = '''def install_cli(timeout_s: int = 600) -> Tuple[bool, str]:
    """Only the Golden host artifact installer owns the managed environment."""
    if _verified_managed_cli():
        return True, "managed Browser Use CLI is verified"
    return False, "Run Golden's managed Browser Use host artifact installer; Browser Use remains disabled."'''



D363_WORKSPACE_MARKER = "HERMES_NATIVE_BROWSER_USE_SAFETY_v1_workspace_r1"


D363_WORKSPACE_OLD = '''def _workspace_dir(task_id: Optional[str]) -> Optional[str]:
    """Stable per-task workspace beneath the private Browser Use state root."""
    try:
        safe = _TASK_ID_SAFE_RE.sub("_", str(task_id or "default"))[:80] or "default"
        path = _browser_use_state_root() / "workspace" / safe
        path.mkdir(parents=True, exist_ok=True)
        return str(path)
    except OSError as e:
        logger.debug("browser_exec workspace unavailable: %s", e)
        return None'''


D363_WORKSPACE_REPLACEMENT = '''def _workspace_dir(task_id: Optional[str]) -> Optional[str]:
    """Stable collision-resistant task workspace beneath private Browser Use state."""
    try:
        # HERMES_NATIVE_BROWSER_USE_SAFETY_v1_workspace_r1
        identity = str(task_id or "default")
        safe_prefix = _TASK_ID_SAFE_RE.sub("_", identity)[:63] or "default"
        workspace_name = f"{safe_prefix}_{hashlib.sha256(identity.encode('utf-8', 'surrogatepass')).hexdigest()[:16]}"
        path = _browser_use_state_root() / "workspace" / workspace_name
        path.mkdir(parents=True, exist_ok=True)
        return str(path)
    except OSError as e:
        logger.debug("browser_exec workspace unavailable: %s", e)
        return None'''


D363_EXEC_ERROR_OLD = '''return tool_error("browser-use CLI not found on PATH, and uvx is unavailable for a zero-install run. "
                          "Install it with `uv tool install browser-use` (or `pipx install browser-use`), "
                          "then run `browser-use --doctor` to verify the setup.")'''


D363_EXEC_ERROR_NEW = '''return tool_error("managed Browser Use CLI is unavailable or failed receipt verification. "
                          "Run Golden's managed Browser Use host artifact installer.")'''


D363_POST_SETUP_OLD = '''        _print_info("    Falling back to zero-install runs via `uvx browser-use`" if shutil.which("uvx")
                    else "    Install manually: uv tool install browser-use  (https://docs.astral.sh/uv/)")'''


D363_POST_SETUP_NEW = '''        _print_info("    Browser Use stays disabled until its managed install completes and verifies.")
        # HERMES_NATIVE_BROWSER_USE_SAFETY_v1_d363'''


LEGACY_RECEIPT_FUNCTIONS = {'_verified_managed_cli': '90320527e588118a0abede595ed77402cd3e5f9bcf6b3a5365a87871c288953a', '_find_cli': '88af0606ad1166d002d35218075e01e55f06b8bc427b34d554d30de676df6494', 'install_cli': 'e95c5e781727ddc32f79140cffea79ea6053aad03f6fa9af1d04c4dc15ac6731'}


def _patch_d363_browser_use(root: Path, source: str) -> bool:
    target = root / TARGET
    post_target = root / D363_POST_SETUP_TARGET
    post_source = post_target.read_text(encoding="utf-8")
    test_target = root / TEST_TARGET
    test_source = test_target.read_text(encoding="utf-8")
    if D363_MARKER in source and RECEIPT_V2_MARKER not in source:
        if D363_MARKER not in post_source or D363_MARKER not in test_source:
            raise RuntimeError("native Browser Use d363 split patch is incomplete")
        nodes = {n.name: n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)}
        for name, digest in LEGACY_RECEIPT_FUNCTIONS.items():
            if name not in nodes or hashlib.sha256(ast.get_source_segment(source, nodes[name]).encode()).hexdigest() != digest:
                raise RuntimeError("native Browser Use d363 executable verification drift")
        replacements = {
            "_verified_managed_cli": next(ast.get_source_segment(D363_CONSTANT_REPLACEMENT, n)
                for n in ast.parse(D363_CONSTANT_REPLACEMENT).body
                if isinstance(n, ast.FunctionDef) and n.name == "_verified_managed_cli"),
            "_find_cli": D363_FIND_REPLACEMENT,
            "install_cli": D363_INSTALL_REPLACEMENT,
        }
        patched = source.replace("Run `hermes tools` to install the managed Browser Use CLI.",
                                 "Run Golden's managed Browser Use host artifact installer.")
        patched = patched.replace("Run `hermes tools` to install it.",
                                  "Run Golden's managed Browser Use host artifact installer.")
        for name, replacement in replacements.items():
            patched = _replace_named_node(patched, name, replacement, "receipt v2 upgrade")
        patched = _replace_named_node(patched, "_sha256_file", "", "remove launcher-only verifier")
        patched = patched.replace("_MAX_MANAGED_CLI_BYTES = 8 * 1024 * 1024\n", "")
        patched += _receipt_verifier_source()
        patched_test = _replace_named_node(test_source, "TestFindCliManagedBin", D363_TEST_MANAGED_REPLACEMENT, "receipt v2 tests")
        patched_test = _replace_named_node(patched_test, "TestInstallCli", D363_TEST_INSTALL_REPLACEMENT, "single installer tests")
        ast.parse(patched)
        ast.parse(patched_test)
        for path in (target, test_target):
            shutil.copy2(path, Path(str(path) + BACKUP_SUFFIX))
        target.write_text(patched, encoding="utf-8")
        test_target.write_text(patched_test, encoding="utf-8")
        return True
    if D363_MARKER in source:
        if D363_MARKER not in post_source or D363_MARKER not in test_source:
            raise RuntimeError("native Browser Use d363 split patch is incomplete")
        expected_verifier = next(node for node in ast.parse(D363_CONSTANT_REPLACEMENT).body
                                 if isinstance(node, ast.FunctionDef) and node.name == "_verified_managed_cli")
        actual_verifiers = [node for node in ast.parse(source).body
                            if isinstance(node, ast.FunctionDef) and node.name == "_verified_managed_cli"]
        if len(actual_verifiers) != 1 or ast.dump(actual_verifiers[0]) != ast.dump(expected_verifier):
            raise RuntimeError("native Browser Use d363 executable verification drift")
        expected_nodes = ast.parse(_receipt_verifier_source()).body
        actual_nodes = ast.parse(source).body
        for expected in expected_nodes:
            if isinstance(expected, (ast.FunctionDef, ast.ClassDef)):
                matches = [n for n in actual_nodes if type(n) is type(expected) and n.name == expected.name]
            elif isinstance(expected, ast.Assign):
                matches = [n for n in actual_nodes if isinstance(n, ast.Assign)
                           and ast.dump(n.targets[0]) == ast.dump(expected.targets[0])]
            else:
                continue
            if len(matches) != 1 or ast.dump(matches[0]) != ast.dump(expected):
                raise RuntimeError("native Browser Use receipt verifier drift")
        old_session = '    return ("hermes_" + hashlib.sha256(receipt).hexdigest()[:16] + "_" + session)[:64]'
        new_session = '    prefix = "hermes_" + hashlib.sha256(receipt).hexdigest()[:16] + "_"\n    suffix = session if len(session) <= 40 else session[:23] + "_" + hashlib.sha256(session.encode("utf-8")).hexdigest()[:16]\n    return prefix + suffix'
        patched = source
        if old_session in patched:
            patched = _replace_once(patched, old_session, new_session, "d363 full session identity")
        elif new_session not in patched:
            raise RuntimeError("native Browser Use d363 session identity drift")
        if D363_WORKSPACE_MARKER not in patched:
            if D363_WORKSPACE_OLD not in patched:
                raise RuntimeError("native Browser Use d363 workspace identity drift")
            patched = _replace_named_node(
                patched,
                "_workspace_dir",
                D363_WORKSPACE_REPLACEMENT,
                "d363 collision-resistant workspace upgrade",
            )
        if patched == source:
            return False
        ast.parse(patched)
        target.write_text(patched, encoding="utf-8")
        return True
    patched = _replace_once(source, D363_IMPORT_ANCHOR, D363_IMPORT_REPLACEMENT, "d363 imports")
    patched = _replace_once(patched, D363_CONSTANT_ANCHOR, D363_CONSTANT_REPLACEMENT, "d363 constants")
    patched = _replace_named_node(patched, "is_browser_use_cli_mode", D363_MODE_REPLACEMENT, "d363 mode")
    patched = _replace_named_node(patched, "_base_subprocess_env", D363_ENV_REPLACEMENT, "d363 environment")
    patched = _replace_named_node(patched, "_find_cli", D363_FIND_REPLACEMENT, "d363 resolver")
    patched = _replace_named_node(patched, "install_cli", D363_INSTALL_REPLACEMENT, "d363 installer")
    patched = _replace_named_node(patched, "_workspace_dir", D363_WORKSPACE_REPLACEMENT, "d363 workspace")
    patched = _replace_once(patched, D363_EXEC_ERROR_OLD, D363_EXEC_ERROR_NEW, "d363 missing CLI error")
    patched = _replace_once(patched, D363_SCHEMA_OLD, D363_SCHEMA_NEW, "d363 schema hint")
    patched = _replace_once(patched, 'env["BU_NAME"] = session', 'env["BU_NAME"] = _profile_session_name(session)', "d363 session namespace")
    call_anchor = ('return {"proc": _run_cli_killing_process_group(cmd, code, env, timeout)}'
                   if 'return {"proc": _run_cli_killing_process_group(cmd, code, env, timeout)}' in patched
                   else "proc = _run_cli_killing_process_group(cmd, code, env, timeout)")
    if call_anchor in patched:
        # The released runtime dispatches through a browser lease closure. Keep that fence.
        patched = _replace_once(patched, call_anchor,
                                call_anchor.replace("_run_cli_killing_process_group(cmd, code, env, timeout)", "_run_isolated_cli(cmd, code, env=env, timeout=timeout)"), "native isolated process group")
        patched = _replace_once(patched, "return subprocess.run(*args, env=isolated, **kwargs)",
                                "return _run_cli_killing_process_group(args[0], args[1], isolated, kwargs['timeout'])", "native process group isolation")
    else:
        patched = _replace_once(patched, '''proc = subprocess.run(
            cmd, input=code, capture_output=True, text=True, timeout=timeout, env=env,''', '''proc = _run_isolated_cli(
            cmd, input=code, capture_output=True, text=True, timeout=timeout, env=env,''', "d363 isolated subprocess")
    patched_post = _replace_once(post_source, D363_POST_SETUP_OLD, D363_POST_SETUP_NEW, "d363 post setup")
    patched_test = _patch_d363_tests(test_source)
    ast.parse(patched)
    ast.parse(patched_post)
    ast.parse(patched_test)
    for path, content in ((target, patched), (post_target, patched_post), (test_target, patched_test)):
        shutil.copy2(path, Path(str(path) + BACKUP_SUFFIX))
        path.write_text(content, encoding="utf-8")
    return True


D363_MODE_REPLACEMENT = '''def is_browser_use_cli_mode() -> bool:
    """Enable Browser Use only when the receipt-bound managed CLI is runnable."""
    if _camofox_active():
        return False
    backend = get_browser_backend()
    if backend and backend != _BACKEND_KEY:
        return False
    return _find_cli() is not None'''


D363_SCHEMA_OLD = '''# Static fallback description, used only when the CLI (and uvx) is unavailable
    "description": (_HEADER_BASE + _HELPERS_DIGEST
                    + "\\n\\n(The browser-use CLI is not installed yet. Install it with `uv tool install browser-use`.)"),'''


D363_SCHEMA_NEW = '''# Static fallback description when the managed receipt-bound CLI is unavailable.
    "description": (_HEADER_BASE + _HELPERS_DIGEST
                    + "\\n\\n(The managed Browser Use CLI is unavailable. Run Golden's managed Browser Use host artifact installer.)"),'''


D363_TEST_FIND_REPLACEMENT = '''class TestFindCli:
    """Only receipt-bound $HERMES_HOME/bin/browser-use may execute."""
    def test_rejects_path_and_uvx(self, monkeypatch):
        monkeypatch.setattr(bu_cli.shutil, "which", lambda *args, **kwargs: "/usr/local/bin/uvx")
        assert bu_cli._find_cli_unpatched() is None

    def test_none_when_no_receipt(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        assert bu_cli._find_cli_unpatched() is None
'''


D363_TEST_MANAGED_REPLACEMENT = '''class TestFindCliManagedBin:
    def test_rejects_legacy_launcher_receipt(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        monkeypatch.setenv("HERMES_HOME", str(home))
        cli = home / "bin" / "browser-use"
        cli.parent.mkdir(parents=True)
        cli.write_text("#!/bin/sh\\n")
        cli.chmod(0o700)
        receipt = home / "state" / bu_cli._MANAGED_RECEIPT
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({"schema": 1, "package": bu_cli._MANAGED_PACKAGE,
                                       "path": str(cli), "sha256": "0" * 64}))
        assert bu_cli._find_cli_unpatched() is None
'''


D363_TEST_INSTALL_REPLACEMENT = '''class TestInstallCli:
    def test_missing_install_requires_host_artifact_owner(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        monkeypatch.setattr(bu_cli.subprocess, "run", lambda *a, **kw: pytest.fail("unexpected install"))
        ok, message = bu_cli.install_cli()
        assert not ok
        assert "host artifact installer" in message
'''



def _patch_d363_tests(source: str) -> str:
    patched = "# HERMES_NATIVE_BROWSER_USE_SAFETY_v1_d363\n" + _replace_named_node(source, "TestFindCli", D363_TEST_FIND_REPLACEMENT, "d363 resolver tests")
    patched = _replace_named_node(patched, "TestFindCliManagedBin", D363_TEST_MANAGED_REPLACEMENT, "d363 receipt tests")
    patched = _replace_named_node(patched, "TestInstallCli", D363_TEST_INSTALL_REPLACEMENT, "d363 installer tests")
    patched = patched.replace('assert "uv tool install browser-use" in desc', 'assert "managed Browser Use" in desc')
    patched = patched.replace('assert "uv tool install browser-use" in result["error"]', 'assert "managed Browser Use" in result["error"]')
    patched = patched.replace('assert "bu:r7k2" in result["output"]', 'assert "bu:hermes_" in result["output"] and result["output"].strip().endswith("r7k2")')
    # Explicit or legacy Browser Use selection cannot re-enable an unverified binary.
    patched = patched.replace('''        assert bu_cli.is_browser_use_cli_mode() is True

    def test_other_backend_value_is_not_cli_mode''', '''        assert bu_cli.is_browser_use_cli_mode() is False

    def test_other_backend_value_is_not_cli_mode''', 1)
    patched = patched.replace("""        monkeypatch.setenv("BROWSER_USE_API_KEY", "bu-key")
        assert bu_cli.is_browser_use_cli_mode() is True

    def test_gateway_config_stays_on_legacy_path""", """        monkeypatch.setenv("BROWSER_USE_API_KEY", "bu-key")
        assert bu_cli.is_browser_use_cli_mode() is False

    def test_gateway_config_stays_on_legacy_path""", 1)
    # The native migration test now stands next to a different test. Bind to
    # its credential fixture, not the following function's name.
    patched = patched.replace('        monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "bb-project")\n        assert bu_cli.is_browser_use_cli_mode() is True',
                              '        monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "bb-project")\n        assert bu_cli.is_browser_use_cli_mode() is False', 1)
    patched = patched.replace('''        assert bu_cli.is_browser_use_cli_mode() is True

    def test_gateway_config_stays_on_legacy_path''', '''        assert bu_cli.is_browser_use_cli_mode() is False

    def test_gateway_config_stays_on_legacy_path''', 1)
    return patched


def _patch_native_harness(root: Path) -> bool:
    target = Path(root) / "tools/browser_use_cli.py"
    source = target.read_text()
    if "HERMES_NATIVE_BROWSER_USE_SAFETY_v1" in source:
        return False
    old = '    env.setdefault("ANONYMIZED_TELEMETRY", "false")\n    return env\n'
    new = '''    # HERMES_NATIVE_BROWSER_USE_SAFETY_v1: force privacy; isolate harness state by profile.
    state = Path(get_hermes_home()) / "state" / "browser-use"
    profile = state / "browser-profile"
    env.update({"ANONYMIZED_TELEMETRY": "false", "BH_TELEMETRY": "0",
                "BROWSER_HARNESS_TELEMETRY": "0", "BROWSER_USE_CLOUD_SYNC": "false",
                "BH_HOME": str(state), "BH_CONFIG_DIR": str(state / "config"),
                "BH_RUNTIME_DIR": str(state / "runtime"), "BH_TMP_DIR": str(state / "tmp"),
                "BH_RUNTIME_DIR_SHARED": "1", "BH_TMP_DIR_SHARED": "1", "BU_NAME": "default",
                "BH_AGENT_WORKSPACE": str(state / "workspace"), "HOME": str(profile),
                "USERPROFILE": str(profile), "XDG_CONFIG_HOME": str(profile / ".config"),
                "XDG_CACHE_HOME": str(profile / ".cache"),
                "BROWSER_USE_CONFIG_DIR": str(profile / ".config" / "browser-use"),
                "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    return env
'''
    if source.count(old) != 1:
        raise RuntimeError("native harness environment owner drift")
    # Preserve the native PYTHONPATH bridge: Desktop bundles need it to import
    # the same exact-pinned package in the spawned interpreter and daemon.
    updated = source.replace(old, new, 1)
    # Preserve Golden's collision-resistant task directories. The native command
    # runner and daemon shutdown continue to own process lifetime.
    old_workspace = ('    if os.environ.get("BH_AGENT_WORKSPACE"):\n'
                     '        return os.environ["BH_AGENT_WORKSPACE"]\n')
    if updated.count(old_workspace) != 1:
        raise RuntimeError("native harness workspace override drift")
    updated = updated.replace(old_workspace, "", 1)
    old_path = ('        safe = _TASK_ID_SAFE_RE.sub("_", str(task_id or "default"))[:80] or "default"\n'
                '        path = Path(get_hermes_home()) / "cache" / "browser-use" / "workspace" / safe\n')
    new_path = ('        import hashlib\n'
                '        identity = str(task_id or "default")\n'
                '        safe = (_TASK_ID_SAFE_RE.sub("_", identity)[:63] or "default") + "_" + hashlib.sha256(identity.encode("utf-8", "surrogatepass")).hexdigest()[:16]\n'
                '        path = Path(get_hermes_home()) / "state" / "browser-use" / "workspace" / safe\n')
    if updated.count(old_path) != 1:
        raise RuntimeError("native harness task directory drift")
    updated = updated.replace(old_path, new_path, 1)
    old_name = '        env["BU_NAME"] = session'
    if updated.count(old_name) != 1:
        raise RuntimeError("native harness session namespace drift")
    updated = updated.replace(old_name,
        '        import hashlib\n        env["BU_NAME"] = "s_" + hashlib.sha256(session.encode()).hexdigest()[:16]', 1)
    compile(updated, str(target), "exec")
    target.write_text(updated)
    return True
