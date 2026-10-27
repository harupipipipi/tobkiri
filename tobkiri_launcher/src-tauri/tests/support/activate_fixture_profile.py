"""Build isolated Host authority inputs using the original Profile capture API.

Only the native metadata integration test invokes this helper. It neither
supplies an invented execution identity nor proves the product GUI approval.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys


MANIFEST = "runtime-resource-manifest.v1.json"


def regular_files(root: Path, *, skip: tuple[str, ...] = ()) -> list[Path]:
    """Enumerate actual regular bytes without following filesystem aliases."""
    result = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if name not in skip]
        for name in directories + filenames:
            path = Path(current) / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ValueError(f"non-regular fixture resource: {path}")
            if path.is_file():
                if path.suffix in {".pyc", ".pyo"}:
                    raise ValueError(
                        "fixture resources must contain source, not bytecode"
                    )
                result.append(path)
    return sorted(result)


def stage(source: Path, host: Path) -> None:
    """Place unchanged original code beside the already generated Profile."""
    for path in regular_files(
        source, skip=(".git", "__pycache__", "node_modules", "tests")
    ):
        relative = path.relative_to(source)
        if relative.parts[:3] in {
            ("ecosystem", "defaultspack", "v4"),
            ("ecosystem", "defaultspack", "platform-artifacts"),
        }:
            continue
        if path.suffix != ".py" and not (
            path.suffix == ".json" and "schemas" in relative.parts
        ):
            continue
        destination = host / relative
        if destination.exists():
            if (
                not destination.is_file()
                or destination.read_bytes() != path.read_bytes()
            ):
                raise ValueError(
                    f"refusing to replace an existing fixture resource: {relative}"
                )
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)

    # Same schema, complete final byte inventory and hashes as normal staging.
    # Rust's production verifier checks canonical paths, ambiguity and the full
    # file set before the separate activation process is allowed to run.
    entries = []
    for path in regular_files(host):
        if path.name == MANIFEST:
            continue
        payload = path.read_bytes()
        entries.append(
            {
                "path": path.relative_to(host).as_posix(),
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    (host / MANIFEST).write_text(
        json.dumps(
            {
                "schema": "io.tobkiri.runtime-resource-manifest.v1",
                "entries": entries,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def activate(host: Path, user_data: Path) -> None:
    """Select the real confirmation and publish its original activation."""
    if (user_data / "profiles" / "active.json").exists():
        raise ValueError("fixture must begin without an active Profile")
    os.environ["TOBKIRI_USER_DATA"] = str(user_data)
    sys.path.insert(0, str(host))
    from ecosystem.defaultspack.defaultspack import profile_runtime_composition
    from core_runtime.bootstrap import profile_capture
    from core_runtime.profile_runtime_port import require_profile_runtime

    for module in (profile_runtime_composition, profile_capture):
        if not Path(module.__file__).resolve(strict=True).is_relative_to(host):
            raise ValueError("original fixture module was imported from another Host")
    runtime = profile_runtime_composition.install_defaultspack_profile_runtime()
    if runtime is not require_profile_runtime():
        raise ValueError("original Profile runtime singleton was replaced")
    if runtime.host_resource_root().resolve(strict=True) != host:
        raise ValueError("original runtime selected a different Host root")
    if runtime.bundled_profile_root().resolve(strict=True) != (
        host / "ecosystem" / "defaultspack" / "v4"
    ).resolve(strict=True):
        raise ValueError("original runtime selected a different Profile bundle")
    if profile_capture.runtime_user_data_root().resolve(strict=True) != user_data:
        raise ValueError("original runtime selected a different state owner")

    # The test explicitly chooses this real confirmation. This is not evidence
    # that a human operated the product's Setup/approval windows.
    confirmation = profile_capture.prepare_default_profile_confirmation()
    active = profile_capture.capture_default_profile(confirmation=confirmation)
    recaptured = profile_capture.capture_active_profile()
    if active.activation != recaptured.activation:
        raise ValueError("published activation differs from the actual capture")
    if active.resolved.profile["profile_id"] != runtime.bootstrap_profile_id():
        raise ValueError("capture selected a different Profile")
    if not (user_data / "profiles" / "active.json").is_file():
        raise ValueError("original capture did not publish its Host pointer")


def main() -> None:
    """Keep every source, state and temporary input inside one fixture owner."""
    action, owner_arg, source_arg, host_arg, user_arg = sys.argv[1:]
    owner = Path(owner_arg).resolve(strict=True)
    source = Path(source_arg).resolve(strict=True)
    host = Path(host_arg).resolve(strict=True)
    user_data = Path(user_arg)
    if user_data.is_symlink():
        raise ValueError("fixture state must not be a symlink")
    user_data.mkdir(parents=True, exist_ok=True)
    user_data = user_data.resolve(strict=True)
    if not all(
        path != owner and path.is_relative_to(owner)
        for path in (source, host, user_data)
    ):
        raise ValueError("fixture roots escape their actual owner")
    roots = (source, host, user_data)
    if any(
        left.is_relative_to(right) or right.is_relative_to(left)
        for index, left in enumerate(roots)
        for right in roots[index + 1 :]
    ):
        raise ValueError("fixture sources and mutable state must stay separate")
    os.environ["TMPDIR"] = str(owner)
    if action == "stage":
        stage(source, host)
    elif action == "activate":
        activate(host, user_data)
    else:
        raise ValueError("unknown fixture operation")


if __name__ == "__main__":
    main()
