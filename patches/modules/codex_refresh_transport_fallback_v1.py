#!/usr/bin/env python3
"""Normalize Codex OAuth refresh transport failures for gateway fallback."""

from __future__ import annotations

from pathlib import Path

MARKER = "HERMES_CODEX_REFRESH_TRANSPORT_FALLBACK_v1"


class PatchError(RuntimeError):
    pass


_TRANSPORT_ANCHOR = '''    timeout = httpx.Timeout(max(5.0, float(timeout_seconds)))
    with httpx.Client(
        timeout=timeout,
        headers={
            "Accept": "application/json",
            "User-Agent": CODEX_OAUTH_USER_AGENT,
        },
    ) as client:
        response = client.post(
            CODEX_OAUTH_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CODEX_OAUTH_CLIENT_ID,
            },
        )
'''

_TRANSPORT_REPLACEMENT = f'''    # [{MARKER}] A DNS, connect, or read failure means the
    # refresh endpoint is temporarily unavailable, not that the credential is
    # invalid. Normalize it to the typed boundary the gateway already uses to
    # enter the configured fallback chain; retain the original exception only
    # as the private cause and never copy endpoint details into the message.
    try:
        timeout = httpx.Timeout(max(5.0, float(timeout_seconds)))
        with httpx.Client(
            timeout=timeout,
            headers={{
                "Accept": "application/json",
                "User-Agent": CODEX_OAUTH_USER_AGENT,
            }},
        ) as client:
            response = client.post(
                CODEX_OAUTH_TOKEN_URL,
                headers={{"Content-Type": "application/x-www-form-urlencoded"}},
                data={{
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": CODEX_OAUTH_CLIENT_ID,
                }},
            )
    except httpx.RequestError as exc:
        raise AuthError(
            "Codex token refresh endpoint is temporarily unavailable.",
            provider="openai-codex",
            code="codex_refresh_unavailable",
            relogin_required=False,
        ) from exc
'''


_SPLIT_TRANSPORT_ANCHOR = '''    with _codex_http_client(
        timeout=httpx.Timeout(max(5.0, float(timeout_seconds))),
        headers={"Accept": "application/json", "User-Agent": CODEX_OAUTH_USER_AGENT}) as client:
        response = client.post(
            CODEX_OAUTH_TOKEN_URL, headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token", "refresh_token": refresh_token,
                "client_id": CODEX_OAUTH_CLIENT_ID})
'''


def patch_auth_source(source: str) -> str:
    if MARKER in source:
        return source
    if source.count(_SPLIT_TRANSPORT_ANCHOR) == 1:
        replacement = (
            f"    # [{MARKER}] Transport failure does not invalidate credentials.\n"
            "    try:\n"
            + "".join("    " + line for line in _SPLIT_TRANSPORT_ANCHOR.splitlines(keepends=True))
            + _TRANSPORT_REPLACEMENT[_TRANSPORT_REPLACEMENT.index("    except httpx.RequestError"):]
        )
        return source.replace(_SPLIT_TRANSPORT_ANCHOR, replacement, 1)
    if source.count(_TRANSPORT_ANCHOR) != 1:
        raise PatchError("required unique anchor missing: Codex refresh transport")
    return source.replace(_TRANSPORT_ANCHOR, _TRANSPORT_REPLACEMENT, 1)


def patch_codex_refresh_transport_fallback_v1(hermes_dir: Path) -> bool:
    target = hermes_dir / "hermes_cli" / "auth_codex.py"
    if not target.exists():
        target = hermes_dir / "hermes_cli" / "auth.py"
    if not target.exists():
        return False
    original = target.read_text(encoding="utf-8")
    patched = patch_auth_source(original)
    if patched == original:
        return False
    target.write_text(patched, encoding="utf-8")
    return True


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("hermes_dir", type=Path)
    args = parser.parse_args()
    changed = patch_codex_refresh_transport_fallback_v1(args.hermes_dir)
    print("patched" if changed else "already-patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
