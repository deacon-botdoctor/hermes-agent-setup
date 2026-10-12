#!/usr/bin/env python3
"""Build a public release from an already reviewed, sanitized source archive.

Only the declared source payload is replaced in an isolated bundle. This command
never changes the source checkout, a live runtime, a service, or a public branch.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "runtime-payload-source-manifest.json"
SOURCE_ROOTS = {"bin", "hooks", "installers.yaml", "kit", "mcp-servers", "patches",
                "plugins", "scripts", "shared-defaults", "shared-rules", "skills", "upstream.lock"}
PUBLIC_OWNER_FILES = {"bin/build-release.py", "bin/assemble-runtime.py", "bin/verify-release.py"}


def run(argv):
    result = subprocess.run(argv, check=True, capture_output=True, text=True)
    return result.stdout


def safe_path(value):
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or ".." in path.parts or "\\" in value
            or str(path) != value or any(part.startswith(".") and part != ".gitignore" for part in path.parts)):
        raise ValueError("Unsafe source path: " + value)
    return path


def read_artifact(path, digest, candidate):
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("Source artifact digest mismatch")
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:*") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if len(names) != len(set(names)) or any(not member.isfile() for member in members):
            raise ValueError("Archive must contain unique regular files only")
        for name in names:
            safe_path(name)
        if MANIFEST not in names:
            raise ValueError("Source manifest missing")
        manifest = json.load(archive.extractfile(MANIFEST))
        if (manifest.get("golden_sha") != candidate or manifest.get("schema_version") != 1
                or manifest.get("kind") != "golden_runtime_payload_manifest"
                or set(manifest.get("components", {})) != {"runtime_payload", "baseline_wiring"}):
            raise ValueError("Exact public source manifest required")
        fingerprint = manifest.get("runtime_fingerprint", {})
        if fingerprint.get("verified") is not True or "runtime_dir" in fingerprint:
            raise ValueError("Verified sanitized runtime fingerprint required")
        entries = {}
        for component in manifest["components"].values():
            for entry in component["files"]:
                name = entry["path"]
                relative = safe_path(name)
                if (relative.parts[0] not in SOURCE_ROOTS or name in PUBLIC_OWNER_FILES
                        or entry.get("type") != "blob" or entry.get("mode") not in {"100644", "100755"}):
                    raise ValueError("Source entry is outside the public payload: " + name)
                if name in entries and entries[name] != entry:
                    raise ValueError("Conflicting source entry: " + name)
                entries[name] = entry
        if set(names) != {MANIFEST, *entries}:
            raise ValueError("Archive files must exactly match the source manifest")
        files = {}
        for name, entry in entries.items():
            data = archive.extractfile(name).read()
            actual = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
            if actual != entry["blob"]:
                raise ValueError("Source blob mismatch: " + name)
            files[name] = (data, 0o755 if entry["mode"] == "100755" else 0o644)
    return manifest, files


def copy_checkout(root, destination):
    records = run(["git", "-C", str(root), "ls-files", "--stage", "-z"]).split("\0")
    digest = hashlib.sha256()
    for record in filter(None, records):
        metadata, name = record.split("\t", 1)
        mode, _blob, stage = metadata.split()
        if mode not in {"100644", "100755"} or stage != "0":
            raise ValueError("Public checkout has unsupported or unmerged entries")
        relative = safe_path(name) if not name.startswith(".") else PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or relative.parts[0] == ".git":
            raise ValueError("Unsafe tracked path")
        source = root / relative
        if source.is_symlink() or not source.is_file():
            raise ValueError("Public checkout must contain regular tracked files: " + name)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        data = source.read_bytes()
        target.write_bytes(data)
        target.chmod(0o755 if mode == "100755" else 0o644)
        digest.update(f"{mode} {name}\0".encode() + hashlib.sha256(data).digest())
    return digest.hexdigest()


def verify(bundle, runtime=None):
    argv = [sys.executable, str(bundle / "bin/verify-release.py"), "--json"]
    if runtime is not None:
        argv.extend(["--runtime-dir", str(runtime)])
    report = json.loads(run(argv))
    if report.get("ok") is not True:
        raise ValueError("Public release verification failed")
    return report


def build_release(root, artifact, digest, candidate, release_id, upstream_source=None):
    if not re.fullmatch(r"[0-9a-f]{40}", candidate) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Exact source identities required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", release_id):
        raise ValueError("Invalid release identifier")
    manifest, files = read_artifact(artifact, digest, candidate)
    identity = {"release_id": release_id, "candidate": candidate, "source_artifact_sha256": digest}
    parent = root.parent / "public-release-builds"
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".public-build-", dir=parent) as temporary:
        staging = Path(temporary)
        bundle = staging / "bundle"
        checkout_digest = copy_checkout(root, bundle)
        identity["public_checkout_sha256"] = checkout_digest
        destination = parent / (release_id + "-" + digest[:16] + "-" + checkout_digest[:12])
        receipt_path = destination / "receipt.json"
        if destination.exists():
            receipt = json.loads(receipt_path.read_text())
            if any(receipt.get(k) != v for k, v in identity.items()) or receipt.get("status") != "built":
                raise ValueError("Existing public build has another identity or is incomplete")
            verify(destination / "bundle", destination / "runtime")
            return receipt_path
        old_manifest = json.loads((bundle / MANIFEST).read_text())
        for component in old_manifest["components"].values():
            for entry in component["files"]:
                relative = safe_path(entry["path"])
                if str(relative) not in files:
                    (bundle / relative).unlink(missing_ok=True)
        for name, (data, mode) in files.items():
            target = bundle / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(mode)
        (bundle / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        release = json.loads((bundle / "release.json").read_text())
        release.update(release=release_id, status="candidate", golden_sha=candidate,
            canonical_upstream_sha=manifest["canonical_upstream_sha"], deployment_digest=manifest["deployment_digest"],
            runtime_payload_digest=manifest["components"]["runtime_payload"]["digest"],
            baseline_wiring_digest=manifest["components"]["baseline_wiring"]["digest"],
            assembled_runtime_fingerprint={k: manifest["runtime_fingerprint"][k] for k in ("digest", "file_count")},
            verification={"golden_suite": "Exact reviewed Golden source: " + candidate,
                          "clean_upstream_rehearsal": "The public build must reproduce the declared runtime fingerprint."})
        helper = release["cua_driver"]["helper"]
        helper["sha256"] = hashlib.sha256((bundle / helper["path"]).read_bytes()).hexdigest()
        (bundle / "release.json").write_text(json.dumps(release, indent=2, sort_keys=True) + "\n")
        verify(bundle)
        argv = [sys.executable, str(bundle / "bin/assemble-runtime.py"), "--output", str(staging / "runtime")]
        if upstream_source is not None:
            argv.extend(["--upstream-source", str(upstream_source)])
        prior_umask = os.umask(0o022)
        try:
            run(argv)
        finally:
            os.umask(prior_umask)
        proof = verify(bundle, staging / "runtime")
        receipt = {"schema_version": 1, "kind": "botdoctor_public_release_build", **identity,
                   "status": "built", "runtime_fingerprint": release["assembled_runtime_fingerprint"],
                   "source_verified": True, "runtime_verified": True}
        if proof["runtime_fingerprint"]["digest"] != receipt["runtime_fingerprint"]["digest"]:
            raise ValueError("Reproduced runtime fingerprint differs from the release")
        release["verification"]["clean_upstream_rehearsal"] = (
            "Clean pinned upstream assembly reproduced " + receipt["runtime_fingerprint"]["digest"]
            + " across " + str(receipt["runtime_fingerprint"]["file_count"]) + " changed runtime files.")
        (bundle / "release.json").write_text(json.dumps(release, indent=2, sort_keys=True) + "\n")
        verify(bundle)
        (staging / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        if destination.exists():
            raise ValueError("A concurrent build already owns this release destination")
        staging.rename(destination)
    return receipt_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--source-artifact", type=Path, required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--upstream-source", type=Path)
    args = parser.parse_args()
    receipt = build_release(ROOT, args.source_artifact, args.source_sha256, args.candidate,
                            args.release_id, args.upstream_source)
    print(json.dumps({"ok": True, "receipt": str(receipt)}))


if __name__ == "__main__":
    main()
