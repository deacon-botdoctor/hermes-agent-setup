"""Materialize Golden's approved native Cua release in the SDK package lock."""
from pathlib import Path
import hashlib
import importlib.util
import json
import re
import sys

# Exact upstream v0.23.0 row, not a blanket authorization to replace other pins.
PREDECESSOR_SHA256 = 'ea564a99b7a2cad769dccce579e0d86ab6b0305ae959be8bf418945af92c19f3'
TARGET_ASSETS = {
    'darwin-arm64': 'macos-universal',
    'darwin-x64': 'macos-universal',
    'linux-arm64': 'linux-arm64',
    'linux-x64': 'linux-x86_64',
    'win32-arm64': 'windows-arm64',
    'win32-x64': 'windows-x86_64',
}


def _native_lock_state(hermes_dir: Path):
    root = Path(hermes_dir).resolve(strict=True)
    target = root / 'pm/lock.json'
    if target.is_symlink() or not target.resolve(strict=True).is_relative_to(root):
        raise RuntimeError('Native Cua lock must be inside the assembled SDK')
    golden = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        'golden_native_cua_release_contract', golden / 'kit/bin/ensure-cua-driver.py')
    if spec is None or spec.loader is None:
        raise RuntimeError('Golden native Cua contract loader is unavailable')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    contract = module.load_contract(golden / 'kit/config/cua-driver-release-v1.json')
    release = contract['release']
    if release['repository'] != 'trycua/cua':
        raise RuntimeError('Native Cua contract repository differs')
    artifacts = {}
    for platform, asset_key in TARGET_ASSETS.items():
        asset = contract['assets'][asset_key]
        if (not isinstance(asset, dict)
                or not re.fullmatch(r'[A-Za-z0-9_.-]+', str(asset.get('name', '')))
                or not re.fullmatch(r'[0-9a-f]{64}', str(asset.get('sha256', '')))):
            raise RuntimeError('Approved native Cua asset is malformed')
        artifacts[platform] = {
            'url': f"https://github.com/{release['repository']}/releases/download/{release['tag']}/{asset['name']}",
            'sha256': asset['sha256'],
        }
    approved = {'version': release['version'], 'artifacts': artifacts}
    data = json.loads(target.read_text())
    if data.get('schema') != 1 or not isinstance(data.get('packages'), dict):
        raise RuntimeError('Native package lock schema differs')
    return target, data, approved


def verify_native_cua_lock_v1(hermes_dir: Path) -> bool:
    """Read-only postimage proof against the same approved release contract."""
    _, data, approved = _native_lock_state(hermes_dir)
    return data['packages'].get('cua-driver') == approved


def patch_native_cua_lock_v1(hermes_dir: Path) -> bool:
    target, data, approved = _native_lock_state(hermes_dir)
    current = data['packages'].get('cua-driver')
    if current == approved:
        return False
    digest = hashlib.sha256(json.dumps(current, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if digest != PREDECESSOR_SHA256:
        raise RuntimeError('Native Cua package pin differs from the qualified predecessor')
    data['packages']['cua-driver'] = approved
    target.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')
    return True
