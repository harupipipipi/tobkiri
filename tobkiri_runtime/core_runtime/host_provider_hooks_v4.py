"""Load explicit Host Provider hooks from exact resolved executable bytes."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import sys

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
        item
        for item in captured_before.files
        if item.path == captured_before.implementation_path
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
            for target in (
                node.targets if isinstance(node, ast.Assign) else (node.target,)
            )
        )
        for node in tree.body
    )
    if not exports_factory:
        return None
    implementation_path = pack_root / captured_before.implementation_path
    module_name = (
        "_tobkiri_host_provider_"
        + binding.function.implementation_digest.removeprefix("sha256:")
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


__all__ = ["load_host_provider_factory"]
