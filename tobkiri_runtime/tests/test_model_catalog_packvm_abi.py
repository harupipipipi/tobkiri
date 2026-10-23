"""Focused sealed-guest ABI coverage for the Model Catalog Pack."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping

import pytest

from ecosystem.rumi_model_catalog_pack.runtime import catalog
from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_host.artifact_materialization import capture_materialized_artifact
from tobkiri_host.contracts import OperationCatalog, OperationRoute
from tobkiri_host.models import OpaqueAuthorityRef


@pytest.fixture
def staged_catalog(tmp_path: Path) -> Path:
    """Stage exactly the verified Host materializer output, with no repo fallback."""
    source = Path(catalog.__file__).resolve().parents[1]
    compiled = compile_pack_root(source)
    contract_id = "tobkiri.resource.ai.model.catalog.v1"
    operation_id = "rumi_model_catalog_pack.bundled-model-catalog.generate"
    route = OperationRoute(
        contract_id=contract_id,
        operation_id=operation_id,
        artifact_digest=compiled.artifact.digest,
        target_principal_ref=OpaqueAuthorityRef("authority:catalog-abi-test"),
        **compiled.routes[(contract_id, operation_id)],
    )
    binding = OperationCatalog((compiled.artifact,), (route,)).resolve(
        contract_id, operation_id, ">=1,<2",
    )
    artifact = capture_materialized_artifact(source, binding)
    assert "runtime/native_filters.py" in {item.path for item in artifact.files}
    destination = tmp_path / "sealed-catalog"
    for item in artifact.files:
        path = destination / item.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(item.content)
    return destination / artifact.implementation_path


def _invoke_staged(source: Path, script: str) -> subprocess.CompletedProcess[str]:
    bootstrap = """
import importlib.util
import json
import sys

def deny_network(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo'}:
        raise AssertionError('isolated catalog tests must not use the network')

sys.addaudithook(deny_network)
spec = importlib.util.spec_from_file_location('staged_catalog', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
"""
    return subprocess.run(
        (sys.executable, "-I", "-S", "-B", "-c", bootstrap + script, str(source)),
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
        cwd=source.parent,
    )


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


def test_staged_catalog_is_importable_without_repo_or_site_packages(
    staged_catalog: Path,
) -> None:
    """The shipped implementation needs only the sealed artifact and stdlib."""

    script = """
result = module.tobkiri_packvm_invoke(
    "rumi_model_catalog_pack.bundled-model-catalog.generate",
    {"provider_id": "does-not-exist"},
)
print(json.dumps(result, sort_keys=True, separators=(",", ":")))
"""
    process = _invoke_staged(staged_catalog, script)

    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert result["providers"] == []
    assert result["models"] == []


def test_staged_catalog_compiles_filtered_discovery_with_mock_http(
    staged_catalog: Path,
) -> None:
    process = _invoke_staged(staged_catalog, """
import io
calls = []
class Response(io.BytesIO):
    status = 200
    headers = {}

def fake_http(request, **kwargs):
    calls.append(request.full_url)
    assert request.get_header('Authorization') is None
    return Response(json.dumps({'data': [{
        'id': 'vendor/test-model', 'name': 'Test Model',
        'architecture': {'input_modalities': ['text'], 'output_modalities': ['text']},
        'pricing': {'prompt': '0', 'completion': '0'},
    }]}).encode())

module.urllib.request.urlopen = fake_http
result = module.tobkiri_packvm_invoke(
    'rumi_model_catalog_pack.bundled-model-catalog.generate',
    {'provider_id': 'openrouter', 'discovery_filters': {
        'output_modalities': ['text'], 'category': 'programming',
    }},
)
assert calls == ['https://openrouter.ai/api/v1/models?'
                 'output_modalities=text&category=programming']
assert [item['provider_model_id'] for item in result['models']] == ['vendor/test-model']
assert result['inventory']['openrouter']['source'] == 'openrouter_models_api_filtered'
assert not any(name == 'tobkiri_protocol' or name.startswith('tobkiri_protocol.')
               or name == 'ecosystem' or name.startswith('ecosystem.')
               for name in sys.modules)
""")
    assert process.returncode == 0, process.stderr


@pytest.mark.parametrize("payload", [
    {"provider_id": "openai", "discovery_filters": {"category": "programming"}},
    {"provider_id": "openrouter", "discovery_filters": {"zdr": True}},
    {"provider_id": "openrouter", "discovery_filters": {"output_modalities": ["bad"]}},
    {"provider_id": "openrouter", "discovery_filters": {"category": "unknown"}},
])
def test_staged_catalog_rejects_invalid_discovery_before_http(
    staged_catalog: Path, payload: dict[str, Any],
) -> None:
    process = _invoke_staged(staged_catalog, f"""
def unexpected_http(*args, **kwargs):
    raise AssertionError('invalid discovery must not reach HTTP')
module.urllib.request.urlopen = unexpected_http
try:
    module.tobkiri_packvm_invoke(
        'rumi_model_catalog_pack.bundled-model-catalog.generate', {payload!r},
    )
except ValueError:
    pass
else:
    raise AssertionError('invalid discovery was accepted')
""")
    assert process.returncode == 0, process.stderr


def test_staged_catalog_fails_closed_without_filter_companion(
    staged_catalog: Path,
) -> None:
    staged_catalog.with_name("native_filters.py").unlink()
    process = _invoke_staged(staged_catalog, "")
    assert process.returncode != 0
    assert "sealed model filter contract is missing or linked" in process.stderr
