#!/usr/bin/env python3
"""Create and verify the single immutable release asset inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import stat
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


TARGET_SCHEMA = "io.tobkiri.release.target-manifest.v1"
INVENTORY_SCHEMA = "io.tobkiri.release.inventory.v1"
TARGET_MANIFEST = "release-target.json"
REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
TARGETS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "aarch64-apple-darwin": ("macos", "arm64", (".dmg",)),
    "x86_64-apple-darwin": ("macos", "x86_64", (".dmg",)),
    "x86_64-pc-windows-msvc": ("windows", "x86_64", (".exe",)),
    "x86_64-unknown-linux-gnu": ("linux", "x86_64", (".deb", ".AppImage")),
}


class InventoryError(RuntimeError):
    """Raised when a release target or inventory is incomplete or altered."""


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _validate_revision(value: object) -> str:
    if not isinstance(value, str) or REVISION_PATTERN.fullmatch(value) is None:
        raise InventoryError("source_revision must be a full lowercase Git SHA")
    return value


def _validate_filename(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or Path(value).name != value
    ):
        raise InventoryError(f"{label} must be one safe relative file name")
    return value


def _validate_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or DIGEST_PATTERN.fullmatch(value) is None:
        raise InventoryError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _require_regular_file(path: Path, label: str) -> None:
    """Reject links and special files before reading release metadata."""
    try:
        metadata = path.lstat()
    except OSError as error:
        raise InventoryError(f"{label} is unavailable: {path}") from error
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise InventoryError(f"{label} must be a regular file: {path}")


def _target_contract(
    target: str, platform: str | None = None, architecture: str | None = None
) -> tuple[str, str, tuple[str, ...]]:
    try:
        expected_platform, expected_architecture, suffixes = TARGETS[target]
    except KeyError as error:
        raise InventoryError(f"unsupported release target: {target}") from error
    if platform is not None and platform != expected_platform:
        raise InventoryError(f"target/platform mismatch for {target}")
    if architecture is not None and architecture != expected_architecture:
        raise InventoryError(f"target/architecture mismatch for {target}")
    return expected_platform, expected_architecture, suffixes


def _file_record(path: Path, relative: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise InventoryError(f"release asset is not a regular file: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return {"path": relative, "sha256": f"sha256:{digest.hexdigest()}", "size": size}


def _check_artifact_suffixes(
    records: Iterable[Mapping[str, Any]], suffixes: tuple[str, ...]
) -> None:
    names: list[str] = []
    for record in records:
        path = record.get("path")
        if not isinstance(path, str):
            raise InventoryError("release target contains a malformed asset path")
        names.append(path)
    if len(names) != len(set(names)):
        raise InventoryError("release target contains duplicate asset paths")
    for suffix in suffixes:
        matching = [name for name in names if name.endswith(suffix)]
        if len(matching) != 1:
            raise InventoryError(
                f"release target must contain exactly one {suffix} asset; found {len(matching)}"
            )
    expected_count = len(suffixes)
    if len(names) != expected_count:
        raise InventoryError(
            f"release target has unexpected asset count: expected {expected_count}, found {len(names)}"
        )


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    if path.is_symlink():
        raise InventoryError(f"release metadata output may not be a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical_json(value))


def collect_target(
    output_root: Path,
    source_dirs: Sequence[Path],
    source_revision: str,
    target: str,
    platform: str,
    architecture: str,
) -> Path:
    """Copy exactly the platform assets and write their bound target manifest."""
    expected_platform, expected_architecture, suffixes = _target_contract(
        target, platform, architecture
    )
    _validate_revision(source_revision)
    if output_root.exists() and output_root.is_symlink():
        raise InventoryError(f"release upload root may not be a symlink: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)
    for child in output_root.iterdir():
        if child.name != TARGET_MANIFEST:
            raise InventoryError(f"release upload root is not empty: {child}")

    candidates: list[Path] = []
    for source_dir in source_dirs:
        if source_dir.is_symlink() or not source_dir.is_dir():
            raise InventoryError(
                f"release bundle directory is missing or symlinked: {source_dir}"
            )
        candidates.extend(
            path
            for path in sorted(source_dir.iterdir())
            if path.is_symlink() or (path.is_file() and path.suffix in suffixes)
        )
        if any(path.is_symlink() for path in candidates):
            raise InventoryError(f"release target contains a symlink: {source_dir}")
    if len({path.name for path in candidates}) != len(candidates):
        raise InventoryError("release target contains duplicate asset names")
    for suffix in suffixes:
        matching = [path for path in candidates if path.name.endswith(suffix)]
        if len(matching) != 1:
            raise InventoryError(
                f"release target must produce exactly one {suffix} asset; found {len(matching)}"
            )
    if len(candidates) != len(suffixes):
        raise InventoryError(
            f"release target produced unexpected asset count: {len(candidates)}"
        )

    for source in candidates:
        destination = output_root / source.name
        shutil.copyfile(source, destination)
    records = [
        _file_record(output_root / path.name, path.name)
        for path in sorted(candidates, key=lambda value: value.name)
    ]
    _check_artifact_suffixes(records, suffixes)
    manifest = {
        "schema": TARGET_SCHEMA,
        "source_revision": source_revision,
        "target": target,
        "platform": expected_platform,
        "architecture": expected_architecture,
        "artifacts": records,
    }
    manifest_path = output_root / TARGET_MANIFEST
    _write_json(manifest_path, manifest)
    return manifest_path


def _load_object(path: Path) -> dict[str, Any]:
    _require_regular_file(path, "release metadata")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InventoryError(f"invalid release inventory JSON: {path}") from error
    if not isinstance(value, dict):
        raise InventoryError(f"release inventory is not an object: {path}")
    return value


def _verify_target_manifest(
    manifest_path: Path, expected_source_revision: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = _load_object(manifest_path)
    if manifest.get("schema") != TARGET_SCHEMA:
        raise InventoryError(f"unexpected target manifest schema: {manifest_path}")
    source_revision = _validate_revision(manifest.get("source_revision"))
    if source_revision != expected_source_revision:
        raise InventoryError(
            f"target manifest source revision mismatch: {manifest_path}"
        )
    target = manifest.get("target")
    platform = manifest.get("platform")
    architecture = manifest.get("architecture")
    if not all(isinstance(value, str) for value in (target, platform, architecture)):
        raise InventoryError(f"target manifest identity is incomplete: {manifest_path}")
    expected_platform, expected_architecture, suffixes = _target_contract(
        target, platform, architecture
    )
    raw_records = manifest.get("artifacts")
    if not isinstance(raw_records, list) or not all(
        isinstance(record, dict) for record in raw_records
    ):
        raise InventoryError(
            f"target manifest artifacts are malformed: {manifest_path}"
        )
    records = [dict(record) for record in raw_records]
    _check_artifact_suffixes(records, suffixes)
    root = manifest_path.parent
    if root.is_symlink():
        raise InventoryError(f"target manifest directory may not be a symlink: {root}")
    if root.name != target:
        raise InventoryError(
            f"target manifest directory does not match target: {manifest_path}"
        )
    actual_files = [path for path in root.iterdir() if path.name != TARGET_MANIFEST]
    if any(path.is_symlink() or not path.is_file() for path in actual_files):
        raise InventoryError(
            f"target manifest directory contains a non-file entry: {root}"
        )
    safe_paths = {
        _validate_filename(record.get("path"), "target manifest asset path")
        for record in records
    }
    actual_names = {path.name for path in actual_files}
    manifest_names = safe_paths
    if actual_names != manifest_names:
        raise InventoryError(f"release asset missing, extra, or replaced: {root}")
    for record in records:
        relative = _validate_filename(record.get("path"), "target manifest asset path")
        digest = record.get("sha256")
        size = record.get("size")
        _validate_digest(digest, "target manifest asset digest")
        if type(size) is not int or size < 0:
            raise InventoryError(
                f"target manifest asset metadata is malformed: {manifest_path}"
            )
        actual = _file_record(root / relative, relative)
        if actual != {"path": relative, "sha256": digest, "size": size}:
            raise InventoryError(
                f"release asset digest or size mismatch: {root / relative}"
            )
        record["sha256"] = digest
        record["size"] = size
    return {
        "source_revision": source_revision,
        "target": target,
        "platform": expected_platform,
        "architecture": expected_architecture,
    }, sorted(records, key=lambda record: str(record["path"]))


def _required_target_set(required_targets: Sequence[str] | None) -> set[str]:
    """Return one explicit, supported, duplicate-free release target set."""

    values = list(TARGETS) if required_targets is None else list(required_targets)
    if not values:
        raise InventoryError("release inventory requires at least one target")
    if any(not isinstance(value, str) or not value for value in values):
        raise InventoryError("release inventory target requirement is malformed")
    if len(values) != len(set(values)):
        raise InventoryError("release inventory target requirement is duplicated")
    unsupported = sorted(set(values) - set(TARGETS))
    if unsupported:
        raise InventoryError(f"unsupported required release targets: {unsupported}")
    return set(values)


def create_inventory(
    root: Path,
    output: Path,
    assets_dir: Path,
    source_revision: str,
    tag: str,
    required_targets: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Verify every uploaded target and create one sorted release inventory."""
    expected_targets = _required_target_set(required_targets)
    _validate_revision(source_revision)
    if not tag or not tag.startswith("v"):
        raise InventoryError("release inventory requires a v-prefixed tag")
    manifests = sorted(root.rglob(TARGET_MANIFEST))
    if len(manifests) != len(expected_targets):
        raise InventoryError(
            "release inventory requires exactly "
            f"{len(expected_targets)} target manifests; found {len(manifests)}"
        )
    identities: dict[str, dict[str, Any]] = {}
    all_artifacts: list[dict[str, Any]] = []
    for manifest_path in manifests:
        identity, records = _verify_target_manifest(manifest_path, source_revision)
        target = str(identity["target"])
        if target in identities:
            raise InventoryError(f"duplicate release target: {target}")
        identities[target] = identity
        for record in records:
            all_artifacts.append(
                {
                    **identity,
                    "asset_name": f"{target}--{record['path']}",
                    "path": f"{target}/{record['path']}",
                    "sha256": record["sha256"],
                    "size": record["size"],
                }
            )
    if set(identities) != expected_targets:
        missing = sorted(expected_targets - set(identities))
        extra = sorted(set(identities) - expected_targets)
        raise InventoryError(
            f"release targets missing or unexpected: missing={missing}, extra={extra}"
        )
    asset_names = [str(record["asset_name"]) for record in all_artifacts]
    if len(asset_names) != len(set(asset_names)):
        raise InventoryError("release inventory contains duplicate asset names")
    all_artifacts.sort(key=lambda record: str(record["asset_name"]))
    inventory = {
        "schema": INVENTORY_SCHEMA,
        "tag": tag,
        "source_revision": source_revision,
        "targets": [identities[target] for target in sorted(identities)],
        "artifacts": all_artifacts,
    }
    if assets_dir.exists() and assets_dir.is_symlink():
        raise InventoryError(
            f"release asset directory may not be a symlink: {assets_dir}"
        )
    if assets_dir.exists() and any(assets_dir.iterdir()):
        raise InventoryError(f"release asset directory is not empty: {assets_dir}")
    _write_json(output, inventory)
    assets_dir.mkdir(parents=True, exist_ok=True)
    for record in all_artifacts:
        source = root / str(record["path"])
        destination = assets_dir / str(record["asset_name"])
        if destination.exists() or destination.is_symlink():
            raise InventoryError(f"duplicate release asset destination: {destination}")
        shutil.copyfile(source, destination)
    return inventory


def verify_inventory(
    inventory_path: Path,
    assets_dir: Path,
    source_revision: str,
    tag: str,
    inventory_sha256: str | None = None,
    required_targets: Sequence[str] | None = None,
) -> None:
    """Reject missing, duplicated, replaced, or unexpected final release assets."""
    expected_targets = _required_target_set(required_targets)
    inventory = _load_object(inventory_path)
    if inventory.get("schema") != INVENTORY_SCHEMA:
        raise InventoryError("unexpected release inventory schema")
    actual_inventory_digest = (
        f"sha256:{hashlib.sha256(inventory_path.read_bytes()).hexdigest()}"
    )
    if inventory_sha256 is not None and actual_inventory_digest != inventory_sha256:
        raise InventoryError("release inventory digest changed after attestation")
    if (
        inventory.get("tag") != tag
        or inventory.get("source_revision") != source_revision
    ):
        raise InventoryError("release inventory source or tag binding mismatch")
    _validate_revision(source_revision)
    if assets_dir.is_symlink() or not assets_dir.is_dir():
        raise InventoryError(
            f"release asset directory is missing or symlinked: {assets_dir}"
        )
    raw_targets = inventory.get("targets")
    if not isinstance(raw_targets, list) or not all(
        isinstance(target, dict) for target in raw_targets
    ):
        raise InventoryError("release inventory targets are malformed")
    target_identities: dict[str, dict[str, str]] = {}
    for raw_target in raw_targets:
        target = raw_target.get("target")
        platform = raw_target.get("platform")
        architecture = raw_target.get("architecture")
        target_source_revision = raw_target.get("source_revision")
        if not all(
            isinstance(value, str)
            for value in (target, platform, architecture, target_source_revision)
        ):
            raise InventoryError("release inventory target identity is malformed")
        expected_platform, expected_architecture, _ = _target_contract(
            target, platform, architecture
        )
        if target_source_revision != source_revision:
            raise InventoryError("release inventory target source binding mismatch")
        if target in target_identities:
            raise InventoryError(f"duplicate release inventory target: {target}")
        target_identities[target] = {
            "source_revision": target_source_revision,
            "target": target,
            "platform": expected_platform,
            "architecture": expected_architecture,
        }
    if set(target_identities) != expected_targets:
        raise InventoryError("release inventory targets are missing or unexpected")

    artifacts = inventory.get("artifacts")
    if not isinstance(artifacts, list) or not all(
        isinstance(record, dict) for record in artifacts
    ):
        raise InventoryError("release inventory artifacts are malformed")
    expected_names: list[str] = []
    counts: dict[str, dict[str, int]] = {
        target: {suffix: 0 for suffix in TARGETS[target][2]}
        for target in expected_targets
    }
    for record in artifacts:
        target = record.get("target")
        platform = record.get("platform")
        architecture = record.get("architecture")
        record_source_revision = record.get("source_revision")
        if not all(
            isinstance(value, str)
            for value in (
                target,
                platform,
                architecture,
                record_source_revision,
            )
        ):
            raise InventoryError("release inventory artifact identity is malformed")
        if record_source_revision != source_revision:
            raise InventoryError("release inventory artifact source binding mismatch")
        if target not in expected_targets:
            raise InventoryError(
                "release inventory artifact target is missing or unexpected"
            )
        expected_platform, expected_architecture, suffixes = _target_contract(
            target, platform, architecture
        )
        raw_path = record.get("path")
        if not isinstance(raw_path, str):
            raise InventoryError("release inventory artifact path is malformed")
        relative = _validate_filename(
            raw_path.removeprefix(f"{target}/"),
            "release inventory artifact path",
        )
        expected_path = f"{target}/{relative}"
        if raw_path != expected_path:
            raise InventoryError("release inventory artifact path binding mismatch")
        expected_asset_name = f"{target}--{relative}"
        asset_name = _validate_filename(
            record.get("asset_name"), "release inventory asset name"
        )
        if asset_name != expected_asset_name or asset_name == inventory_path.name:
            raise InventoryError("release inventory asset name binding mismatch")
        if not any(relative.endswith(suffix) for suffix in suffixes):
            raise InventoryError("release inventory artifact suffix is not allowed")
        for suffix in suffixes:
            if relative.endswith(suffix):
                counts[target][suffix] += 1
                break
        _validate_digest(record.get("sha256"), "release inventory asset digest")
        if type(record.get("size")) is not int or record["size"] < 0:
            raise InventoryError("release inventory asset size is malformed")
        if platform != expected_platform or architecture != expected_architecture:
            raise InventoryError("release inventory artifact platform binding mismatch")
        expected_names.append(asset_name)
    if len(expected_names) != len(set(expected_names)):
        raise InventoryError("release inventory contains duplicate asset names")
    if any(
        counts[target][suffix] != 1
        for target, suffix_counts in counts.items()
        for suffix in suffix_counts
    ):
        raise InventoryError(
            "release inventory has missing, duplicate, or unexpected assets"
        )
    actual_paths = [
        path for path in assets_dir.iterdir() if path.name != inventory_path.name
    ]
    if any(path.is_symlink() or not path.is_file() for path in actual_paths):
        raise InventoryError("final release asset directory contains a non-file entry")
    actual_names = {path.name for path in actual_paths}
    if actual_names != set(expected_names):
        raise InventoryError(
            "final release assets are missing, duplicated, or unexpected"
        )
    for record in artifacts:
        asset_name = _validate_filename(
            record.get("asset_name"), "release inventory asset name"
        )
        actual = _file_record(assets_dir / asset_name, asset_name)
        expected = {
            "path": asset_name,
            "sha256": record.get("sha256"),
            "size": record.get("size"),
        }
        if actual != expected:
            raise InventoryError(
                f"final release asset digest or size mismatch: {asset_name}"
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser("collect-target")
    collect.add_argument("--output-root", type=Path, required=True)
    collect.add_argument("--source-dir", type=Path, action="append", required=True)
    collect.add_argument("--source-revision", required=True)
    collect.add_argument("--target", required=True)
    collect.add_argument("--platform", required=True)
    collect.add_argument("--architecture", required=True)

    inventory = subparsers.add_parser("create")
    inventory.add_argument("--root", type=Path, required=True)
    inventory.add_argument("--output", type=Path, required=True)
    inventory.add_argument("--assets-dir", type=Path, required=True)
    inventory.add_argument("--source-revision", required=True)
    inventory.add_argument("--tag", required=True)
    inventory.add_argument("--required-target", action="append", dest="required_targets")

    verify = subparsers.add_parser("verify")
    verify.add_argument("--inventory", type=Path, required=True)
    verify.add_argument("--assets-dir", type=Path, required=True)
    verify.add_argument("--source-revision", required=True)
    verify.add_argument("--tag", required=True)
    verify.add_argument("--inventory-sha256")
    verify.add_argument("--required-target", action="append", dest="required_targets")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "collect-target":
            path = collect_target(
                args.output_root,
                args.source_dir,
                args.source_revision,
                args.target,
                args.platform,
                args.architecture,
            )
            print(path)
        elif args.command == "create":
            inventory = create_inventory(
                args.root,
                args.output,
                args.assets_dir,
                args.source_revision,
                args.tag,
                args.required_targets,
            )
            digest = hashlib.sha256(args.output.read_bytes()).hexdigest()
            print(
                json.dumps(
                    {
                        "inventory_sha256": f"sha256:{digest}",
                        "assets": len(inventory["artifacts"]),
                    },
                    sort_keys=True,
                )
            )
        else:
            verify_inventory(
                args.inventory,
                args.assets_dir,
                args.source_revision,
                args.tag,
                args.inventory_sha256,
                args.required_targets,
            )
            print("release inventory verification passed")
    except (InventoryError, OSError, ValueError) as error:
        print(f"release inventory failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
