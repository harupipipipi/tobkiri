"""Verify the exact HTTP provider-status projection and fail-closed validation."""

from pathlib import Path
from types import ModuleType, SimpleNamespace as NS
import importlib.util
import json
import sys

import pytest

BASE = Path(__file__).resolve().parents[1]
RUNTIME = next(
    candidate / "tobkiri_runtime"
    for candidate in Path(__file__).resolve().parents
    if (candidate / "tobkiri_runtime/tobkiri_protocol").is_dir()
)
sys.path[:0] = [str(RUNTIME), str(RUNTIME / "ecosystem/defaultspack")]


def load(relative: str) -> ModuleType:
    path = BASE / relative
    name = "ecosystem.defaultspack.defaultspack.test_status_" + path.stem
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


projection = load("ecosystem/defaultspack/defaultspack/provider_status_presentation.py")
present = projection.present_provider_connection_status


def provider() -> dict[str, object]:
    return {
        "provider_instance_id": "connection/local:main",
        "display_name": "Local main",
        "enabled": True,
        "credential_status": "not_required",
        "health_status": "unverified",
        "reachability": "unknown",
        "observed_at": None,
    }


def test_status_projection_strips_internal_fields_without_mutating_source() -> None:
    public = {**provider(), "catalog_provider_id": "local"}
    internal = {
        **public,
        "adapter_id": "local-openai-compatible",
        "credential_handle": "synthetic-secret-marker",
        "endpoint": "https://synthetic-private-endpoint.invalid",
        "metadata": {"api_key": "synthetic-secret-marker"},
    }
    source = {"revision": 3, "providers": [internal], "internal": "private"}
    before = json.dumps(source, sort_keys=True)
    result = present(source)
    assert result == {"revision": 3, "providers": [public]}
    assert json.dumps(source, sort_keys=True) == before
    assert "synthetic-secret-marker" not in json.dumps(result)
    assert "synthetic-private-endpoint" not in json.dumps(result)
    assert result["providers"][0] is not internal


def test_empty_registry_is_valid_explicit_success() -> None:
    assert present({"revision": 0, "providers": []}) == {
        "revision": 0,
        "providers": [],
    }


def test_optional_catalog_id_is_omitted_and_required_null_is_preserved() -> None:
    result = present({"revision": 0, "providers": [provider()]})
    assert "catalog_provider_id" not in result["providers"][0]
    assert result["providers"][0]["observed_at"] is None


@pytest.mark.parametrize("value", [None, "", "   ", False, 0, [], {}])
def test_present_but_invalid_optional_catalog_id_fails_closed(value: object) -> None:
    with pytest.raises(ValueError):
        present(
            {
                "revision": 0,
                "providers": [
                    {
                        **provider(),
                        "catalog_provider_id": value,
                    }
                ],
            }
        )


@pytest.mark.parametrize("field", list(provider()))
def test_missing_required_provider_field_fails_closed(field: str) -> None:
    item = provider()
    del item[field]
    with pytest.raises(ValueError):
        present({"revision": 0, "providers": [item]})


@pytest.mark.parametrize(
    "changes",
    [
        {"provider_instance_id": ""},
        {"provider_instance_id": "   "},
        {"provider_instance_id": 1},
        {"display_name": ""},
        {"display_name": "   "},
        {"display_name": None},
        {"enabled": 1},
        {"enabled": "true"},
        {"credential_status": "optional"},
        {"credential_status": None},
        {"health_status": "healthy"},
        {"reachability": "connected"},
        {"observed_at": True},
        {"observed_at": "123"},
        {"observed_at": float("inf")},
        {"observed_at": float("nan")},
    ],
)
def test_invalid_provider_types_and_states_fail_closed(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        present({"revision": 0, "providers": [{**provider(), **changes}]})


@pytest.mark.parametrize(
    "source",
    [
        {},
        {"revision": 0},
        {"providers": []},
        {"revision": True, "providers": []},
        {"revision": -1, "providers": []},
        {"revision": 1.0, "providers": []},
        {"revision": 2**53, "providers": []},
        {"revision": 0, "providers": None},
        {"revision": 0, "providers": {}},
        {"revision": 0, "providers": [None]},
    ],
)
def test_invalid_snapshot_fails_closed(source: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        present(source)


def test_credentials_do_not_infer_health_or_reachability() -> None:
    item = {**provider(), "credential_status": "configured"}
    assert present({"revision": 1, "providers": [item]})["providers"] == [item]
    verified = {
        **item,
        "health_status": "verified",
        "reachability": "unavailable",
        "observed_at": 123.5,
    }
    assert present({"revision": 1, "providers": [verified]})["providers"] == [verified]


def test_http_projection_matches_only_exact_connection_status_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "ecosystem.defaultspack.defaultspack.provider_status_presentation"
    monkeypatch.setitem(sys.modules, name, projection)
    presentation = load("ecosystem/defaultspack/defaultspack/http_surface_presentation.py")
    identity = presentation._CONNECTION_STATUS_TARGET
    keys = ("contribution_id", "contract_id", "operation_id", "provider_id", "function_id")
    target = dict(zip(keys, identity))
    source = {"revision": 1, "providers": [{**provider(), "adapter_id": "internal"}]}
    render = presentation.DefaultspackHTTPPresentation().present_result

    def invoke(values: dict[str, str], **overrides: object) -> object:
        binding = NS(
            method="GET",
            path="/api/connections/status",
            presentation="broker_result",
            targets=(NS(**values),),
        )
        for key, value in overrides.items():
            setattr(binding, key, value)
        return render(binding, source, session=None, routes={}, capability_snapshot=None)

    assert invoke(target) == {"revision": 1, "providers": [provider()]}
    for key in keys:
        unrelated = {**target, key: target[key] + ".other"}
        assert invoke(unrelated) == source

    for overrides in (
        {"method": "POST"},
        {"path": "/api/connections/status/other"},
        {"presentation": "unrelated"},
        {"targets": ()},
        {"targets": (NS(**target), NS(**target))},
    ):
        assert invoke(target, **overrides) == source
