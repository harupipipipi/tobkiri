"""Stage the original publishing build with exactly its thirteen real assets."""
from pathlib import Path
import shutil
import stat
import tomllib

from build_native import fingerprint
from fetch_sdk import inventory
from packager.package import ASSETS, MAX_OUTPUT


def publishing_manifest(original: bytes) -> bytes:
    """Add only the original alternative build entry to the root package."""
    document = tomllib.loads(original.decode())
    if (document['package']['name'] != 'componentize-py'
            or 'build' in document['package'] or original.count(b'[package]\n') != 1):
        raise ValueError('unexpected original producer package manifest')
    updated = original.replace(b'[package]\n', b'[package]\nbuild = "alt-build.rs"\n', 1)
    expected = {**document, 'package': {**document['package'], 'build': 'alt-build.rs'}}
    if tomllib.loads(updated.decode()) != expected:
        raise ValueError('publishing manifest changed unrelated source fields')
    return updated


def stage(source: Path, assets: Path, expected_assets: dict, destination: Path) -> dict:
    """Copy only authenticated original sources and complete packaged outputs."""
    if (not source.is_dir() or source.resolve() != source
            or not assets.is_dir() or assets.resolve() != assets
            or destination.parent.resolve() != destination.parent
            or set(expected_assets) != ASSETS):
        raise ValueError('physical original inputs and complete assets required')
    original = inventory(source)
    if len(original) > 1000:
        raise ValueError('original producer source file bound exceeded')
    if any(name in original for name in ASSETS):
        raise ValueError('original source unexpectedly contains packaged assets')
    if any('link' in entry for entry in original.values()):
        raise ValueError('producer source links are not accepted')
    manifest = publishing_manifest((source / 'Cargo.toml').read_bytes())
    destination.mkdir(mode=0o700)
    expected = dict(original)
    for name, entry in original.items():
        path = source / name
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError('regular original producer source required')
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target, follow_symlinks=False)
        if name == 'Cargo.toml':
            target.write_bytes(manifest)
            expected[name] = {**entry, **fingerprint(target)}
    for name, identity in expected_assets.items():
        path = assets / name
        if fingerprint(path, MAX_OUTPUT) != identity:
            raise ValueError('packaged producer input changed')
        target = destination / name
        with path.open('rb') as incoming, target.open('xb') as output:
            shutil.copyfileobj(incoming, output, 65536)
        target.chmod(0o644)
        expected[name] = {**identity, 'mode': oct(0o644)}
    if inventory(source) != original or inventory(destination) != expected:
        raise ValueError('publishing source differs from exact recorded derivation')
    return {'files': expected, 'original_files': original,
            'delta': 'only root package build=alt-build.rs and thirteen packaged assets'}
