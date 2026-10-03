from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
DEFAULTSPACK_ROOT = RUNTIME_ROOT / "ecosystem" / "defaultspack"
COMPATIBILITY_MODULE = DEFAULTSPACK_ROOT / "domain" / "components" / "__init__.py"
if str(DEFAULTSPACK_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from ecosystem.defaultspack.domain.components import (  # noqa: E402
    ComponentEntrypointResolutionError,
    DomainComponentRegistry,
    resolve_component_entrypoint,
)
from domain.ai_client.providers.component_metadata import (  # noqa: E402
    model_manifests_from_provider_components,
)
from domain.chat.stream_engine import _ai_failure_message  # noqa: E402


@pytest.mark.parametrize(
    ("first_name", "second_name"),
    (
        (
            "domain.components.validation",
            "ecosystem.defaultspack.domain.components.validation",
        ),
        (
            "ecosystem.defaultspack.domain.components.validation",
            "domain.components.validation",
        ),
    ),
)
def test_component_imports_share_one_identity_in_fresh_interpreter(
    first_name: str,
    second_name: str,
) -> None:
    script = f"""
import importlib
import sys
from pathlib import Path

runtime_root = Path({str(RUNTIME_ROOT)!r})
pack_root = Path({str(DEFAULTSPACK_ROOT)!r})
sys.path.insert(0, str(runtime_root))
sys.path.insert(0, str(pack_root))

first = importlib.import_module({first_name!r})
second = importlib.import_module({second_name!r})
legacy_package = importlib.import_module("domain.components")
canonical_package = importlib.import_module(
    "ecosystem.defaultspack.domain.components"
)
legacy_registry = importlib.import_module("domain.components.registry")
canonical_registry = importlib.import_module(
    "ecosystem.defaultspack.domain.components.registry"
)

assert first is second
assert first.ComponentManifestError is second.ComponentManifestError
assert legacy_package is canonical_package
assert legacy_registry is canonical_registry
assert legacy_registry.DomainComponentRegistry is canonical_registry.DomainComponentRegistry
assert (
    legacy_registry.get_domain_component_registry
    is canonical_registry.get_domain_component_registry
)
registry_sentinel = object()
legacy_registry._REGISTRY = registry_sentinel
assert canonical_registry._REGISTRY is registry_sentinel
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=RUNTIME_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def _write_component(
    root: Path,
    *,
    component_id: str = "xiaomi-mimo-global",
    entrypoint: str | None = "models.json",
    models_payload: object | None = None,
) -> None:
    component_root = root / "providers" / component_id
    component_root.mkdir(parents=True)
    manifest = {
        "id": component_id,
        "aliases": ["mimo"] if component_id == "xiaomi-mimo-global" else [],
        "category": "providers",
        "kind": "llm_provider",
        "version": "2026.08",
        "status": "stable",
        "entrypoints": {"models": entrypoint} if entrypoint is not None else {},
    }
    (component_root / "manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    if models_payload is not None:
        (component_root / "models.json").write_text(
            json.dumps(models_payload),
            encoding="utf-8",
        )


def test_component_entrypoint_resolves_alias_to_canonical_manifest_id(
    tmp_path: Path,
) -> None:
    domain_root = tmp_path / "domain"
    _write_component(domain_root, models_payload={"models": []})
    registry = DomainComponentRegistry(domain_root, strict=True)

    resolved = resolve_component_entrypoint(
        registry,
        category="providers",
        component_id="mimo",
        contract_id="models",
    )

    assert resolved is not None
    assert resolved.component_id == "xiaomi-mimo-global"
    assert resolved.contract_id == "models"
    assert resolved.revision == "2026.08"
    assert resolved.path == (
        domain_root / "providers" / "xiaomi-mimo-global" / "models.json"
    )


def test_provider_models_do_not_infer_undeclared_neighbor_file(
    tmp_path: Path,
) -> None:
    import ecosystem.defaultspack.domain.components.registry as registry_module

    domain_root = tmp_path / "domain"
    component_id = "issue-385-no-implicit-fallback"
    _write_component(
        domain_root,
        component_id=component_id,
        entrypoint=None,
        models_payload={"models": [{"id": "must-not-load"}]},
    )
    previous_registry = registry_module._REGISTRY
    registry_module._REGISTRY = DomainComponentRegistry(domain_root, strict=True)
    try:
        assert model_manifests_from_provider_components(component_id) == []
    finally:
        registry_module._REGISTRY = previous_registry


@pytest.mark.parametrize(
    ("entrypoint", "reason"),
    (
        ("../models.json", "invalid_relative_path"),
        ("missing.json", "entrypoint_outside_component_or_unavailable"),
    ),
)
def test_component_entrypoint_fails_closed_with_safe_diagnostics(
    tmp_path: Path,
    entrypoint: str,
    reason: str,
) -> None:
    domain_root = tmp_path / "domain"
    _write_component(domain_root, entrypoint=entrypoint)
    registry = DomainComponentRegistry(domain_root, strict=True)

    with pytest.raises(ComponentEntrypointResolutionError) as error:
        resolve_component_entrypoint(
            registry,
            category="providers",
            component_id="xiaomi-mimo-global",
            contract_id="models",
        )

    diagnostic = error.value.safe_diagnostic()
    assert "module=ecosystem.defaultspack.domain.components.entrypoints" in diagnostic
    assert "component=providers/xiaomi-mimo-global" in diagnostic
    assert "contract=models" in diagnostic
    assert "revision=2026.08" in diagnostic
    assert f"reason={reason}" in diagnostic
    assert str(tmp_path) not in diagnostic
    assert _ai_failure_message(error.value) == diagnostic
    assert not diagnostic.startswith("AI request failed")


def test_runtime_code_does_not_add_new_legacy_component_imports() -> None:
    findings: list[str] = []
    for path in sorted(DEFAULTSPACK_ROOT.rglob("*.py")):
        if path == COMPATIBILITY_MODULE or "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            module_names: list[str] = []
            if isinstance(node, ast.Import):
                module_names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                module_names.append(node.module)
            for module_name in module_names:
                if module_name == "domain.components" or module_name.startswith(
                    "domain.components."
                ):
                    relative = path.relative_to(DEFAULTSPACK_ROOT)
                    findings.append(
                        f"{relative}:{getattr(node, 'lineno', 0)}:{module_name}"
                    )
    assert findings == []


def test_legacy_alias_inventory_covers_every_owned_component_module() -> None:
    import ecosystem.defaultspack.domain.components as components_package

    component_dir = DEFAULTSPACK_ROOT / "domain" / "components"
    expected = {
        path.stem
        for path in component_dir.glob("*.py")
        if path.name != "__init__.py"
    }
    assert set(components_package._OWNED_SUBMODULES) == expected


def test_mimo_scheduler_chat_path_keeps_component_identity_and_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_model_catalog_selected: None,
) -> None:
    del provider_model_catalog_selected
    import importlib

    from domain.agent import scheduler as scheduler_module
    from domain.agent.scheduler import Scheduler

    schedules_dir = tmp_path / "schedules"
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_AGENT_SCHEDULES_DIR",
        str(schedules_dir),
    )
    monkeypatch.setattr(Scheduler, "_arm_timer", lambda self, schedule_id: None)
    monkeypatch.setattr(
        scheduler_module,
        "_current_conversation_node_id",
        lambda conversation_id: "",
    )
    Scheduler._instance = None
    calls: list[dict[str, object]] = []

    def fake_chat_send(
        payload: dict[str, object],
        context: dict[str, object],
    ) -> dict[str, object]:
        legacy_validation = importlib.import_module("domain.components.validation")
        canonical_validation = importlib.import_module(
            "ecosystem.defaultspack.domain.components.validation"
        )
        legacy_registry = importlib.import_module("domain.components.registry")
        canonical_registry = importlib.import_module(
            "ecosystem.defaultspack.domain.components.registry"
        )
        component_metadata = importlib.import_module(
            "domain.ai_client.providers.component_metadata"
        )

        assert legacy_validation is canonical_validation
        assert legacy_registry is canonical_registry
        assert (
            legacy_registry.get_domain_component_registry()
            is canonical_registry.get_domain_component_registry()
        )
        assert "xiaomi-mimo-global" in component_metadata.provider_component_metadata_map()
        calls.append({"payload": payload, "context": context})
        return {
            "status": "ok",
            "data": {
                "id": "assistant-import-stable",
                "content": "component validation import stable",
                "finish_reason": "stop",
            },
        }

    monkeypatch.setattr("blocks.chat.send.run", fake_chat_send)
    scheduler = Scheduler()
    schedule = scheduler.create_schedule(
        "interval",
        {
            "message": "Run the MiMo self-improvement loop.",
            "model": "stub/default",
            "conversation_id": "conversation-issue-385",
            "profile_id": "defaultspack.mimo_coding_company",
            "agent_id": "scheduler",
            "metadata": {
                "profile_id": "defaultspack.mimo_coding_company",
                "loop_key": "improvement_loop",
            },
        },
        {"value": 180, "unit": "minutes"},
        name="MiMo Coding Company improvement loop",
        description="Regression coverage for issue #385.",
    )

    try:
        result = scheduler.trigger_now(schedule["id"])
        history = scheduler.get_history(schedule["id"])

        assert result["status"] == "completed"
        assert result["error"] is None
        assert result["result"] == "component validation import stable"
        assert history["total"] == 1
        assert "components.validation" not in str(history["entries"][0].get("error"))
        assert len(calls) == 1
        payload = calls[0]["payload"]
        assert isinstance(payload, dict)
        message = payload["message"]
        assert isinstance(message, dict)
        assert message["metadata"]["source"] == "scheduler"
        assert message["metadata"]["loop_key"] == "improvement_loop"
    finally:
        scheduler.delete_schedule(schedule["id"])
        Scheduler._instance = None
