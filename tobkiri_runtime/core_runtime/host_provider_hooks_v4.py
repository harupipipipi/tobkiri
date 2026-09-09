"""Load explicit Host Provider hooks from exact resolved executable bytes."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import sys
from typing import Any, Sequence

from tobkiri_host.artifact_materialization import capture_materialized_artifact
from tobkiri_host.contracts import ResolvedOperationBinding
from tobkiri_host.errors import AuthorizationError

from .host_provider_backend_v4 import HostProviderFactoryV4


def load_host_provider_factory(
    pack_root: Path,
    binding: ResolvedOperationBinding,
) -> HostProviderFactoryV4 | None:
    """Load an explicit factory from one digest-verified implementation.

    Absence of the named export is not discovery failure: it means the exact
    executable does not contribute an in-process Host Provider hook.
    """
    captured_before = capture_materialized_artifact(pack_root, binding)
    implementation = next(
        item for item in captured_before.files if item.path == captured_before.implementation_path
    )
    try:
        tree = ast.parse(
            implementation.content.decode("utf-8"),
            filename=implementation.path,
        )
    except (SyntaxError, UnicodeDecodeError) as error:
        raise AuthorizationError("Host Provider executable source is invalid") from error
    exports_factory = any(
        isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(target, ast.Name) and target.id == "HOST_PROVIDER_FACTORY"
            for target in (node.targets if isinstance(node, ast.Assign) else (node.target,))
        )
        for node in tree.body
    )
    if not exports_factory:
        return None
    implementation_path = pack_root / captured_before.implementation_path
    module_name = "_tobkiri_host_provider_" + binding.function.implementation_digest.removeprefix(
        "sha256:"
    )
    spec = importlib.util.spec_from_file_location(module_name, implementation_path)
    if spec is None or spec.loader is None:
        raise AuthorizationError("Host Provider executable loader is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as error:
        sys.modules.pop(module_name, None)
        raise AuthorizationError("Host Provider executable import failed") from error
    captured_after = capture_materialized_artifact(pack_root, binding)
    if captured_after.materialization_digest != captured_before.materialization_digest:
        sys.modules.pop(module_name, None)
        raise AuthorizationError("Host Provider executable changed during import")
    exported_factory = getattr(module, "HOST_PROVIDER_FACTORY", None)
    if isinstance(exported_factory, dict):
        if binding.function.function_id not in exported_factory:
            sys.modules.pop(module_name, None)
            return None
        factory = exported_factory[binding.function.function_id]
    else:
        factory = exported_factory
    if (
        factory is None
        or not isinstance(getattr(factory, "function_id", None), str)
        or not callable(getattr(factory, "capture", None))
        or factory.function_id != binding.function.function_id
    ):
        sys.modules.pop(module_name, None)
        raise AuthorizationError("Host Provider factory identity is invalid")
    return factory


def group_host_provider_captures(
    loaded: Sequence[tuple[str, tuple[ResolvedOperationBinding, ...], Any, str]],
) -> tuple[tuple[tuple[ResolvedOperationBinding, ...], HostProviderFactoryV4, str], ...]:
    """Share resource lifetime only within one verified implementation/artifact.

    Authority principals and edges remain operation-specific. An explicit finite
    factory declaration only groups capture/close; it creates no dispatch edge.
    The caller still checks the exact contribution set against these bindings.
    """
    groups: dict[tuple[Any, ...], tuple[list[ResolvedOperationBinding], Any, str]] = {}
    for function_id, bindings, factory, backend_id in loaded:
        key: tuple[Any, ...]
        declaration = getattr(factory, "capture_group", None)
        if declaration is None:
            key = ("single", function_id)
        else:
            if (
                not isinstance(declaration, tuple)
                or not 1 <= len(declaration) <= 16
                or any(
                    not isinstance(item, str) or not 0 < len(item) <= 256 for item in declaration
                )
                or len(set(declaration)) != len(declaration)
                or tuple(sorted(declaration)) != declaration
                or function_id not in declaration
                or not bindings
            ):
                raise AuthorizationError("Host Provider capture group is invalid")
            first = bindings[0]
            identity = (
                first.artifact.pack_id,
                first.artifact.publisher_lineage,
                first.artifact.digest,
                first.function.implementation_digest,
            )
            if any(
                (
                    binding.artifact.pack_id,
                    binding.artifact.publisher_lineage,
                    binding.artifact.digest,
                    binding.function.implementation_digest,
                )
                != identity
                for binding in bindings
            ):
                raise AuthorizationError("Host Provider capture group identity changed")
            key = ("shared", *identity, backend_id, declaration)
        if key in groups:
            members, representative, _ = groups[key]
            if type(representative).__qualname__ != type(factory).__qualname__:
                raise AuthorizationError("Host Provider capture group factory changed")
            members.extend(bindings)
        else:
            groups[key] = (list(bindings), factory, backend_id)
    return tuple(
        (tuple(bindings), factory, backend) for bindings, factory, backend in groups.values()
    )


__all__ = ["load_host_provider_factory", "group_host_provider_captures"]
