"""Independent Pack-owned validators conform to the same endpoint contract."""

import importlib
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture(params=["rumi_provider_registry_pack", "rumi_provider_adapters_pack"])
def validate_endpoint(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[1] / "tobkiri_runtime")
    )
    return importlib.import_module(
        f"ecosystem.{request.param}.runtime.local_endpoint"
    ).local_openai_endpoint


@pytest.mark.parametrize(
    "endpoint,expected",
    [
        ("http://127.0.0.1:1024/v1", "http://127.0.0.1:1024/v1"),
        ("http://127.0.0.1:18080/v1/", "http://127.0.0.1:18080/v1"),
        ("http://[::1]:65535/v1", "http://[::1]:65535/v1"),
    ],
)
def test_valid_local_endpoint(validate_endpoint, endpoint: str, expected: str) -> None:
    assert validate_endpoint(endpoint) == expected


@pytest.mark.parametrize(
    "endpoint",
    [
        None,
        8080,
        True,
        "",
        "x" * 2049,
        "http://localhost:18080/v1",
        "https://127.0.0.1:18080/v1",
        "http://127.0.0.2:18080/v1",
        "http://example.invalid:18080/v1",
        "http://127.0.0.1:80/v1",
        "http://127.0.0.1:65536/v1",
        "http://user@127.0.0.1:18080/v1",
        "http://127.0.0.1:18080/v1?token=x",
        "http://127.0.0.1:18080/v1#fragment",
        " http://127.0.0.1:18080/v1",
        "http://127.0.0.1:18080/v1\n",
        "http://127.0.0.1:18080/v1/chat/completions",
        "http://127.0.0.1:018080/v1",
        "http://[::ffff:127.0.0.1]:18080/v1",
        "http://127.0.0.1:18080/v1/../admin",
        "HTTP://127.0.0.1:18080/v1",
    ],
)
def test_unsafe_or_noncanonical_endpoint_is_rejected(
    validate_endpoint, endpoint
) -> None:
    with pytest.raises(ValueError, match="endpoint is invalid"):
        validate_endpoint(endpoint)


def test_adapter_import_does_not_require_registry_private_runtime() -> None:
    runtime = Path(__file__).resolve().parents[1] / "tobkiri_runtime"
    script = """
import importlib.abc
import importlib.util
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
class RejectRegistryRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'ecosystem.rumi_provider_registry_pack' or fullname.startswith(
            'ecosystem.rumi_provider_registry_pack.'
        ):
            raise ImportError('Registry private runtime is unavailable')
sys.meta_path.insert(0, RejectRegistryRuntime())
from ecosystem.rumi_provider_adapters_pack.runtime import adapter
assert adapter.local_openai_endpoint('http://127.0.0.1:18080/v1') == 'http://127.0.0.1:18080/v1'
source = Path(sys.argv[1]) / 'ecosystem/rumi_provider_adapters_pack/runtime/adapter.py'
spec = importlib.util.spec_from_file_location('_tobkiri_host_provider_import_test', source)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
assert module.HOST_PROVIDER_FACTORY
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, str(runtime)],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
