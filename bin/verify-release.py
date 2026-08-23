#!/usr/bin/env python3
"""Verify the public payload and, optionally, an assembled Hermes runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RELEASE_PATH = ROOT / "release.json"
SOURCE_MANIFEST_PATH = ROOT / "runtime-payload-source-manifest.json"
NATIVE_CONTINUITY_CONTRACT_PATH = (
    ROOT / "contracts/native-agent-continuity-release-v1.json"
)
RUNTIME_COHERENCE_FILES = (
    "maintenance/bin/install-runtime-coherence.py",
    "maintenance/launchd/com.hermes.runtime-coherence.plist.template",
    "maintenance/systemd/hermes-runtime-coherence@.service",
    "maintenance/systemd/hermes-runtime-coherence@.timer",
    "maintenance/windows/hermes-runtime-coherence-task.ps1.template",
    "checks/agent-runtime-coherence.py",
    "spec/runtime-coherence.json",
)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def _file_mode(path: Path) -> str:
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode):
        return "120000"
    return "100755" if mode & stat.S_IXUSR else "100644"


def _canonical_source_data(
    data: bytes, declared_blob: str, windows: bool
) -> bytes | None:
    if _git_blob_sha1(data) == declared_blob:
        return data
    if windows and b"\r\n" in data:
        normalized = data.replace(b"\r\n", b"\n")
        if _git_blob_sha1(normalized) == declared_blob:
            return normalized
    return None


def _tracked_mode(relative: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "--stage", "--", relative],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode:
        raise RuntimeError(
            (proc.stderr or f"cannot read tracked mode for {relative}").strip()
        )
    rows = [line for line in proc.stdout.splitlines() if line]
    if len(rows) != 1 or "\t" not in rows[0]:
        raise RuntimeError(f"cannot resolve tracked mode for {relative}")
    return rows[0].split(maxsplit=1)[0]


def _safe_path(raw: object) -> str:
    value = str(raw or "")
    path = Path(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or "\t" in value
        or "\n" in value
    ):
        raise ValueError(f"unsafe manifest path: {value!r}")
    return value


def verify_cua_driver_contract(
    release: dict[str, Any], errors: list[str]
) -> None:
    driver = release.get("cua_driver")
    if not isinstance(driver, dict):
        errors.append("release cua_driver contract is missing")
        return
    version = str(driver.get("version") or "")
    if (
        re.fullmatch(r"\d+(?:\.\d+){2,}", version) is None
        or driver.get("tag") != f"cua-driver-rs-v{version}"
        or re.fullmatch(r"[0-9a-f]{40}", str(driver.get("source_commit") or ""))
        is None
        or driver.get("baseline_acceptance") != "exact_version_present"
        or driver.get("gui_acceptance") != "doctor_ready_and_list_windows"
    ):
        errors.append("release cua_driver identity is invalid")
        return
    loaded: dict[str, dict[str, Any]] = {}
    for label in ("helper", "contract"):
        entry = driver.get(label)
        if not isinstance(entry, dict):
            errors.append(f"release cua_driver {label} is invalid")
            continue
        try:
            relative = _safe_path(entry.get("path"))
        except ValueError as exc:
            errors.append(f"release cua_driver {label}: {exc}")
            continue
        expected = str(entry.get("sha256") or "")
        path = ROOT / relative
        if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
            errors.append(f"release cua_driver {label} digest is invalid")
        elif not path.is_file() or path.is_symlink():
            errors.append(f"release cua_driver {label} is missing")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            errors.append(f"release cua_driver {label} digest mismatch")
        if path.is_file() and label == "helper" and not os.access(path, os.X_OK):
            errors.append("release cua_driver helper is not executable")
        if path.is_file() and label == "contract":
            try:
                payload = _read_json(path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"release cua_driver contract is invalid: {exc}")
            else:
                loaded[label] = payload
    contract = loaded.get("contract")
    if contract is not None:
        contract_release = contract.get("release")
        if not isinstance(contract_release, dict) or any(
            contract_release.get(field) != driver.get(field)
            for field in ("version", "tag", "source_commit")
        ):
            errors.append("release and packaged cua_driver contract disagree")


def verify_runtime_coherence_contract(
    release: dict[str, Any], errors: list[str]
) -> None:
    contract = release.get("runtime_coherence")
    if not isinstance(contract, dict):
        errors.append("release runtime_coherence contract is missing")
        return
    if (
        re.fullmatch(
            r"[0-9a-f]{40}", str(contract.get("source_commit") or "")
        )
        is None
        or contract.get("platforms") != ["macos", "linux", "windows"]
        or contract.get("file_count") != len(RUNTIME_COHERENCE_FILES)
    ):
        errors.append("release runtime_coherence identity is invalid")
        return

    canonical = ""
    actual: dict[str, str] = {}
    for relative in RUNTIME_COHERENCE_FILES:
        path = ROOT / relative
        if not path.is_file() or path.is_symlink():
            errors.append(f"release runtime_coherence file is missing: {relative}")
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        actual[relative] = digest
        canonical += f"{relative}\0{digest}\n"
    package_digest = hashlib.sha256(canonical.encode()).hexdigest()
    if package_digest != contract.get("package_digest"):
        errors.append("release runtime_coherence package digest mismatch")

    for label in ("installer", "probe"):
        entry = contract.get(label)
        if not isinstance(entry, dict):
            errors.append(f"release runtime_coherence {label} is invalid")
            continue
        try:
            relative = _safe_path(entry.get("path"))
        except ValueError as exc:
            errors.append(f"release runtime_coherence {label}: {exc}")
            continue
        if relative not in RUNTIME_COHERENCE_FILES:
            errors.append(f"release runtime_coherence {label} is not packaged")
        elif actual.get(relative) != entry.get("sha256"):
            errors.append(f"release runtime_coherence {label} digest mismatch")
        path = ROOT / relative
        if label == "installer" and path.is_file() and not os.access(path, os.X_OK):
            errors.append("release runtime_coherence installer is not executable")


def verify_native_agent_continuity_contract(
    release: dict[str, Any], errors: list[str]
) -> None:
    metadata = release.get("native_agent_continuity")
    if not isinstance(metadata, dict):
        errors.append("release native_agent_continuity contract is missing")
        return
    entry = metadata.get("contract")
    if not isinstance(entry, dict):
        errors.append("release native_agent_continuity contract pointer is invalid")
        return
    try:
        relative = _safe_path(entry.get("path"))
    except ValueError as exc:
        errors.append(f"release native_agent_continuity contract: {exc}")
        return
    expected_contract_sha = str(entry.get("sha256") or "")
    if (
        ROOT / relative != NATIVE_CONTINUITY_CONTRACT_PATH
        or re.fullmatch(r"[0-9a-f]{64}", expected_contract_sha) is None
        or not NATIVE_CONTINUITY_CONTRACT_PATH.is_file()
        or NATIVE_CONTINUITY_CONTRACT_PATH.is_symlink()
        or hashlib.sha256(NATIVE_CONTINUITY_CONTRACT_PATH.read_bytes()).hexdigest()
        != expected_contract_sha
    ):
        errors.append("release native_agent_continuity contract digest mismatch")
        return
    try:
        contract = _read_json(NATIVE_CONTINUITY_CONTRACT_PATH)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"release native_agent_continuity contract is invalid: {exc}")
        return
    if (
        contract.get("schema_version") != 1
        or contract.get("capability") != "native-agent-continuity"
        or re.fullmatch(r"[0-9a-f]{40}", str(contract.get("source_commit") or ""))
        is None
        or contract.get("platforms") != ["linux", "macos", "windows"]
        or contract.get("activation") != "manifest_driven_existing_selfheal"
        or any(
            metadata.get(field) != contract.get(field)
            for field in ("source_commit", "platforms", "activation", "package_digest")
        )
    ):
        errors.append("release native_agent_continuity identity is invalid")
        return
    declared = contract.get("files")
    if not isinstance(declared, list) or len(declared) != 10:
        errors.append("release native_agent_continuity file inventory is invalid")
        return
    actual: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in declared:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "mode"}:
            errors.append("release native_agent_continuity file entry is invalid")
            continue
        try:
            item = _safe_path(row.get("path"))
        except ValueError as exc:
            errors.append(f"release native_agent_continuity file: {exc}")
            continue
        if item in seen or not item.startswith("native-continuity/"):
            errors.append(f"release native_agent_continuity file ownership is invalid: {item}")
            continue
        seen.add(item)
        path = ROOT / item
        mode = str(row.get("mode") or "")
        digest = str(row.get("sha256") or "")
        if (
            re.fullmatch(r"0[0-7]{3}", mode) is None
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or not path.is_file()
            or path.is_symlink()
            or hashlib.sha256(path.read_bytes()).hexdigest() != digest
            or format(path.stat().st_mode & 0o777, "04o") != mode
        ):
            errors.append(f"release native_agent_continuity file drifted: {item}")
            continue
        actual.append({"path": item, "sha256": digest, "mode": mode})
    canonical = json.dumps(actual, sort_keys=True, separators=(",", ":")).encode()
    if (
        actual != declared
        or hashlib.sha256(canonical).hexdigest() != contract.get("package_digest")
    ):
        errors.append("release native_agent_continuity package digest mismatch")


def verify_public_source() -> tuple[dict[str, Any], list[str]]:
    release = _read_json(RELEASE_PATH)
    manifest = _read_json(SOURCE_MANIFEST_PATH)
    errors: list[str] = []

    if manifest.get("schema_version") != 1:
        errors.append("source manifest schema_version is not 1")
    if manifest.get("kind") != "golden_runtime_payload_manifest":
        errors.append("source manifest kind is invalid")
    if release.get("golden_sha") != manifest.get("golden_sha"):
        errors.append("release Golden SHA does not match source manifest")
    if release.get("canonical_upstream_sha") != manifest.get(
        "canonical_upstream_sha"
    ):
        errors.append("release upstream SHA does not match source manifest")
    verify_cua_driver_contract(release, errors)
    verify_runtime_coherence_contract(release, errors)
    verify_native_agent_continuity_contract(release, errors)

    components = manifest.get("components")
    if not isinstance(components, dict):
        return release, [*errors, "source manifest components are invalid"]

    component_digests: dict[str, str] = {}
    for name in sorted(components):
        component = components[name]
        if not isinstance(component, dict):
            errors.append(f"{name}: component is invalid")
            continue
        entries = component.get("files")
        if not isinstance(entries, list):
            errors.append(f"{name}: files is not a list")
            continue
        canonical: list[tuple[str, str]] = []
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                errors.append(f"{name}: file entry is invalid")
                continue
            try:
                relative = _safe_path(entry.get("path"))
            except ValueError as exc:
                errors.append(f"{name}: {exc}")
                continue
            if relative in seen:
                errors.append(f"{name}: duplicate path {relative}")
                continue
            seen.add(relative)
            path = ROOT / relative
            declared_mode = str(entry.get("mode") or "")
            declared_blob = str(entry.get("blob") or "")
            if entry.get("type") != "blob":
                errors.append(f"{name}: unsupported object type for {relative}")
                continue
            if not path.is_file() and not path.is_symlink():
                errors.append(f"{name}: missing {relative}")
                continue
            data = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
            windows = os.name == "nt"
            actual_mode = _tracked_mode(relative) if windows else _file_mode(path)
            canonical_data = _canonical_source_data(data, declared_blob, windows)
            if actual_mode != declared_mode:
                errors.append(
                    f"{name}: mode mismatch for {relative}: "
                    f"{actual_mode} != {declared_mode}"
                )
            if canonical_data is None:
                errors.append(f"{name}: blob mismatch for {relative}")
            canonical.append(
                (
                    relative,
                    f"{declared_mode} blob {declared_blob}\t{relative}\n",
                )
            )
        digest = hashlib.sha256(
            "".join(value for _path, value in sorted(canonical)).encode()
        ).hexdigest()
        component_digests[name] = digest
        if digest != component.get("digest"):
            errors.append(f"{name}: component digest mismatch")
        if len(entries) != component.get("file_count"):
            errors.append(f"{name}: file count mismatch")

    combined = "".join(
        f"{name}:{component_digests[name]}\n" for name in sorted(component_digests)
    )
    deployment_digest = hashlib.sha256(combined.encode()).hexdigest()
    if deployment_digest != manifest.get("deployment_digest"):
        errors.append("source manifest deployment digest mismatch")
    if deployment_digest != release.get("deployment_digest"):
        errors.append("release deployment digest mismatch")
    expected_component_fields = {
        "baseline_wiring": "baseline_wiring_digest",
        "runtime_payload": "runtime_payload_digest",
    }
    for component, field in expected_component_fields.items():
        if component_digests.get(component) != release.get(field):
            errors.append(f"release {field} mismatch")

    assembled = manifest.get("runtime_fingerprint")
    expected_assembled = release.get("assembled_runtime_fingerprint")
    if not isinstance(assembled, dict) or not isinstance(
        assembled.get("files"), dict
    ):
        errors.append("source manifest assembled runtime fingerprint is invalid")
    elif not isinstance(expected_assembled, dict):
        errors.append("release assembled runtime fingerprint is invalid")
    else:
        files = assembled["files"]
        if assembled.get("verified") is not True:
            errors.append("source manifest assembled runtime fingerprint is unverified")
        if assembled.get("golden_sha") != release.get("golden_sha"):
            errors.append("source manifest assembled runtime Golden SHA mismatch")
        for field in ("upstream_sha", "expected_upstream_sha"):
            if assembled.get(field) != release.get("canonical_upstream_sha"):
                errors.append(
                    f"source manifest assembled runtime {field} mismatch"
                )
        canonical = ""
        for relative in sorted(files):
            try:
                safe_relative = _safe_path(relative)
            except ValueError as exc:
                errors.append(f"assembled runtime: {exc}")
                continue
            entry = files[relative]
            if (
                safe_relative != relative
                or not isinstance(entry, dict)
                or entry.get("type") != "blob"
                or entry.get("mode") not in {"100644", "100755", "120000"}
                or re.fullmatch(
                    r"[0-9a-f]{64}", str(entry.get("sha256") or "")
                )
                is None
            ):
                errors.append(
                    f"assembled runtime: invalid file identity for {relative}"
                )
                continue
            canonical += (
                f"{relative}\t{entry['mode']}\t{entry['type']}\t"
                f"{entry['sha256']}\n"
            )
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        if digest != assembled.get("digest"):
            errors.append("source manifest assembled runtime digest mismatch")
        if len(files) != assembled.get("file_count"):
            errors.append("source manifest assembled runtime file count mismatch")
        for field in ("digest", "file_count"):
            if assembled.get(field) != expected_assembled.get(field):
                errors.append(
                    f"source manifest assembled runtime {field} mismatch"
                )

    return release, errors


def verify_runtime(
    runtime_dir: Path, release: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    command = [
        sys.executable,
        str(ROOT / "bin" / "runtime-payload-manifest.py"),
        "--repo",
        str(ROOT),
        "--source-manifest",
        str(SOURCE_MANIFEST_PATH),
        "--runtime-dir",
        str(runtime_dir),
        "--compact",
    ]
    proc = subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=120
    )
    if proc.returncode:
        return {}, [
            "runtime fingerprint command failed: "
            + (proc.stderr or proc.stdout).strip()[-1000:]
        ]
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {}, [f"runtime fingerprint output is invalid: {exc}"]
    fingerprint = payload.get("runtime_fingerprint")
    expected = release.get("assembled_runtime_fingerprint")
    errors: list[str] = []
    if not isinstance(fingerprint, dict) or fingerprint.get("verified") is not True:
        errors.append(
            "runtime fingerprint is unverified: "
            + str((fingerprint or {}).get("reason") or "unknown")
        )
        return payload, errors
    if not isinstance(expected, dict):
        errors.append("release assembled_runtime_fingerprint is invalid")
        return payload, errors
    for field in ("digest", "file_count"):
        if fingerprint.get(field) != expected.get(field):
            errors.append(f"runtime {field} does not match release")
    if fingerprint.get("upstream_sha") != release.get("canonical_upstream_sha"):
        errors.append("runtime upstream SHA does not match release")
    if fingerprint.get("golden_sha") != release.get("golden_sha"):
        errors.append("runtime Golden SHA does not match release")
    return payload, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        release, errors = verify_public_source()
        runtime_payload: dict[str, Any] | None = None
        if args.runtime_dir:
            runtime_payload, runtime_errors = verify_runtime(
                args.runtime_dir.expanduser().resolve(), release
            )
            errors.extend(runtime_errors)
        result = {
            "ok": not errors,
            "release": release.get("release"),
            "golden_sha": release.get("golden_sha"),
            "upstream_sha": release.get("canonical_upstream_sha"),
            "deployment_digest": release.get("deployment_digest"),
            "runtime_fingerprint": (
                (runtime_payload or {}).get("runtime_fingerprint")
                if args.runtime_dir
                else None
            ),
            "errors": errors,
        }
    except Exception as exc:
        result = {
            "ok": False,
            "errors": [f"{type(exc).__name__}: {exc}"],
        }

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif result["ok"]:
        suffix = " and assembled runtime" if args.runtime_dir else ""
        print(f"PASS: verified {result.get('release')}{suffix}")
    else:
        for error in result["errors"]:
            print(f"FAIL: {error}", file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
