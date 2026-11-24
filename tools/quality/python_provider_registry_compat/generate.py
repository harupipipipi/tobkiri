"""Capture bounded stateful observations from the original Python registry.

This source oracle imports captured Python source from a private snapshot tree.
It does not invoke Rust, Wasm, real profiles, credentials, or network services.
"""

from __future__ import annotations

import argparse
import ast
from contextlib import AbstractContextManager
from dataclasses import dataclass
import hashlib
import importlib
import json
import os
from pathlib import Path
import platform
import re
import socket
import stat
import sys
import tempfile
import types
import unicodedata
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
RUNTIME_ROOT = ROOT / "tobkiri_runtime"
GENERATOR_PATH = "tools/quality/python_provider_registry_compat/generate.py"
CASES_PATH = "tools/quality/python_provider_registry_compat/cases.py"
README_PATH = "tools/quality/python_provider_registry_compat/README.md"
TEST_PATHS = (
    "tobkiri_runtime/tests/test_provider_registry_pack.py",
    "tobkiri_runtime/tests/test_provider_registry_host.py",
    "tobkiri_runtime/tests/test_provider_model_access_configuration.py",
)
ENTRY_MODULES = (
    "ecosystem.rumi_provider_registry_pack.runtime.registry",
    "ecosystem.rumi_provider_registry_pack.runtime.process",
)
MAX_SOURCE_FILE_BYTES = 1024 * 1024
MAX_SOURCE_TOTAL_BYTES = 16 * 1024 * 1024
MAX_SOURCE_FILES = 512
MAX_CASES = 64
MAX_ACTIONS_PER_CASE = 32
MAX_INPUT_BYTES_PER_CASE = 256 * 1024
MAX_INPUT_BYTES_TOTAL = 2 * 1024 * 1024
MAX_STATE_FILE_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 1024 * 1024
MAX_TOTAL_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_RECEIPT_BYTES = 1024 * 1024
MAX_EXECUTABLE_BYTES = 512 * 1024 * 1024
MAX_RUNTIME_MODULE_BYTES = 128 * 1024 * 1024
CASE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True)
class _HostCaptureDescriptor:
    """JSON-safe structural evidence for the original non-JSON Host capture."""

    source_return_type: str
    contribution_count: int
    operation_ids: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        """Return the original capture's observable structural projection."""
        return {
            "source_return_type": self.source_return_type,
            "contribution_count": self.contribution_count,
            "operation_ids": list(self.operation_ids),
            "close_invoked": True,
        }


@dataclass(frozen=True)
class _PreparedAction:
    """Harness-resolved method and arguments before a source call is caught."""

    kind: str
    source_method: Any
    source_args: tuple[Any, ...] = ()
    source_kwargs: dict[str, Any] | None = None
    operation_id: str | None = None
    payload: Any = None


class _SourceCallFailure(Exception):
    """Carry an exception raised only by an invoked original source method."""

    def __init__(self, error: Exception) -> None:
        super().__init__(str(error))
        self.error = error


def _sha256(value: bytes) -> str:
    """Return the SHA-256 digest of exact source or artifact bytes."""
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value: Any) -> bytes:
    """Encode JSON without reordering mappings or inventing non-finite values."""
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")


def _open_directory(path: Path) -> int:
    """Open an absolute directory without following any symlink component."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("directory path must be absolute without parent traversal")
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            if part in ("", "."):
                continue
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_regular_at(directory_fd: int, name: str, limit: int) -> bytes:
    """Read one bounded regular file without following a symlink."""
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
        dir_fd=directory_fd,
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("source is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
        before_id = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_id = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if len(content) > limit or len(content) != before.st_size:
            raise ValueError("source exceeded its read bound")
        if before_id != after_id:
            raise ValueError("source changed while it was read")
        return content
    finally:
        os.close(descriptor)


def _capture_repo_file(path_text: str) -> bytes:
    """Capture a repository source file safely and with a per-file bound."""
    relative = Path(path_text)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("source path must remain within the repository")
    parent_fd = _open_directory(ROOT / relative.parent)
    try:
        return _read_regular_at(parent_fd, relative.name, MAX_SOURCE_FILE_BYTES)
    finally:
        os.close(parent_fd)


def _module_path(module_name: str) -> Path | None:
    """Resolve one import to an in-repository Python module, if present."""
    candidate = RUNTIME_ROOT.joinpath(*module_name.split("."))
    source = candidate.with_suffix(".py")
    if source.is_file():
        return source
    initializer = candidate / "__init__.py"
    if initializer.is_file():
        return initializer
    return None


def _parent_initializers(path: Path) -> tuple[Path, ...]:
    """Return package initializers from a module up to the runtime root."""
    current = path.parent
    result: list[Path] = []
    while current == RUNTIME_ROOT or RUNTIME_ROOT in current.parents:
        initializer = current / "__init__.py"
        if initializer.is_file():
            result.append(initializer)
        if current == RUNTIME_ROOT:
            break
        current = current.parent
    return tuple(result)


def _relative_runtime_path(path: Path) -> str:
    """Return one source path relative to the repository root."""
    return path.resolve(strict=True).relative_to(ROOT).as_posix()


def _import_targets(tree: ast.Module, package: str) -> tuple[str, ...]:
    """Resolve static imports to local module names without importing them."""
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            parts = package.split(".") if package else []
            base = parts[: len(parts) - node.level + 1]
            if node.module:
                base.extend(node.module.split("."))
            base_name = ".".join(base)
        else:
            base_name = node.module or ""
        if base_name:
            targets.add(base_name)
        for alias in node.names:
            if alias.name != "*" and base_name:
                targets.add(f"{base_name}.{alias.name}")
    return tuple(sorted(targets))


def _source_paths() -> list[str]:
    """Find the bounded static import closure for registry and Host factory."""
    paths = {
        GENERATOR_PATH,
        CASES_PATH,
        README_PATH,
        *TEST_PATHS,
    }
    queue = list(ENTRY_MODULES)
    visited_modules: set[str] = set()
    while queue:
        module_name = queue.pop()
        if module_name in visited_modules:
            continue
        visited_modules.add(module_name)
        module_path = _module_path(module_name)
        if module_path is None:
            continue
        paths.add(_relative_runtime_path(module_path))
        for initializer in _parent_initializers(module_path):
            paths.add(_relative_runtime_path(initializer))
            package_parts = initializer.parent.relative_to(RUNTIME_ROOT).parts
            if package_parts:
                # Parent __init__.py files execute during normal package import;
                # traverse their local imports too, rather than pinning bytes
                # for package initializers while omitting their dependencies.
                queue.append(".".join(package_parts))
        data = _capture_repo_file(_relative_runtime_path(module_path))
        tree = ast.parse(data, filename=module_name)
        package = (
            module_name
            if module_path.name == "__init__.py"
            else module_name.rpartition(".")[0]
        )
        for target in _import_targets(tree, package):
            if _module_path(target) is not None:
                queue.append(target)
    if len(paths) > MAX_SOURCE_FILES:
        raise ValueError("provider registry source closure exceeds its file limit")
    return sorted(paths)


def _capture_sources() -> tuple[dict[str, bytes], list[dict[str, Any]]]:
    """Capture source bytes and build one immutable manifest."""
    paths = _source_paths()
    contents = {path: _capture_repo_file(path) for path in paths}
    total = sum(len(data) for data in contents.values())
    if total > MAX_SOURCE_TOTAL_BYTES:
        raise ValueError("provider registry source closure exceeds its byte limit")
    records = [
        {
            "path": path,
            "bytes": len(data),
            "sha256": _sha256(data),
            "role": _source_role(path),
        }
        for path, data in sorted(contents.items())
    ]
    return contents, records


def _source_role(path: str) -> str:
    """Classify captured files without changing their source bytes."""
    if path in {GENERATOR_PATH, CASES_PATH, README_PATH}:
        return "oracle-producer"
    if path in TEST_PATHS:
        return "original-test-input-source"
    return "runtime-import-closure"


def _copy_snapshot(contents: dict[str, bytes], destination: Path) -> Path:
    """Stage captured runtime bytes into an isolated import tree."""
    runtime_snapshot = destination / "tobkiri_runtime"
    for relative, data in sorted(contents.items()):
        if not relative.startswith("tobkiri_runtime/"):
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(target.parent, 0o700)
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        try:
            view = memoryview(data)
            offset = 0
            while offset < len(view):
                offset += os.write(descriptor, view[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return runtime_snapshot


def _load_case_module(contents: dict[str, bytes]) -> types.ModuleType:
    """Execute the exact captured input module, not an unpinned import."""
    name = "python_provider_registry_compat_cases"
    module = types.ModuleType(name)
    module.__file__ = str(ROOT / CASES_PATH)
    module.__package__ = ""
    sys.modules[name] = module
    exec(compile(contents[CASES_PATH], CASES_PATH, "exec", dont_inherit=True), module.__dict__)
    return module


def _source_module_records(
    snapshot_root: Path,
    contents: dict[str, bytes],
) -> list[dict[str, str]]:
    """Verify modules came from the import tree under a canonical temp root."""
    loaded: dict[str, str] = {}
    if not snapshot_root.is_absolute() or ".." in snapshot_root.parts:
        raise ValueError("captured import root must be canonical and absolute")
    for name, module in sorted(sys.modules.items()):
        file_name = getattr(module, "__file__", None)
        if not isinstance(file_name, str):
            continue
        path = Path(file_name).resolve(strict=False)
        try:
            relative_runtime = path.relative_to(snapshot_root).as_posix()
        except ValueError:
            continue
        relative = f"tobkiri_runtime/{relative_runtime}"
        if relative not in contents:
            raise ValueError(f"loaded runtime source was not captured: {relative}")
        loaded[relative] = name
    required = {
        "tobkiri_runtime/ecosystem/rumi_provider_registry_pack/runtime/registry.py",
        "tobkiri_runtime/ecosystem/rumi_provider_registry_pack/runtime/process.py",
        "tobkiri_runtime/core_runtime/host_provider_backend_v4.py",
    }
    if not required.issubset(loaded):
        raise ValueError("captured original registry or Host profile source was not loaded")
    return [
        {"path": path, "module": name, "sha256": _sha256(contents[path])}
        for path, name in sorted(loaded.items())
    ]


class _NetworkFence(AbstractContextManager["_NetworkFence"]):
    """Block and count network entry points while source modules are observed."""

    def __init__(self) -> None:
        self.attempts: list[str] = []
        self._originals: list[tuple[Any, str, Any]] = []

    def _replace(self, owner: Any, name: str) -> None:
        original = getattr(owner, name)

        def blocked(*_args: Any, **_kwargs: Any) -> Any:
            self.attempts.append(f"{owner.__name__}.{name}")
            raise RuntimeError("network is disabled in provider registry source oracle")

        self._originals.append((owner, name, original))
        setattr(owner, name, blocked)

    def __enter__(self) -> _NetworkFence:
        self._replace(socket.socket, "connect")
        self._replace(socket, "create_connection")
        try:
            import urllib.request
        except ImportError:
            return self
        self._replace(urllib.request, "urlopen")
        return self

    def __exit__(self, *_exc: object) -> None:
        for owner, name, original in reversed(self._originals):
            setattr(owner, name, original)


def _runtime_file_hash(path_text: str, byte_limit: int) -> dict[str, Any]:
    """Hash one actual runtime file without persisting its local path."""
    path = Path(path_text).resolve(strict=True)
    parent_fd = _open_directory(path.parent)
    try:
        data = _read_regular_at(parent_fd, path.name, byte_limit)
    finally:
        os.close(parent_fd)
    return {"bytes": len(data), "sha256": _sha256(data)}


def _runtime_identity() -> dict[str, Any]:
    """Record the actual CPython build and the limits relevant to replay."""
    if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 13):
        raise ValueError("provider registry source oracle requires CPython 3.13")
    version = sys.version_info
    executable = _runtime_file_hash(sys.executable, MAX_EXECUTABLE_BYTES)
    unicode_module = None
    module_path = getattr(unicodedata, "__file__", None)
    if isinstance(module_path, str):
        try:
            unicode_module = _runtime_file_hash(module_path, MAX_RUNTIME_MODULE_BYTES)
        except (OSError, ValueError):
            unicode_module = None
    identity = {
        "implementation": sys.implementation.name,
        "version": {
            "major": version.major,
            "minor": version.minor,
            "micro": version.micro,
            "releaselevel": version.releaselevel,
            "serial": version.serial,
            "text": sys.version.splitlines()[0],
        },
        "cache_tag": sys.implementation.cache_tag,
        "unicode_version": unicodedata.unidata_version,
        "int_max_str_digits": sys.get_int_max_str_digits(),
        "recursion_limit": sys.getrecursionlimit(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "python_executable": executable,
        "unicodedata_module": unicode_module,
    }
    canonical = json.dumps(
        identity,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return {**identity, "runtime_identity_sha256": _sha256(canonical)}


def _new_output(path_text: str) -> tuple[int, int]:
    """Create a fresh private output directory without following symlinks."""
    path = Path(path_text)
    if not path.is_absolute() or ".." in path.parts or path.name in ("", ".", ".."):
        raise ValueError("--output-dir must be a new absolute path without traversal")
    parent_fd = _open_directory(path.parent)
    try:
        os.mkdir(path.name, 0o700, dir_fd=parent_fd)
        output_fd = os.open(
            path.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
    except BaseException:
        os.close(parent_fd)
        raise
    return parent_fd, output_fd


def _ensure_dir_at(parent_fd: int, name: str) -> int:
    """Open a newly created output subdirectory, allowing prior sidecars."""
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    return os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )


def _write_at(directory_fd: int, name: str, content: bytes) -> None:
    """Create one bounded artifact without replacing an existing path."""
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        view = memoryview(content)
        offset = 0
        while offset < len(view):
            offset += os.write(descriptor, view[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_artifacts(
    output_path: str,
    sidecars: dict[str, bytes],
    receipt: bytes,
) -> None:
    """Write all outputs into a new mode-0700 directory with mode-0600 files."""
    parent_fd, output_fd = _new_output(output_path)
    try:
        directories: dict[tuple[str, ...], int] = {(): output_fd}
        try:
            for relative, content in sorted(sidecars.items()):
                parts = tuple(relative.split("/"))
                if not parts or any(part in ("", ".", "..") for part in parts):
                    raise ValueError("invalid generated artifact path")
                parent_key = parts[:-1]
                for index in range(1, len(parent_key) + 1):
                    key = parent_key[:index]
                    if key not in directories:
                        directories[key] = _ensure_dir_at(
                            directories[key[:-1]], key[-1],
                        )
                _write_at(directories[parent_key], parts[-1], content)
            _write_at(output_fd, "receipt.json", receipt)
        finally:
            for key, descriptor in sorted(
                directories.items(), key=lambda item: len(item[0]), reverse=True,
            ):
                if key:
                    os.close(descriptor)
    finally:
        os.close(output_fd)
        os.close(parent_fd)


def _tree_manifest(state_root: Path) -> list[dict[str, Any]]:
    """Hash local state and retain directory effects without following links."""
    rows: list[dict[str, Any]] = []
    total = 0
    try:
        root_fd = _open_directory(state_root)
    except FileNotFoundError:
        return []

    def visit(directory_fd: int, parent: str) -> None:
        nonlocal total
        for name in sorted(os.listdir(directory_fd)):
            relative = f"{parent}/{name}" if parent else name
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISDIR(info.st_mode):
                rows.append(
                    {"path": relative, "kind": "directory", "mode": mode},
                )
                child_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                try:
                    visit(child_fd, relative)
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STATE_FILE_BYTES:
                rows.append(
                    {
                        "path": relative,
                        "kind": "non-regular-or-oversized",
                        "mode": mode,
                        "bytes": info.st_size,
                    }
                )
                continue
            data = _read_regular_at(directory_fd, name, MAX_STATE_FILE_BYTES)
            total += len(data)
            if len(data) != info.st_size or total > MAX_TOTAL_OUTPUT_BYTES:
                raise ValueError("provider registry state exceeded its read bound")
            rows.append(
                {
                    "path": relative,
                    "kind": "file",
                    "mode": mode,
                    "bytes": len(data),
                    "sha256": _sha256(data),
                }
            )

    try:
        visit(root_fd, "")
    finally:
        os.close(root_fd)
    return rows


def _tree_delta(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
) -> dict[str, Any]:
    """Describe local state additions, removals, and byte changes."""
    before_map = {row["path"]: row for row in before}
    after_map = {row["path"]: row for row in after}
    added = sorted(after_map.keys() - before_map.keys())
    removed = sorted(before_map.keys() - after_map.keys())
    changed = sorted(
        path
        for path in before_map.keys() & after_map.keys()
        if before_map[path] != after_map[path]
    )
    return {
        "changed": bool(added or removed or changed),
        "added_paths": added,
        "removed_paths": removed,
        "changed_paths": changed,
    }


def _state_snapshot(
    state_root: Path,
    registry: Any | None,
    sidecars: dict[str, bytes],
    case_id: str,
    action_id: str,
    boundary: str,
) -> dict[str, Any]:
    """Capture current tree and exact store bytes around one source call."""
    tree = _tree_manifest(state_root)
    result: dict[str, Any] = {"tree": tree}
    if registry is None:
        result["store"] = {"exists": False}
        return result
    path = registry.path
    if not path.exists():
        result["store"] = {"exists": False}
        return result
    parent_fd = _open_directory(path.parent)
    try:
        data = _read_regular_at(
            parent_fd, path.name, MAX_STATE_FILE_BYTES,
        )
    finally:
        os.close(parent_fd)
    relative = f"cases/{case_id}/actions/{action_id}.store-{boundary}.json"
    sidecars[relative] = data
    result["store"] = {
        "exists": True,
        "path": relative,
        "bytes": len(data),
        "sha256": _sha256(data),
    }
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        result["store"]["parse"] = "invalid-json"
    else:
        if isinstance(value, dict):
            result["store"]["revision"] = value.get("revision")
            providers = value.get("providers")
            if isinstance(providers, dict):
                result["store"]["provider_ids"] = sorted(
                    str(identifier) for identifier in providers
                )
                result["store"]["record_revisions"] = {
                    str(identifier): item.get("record_revision")
                    for identifier, item in sorted(providers.items())
                    if isinstance(item, dict)
                }
        result["store"]["parse"] = "json"
    return result


def _action_input_bytes(case: Any) -> bytes:
    """Serialize one static case as exact generator input evidence."""
    value = {
        "case_id": case.case_id,
        "purpose": case.purpose,
        "profile_id": case.profile_id,
        "initial_store_text": case.initial_store_text,
        "actions": [
            {
                "action_id": item.action_id,
                "operation": item.operation,
                "payload": item.payload,
            }
            for item in case.actions
        ],
    }
    return _json_bytes(value)


def _mkdir_private(path: Path, *, stop_at: Path | None = None) -> None:
    """Create one private local fixture directory and its parents."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    current = path.resolve(strict=True)
    boundary = stop_at.resolve(strict=True) if stop_at is not None else current
    while current == boundary or boundary in current.parents:
        os.chmod(current, 0o700)
        if current == boundary:
            break
        current = current.parent


def _seed_store(registry: Any, text: str) -> None:
    """Install a static malformed/read-validation fixture under the temp root."""
    _mkdir_private(registry.root, stop_at=registry.root.parents[3])
    data = text.encode("utf-8")
    if len(data) > MAX_STATE_FILE_BYTES:
        raise ValueError("initial store fixture exceeds its byte limit")
    descriptor = os.open(
        registry.path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _make_host_context(
    context_type: type[Any],
    factory: Any,
    state_root: Path,
    profile_id: str,
    *,
    binding_mode: str = "complete",
    catalog_owners: tuple[dict[str, str], ...] = (),
) -> tuple[Any, dict[str, Any]]:
    """Bind exact captured Host operations to one synthetic profile context."""
    from types import SimpleNamespace

    if binding_mode == "empty":
        operations: tuple[str, ...] = ()
    elif binding_mode == "complete":
        operations = tuple(sorted(factory.operations))
    else:
        raise ValueError("unknown synthetic Host binding mode")
    bindings = tuple(
        SimpleNamespace(
            function=SimpleNamespace(
                function_id=factory.function_id,
                implementation_digest="sha256:" + "b" * 64,
            ),
            operation=SimpleNamespace(
                contract_id=factory.contract_id,
                operation_id=operation,
                contract_version="1.0.0",
            ),
            principal_ref=SimpleNamespace(value="provider-registry-principal"),
            artifact=SimpleNamespace(digest="sha256:" + "a" * 64),
        )
        for operation in operations
    )
    principal = "provider-registry-principal"
    domain_ids = {
        (factory.contract_id, operation, principal): "provider-registry-domain"
        for operation in operations
    }
    catalog_bindings = tuple(
        SimpleNamespace(
            operation=SimpleNamespace(
                contract_id=f"tobkiri.service.ai.provider.{item['operation']}.v1",
            ),
            artifact=SimpleNamespace(pack_id=item["pack_id"]),
        )
        for item in catalog_owners
    )
    context = context_type(
        profile_id=profile_id,
        plan_digest="sha256:" + "c" * 64,
        security_epoch=1,
        activation={"profile_id": profile_id},
        state_root=state_root,
        provider_bindings=bindings,
        catalog_bindings=catalog_bindings,
        domain_ids=domain_ids,
        user_data_root=state_root,
    )
    binding_record = {
        "context_type": (
            "core_runtime.host_provider_backend_v4.HostProviderCaptureContextV4"
        ),
        "factory_type": (
            "ecosystem.rumi_provider_registry_pack.runtime.process."
            "ProviderRegistryHostFactoryV4"
        ),
        "profile_id": profile_id,
        "user_data_root_role": "fresh-private-fixture-root",
        "plan_digest": context.plan_digest,
        "security_epoch": context.security_epoch,
        "function_id": factory.function_id,
        "contract_id": factory.contract_id,
        "operation_ids": list(operations),
        "principal_id": principal,
        "domain_ids": {
            operation: domain_ids[(factory.contract_id, operation, principal)]
            for operation in operations
        },
        "artifact_digest": "sha256:" + "a" * 64,
        "implementation_digest": "sha256:" + "b" * 64,
        "catalog_owner_bindings": [
            {
                "contract_id": item.operation.contract_id,
                "pack_id": item.artifact.pack_id,
            }
            for item in catalog_bindings
        ],
    }
    return context, binding_record


def _prepare_action(
    action: Any,
    registry: Any,
    state_root: Path,
    provider_module: Any,
    host_context_type: type[Any],
    host_observation: dict[str, Any],
) -> _PreparedAction:
    """Resolve harness selectors, bound methods, and arguments before capture."""
    payload = action.payload
    if action.operation == "registry.snapshot":
        return _PreparedAction("registry", registry.snapshot)
    if action.operation == "registry.health":
        return _PreparedAction("registry", registry.health)
    if action.operation == "registry.save":
        return _PreparedAction(
            "registry",
            registry.save,
            (payload["record"],),
            {"expected_revision": payload["expected_revision"]},
        )
    if action.operation == "registry.prepare_save":
        return _PreparedAction(
            "registry",
            registry.prepare_save,
            (payload["record"],),
            {"expected_revision": payload["expected_revision"]},
        )
    if action.operation == "registry.set_model_access":
        return _PreparedAction(
            "registry",
            registry.set_model_access,
            (payload["provider_instance_id"], payload["policy"]),
            {"expected_revision": payload["expected_revision"]},
        )
    if action.operation == "registry.delete":
        return _PreparedAction(
            "registry",
            registry.delete,
            (payload["provider_instance_id"],),
            {"expected_revision": payload["expected_revision"]},
        )
    if action.operation == "host.capture":
        factory_name = payload.get("factory", "readonly")
        if factory_name in {"readonly", "manage"}:
            factory = provider_module.ProviderRegistryHostFactoryV4(
                readonly=factory_name == "readonly",
            )
        elif factory_name == "configure-prepare":
            factory = provider_module.ProviderRegistryHostFactoryV4(
                configuration_phase="prepare",
            )
        elif factory_name == "configure-execute":
            factory = provider_module.ProviderRegistryHostFactoryV4(
                configuration_phase="execute",
            )
        else:
            raise ValueError("unknown synthetic Host factory selector")
        context, binding = _make_host_context(
            host_context_type,
            factory,
            state_root,
            registry.profile_id,
            binding_mode=payload.get("binding_mode", "complete"),
            catalog_owners=tuple(payload.get("catalog_owners", ())),
        )
        host_observation["binding"] = binding
        return _PreparedAction(
            "host-capture", factory.capture, (context,),
        )
    if action.operation == "host.invoke":
        factory_name = payload["factory"]
        if factory_name not in {"readonly", "manage"}:
            raise ValueError("unknown synthetic Host factory selector")
        factory = provider_module.ProviderRegistryHostFactoryV4(
            readonly=factory_name == "readonly",
        )
        context, binding = _make_host_context(
            host_context_type,
            factory,
            state_root,
            registry.profile_id,
        )
        host_observation["binding"] = binding
        return _PreparedAction(
            "host-invoke",
            factory.capture,
            (context,),
            operation_id=payload["operation_id"],
            payload=payload["payload"],
        )
    raise ValueError(f"unsupported source-oracle operation: {action.operation}")


def _call_source(method: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """Catch only an exception from the actual canonical source call."""
    if not callable(method):
        raise TypeError("prepared source target is not callable")
    try:
        return method(*args, **kwargs)
    except Exception as error:
        raise _SourceCallFailure(error) from None


def _run_prepared_action(
    prepared: _PreparedAction,
    host_observation: dict[str, Any],
) -> Any:
    """Invoke prepared source methods; let harness/setup failures propagate."""
    kwargs = prepared.source_kwargs or {}
    if prepared.kind == "registry":
        return _call_source(prepared.source_method, prepared.source_args, kwargs)

    capture = _call_source(prepared.source_method, prepared.source_args, kwargs)
    try:
        contributions = tuple(capture.contributions)
        if prepared.kind == "host-capture":
            return _HostCaptureDescriptor(
                source_return_type=type(capture).__name__,
                contribution_count=len(contributions),
                operation_ids=tuple(item.operation_id for item in contributions),
            )
        if prepared.kind != "host-invoke":
            raise ValueError(f"unknown prepared action kind: {prepared.kind}")
        matches = [
            item for item in contributions
            if item.operation_id == prepared.operation_id
        ]
        if len(matches) != 1:
            raise ValueError(
                "captured Host operation selector did not resolve exactly once"
            )
        contribution = matches[0]
        host_observation["capture"] = {
            "kind": type(capture).__name__,
            "contribution_count": len(contributions),
            "selected_operation_id": contribution.operation_id,
            "domain_id": contribution.domain_id,
            "principal_id": contribution.principal_id,
            "artifact_digest": contribution.artifact_digest,
            "implementation_digest": contribution.implementation_digest,
        }
        return _call_source(
            contribution.invoke,
            (prepared.operation_id, prepared.payload, None),
            {},
        )
    finally:
        capture.close()


def _observe_case(
    case: Any,
    registry_type: type[Any],
    provider_module: Any,
    host_context_type: type[Any],
    work_root: Path,
    sidecars: dict[str, bytes],
    source_manifest_sha256: str,
    network: _NetworkFence,
) -> tuple[dict[str, Any], int, int]:
    """Run every declared action and retain results, exceptions and state."""
    if not CASE_ID.fullmatch(case.case_id):
        raise ValueError("case ID is not a lowercase source-oracle slug")
    if len(case.actions) > MAX_ACTIONS_PER_CASE:
        raise ValueError(f"case has too many actions: {case.case_id}")
    if any(not CASE_ID.fullmatch(action.action_id) for action in case.actions):
        raise ValueError(f"case action ID is not a safe slug: {case.case_id}")
    if len({action.action_id for action in case.actions}) != len(case.actions):
        raise ValueError(f"case action IDs are not unique: {case.case_id}")
    state_root = work_root / case.case_id
    _mkdir_private(state_root)
    input_path = f"cases/{case.case_id}/input.json"
    input_bytes = _action_input_bytes(case)
    if len(input_bytes) > MAX_INPUT_BYTES_PER_CASE:
        raise ValueError(f"case input exceeds the byte limit: {case.case_id}")
    sidecars[input_path] = input_bytes
    input_record = {
        "path": input_path,
        "encoding": "ascii-json",
        "bytes": len(input_bytes),
        "sha256": _sha256(input_bytes),
    }

    try:
        registry = registry_type(case.profile_id, user_data_root=state_root)
    except Exception as error:
        observation = {
            "id": case.case_id,
            "purpose": case.purpose,
            "source_manifest_sha256": source_manifest_sha256,
            "profile_id": case.profile_id,
            "initial_state": {"tree": _tree_manifest(state_root)},
            "setup_error": {
                "exception_class": type(error).__name__,
                "message": str(error),
                "args_repr": repr(error.args),
            },
            "actions": [
                {
                    "id": action.action_id,
                    "operation": action.operation,
                    "status": "not-run",
                    "reason": "registry construction raised before dispatch",
                }
                for action in case.actions
            ],
        }
        observation_bytes = _json_bytes(observation)
        observation_path = f"cases/{case.case_id}/observation.json"
        sidecars[observation_path] = observation_bytes
        return (
            {
                "id": case.case_id,
                "purpose": case.purpose,
                "input": input_record,
                "observation": {
                    "path": observation_path,
                    "bytes": len(observation_bytes),
                    "sha256": _sha256(observation_bytes),
                },
                "setup_status": "exception",
                "setup_exception_class": type(error).__name__,
            },
            len(input_bytes),
            len(observation_bytes),
        )

    initial_seed: dict[str, Any] = {"kind": "none"}
    if case.initial_store_text is not None:
        seed = case.initial_store_text.encode("utf-8")
        _seed_store(registry, case.initial_store_text)
        initial_seed = {
            "kind": "fixture-store-bytes",
            "bytes": len(seed),
            "sha256": _sha256(seed),
            "fixture_only": True,
        }
    initial_tree = _tree_manifest(state_root)
    network_case_start = len(network.attempts)
    observation_rows: list[dict[str, Any]] = []
    result_bytes_total = 0
    for item in case.actions:
        host_observation: dict[str, Any] = {}
        before = _state_snapshot(
            state_root,
            registry,
            sidecars,
            case.case_id,
            item.action_id,
            "before",
        )
        prepared = _prepare_action(
            item,
            registry,
            state_root,
            provider_module,
            host_context_type,
            host_observation,
        )
        before_calls = len(network.attempts)
        try:
            value = _run_prepared_action(prepared, host_observation)
        except _SourceCallFailure as failure:
            error = failure.error
            outcome: dict[str, Any] = {
                "kind": "exception",
                "exception_class": type(error).__name__,
                "message": str(error),
                "args_repr": repr(error.args),
            }
        else:
            if isinstance(value, _HostCaptureDescriptor):
                outcome = {
                    "kind": "host-capture-structure",
                    **value.as_json(),
                }
            else:
                output_bytes = _json_bytes(value)
                if len(output_bytes) > MAX_RESULT_BYTES:
                    raise ValueError(
                        "source result exceeds its byte cap: "
                        f"{case.case_id}/{item.action_id}"
                    )
                result_path = (
                    f"cases/{case.case_id}/actions/{item.action_id}.result.json"
                )
                sidecars[result_path] = output_bytes
                result_bytes_total += len(output_bytes)
                outcome = {
                    "kind": "result",
                    "python_type": type(value).__name__,
                    "result": {
                        "path": result_path,
                        "encoding": "ascii-json",
                        "bytes": len(output_bytes),
                        "sha256": _sha256(output_bytes),
                    },
                }
        after = _state_snapshot(
            state_root,
            registry,
            sidecars,
            case.case_id,
            item.action_id,
            "after",
        )
        observation_rows.append(
            {
                "id": item.action_id,
                "operation": item.operation,
                "payload": item.payload,
                "outcome": outcome,
                "host_binding": host_observation or None,
                "state_before": before,
                "state_after": after,
                "effects": {
                    "profile_root_tree": _tree_delta(before["tree"], after["tree"]),
                    "store_bytes_changed": {
                        key: value for key, value in before["store"].items()
                        if key != "path"
                    } != {
                        key: value for key, value in after["store"].items()
                        if key != "path"
                    },
                    "network_attempts_blocked": len(network.attempts) - before_calls,
                },
            }
        )
        if result_bytes_total > MAX_TOTAL_OUTPUT_BYTES:
            raise ValueError("source result output exceeds the corpus limit")

    observation = {
        "id": case.case_id,
        "purpose": case.purpose,
        "source_manifest_sha256": source_manifest_sha256,
        "profile_id": case.profile_id,
        "fixture_root_role": "fresh-private-per-case-user-data-root",
        "initial_store": initial_seed,
        "initial_state": initial_tree,
        "actions": observation_rows,
        "network_attempts_total": len(network.attempts) - network_case_start,
    }
    observation_bytes = _json_bytes(observation)
    if len(observation_bytes) > MAX_RESULT_BYTES:
        raise ValueError(f"case observation exceeds its byte cap: {case.case_id}")
    observation_path = f"cases/{case.case_id}/observation.json"
    sidecars[observation_path] = observation_bytes
    return (
        {
            "id": case.case_id,
            "purpose": case.purpose,
            "input": input_record,
            "observation": {
                "path": observation_path,
                "bytes": len(observation_bytes),
                "sha256": _sha256(observation_bytes),
            },
            "setup_status": "ok",
            "action_count": len(observation_rows),
            "network_attempts_total": len(network.attempts) - network_case_start,
        },
        len(input_bytes),
        len(observation_bytes),
    )


def _verify_sources_unchanged(contents: dict[str, bytes]) -> None:
    """Reject source changes made while this oracle was being prepared."""
    for path, original in contents.items():
        current = _capture_repo_file(path)
        if current != original:
            raise ValueError(f"captured source changed during observation: {path}")


def _validate_corpus(cases: tuple[Any, ...]) -> None:
    """Require the planned stateful surface and bounded unique inputs."""
    if not cases or len(cases) > MAX_CASES:
        raise ValueError("static provider registry corpus is empty or too large")
    identities: set[str] = set()
    required = {
        "registry.snapshot",
        "registry.save",
        "registry.prepare_save",
        "registry.set_model_access",
        "registry.delete",
        "registry.health",
        "host.invoke",
        "host.capture",
    }
    observed: set[str] = set()
    registry_payload_keys = {
        "registry.snapshot": set(),
        "registry.health": set(),
        "registry.save": {"record", "expected_revision"},
        "registry.prepare_save": {"record", "expected_revision"},
        "registry.set_model_access": {
            "provider_instance_id", "policy", "expected_revision",
        },
        "registry.delete": {"provider_instance_id", "expected_revision"},
    }
    operation_ids = {
        "readonly": {
            "rumi_provider_registry_pack.provider-registry-resource",
            "rumi_provider_registry_pack.provider-registry-resource.generate",
            "rumi_provider_registry_pack.provider-registry-resource.stream",
        },
        "manage": {"rumi_provider_registry_pack.provider-registry-manage"},
    }
    allowed_host_factories = {
        "readonly", "manage", "configure-prepare", "configure-execute",
    }
    allowed_operations = set(registry_payload_keys) | {"host.invoke", "host.capture"}

    for case in cases:
        if not isinstance(case.profile_id, str) or not isinstance(case.purpose, str):
            raise ValueError("case profile and purpose must be strings")
        if case.initial_store_text is not None and not isinstance(
            case.initial_store_text, str,
        ):
            raise ValueError(f"initial store fixture must be text: {case.case_id}")
        if not CASE_ID.fullmatch(case.case_id) or case.case_id in identities:
            raise ValueError("case IDs must be unique lowercase slugs")
        identities.add(case.case_id)
        if not isinstance(case.actions, tuple) or not 0 < len(case.actions) <= MAX_ACTIONS_PER_CASE:
            raise ValueError(f"case action count is invalid: {case.case_id}")
        action_ids: set[str] = set()
        for item in case.actions:
            if not CASE_ID.fullmatch(item.action_id) or item.action_id in action_ids:
                raise ValueError(f"case action IDs are unsafe or duplicated: {case.case_id}")
            action_ids.add(item.action_id)
            if item.operation not in allowed_operations:
                raise ValueError(
                    f"unsupported source-oracle operation in corpus: {item.operation!r}"
                )
            if not isinstance(item.payload, dict):
                raise ValueError(f"action payload must be an object: {item.action_id}")
            if item.operation in registry_payload_keys:
                if set(item.payload) != registry_payload_keys[item.operation]:
                    raise ValueError(
                        f"unexpected registry dispatch payload shape: {item.action_id}"
                    )
            elif item.operation == "host.capture":
                allowed_keys = {"factory", "binding_mode", "catalog_owners"}
                if set(item.payload) - allowed_keys:
                    raise ValueError(
                        f"unexpected Host capture payload key: {item.action_id}"
                    )
                factory_name = item.payload.get("factory", "readonly")
                binding_mode = item.payload.get("binding_mode", "complete")
                if factory_name not in allowed_host_factories:
                    raise ValueError(f"unknown Host factory selector: {item.action_id}")
                if binding_mode not in {"complete", "empty"}:
                    raise ValueError(f"unknown Host binding selector: {item.action_id}")
                owners = item.payload.get("catalog_owners", [])
                if not isinstance(owners, list) or len(owners) > 8:
                    raise ValueError(f"invalid synthetic catalog owner list: {item.action_id}")
                if owners and factory_name != "configure-execute":
                    raise ValueError(
                        f"catalog owners are only valid for execute capture: {item.action_id}"
                    )
                for owner in owners:
                    if (
                        not isinstance(owner, dict)
                        or set(owner) != {"operation", "pack_id"}
                        or owner["operation"] not in {"generate", "stream"}
                        or not isinstance(owner["pack_id"], str)
                        or not owner["pack_id"]
                        or len(owner["pack_id"]) > 256
                    ):
                        raise ValueError(
                            f"invalid synthetic catalog owner binding: {item.action_id}"
                        )
            else:
                if set(item.payload) != {"factory", "operation_id", "payload"}:
                    raise ValueError(
                        f"unexpected Host invocation payload shape: {item.action_id}"
                    )
                factory_name = item.payload["factory"]
                operation_id = item.payload["operation_id"]
                if (
                    factory_name not in operation_ids
                    or operation_id not in operation_ids[factory_name]
                    or not isinstance(item.payload["payload"], dict)
                ):
                    raise ValueError(
                        f"invalid Host invocation selector or payload: {item.action_id}"
                    )
            _action_input_bytes(case)
            observed.add(item.operation)
    if not required.issubset(observed):
        raise ValueError("corpus does not cover the requested registry and Host surface")
    for case in cases:
        if any(not CASE_ID.fullmatch(item.action_id) for item in case.actions):
            raise ValueError(f"case has an unsafe action ID: {case.case_id}")


def main(argv: list[str] | None = None) -> int:
    """Capture original source observations into a new private directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        required=True,
        help="new absolute private directory; existing paths are rejected",
    )
    arguments = parser.parse_args(argv)
    sys.dont_write_bytecode = True
    if not Path(arguments.output_dir).is_absolute():
        raise ValueError("--output-dir must be an absolute path")

    contents, sources = _capture_sources()
    case_module = _load_case_module(contents)
    static_cases = tuple(case_module.CASES)
    _validate_corpus(static_cases)
    runtime = _runtime_identity()
    source_manifest = json.dumps(
        sources,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    source_manifest_sha256 = _sha256(source_manifest)
    generator_source_sha256 = next(
        item["sha256"] for item in sources if item["path"] == GENERATOR_PATH
    )
    case_source_sha256 = next(
        item["sha256"] for item in sources if item["path"] == CASES_PATH
    )

    sidecars: dict[str, bytes] = {}
    input_total = 0
    observation_total = 0
    captured_cases: list[dict[str, Any]] = []
    all_network_attempts: list[str] = []

    with tempfile.TemporaryDirectory(prefix="provider-registry-source-") as scratch:
        # macOS may expose TMPDIR through /var -> /private/var. Resolve only
        # this freshly-created private root once so later descriptor walks can
        # retain their strict no-follow policy on every existing input path.
        scratch_root = Path(scratch).resolve(strict=True)
        os.chmod(scratch_root, 0o700)
        snapshot_runtime = _copy_snapshot(contents, scratch_root)
        sys.path[:] = [
            entry
            for entry in sys.path
            if Path(entry or ".").resolve(strict=False) not in {ROOT, RUNTIME_ROOT}
        ]
        sys.path.insert(0, str(snapshot_runtime))
        importlib.invalidate_caches()
        for module_name in tuple(sys.modules):
            if module_name.split(".", 1)[0] in {
                "core_runtime",
                "ecosystem",
                "tobkiri_host",
                "tobkiri_protocol",
            }:
                sys.modules.pop(module_name, None)

        with _NetworkFence() as network:
            registry_module = importlib.import_module(
                "ecosystem.rumi_provider_registry_pack.runtime.registry"
            )
            provider_module = importlib.import_module(
                "ecosystem.rumi_provider_registry_pack.runtime.process"
            )
            host_backend = importlib.import_module(
                "core_runtime.host_provider_backend_v4"
            )
            with tempfile.TemporaryDirectory(prefix="provider-registry-state-") as state:
                # Use the canonical spelling of our own private temp root;
                # _open_directory remains no-follow for all existing sources.
                state_root = Path(state).resolve(strict=True)
                os.chmod(state_root, 0o700)
                for case in static_cases:
                    case_observation, input_bytes, observation_bytes = _observe_case(
                        case,
                        registry_module.ProviderRegistry,
                        provider_module,
                        host_backend.HostProviderCaptureContextV4,
                        state_root,
                        sidecars,
                        source_manifest_sha256,
                        network,
                    )
                    captured_cases.append(case_observation)
                    input_total += input_bytes
                    observation_total += observation_bytes
                    if input_total > MAX_INPUT_BYTES_TOTAL:
                        raise ValueError("provider registry input corpus exceeded its cap")
                    if sum(map(len, sidecars.values())) > MAX_TOTAL_OUTPUT_BYTES:
                        raise ValueError("provider registry sidecars exceeded their cap")
            loaded_sources = _source_module_records(snapshot_runtime, contents)
            all_network_attempts = list(network.attempts)

        _verify_sources_unchanged(contents)

    runtime_after_source = _runtime_identity()
    if runtime_after_source != runtime:
        raise ValueError("source observation changed the pinned interpreter identity")

    limits = {
        "case_count": len(captured_cases),
        "case_count_limit": MAX_CASES,
        "action_count": sum(item.get("action_count", 0) for item in captured_cases),
        "declared_action_count": sum(len(item.actions) for item in static_cases),
        "actions_per_case_limit": MAX_ACTIONS_PER_CASE,
        "input_bytes_total": input_total,
        "input_bytes_limit_total": MAX_INPUT_BYTES_TOTAL,
        "input_bytes_limit_per_case": MAX_INPUT_BYTES_PER_CASE,
        "source_file_count": len(sources),
        "source_file_count_limit": MAX_SOURCE_FILES,
        "source_bytes_total": sum(item["bytes"] for item in sources),
        "source_bytes_limit_total": MAX_SOURCE_TOTAL_BYTES,
        "source_bytes_limit_per_file": MAX_SOURCE_FILE_BYTES,
        "sidecar_bytes_total": sum(map(len, sidecars.values())),
        "observation_bytes_total": observation_total,
        "sidecar_bytes_limit_total": MAX_TOTAL_OUTPUT_BYTES,
        "result_bytes_limit_per_file": MAX_RESULT_BYTES,
        "receipt_bytes_limit": MAX_RECEIPT_BYTES,
    }
    receipt = {
        "schema": "io.tobkiri.original-python-provider-registry-stateful.v1",
        "scope": (
            "captured original Python ProviderRegistry and its profile-bound Host "
            "factory observations only; no Rust/Wasm execution or parity claim"
        ),
        "runtime": runtime,
        "runtime_after_source": {
            "runtime_identity_sha256": runtime_after_source["runtime_identity_sha256"],
            "unchanged": True,
        },
        "source_manifest_sha256": source_manifest_sha256,
        "generator_source": {
            "path": GENERATOR_PATH,
            "sha256": generator_source_sha256,
        },
        "case_source": {"path": CASES_PATH, "sha256": case_source_sha256},
        "source_manifest": sources,
        "executed_runtime_sources": loaded_sources,
        "host_profile_binding": {
            "python_context_type": (
                "core_runtime.host_provider_backend_v4.HostProviderCaptureContextV4"
            ),
            "python_factory_type": (
                "ecosystem.rumi_provider_registry_pack.runtime.process."
                "ProviderRegistryHostFactoryV4"
            ),
            "binding_source": (
                "original ProviderRegistryHostFactoryV4.capture with a fresh "
                "HostProviderCaptureContextV4 and resolved-operation-shaped fixture"
            ),
            "profile_value_is_fixture_only": True,
            "host_effects_are_limited_to_private_state_root": True,
        },
        "observation_format": {
            "result": "exact Python return value encoded as ordered ASCII JSON sidecar",
            "exception": "exact exception class, str(exception), and repr(args)",
            "state": "before/after profile-root tree and exact raw store bytes",
            "effects": "local tree delta and blocked network attempt count per action",
            "timestamp_policy": (
                "persisted timestamps remain present; consumers compare explicit "
                "same-record/revision relationships, not an unrecorded wildcard"
            ),
            "uuid_policy": (
                "migration and rollback are outside this corpus; no UUID-bearing "
                "result is omitted from the selected operations"
            ),
        },
        "limits": limits,
        "network_attempts_blocked": all_network_attempts,
        "cases": captured_cases,
        "limitations": [
            "Every profile and credential handle is a synthetic fixture value.",
            "The registry receives a new private user_data_root per case; the user's real profiles are not opened.",
            "Delete observations cover the registry store only; the original registry has no credential revocation port, so this does not prove revocation behavior in another component.",
            "This source oracle does not verify provider Pack WIT, Rust Core, Wasm behavior, or import/dependency timing in production.",
            "No Setup UI, GUI approval, settings, chat state, or external account is opened or changed.",
        ],
    }
    receipt_bytes = _json_bytes(receipt)
    if len(receipt_bytes) > MAX_RECEIPT_BYTES:
        raise ValueError("provider registry receipt exceeded its output cap")
    _write_artifacts(arguments.output_dir, sidecars, receipt_bytes)
    print(
        "Original Python provider registry observations written: "
        f"cases={len(captured_cases)} actions={limits['action_count']} "
        f"source-files={len(sources)} source-bytes={limits['source_bytes_total']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
