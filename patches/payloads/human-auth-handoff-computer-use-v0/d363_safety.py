# HERMES_HUMAN_AUTH_HANDOFF_COMPUTER_USE_v0_d363
import math
import subprocess
import time
from pathlib import Path
try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None
try:
    import msvcrt as _msvcrt
except ImportError:
    _msvcrt = None

# HERMES_HUMAN_AUTH_HANDOFF_COMPUTER_USE_v0
_blocked_sensitive_surfaces: Dict[Any, str] = {}
_blocked_login_targets: Dict[int, Tuple[Any, Any]] = {}
_sensitive_handoff_lock = threading.Lock()
_sensitive_handoff_owner: Dict[int, int] = {}
_native_safe_snapshot: Dict[int, Dict[int, Tuple[Any, ...]]] = {}


def _sensitive_surface_key(
    backend: ComputerUseBackend,
    *,
    target: Optional[Dict[str, Any]] = None,
) -> Any:
    """Bind payment/permission state to one exact browser surface.

    Login handoff remains backend-wide because it freezes every input lane.
    Payment and permission refusals are narrower: Chromium AX snapshots can
    include labels from sibling windows, so a refusal discovered in one
    window/tab must not poison every other target owned by that process.
    """
    backend_key = id(backend)
    exact = target if target is not None else getattr(backend, "_last_target", None)
    if isinstance(exact, dict):
        pid = exact.get("pid")
        window_id = exact.get("window_id")
        if pid is not None and window_id is not None:
            return (backend_key, "window", pid, window_id)
    return backend_key


def _clear_blocked_sensitive_surfaces(backend_key: int) -> None:
    for key in list(_blocked_sensitive_surfaces):
        if key == backend_key or (
            isinstance(key, tuple) and key and key[0] == backend_key
        ):
            _blocked_sensitive_surfaces.pop(key, None)
    _blocked_login_targets.pop(backend_key, None)


def _blocked_sensitive_kind(
    backend: ComputerUseBackend,
    action: str,
    args: Dict[str, Any],
) -> Optional[str]:
    backend_key = id(backend)
    # Login intentionally freezes the whole backend during human handoff.
    backend_kind = _blocked_sensitive_surfaces.get(backend_key)
    if backend_kind == "login":
        return backend_kind
    target = None
    if args.get("pid") is not None and args.get("window_id") is not None:
        target = {"pid": args["pid"], "window_id": args["window_id"]}
    return _blocked_sensitive_surfaces.get(
        _sensitive_surface_key(backend, target=target)
    )
_computer_state_changed = threading.Condition(_sensitive_handoff_lock)
_computer_active_operations = 0
_computer_active_threads: set[int] = set()
_computer_auth_pending = False
_computer_handoff_epoch = 0
_process_gate_mutex = threading.RLock()
_process_gate_users = 0
_process_gate_handle: Optional[Any] = None


def _process_gate_path() -> Path:
    configured = os.environ.get("HERMES_COMPUTER_USE_GATE_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    # Multiple Hermes runtimes may use different HERMES_HOME directories while
    # controlling the same logged-in desktop. Keep the default per-user and
    # host-wide so every runtime participates in one input freeze.
    return Path.home() / "Library/Caches/Hermes/computer-use-input.lock"


def _shared_handoff_epoch_path() -> Path:
    return Path(f"{_process_gate_path()}.epoch")


def _shared_handoff_owner_path() -> Path:
    return Path(f"{_process_gate_path()}.pending.json")


def _read_shared_handoff_epoch() -> int:
    try:
        return int(_shared_handoff_epoch_path().read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError, ValueError):
        return 0


def _advance_shared_handoff_epoch() -> int:
    path = _shared_handoff_epoch_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    value = _read_shared_handoff_epoch() + 1
    temp = path.with_suffix(
        f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    temp.write_text(str(value), encoding="utf-8")
    os.replace(temp, path)
    return value


def _write_shared_handoff_owner() -> None:
    path = _shared_handoff_owner_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        timeout = float(os.environ.get("HERMES_AUTH_HANDOFF_TIMEOUT_S", "600"))
    except ValueError:
        timeout = 600.0
    if not math.isfinite(timeout) or timeout < 1:
        timeout = 600.0
    payload = {
        "pid": os.getpid(),
        "pending_until": time.time() + timeout + 30.0,
    }
    temp = path.with_suffix(
        f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    temp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(temp, path)


def _clear_shared_handoff_owner() -> None:
    try:
        _shared_handoff_owner_path().unlink()
    except FileNotFoundError:
        pass


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        process = kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            # Access denied proves the PID exists; unknown errors stay live so
            # recovery never steals a possibly active handoff.
            return ctypes.get_last_error() != 87
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
                return True
            return exit_code.value == 259
        finally:
            kernel32.CloseHandle(process)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _shared_handoff_owner_is_active() -> bool:
    """Read-only fast refusal; recovery rechecks this while holding the OS gate."""
    try:
        owner = json.loads(_shared_handoff_owner_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        owner = {}
    try:
        owner_pid = int(owner.get("pid", 0))
        pending_until = float(owner.get("pending_until", 0))
    except (TypeError, ValueError):
        owner_pid = 0
        pending_until = 0
    return _pid_is_alive(owner_pid) and pending_until > time.time()


def _recover_orphaned_shared_handoff(epoch: int) -> bool:
    """Normalize an orphaned odd epoch while the caller holds the OS gate."""
    if epoch % 2 == 0 or _shared_handoff_owner_is_active():
        return False
    _advance_shared_handoff_epoch()
    _clear_shared_handoff_owner()
    return True


def _enter_process_gate() -> None:
    """Exclude other runtime processes while this process can issue input."""
    global _process_gate_handle, _process_gate_users
    with _process_gate_mutex:
        if _process_gate_users == 0:
            path = _process_gate_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a+", encoding="utf-8")
            try:
                if _fcntl is not None:
                    _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
                elif _msvcrt is not None:
                    handle.seek(0, os.SEEK_END)
                    if handle.tell() == 0:
                        handle.write("\0")
                        handle.flush()
                    handle.seek(0)
                    while True:
                        try:
                            _msvcrt.locking(
                                handle.fileno(), _msvcrt.LK_NBLCK, 1
                            )
                            break
                        except OSError:
                            time.sleep(0.1)
                            handle.seek(0)
                else:  # pragma: no cover - unsupported Python platform
                    raise RuntimeError("no supported interprocess input lock")
            except Exception:
                handle.close()
                raise
            _process_gate_handle = handle
        _process_gate_users += 1


def _leave_process_gate() -> None:
    global _process_gate_handle, _process_gate_users
    with _process_gate_mutex:
        if _process_gate_users <= 0:
            return
        _process_gate_users -= 1
        if _process_gate_users == 0 and _process_gate_handle is not None:
            handle = _process_gate_handle
            _process_gate_handle = None
            try:
                if _fcntl is not None:
                    _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
                elif _msvcrt is not None:
                    handle.seek(0)
                    _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)
                else:  # pragma: no cover - guarded during acquisition
                    raise RuntimeError("no supported interprocess input lock")
            finally:
                handle.close()


def _login_page_title(value: str) -> bool:
    """Match a login page title without trapping history/help page titles."""
    title = " ".join(str(value or "").lower().split())
    return bool(re.fullmatch(
        r"(?:sign in|log in|login)(?:\s*(?:[-|:\u2013\u2014]\s*|to\s+).+)?",
        title,
    ))


def _surface_kind(cap: CaptureResult) -> Optional[str]:
    """Classify sensitive UI from AX labels only; never inspect image bytes."""
    title = str(cap.window_title or "").lower()
    parts = [title, str(cap.app or "")]
    credential_field = False
    actionable_control = False
    actionable_login_control = False
    has_dialog = False
    has_window_container = False
    has_page_container = False
    has_prompt_action = False
    has_payment_action = False
    has_permission_action = False
    protected_control_parts: List[str] = []
    identity_positions: List[int] = []
    identity_submit_positions: List[int] = []
    generic_submit_positions: List[int] = []
    login_copy_positions: List[int] = []
    for position, element in enumerate(cap.elements):
        role = str(element.role or "").lower()
        label = str(element.label or "").lower()
        parts.extend((role, label))
        dialog_role = any(token in role for token in ("dialog", "sheet", "modal"))
        has_dialog = has_dialog or dialog_role
        has_window_container = has_window_container or "window" in role
        has_page_container = has_page_container or any(
            token in role
            for token in (
                "form", "webarea", "web area", "rootwebarea", "document", "html",
            )
        )
        action_role = any(
            token in role
            for token in ("button", "link", "menuitem", "radio", "listitem")
        )
        actionable_control = actionable_control or action_role
        payment_action_label = bool(re.match(
            r"^(?:pay(?:\s|$)|purchase(?:\s|$)|buy now$|place order$|"
            r"confirm purchase$|complete purchase$|confirm order$|submit order$|order now$)",
            label.strip(),
        ))
        has_prompt_action = has_prompt_action or (
            action_role
            and any(token in label for token in (
                "allow", "deny", "don't allow", "cancel", "pay", "purchase",
            ))
        )
        has_payment_action = has_payment_action or (
            action_role and payment_action_label
        )
        has_permission_action = has_permission_action or (
            action_role
            and label.strip() in {
                "allow", "deny", "don't allow", "dont allow",
                "allow once", "allow while using app",
                "allow while using the app",
            }
        )
        actionable_login_control = actionable_login_control or (
            action_role
            and any(token in label for token in (
                "sign in with", "log in with", "continue with",
                "use another account", "add account", "choose an account",
                "select an account",
            ))
        )
        if action_role and label.strip() in {"continue", "next"}:
            generic_submit_positions.append(position)
        if action_role and (
            any(token in label for token in (
                "magic link", "email me a code", "send code", "send a code",
                "send link", "verify",
            ))
        ):
            identity_submit_positions.append(position)
        if action_role and label.strip() in {"submit", "proceed"}:
            generic_submit_positions.append(position)
        field_role = any(
            token in role
            for token in ("secure", "textfield", "textbox", "input", "edit")
        )
        if dialog_role or "window" in role or field_role:
            protected_control_parts.extend((role, label))
        credential_label = any(
            token in label
            for token in (
                "password", "one-time code", "otp", "security code",
                "verification code", "recovery code",
            )
        )
        credential_field = credential_field or (
            "secure" in role or (field_role and credential_label)
        )
        if field_role and any(token in label for token in (
            "email", "username", "phone number", "mobile number",
            "account id", "login id", "member id",
        )):
            identity_positions.append(position)
        if re.search(r"\b(?:sign in|log in|login)\b", label):
            login_copy_positions.append(position)
    text = " ".join(parts).lower()
    protected_text = " ".join(protected_control_parts).lower()
    modal_surface = has_dialog or (
        has_window_container and not has_page_container and has_prompt_action
    )

    if _login_page_title(title):
        return "login"

    payment_evidence = any(token in protected_text for token in (
        "credit card", "card number", "cvv", "payment method",
    )) or (
        "security code" in protected_text
        and any(token in protected_text for token in ("card", "payment"))
    )
    if payment_evidence or has_payment_action or (
        modal_surface
        and any(token in text for token in (
            "credit card", "card number", "cvv", "payment method",
            "apple pay", "google pay", "paypal",
        ))
    ):
        return "payment"
    if modal_surface and (has_permission_action or any(token in text for token in (
        "accessibility permission", "screen recording permission", "grant permission",
        "allow access to", "would like to access",
    ))):
        return "permission"
    if credential_field:
        return "login"
    if actionable_login_control:
        return "login"
    if actionable_control and any(token in title for token in (
        "choose an account", "select an account", "pick an account",
    )):
        return "login"
    def nearby(left: List[int], right: List[int]) -> bool:
        return any(abs(a - b) <= 3 for a in left for b in right)

    if nearby(identity_positions, login_copy_positions):
        return "login"
    if nearby(identity_positions, identity_submit_positions):
        return "login"
    if has_page_container and nearby(identity_positions, generic_submit_positions):
        return "login"
    return None


def _empty_ax_capture(cap: CaptureResult) -> bool:
    return not cap.elements


def _element_fingerprint(element: UIElement, backend: ComputerUseBackend) -> Tuple[Any, ...]:
    bounds = tuple(element.bounds) if element.bounds is not None else None
    return (element.role, element.label, bounds, element.app,
            getattr(element, "pid", None), getattr(element, "window_id", None),
            _sensitive_surface_key(backend))


def _remember_native_safe_snapshot(
    backend: ComputerUseBackend,
    exposed_elements: List[UIElement],
) -> None:
    fingerprints = {
        element.index: _element_fingerprint(element, backend)
        for element in exposed_elements
    }
    if fingerprints:
        _native_safe_snapshot[id(backend)] = fingerprints
    else:
        _native_safe_snapshot.pop(id(backend), None)


def _browser_capable_capture(
    cap: CaptureResult,
    backend: Optional[ComputerUseBackend] = None,
) -> bool:
    identity = str(cap.app or "").lower()
    if not identity.strip() and backend is not None:
        identity = str(getattr(backend, "_last_app", "") or "").lower()
    if not identity.strip():
        return True
    if any(token in identity for token in (
        "browser", "brave", "chrome", "chromium", "arc", "safari",
        "firefox", "edge", "opera", "webkit", "orion", "vivaldi",
    )):
        return True
    roles = [str(element.role or "").lower() for element in cap.elements]
    if any(
        any(token in role for token in (
            "webarea", "web area", "document", "html", "rootwebarea",
        ))
        for role in roles
    ):
        return True
    # Unknown apps remain untrusted: alternative browsers and embedded web
    # views must not bypass browser semantic completeness merely because their
    # product name is absent from an allowlist. Only established native apps
    # take the non-browser path.
    known_native = (
        "finder", "preview", "calculator", "textedit", "terminal", "iterm",
        "mail", "messages", "notes", "calendar", "system settings",
        "system preferences", "xcode", "visual studio code", "vscode",
        "music", "photos", "pages", "numbers", "keynote", "canvas app",
        "freecad", "qt6application", "test app",
    )
    return identity.strip() not in known_native


def _browser_semantics_complete(
    cap: CaptureResult,
    backend: Optional[ComputerUseBackend] = None,
) -> bool:
    if not _browser_capable_capture(cap, backend):
        return True
    if _surface_kind(cap) is not None:
        return True
    roles = [str(element.role or "").lower() for element in cap.elements]
    has_page_root = any(
        any(token in role for token in (
            "webarea", "web area", "document", "html", "rootwebarea", "form",
        ))
        for role in roles
    )
    has_actionable_subtree = any(
        any(token in role for token in (
            "button", "link", "textfield", "textbox", "input", "combobox",
            "checkbox", "radio", "menuitem", "tab", "slider", "switch",
        ))
        for role in roles
    )
    return has_page_root and has_actionable_subtree


def _sensitive_native_capture(cap: CaptureResult) -> bool:
    """Recognize native security/payment identities even without AX nodes."""
    identity = f"{cap.app or ''} {cap.window_title or ''}".lower()
    return any(token in identity for token in (
        "system settings", "system preferences", "securityagent", "wallet",
        "apple pay", "google pay", "paypal", "payment", "checkout",
        "permission", "would like to", "notifications", "bluetooth",
        "location", "microphone", "camera", "contacts", "photos access",
    ))


def _capture_ax_target(
    backend: ComputerUseBackend,
    *,
    app: Optional[str] = None,
    target: Optional[Dict[str, Any]] = None,
) -> CaptureResult:
    pinned = dict(target if target is not None else (getattr(backend, "_last_target", None) or {}))
    resolved_app = app or (
        None if target is not None else getattr(backend, "_last_app", None)
    )
    kwargs: Dict[str, Any] = {"mode": "ax", "app": resolved_app}
    if pinned.get("pid") is not None and pinned.get("window_id") is not None:
        kwargs.update({"pid": pinned["pid"], "window_id": pinned["window_id"]})
    cap = backend.capture(**kwargs)
    if pinned.get("pid") is None or pinned.get("window_id") is None:
        raise RuntimeError("exact pid/window is required")
    returned = getattr(backend, "_last_target", None) or {}
    for key in ("pid", "window_id"):
        if returned.get(key) != pinned[key] or getattr(cap, key, pinned[key]) != pinned[key]:
            raise RuntimeError("capture returned a different pid/window")
    for element in cap.elements:
        for key in ("pid", "window_id"):
            identity = getattr(element, key, None)
            if identity and identity != pinned[key]:
                raise RuntimeError("capture elements belong to a different pid/window")
    return cap


def _guard_capture(
    backend: ComputerUseBackend,
    cap: CaptureResult,
) -> Optional[CaptureResult]:
    """Return pixels only when semantic state is bound to the same capture."""
    if cap.mode == "ax":
        if _empty_ax_capture(cap) and _surface_kind(cap) != "login":
            return None if (
                _browser_capable_capture(cap, backend)
                or _sensitive_native_capture(cap)
            ) else cap
        return cap if _browser_semantics_complete(cap, backend) else None
    # SOM elements and pixels are produced by one backend capture, so the
    # caller can classify the exact state represented by the image. Vision
    # captures have no bound semantic state and must never be returned.
    if cap.elements:
        return cap if _browser_semantics_complete(cap, backend) else None
    ax_capture = _capture_ax_target(backend, app=cap.app or None)
    if not ax_capture.app:
        ax_capture.app = cap.app
    if not ax_capture.window_title:
        ax_capture.window_title = cap.window_title
    if _empty_ax_capture(ax_capture) and _surface_kind(ax_capture) != "login":
        if (
            _browser_capable_capture(ax_capture, backend)
            or _sensitive_native_capture(ax_capture)
        ):
            return None
        return cap
    if not _browser_semantics_complete(ax_capture, backend):
        return None
    # The AX probe supplies the safety semantics, but the original capture is
    # the requested visual artifact. Merge the validated elements into that
    # object instead of replacing it with the normally pixel-free AX result.
    cap.elements = ax_capture.elements
    if not cap.app:
        cap.app = ax_capture.app
    if not cap.window_title:
        cap.window_title = ax_capture.window_title
    return cap


def _invoke_auth_handoff(site: str) -> str:
    hermes_home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    script = hermes_home / "bin/website_auth_access.py"
    child_env = os.environ.copy()
    # computer_use has no target-bound broker session. Never inherit a handle
    # from another lane and accidentally route credentials to its browser.
    child_env.pop("HERMES_AUTH_SESSION_HANDLE", None)
    session_keys = ("PLATFORM", "CHAT_ID", "THREAD_ID", "USER_ID")
    for key in session_keys:
        child_env.pop(f"HERMES_SESSION_{key}", None)
    try:
        from gateway.session_context import get_session_env

        for key in session_keys:
            value = get_session_env(f"HERMES_SESSION_{key}", "")
            if value:
                child_env[f"HERMES_SESSION_{key}"] = str(value)
    except Exception:
        pass
    # Match the shared owner's existing deadline horizon, including setup.
    try:
        timeout = float(os.environ.get("HERMES_AUTH_HANDOFF_TIMEOUT_S", "600"))
    except (TypeError, ValueError):
        timeout = 600.0
    if not math.isfinite(timeout) or timeout < 1:
        timeout = 600.0
    try:
        proc = subprocess.run(
            [
                sys.executable,
                str(script),
                "handoff",
                "--site",
                site,
                "--reason",
                "computer_use reached a website login wall",
                "--lane",
                "computer_use",
            ],
            text=True,
            capture_output=True,
            check=False,
            env=child_env,
            timeout=timeout + 30.0,
        )
    except subprocess.TimeoutExpired:
        return "timeout"
    if proc.returncode != 0:
        return "timeout"
    try:
        status = json.loads(proc.stdout).get("status")
    except (json.JSONDecodeError, AttributeError):
        return "timeout"
    return status if status in {"done", "skip", "timeout"} else "timeout"


def _guard_sensitive_surface(
    backend: ComputerUseBackend,
    cap: CaptureResult,
    *,
    site: str = "",
) -> Optional[str]:
    kind = _surface_kind(cap)
    if kind in {"payment", "permission"}:
        surface_key = _sensitive_surface_key(backend)
        _blocked_sensitive_surfaces[surface_key] = kind
        response = {
            "error": f"computer_use refuses {kind} dialogs",
            "code": f"{kind}_dialog_refused",
        }
        if isinstance(surface_key, tuple):
            response["scope"] = "exact_window"
            response["hint"] = (
                "Capture a different exact pid/window before unrelated work; "
                "this surface remains refused."
            )
        return json.dumps(response)
    if kind != "login":
        if not (cap.mode == "ax" and _empty_ax_capture(cap)):
            surface_key = _sensitive_surface_key(backend)
            _blocked_sensitive_surfaces.pop(surface_key, None)
            blocked_target = _blocked_login_targets.get(id(backend))
            if (
                blocked_target is not None
                and surface_key
                == (id(backend), "window", blocked_target[0], blocked_target[1])
                and _browser_semantics_complete(cap, backend)
            ):
                _blocked_sensitive_surfaces.pop(id(backend), None)
                _blocked_login_targets.pop(id(backend), None)
        return None

    original_target = dict(getattr(backend, "_last_target", None) or {})
    if (
        original_target.get("pid") is None
        or original_target.get("window_id") is None
    ):
        return json.dumps({
            "status": "auth_required",
            "handoff_result": "timeout",
            "message": "could not bind the login wall to an exact pid/window",
        })

    global _computer_auth_pending, _computer_handoff_epoch
    backend_key = id(backend)
    owner = threading.get_ident()
    with _computer_state_changed:
        existing_owner = _sensitive_handoff_owner.get(backend_key)
        if _computer_auth_pending and existing_owner is None:
            return json.dumps({
                "status": "auth_required",
                "message": "computer input is frozen pending human authentication",
            })
        if existing_owner is not None and existing_owner != owner:
            return json.dumps({
                "status": "auth_required",
                "message": "computer input is frozen pending human authentication",
            })
        _sensitive_handoff_owner[backend_key] = owner
        _computer_auth_pending = True
        _computer_handoff_epoch += 1
        own_active = 1 if owner in _computer_active_threads else 0
        while _computer_active_operations > own_active:
            _computer_state_changed.wait()

    try:
        _write_shared_handoff_owner()
        _advance_shared_handoff_epoch()
    except Exception as exc:
        logger.warning("computer_use shared auth freeze setup failed: %s", exc)
        with _computer_state_changed:
            if _sensitive_handoff_owner.get(backend_key) == owner:
                _sensitive_handoff_owner.pop(backend_key, None)
            if not _sensitive_handoff_owner:
                _computer_auth_pending = False
            _computer_state_changed.notify_all()
        try:
            _clear_shared_handoff_owner()
        except OSError:
            pass
        return json.dumps({
            "status": "auth_required",
            "handoff_result": "timeout",
            "message": "could not establish the host-wide authentication freeze",
        })

    _blocked_sensitive_surfaces[backend_key] = "login"
    _blocked_login_targets[backend_key] = (
        original_target["pid"], original_target["window_id"]
    )

    try:
        # The caller blocks here, so no further agent input can reach this
        # backend while the human is authenticating. The captured image is
        # intentionally discarded and never returned to the model.
        # CU never supplies an opaque broker session handle, so this route
        # remains human-only. Native AX does not expose a trusted page origin;
        # never present the page-controlled window title as a site identity.
        decision = _invoke_auth_handoff(site or "unverified browser window")
        if decision != "done":
            return json.dumps({
                "status": "auth_required",
                "handoff_result": decision,
                "message": "website authentication was not completed",
            })

        try:
            verify = _capture_ax_target(
                backend,
                app=cap.app or None,
                target=original_target,
            )
        except Exception as exc:
            logger.warning(
                "computer_use post-auth verification capture failed: %s", exc
            )
            return json.dumps({
                "status": "auth_required",
                "handoff_result": "done",
                "message": "could not verify authentication from accessibility data",
            })
        if (
            _empty_ax_capture(verify)
            or not _browser_semantics_complete(verify, backend)
        ):
            return json.dumps({
                "status": "auth_required",
                "handoff_result": "done",
                "message": "could not verify authentication from accessibility data",
            })
        if _surface_kind(verify) is not None:
            return json.dumps({
                "status": "auth_required",
                "handoff_result": "done",
                "message": "sensitive surface is still present after Done",
            })
        _blocked_sensitive_surfaces.pop(backend_key, None)
        _blocked_login_targets.pop(backend_key, None)
        return json.dumps({
            "status": "ok",
            "handoff_result": "done",
            "message": "authentication verified; continue on the same controlled window",
        })
    finally:
        with _computer_state_changed:
            if _sensitive_handoff_owner.get(backend_key) == owner:
                _sensitive_handoff_owner.pop(backend_key, None)
            if not _sensitive_handoff_owner:
                _computer_auth_pending = False
                # Complete the two durable cleanup operations independently.
                # Either an even epoch or a missing owner record invalidates
                # the shared freeze. A failure in one must not prevent the
                # other, mask the handoff result, or skip local notification.
                try:
                    _advance_shared_handoff_epoch()
                except OSError as exc:
                    logger.warning(
                        "computer_use shared auth epoch cleanup failed: %s", exc
                    )
                try:
                    _clear_shared_handoff_owner()
                except OSError as exc:
                    logger.warning(
                        "computer_use shared auth owner cleanup failed: %s", exc
                    )
            _computer_state_changed.notify_all()



# Serialize native dispatch and keep the reviewed OS gate held through input and
# opaque human waits. Epoch rejection prevents queued calls replaying after Done.
_native_auth_dispatch_lock = threading.RLock()

def _auth_frozen():
    return json.dumps({"status": "auth_required", "message": "computer input is frozen pending human authentication"})

def _dispatch(backend: ComputerUseBackend, action: str, args: Dict[str, Any]) -> Any:
    epoch = _read_shared_handoff_epoch()
    if _computer_auth_pending or (epoch % 2 and _shared_handoff_owner_is_active()):
        return _auth_frozen()
    with _native_auth_dispatch_lock:
        if _computer_auth_pending or _read_shared_handoff_epoch() != epoch:
            return _auth_frozen()
        _enter_process_gate()
        try:
            if _read_shared_handoff_epoch() != epoch:
                return _auth_frozen()
            if epoch % 2:
                _recover_orphaned_shared_handoff(epoch)
                # Never replay the request that observed the stale auth epoch.
                return _auth_frozen()
            spec = _ACTIONS.get(action)
            if spec is not None and spec.input:
                requested_app = str(args.get("app") or "").strip()
                if requested_app and _input_target_mismatch(backend, requested_app):
                    return _dispatch_native(backend, action, args)
                # Explicit destination IDs take priority over the prior window.
                target = dict(getattr(backend, "_last_target", None) or {})
                if args.get("pid") is not None or args.get("window_id") is not None:
                    target = {key: args.get(key) for key in ("pid", "window_id")}
                try:
                    cap = _capture_ax_target(backend, app=args.get("app"), target=target)
                    cap = _guard_capture(backend, cap)
                except Exception:
                    cap = None
                if cap is None:
                    return json.dumps({"status": "auth_required", "code": "auth_window_unverified"})
                result = _guard_sensitive_surface(backend, cap)
                if result is not None:
                    return result
                element_indices = [args[key] for key in ("element", "from_element", "to_element") if args.get(key) is not None]
                if element_indices:
                    current = {element.index: _element_fingerprint(element, backend) for element in cap.elements}
                    safe = _native_safe_snapshot.get(id(backend), {})
                    if any(index not in current or safe.get(index) != current[index] for index in element_indices):
                        return json.dumps({"status": "auth_required", "message": "take a fresh safe capture before element-index input"})
                _native_safe_snapshot.pop(id(backend), None)
                if _blocked_sensitive_kind(backend, action, args):
                    return _auth_frozen()
            return _dispatch_native(backend, action, args)
        finally:
            _leave_process_gate()

def _auth_capture_response(backend, cap):
    cap = _guard_capture(backend, cap)
    if cap is None:
        return json.dumps({"status": "auth_required", "code": "auth_window_unverified"})
    guarded = _guard_sensitive_surface(backend, cap)
    if guarded is not None:
        return guarded
    _remember_native_safe_snapshot(backend, cap.elements[:_DEFAULT_MAX_ELEMENTS])
    return _capture_response(cap)
