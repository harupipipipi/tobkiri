"""Focused sealed-guest ABI coverage for the Model Catalog Pack."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping

import pytest

from ecosystem.rumi_model_catalog_pack.runtime import catalog


@pytest.mark.parametrize(
    "operation_id",
    (
        "rumi_model_catalog_pack.bundled-model-catalog.generate",
        "rumi_model_catalog_pack.bundled-model-catalog.stream",
    ),
)
def test_packvm_catalog_abi_delegates_only_the_exact_catalog_operations(
    monkeypatch: pytest.MonkeyPatch,
    operation_id: str,
) -> None:
    """The sealed entrypoint has neither a Host client nor an ambient target."""

    calls: list[tuple[object, str, Mapping[str, Any]]] = []

    def factory(client: object) -> Any:
        def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
            calls.append((client, name, payload))
            return {"catalog_revision": "sha256:catalog", "models": []}

        return operation

    monkeypatch.setattr(catalog, "create_model_catalog_operation", factory)
    payload = {"provider_id": "fixture"}

    result = catalog.tobkiri_packvm_invoke(operation_id, payload)

    assert result == {"catalog_revision": "sha256:catalog", "models": []}
    assert calls == [(None, operation_id, payload)]


@pytest.mark.parametrize(
    "operation_id, payload",
    (
        ("list", {}),
        ("rumi_model_catalog_pack.bundled-model-catalog", {}),
        ("rumi_ai_gateway_pack.ai-gateway.generate", {}),
        ("rumi_model_catalog_pack.bundled-model-catalog.generate", []),
    ),
)
def test_packvm_catalog_abi_fails_closed_for_unknown_operation_or_payload(
    operation_id: object,
    payload: object,
) -> None:
    """Guest input cannot use this catalog file to select another target."""

    with pytest.raises(ValueError):
        catalog.tobkiri_packvm_invoke(operation_id, payload)


def test_staged_catalog_is_importable_without_repo_or_site_packages() -> None:
    """The shipped implementation needs only the sealed artifact and stdlib."""

    source = Path(catalog.__file__).resolve()
    script = """
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location("staged_catalog", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
result = module.tobkiri_packvm_invoke(
    "rumi_model_catalog_pack.bundled-model-catalog.generate",
    {"provider_id": "does-not-exist"},
)
print(json.dumps(result, sort_keys=True, separators=(",", ":")))
"""
    process = subprocess.run(
        (sys.executable, "-I", "-S", "-c", script, str(source)),
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )

    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert result["providers"] == []
    assert result["models"] == []
