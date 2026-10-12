"""Relative imports retain the verified Host Provider artifact boundary."""

from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import sys
from types import ModuleType, SimpleNamespace
from typing import Any, Iterator

import pytest

from core_runtime import host_provider_hooks_v4 as hooks
from core_runtime.captured_provider_modules_v4 import CapturedProviderModules
from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_host.contracts import OperationCatalog, OperationRoute
from tobkiri_host.errors import AuthorizationError, InvalidArtifactError
from tobkiri_host.models import OpaqueAuthorityRef

RUNTIME = Path(__file__).resolve().parents[1]
GATEWAY = RUNTIME / "ecosystem/rumi_ai_gateway_pack"
FUNCTION = "test.host.provider"
SOURCE = b"""
import json
class Factory:
    function_id = "test.host.provider"
    def capture(self, context):
        return None
    def helper(self):
        from .helper import value
        return json.loads(value)
HOST_PROVIDER_FACTORY = Factory()
"""


@pytest.fixture(autouse=True)
def clean_import_contexts() -> Iterator[None]:
    """Remove only private import contexts created by this test."""
    before = {id(finder) for finder in sys.meta_path}
    yield
    for finder in tuple(sys.meta_path):
        if isinstance(finder, CapturedProviderModules) and id(finder) not in before:
            finder.close()


def _capture(files: dict[str, bytes]) -> SimpleNamespace:
    content_digest = hashlib.sha256(
        b"".join(
            path.encode() + b"\0" + content for path, content in sorted(files.items())
        )
    ).hexdigest()
    return SimpleNamespace(
        pack_id="test_host_extension_pack",
        implementation_path="runtime/provider.py",
        materialization_digest="sha256:" + content_digest,
        files=tuple(
            SimpleNamespace(path=path, content=content)
            for path, content in files.items()
        ),
    )


def _binding(function_id: str = FUNCTION, artifact: str = "a") -> SimpleNamespace:
    return SimpleNamespace(
        artifact=SimpleNamespace(digest="sha256:" + artifact * 64),
        function=SimpleNamespace(function_id=function_id),
    )


def _load(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    files: dict[str, bytes],
    *,
    function_id: str = FUNCTION,
    artifact: str = "a",
) -> Any:
    captured = _capture(files)
    monkeypatch.setattr(hooks, "capture_materialized_artifact", lambda *_: captured)
    return hooks.load_host_provider_factory(root, _binding(function_id, artifact))


def _module(factory: Any) -> Any:
    return sys.modules[type(factory).__module__]


def _real_gateway_binding(root: Path) -> Any:
    compiled = compile_pack_root(root)
    contract = "tobkiri.service.ai.generate.v1"
    operation = "rumi_ai_gateway_pack.ai-gateway.generate"
    metadata = compiled.routes[(contract, operation)]
    function = next(
        item for item in compiled.artifact.functions if item.function_id == operation
    )
    route = OperationRoute(
        contract_id=contract,
        operation_id=operation,
        artifact_digest=compiled.artifact.digest,
        function_id=function.function_id,
        variant_id=metadata["variant_id"],
        catalog_digest=compiled.artifact.catalog_digest,
        platform=metadata["platform"],
        architecture=metadata["architecture"],
        runtime_abi=metadata["runtime_abi"],
        backend=metadata["backend"],
        execution_kind=metadata["execution_kind"],
        domain_kind=metadata["domain_kind"],
        execution_domain_profile=metadata["execution_domain_profile"],
        materialization_mode=metadata["materialization_mode"],
        target_principal_ref=OpaqueAuthorityRef("authority:captured-import-test"),
    )
    return OperationCatalog((compiled.artifact,), (route,)).resolve(
        contract, operation, ">=1,<2"
    )


def test_lazy_relative_helper_and_standard_absolute_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unindexed parents stay empty while declared helpers and stdlib work."""
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime/__init__.py").write_text("raise AssertionError('live init')")
    files = {"runtime/provider.py": SOURCE, "runtime/helper.py": b"value = '7'"}
    factory = _load(monkeypatch, tmp_path, files)
    module = _module(factory)
    assert module.__package__.endswith(".runtime")
    assert sys.modules[module.__package__].__path__ == []
    assert factory.helper() == 7


def test_indexed_package_init_and_nested_relative_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = {
        "runtime/provider.py": SOURCE.replace(
            b"from .helper import value", b"from .nested.helper import value"
        ),
        "runtime/__init__.py": b"from .nested import marker\n",
        "runtime/nested/__init__.py": b"marker = 11\n",
        "runtime/nested/helper.py": b"from . import marker\nvalue = str(marker)\n",
    }
    factory = _load(monkeypatch, tmp_path, files)
    assert factory.helper() == 11
    assert sys.modules[_module(factory).__package__].marker == 11


@pytest.mark.parametrize("helper", ["undeclared", "escape"])
def test_undeclared_live_helper_and_relative_escape_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, helper: str
) -> None:
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime/helper.py").write_text("value = '99'")
    source = SOURCE
    if helper == "escape":
        source = source.replace(
            b"from .helper import value", b"from ....helper import value"
        )
    factory = _load(monkeypatch, tmp_path, {"runtime/provider.py": source})
    with pytest.raises(ImportError):
        factory.helper()


def test_lazy_import_uses_captured_bytes_after_disk_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = {"runtime/provider.py": SOURCE, "runtime/helper.py": b"value = '7'"}
    factory = _load(monkeypatch, tmp_path, files)
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime/helper.py").write_text(
        "raise AssertionError('changed helper')"
    )
    assert factory.helper() == 7


@pytest.mark.parametrize(
    "body",
    [
        "from ecosystem.test_host_extension_pack.runtime.helper import value\n"
        "        return json.loads(value)",
        "import ecosystem.test_host_extension_pack.runtime.helper as helper\n"
        "        return json.loads(helper.value)",
        "import ecosystem.test_host_extension_pack.runtime.helper\n"
        "        return json.loads(ecosystem.test_host_extension_pack.runtime.helper.value)",
        "from ecosystem.test_host_extension_pack.runtime import helper\n"
        "        return json.loads(helper.value)",
        "from ecosystem.test_host_extension_pack import runtime\n"
        "        return runtime.provider.HOST_PROVIDER_FACTORY.function_id",
        "from ecosystem import test_host_extension_pack\n"
        "        return test_host_extension_pack.runtime.provider.HOST_PROVIDER_FACTORY.function_id",
    ],
)
def test_own_pack_absolute_imports_ignore_poisoned_canonical_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    """Both import forms bind captured bytes without changing canonical modules."""
    prefix = "ecosystem.test_host_extension_pack"
    poison = ModuleType(prefix)
    poison.value = "99"
    for suffix in ("", ".runtime", ".runtime.helper"):
        monkeypatch.setitem(sys.modules, prefix + suffix, poison)
    source = SOURCE.replace(
        b"from .helper import value\n        return json.loads(value)", body.encode()
    )
    factory = _load(
        monkeypatch,
        tmp_path,
        {"runtime/provider.py": source, "runtime/helper.py": b"value = '7'"},
    )
    assert factory.helper() in (7, FUNCTION)
    for suffix in ("", ".runtime", ".runtime.helper"):
        assert sys.modules[prefix + suffix] is poison


def test_own_pack_absolute_undeclared_helper_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = "ecosystem.test_host_extension_pack.runtime.helper"
    poison = ModuleType(prefix)
    poison.value = "99"
    monkeypatch.setitem(sys.modules, prefix, poison)
    source = SOURCE.replace(
        b"from .helper import value",
        b"from ecosystem.test_host_extension_pack.runtime.helper import value",
    )
    factory = _load(monkeypatch, tmp_path, {"runtime/provider.py": source})
    with pytest.raises(ImportError):
        factory.helper()


def test_repeated_loads_reuse_one_namespace_and_old_lazy_factory_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = {"runtime/provider.py": SOURCE, "runtime/helper.py": b"value = '7'"}
    first = _load(monkeypatch, tmp_path, files)
    namespace = type(first).__module__.split(".")[0]
    finders = len(sys.meta_path)
    module_names = {name for name in sys.modules if name.startswith(namespace)}
    for _ in range(20):
        assert _load(monkeypatch, tmp_path, files) is first
    assert len(sys.meta_path) == finders
    assert {name for name in sys.modules if name.startswith(namespace)} == module_names
    second = _load(
        monkeypatch, tmp_path, {**files, "runtime/helper.py": b"value = '9'"}
    )
    third = _load(monkeypatch, tmp_path, files, artifact="b")
    assert type(first).__module__ != type(second).__module__ != type(third).__module__
    assert _load(monkeypatch, tmp_path, files, function_id="missing.function") is None
    assert first.helper() == 7
    assert second.helper() == 9
    assert third.helper() == 7


@pytest.mark.parametrize("failure", ["import", "factory", "changed", "recapture"])
def test_failed_load_cleans_its_namespace_and_keeps_other_finders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    files = {"runtime/provider.py": SOURCE, "runtime/helper.py": b"value = '7'"}
    existing = _load(monkeypatch, tmp_path, files)
    finders_before = tuple(sys.meta_path)
    modules_before = set(sys.modules)
    bad = SOURCE + b"\nfrom .helper import value\n"
    if failure == "import":
        bad += b"raise RuntimeError('import failure')\n"
    elif failure == "factory":
        bad += b"HOST_PROVIDER_FACTORY = object()\n"
    captured = _capture({**files, "runtime/provider.py": bad})
    calls = 0

    def recapture(*_: Any) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        if calls == 2 and failure == "recapture":
            raise InvalidArtifactError("capture failed")
        if calls == 2 and failure == "changed":
            return SimpleNamespace(materialization_digest="sha256:" + "c" * 64)
        return captured

    monkeypatch.setattr(hooks, "capture_materialized_artifact", recapture)
    with pytest.raises((AuthorizationError, InvalidArtifactError)):
        hooks.load_host_provider_factory(tmp_path, _binding())
    assert tuple(sys.meta_path) == finders_before
    assert not {
        name
        for name in set(sys.modules) - modules_before
        if name.startswith("_tobkiri_host_provider_")
    }
    assert existing.helper() == 7


def test_real_materialization_rejects_tampered_or_symlink_helper(
    tmp_path: Path,
) -> None:
    """Exercise the production capture, with no signature or digest bypass."""
    root = tmp_path / GATEWAY.name
    shutil.copytree(GATEWAY, root)
    binding = _real_gateway_binding(root)
    helper = root / "runtime/model_parameters.py"
    helper.write_bytes(helper.read_bytes() + b"\n# changed\n")
    with pytest.raises(InvalidArtifactError, match="digest"):
        hooks.load_host_provider_factory(root, binding)
    helper.unlink()
    helper.symlink_to(GATEWAY / "runtime/model_parameters.py")
    with pytest.raises(InvalidArtifactError):
        hooks.load_host_provider_factory(root, binding)


def test_legacy_gateway_model_resolution_imports_relative_captured_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduce the old Gateway lazy import without depending on Git history.

    Use the checked-in Gateway logic and restore its legacy relative import if
    that source has migrated to an absolute import. This is a regression fixture,
    not a claim that transformed bytes retain an old signed artifact identity.
    """
    source = (GATEWAY / "runtime/gateway.py").read_bytes()
    source = source.replace(
        b"from ecosystem.rumi_ai_gateway_pack.runtime.model_parameters import (\n"
        b"        bounded_model_parameters,\n    )",
        b"from .model_parameters import bounded_model_parameters",
    )
    assert b"from .model_parameters import bounded_model_parameters" in source
    files = {
        "runtime/provider.py": source,
        "runtime/model_parameters.py": (
            GATEWAY / "runtime/model_parameters.py"
        ).read_bytes(),
    }
    factory = _load(
        monkeypatch,
        tmp_path,
        files,
        function_id="rumi_ai_gateway_pack.ai-gateway.generate",
    )

    class Client:
        def invoke(self, *_: Any) -> dict[str, Any]:
            return {"profile": {"model_id": "model", "parameters": {"max_tokens": 64}}}

    request = {"model_profile_id": "saved", "parameters": {"max_tokens": 100}}
    _module(factory)._resolve_model_reference(Client(), request, streaming=False)
    assert request["parameters"]["max_tokens"] == 64
