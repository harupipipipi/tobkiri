"""Selected v4 Pack UI declarations enter the production catalog only when verified."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core_runtime.pack_sdk import refresh_scaffold_artifacts, scaffold_pack
from tobkiri_host.runtime import _selected_frontend_pack_closure
from ecosystem.defaultspack.defaultspack import v4_frontend_contributions as frontend
from ecosystem.defaultspack.defaultspack.http_surface_presentation import (
    DefaultspackHTTPPresentation,
    _selected_pack_frontend_routes,
)
from core_runtime.global_contracts.http_contract_dispatch import (
    HTTPCapabilitySnapshot,
    HTTPContractBinding,
)
from tobkiri_protocol.canonical import canonical_digest


PACK_ID = "qa.frontend.route"
DESCRIPTOR = "frontend/contributions/route.json"
STRATEGY_DESCRIPTOR = "frontend/contributions/strategy.json"
STRATEGY_REFERENCE = "qa_frontend_route.strategy.execute"
_NO_INPUT = object()


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _pack(
    root: Path, *, route: str = "/qa-route", mode: str = "declarative",
    input_view: object = _NO_INPUT,
) -> tuple[Path, str]:
    scaffold_pack(root, pack_id=PACK_ID, display_name="QA frontend route")
    descriptor = root / DESCRIPTOR
    descriptor.parent.mkdir(parents=True)
    payload = {
        "version": "rumi.ui.contribution.v1",
        "id": "qa.frontend.route.view",
        "kind": "route",
        "mode": mode,
        "label": "QA route",
        "priority": 0,
        "route": route,
        "view": {"title": "Pack route", "body": "Selected Pack content"},
        "accessibility": {"name": "QA route", "keyboard": True},
    }
    if input_view is not _NO_INPUT:
        payload["view"]["input"] = input_view
    if mode == "isolated":
        payload["isolated"] = {
            "path": f"/isolated/packs/{PACK_ID}/index.html", "rpc_contracts": [],
        }
    _write(descriptor, payload)
    refresh_scaffold_artifacts(root)
    manifest_path = root / "pack.v4.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    declaration = next(item for item in manifest["artifacts"] if item["path"] == DESCRIPTOR)
    declaration["kind"] = "ui.contribution"
    artifact_digest = canonical_digest(manifest["artifacts"])
    manifest["pack"]["artifact_digest"] = artifact_digest
    manifest["integrity"]["artifact_set_digest"] = artifact_digest
    _write(manifest_path, manifest)
    index_path = root / "artifact-index.v4.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["artifact_set_digest"] = artifact_digest
    next(item for item in index["artifacts"] if item["path"] == "pack.v4.json")["digest"] = _digest(manifest_path)
    unsigned = {key: value for key, value in index.items() if key != "integrity_seal"}
    index["integrity_seal"]["signed_digest"] = canonical_digest(unsigned)
    _write(index_path, index)
    return root, artifact_digest


def _project(digest: str, occupied: list[dict[str, object]] | None = None):
    return frontend.project_selected_declarative_routes(
        [{"role": "pack", "identity": PACK_ID, "artifact_digest": digest}],
        occupied or [],
        profile_id="defaults",
        profile_revision="revision-1",
        activation_id="activation-1",
        plan_digest="plan-1",
    )


def _admit_fixture(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(frontend, "resolve_admitted_pack_root", lambda pack_id: root)
    monkeypatch.setattr(frontend, "load_admitted_pack_catalog", lambda: {
        PACK_ID: {"runtime_artifacts": [{"kind": "ui.contribution", "path": DESCRIPTOR}]},
    })


def _strategy_pack(
    root: Path,
    *,
    pack_kind: str = "normal_sandbox",
) -> tuple[Path, str]:
    """Build one signed declarative strategy descriptor fixture."""

    scaffold_pack(root, pack_id=PACK_ID, display_name="QA strategy")
    descriptor = root / STRATEGY_DESCRIPTOR
    descriptor.parent.mkdir(parents=True)
    _write(
        descriptor,
        {
            "version": "rumi.ui.contribution.v1",
            "id": "qa.frontend.route.strategy",
            "kind": "ai_strategy",
            "mode": "declarative",
            "label": "QA strategy",
            "description": "A strategy supplied by the selected Pack.",
            "priority": 0,
            "strategy_reference": STRATEGY_REFERENCE,
            "accessibility": {"name": "QA strategy", "keyboard": True},
            "command": {
                "name": "qa-strategy",
                "label": "QA strategy",
                "description": "Select QA strategy.",
                "aliases": ["qa"],
            },
        },
    )
    refresh_scaffold_artifacts(root)
    manifest_path = root / "pack.v4.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pack"]["kind"] = pack_kind
    declaration = next(
        item for item in manifest["artifacts"] if item["path"] == STRATEGY_DESCRIPTOR
    )
    declaration["kind"] = "ui.contribution"
    artifact_digest = canonical_digest(manifest["artifacts"])
    manifest["pack"]["artifact_digest"] = artifact_digest
    manifest["integrity"]["artifact_set_digest"] = artifact_digest
    _write(manifest_path, manifest)
    index_path = root / "artifact-index.v4.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["artifact_set_digest"] = artifact_digest
    next(
        item for item in index["artifacts"] if item["path"] == "pack.v4.json"
    )["digest"] = _digest(manifest_path)
    unsigned = {key: value for key, value in index.items() if key != "integrity_seal"}
    index["integrity_seal"]["signed_digest"] = canonical_digest(unsigned)
    _write(index_path, index)
    return root, artifact_digest


def _project_strategy(digest: str, catalog: list[dict[str, object]]):
    return frontend.project_selected_ai_strategies(
        [{"role": "pack", "identity": PACK_ID, "artifact_digest": digest}],
        {"strategies": catalog},
        profile_id="defaults",
        profile_revision="revision-1",
        activation_id="activation-1",
        plan_digest="plan-1",
    )


def _admit_strategy_fixture(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(frontend, "resolve_admitted_pack_root", lambda pack_id: root)
    monkeypatch.setattr(frontend, "load_admitted_pack_catalog", lambda: {
        PACK_ID: {
            "runtime_artifacts": [
                {"kind": "ui.contribution", "path": STRATEGY_DESCRIPTOR}
            ]
        },
    })


@pytest.mark.parametrize("pack_kind", ["normal_sandbox", "host_extension"])
def test_selected_signed_strategy_descriptor_is_projected_generically(
    tmp_path: Path,
    monkeypatch,
    pack_kind: str,
) -> None:
    """A signed descriptor works for either supported Pack execution boundary."""

    root, digest = _strategy_pack(tmp_path / PACK_ID, pack_kind=pack_kind)
    _admit_strategy_fixture(monkeypatch, root)
    projected, diagnostics, quarantined = _project_strategy(
        digest,
        [{
            "strategy_reference": STRATEGY_REFERENCE,
            "pack_id": PACK_ID,
            "artifact_digest": digest,
        }],
    )

    assert diagnostics == []
    assert quarantined == []
    assert len(projected) == 1
    strategy = projected[0]
    assert strategy["build_identity"].startswith("sha256:")
    assert strategy["descriptor_hash"].startswith("sha256:")
    assert {
        key: value
        for key, value in strategy.items()
        if key not in {"build_identity", "descriptor_hash"}
    } == {
        "accessibility": {"name": "QA strategy", "keyboard": True},
        "available": True,
        "command": {
            "name": "qa-strategy",
            "label": "QA strategy",
            "description": "Select QA strategy.",
            "aliases": ["qa"],
        },
        "contribution_id": "qa.frontend.route.strategy",
        "description": "A strategy supplied by the selected Pack.",
        "kind": "ai_strategy",
        "label": "QA strategy",
        "mode": "declarative",
        "owner_pack_hash": digest,
        "owner_pack_id": PACK_ID,
        "plan_admitted": True,
        "priority": 0,
        "resolved_activation_id": "activation-1",
        "resolved_plan_hash": "plan-1",
        "resolved_profile_id": "defaults",
        "resolved_profile_revision": "revision-1",
        "signature_verified": True,
        "strategy_reference": STRATEGY_REFERENCE,
    }


def test_strategy_descriptor_without_matching_plan_provider_is_quarantined(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Descriptor text cannot introduce a strategy the active Plan did not admit."""

    root, digest = _strategy_pack(tmp_path / PACK_ID)
    _admit_strategy_fixture(monkeypatch, root)
    projected, diagnostics, quarantined = _project_strategy(digest, [])

    assert projected == []
    assert quarantined == [PACK_ID]
    assert diagnostics[0]["code"] == "v4_strategy_pack_quarantined"


def test_selected_v4_declarative_route_is_bound_to_capture(tmp_path: Path, monkeypatch) -> None:
    root, digest = _pack(tmp_path / PACK_ID)
    _admit_fixture(monkeypatch, root)
    projected, diagnostics, quarantined = _project(digest)
    assert diagnostics == []
    assert quarantined == []
    assert len(projected) == 1
    assert projected[0]["route"] == "/qa-route"
    assert projected[0]["mode"] == "declarative"
    assert projected[0]["owner_pack_hash"] == digest
    assert projected[0]["resolved_activation_id"] == "activation-1"
    assert projected[0]["resolved_plan_hash"] == "plan-1"
    assert "action_contract" not in projected[0]


def test_selected_route_can_contain_local_input(tmp_path: Path, monkeypatch) -> None:
    root, digest = _pack(
        tmp_path / PACK_ID,
        input_view={"label": "Pack note", "placeholder": "Type here"},
    )
    _admit_fixture(monkeypatch, root)
    projected, diagnostics, quarantined = _project(digest)
    assert diagnostics == []
    assert quarantined == []
    assert projected[0]["view"]["input"] == {
        "label": "Pack note", "placeholder": "Type here",
    }
    assert "action_contract" not in projected[0]


@pytest.mark.parametrize("input_view", [
    None,
    {},
    {"label": " "},
    {"label": "x" * 257},
    {"label": "Pack note", "placeholder": "x" * 257},
    {"label": "Pack note", "placeholder": 4},
    {"label": "Pack note", "action": "submit"},
])
def test_invalid_or_active_input_is_quarantined(
    tmp_path: Path, monkeypatch, input_view: object,
) -> None:
    root, digest = _pack(tmp_path / PACK_ID, input_view=input_view)
    _admit_fixture(monkeypatch, root)
    projected, diagnostics, quarantined = _project(digest)
    assert projected == []
    assert quarantined == [PACK_ID]
    assert diagnostics[0]["code"] == "v4_frontend_pack_quarantined"


@pytest.mark.parametrize("defect", ["digest", "descriptor", "index", "mode", "collision", "subpath"])
def test_unverified_or_colliding_pack_route_is_not_projected(
    tmp_path: Path, monkeypatch, defect: str,
) -> None:
    root, digest = _pack(
        tmp_path / PACK_ID,
        route="/qa/route" if defect == "subpath" else "/qa-route",
        mode="isolated" if defect == "mode" else "declarative",
    )
    _admit_fixture(monkeypatch, root)
    occupied: list[dict[str, object]] = []
    if defect == "digest":
        digest = "sha256:" + "0" * 64
    if defect == "descriptor":
        (root / DESCRIPTOR).write_text("{}", encoding="utf-8")
    if defect == "index":
        index_path = root / "artifact-index.v4.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        next(item for item in index["artifacts"] if item["path"] == DESCRIPTOR)["digest"] = (
            "sha256:" + "0" * 64
        )
        unsigned = {key: value for key, value in index.items() if key != "integrity_seal"}
        index["integrity_seal"]["signed_digest"] = canonical_digest(unsigned)
        _write(index_path, index)
    if defect == "collision":
        occupied = [{"kind": "route", "route": "/qa-route", "contribution_id": "app.route", "owner_pack_id": "defaultspack"}]
    if defect == "subpath":
        occupied = [{"kind": "route", "route": "/qa", "route_match": "subpath", "contribution_id": "app.route", "owner_pack_id": "defaultspack"}]
    projected, diagnostics, quarantined = _project(digest, occupied)
    assert projected == []
    assert quarantined == [PACK_ID]
    assert diagnostics[0]["severity"] == "error"


def test_unselected_pack_is_never_read(tmp_path: Path, monkeypatch) -> None:
    root, digest = _pack(tmp_path / PACK_ID)
    _admit_fixture(monkeypatch, root)
    projected, diagnostics, quarantined = frontend.project_selected_declarative_routes(
        [{"role": "base", "identity": PACK_ID, "artifact_digest": digest}],
        [],
        profile_id="defaults",
        profile_revision="revision-1",
        activation_id="activation-1",
        plan_digest="plan-1",
    )
    assert (projected, diagnostics, quarantined) == ([], [], [])


def test_catalog_without_pack_closure_has_no_optional_frontend_diagnostic() -> None:
    session = SimpleNamespace(selected_pack_closure=())
    assert _selected_pack_frontend_routes(session, []) == ([], [], [])
    assert _selected_frontend_pack_closure({"application": None, "effective_set": []}) == ()


def test_defaults_bundle_only_application_does_not_raise_frontend_diagnostic() -> None:
    runtime_root = Path(__file__).resolve().parents[1]
    lock = json.loads((
        runtime_root / "ecosystem/defaultspack/v4/defaults.profile.lock.v5.json"
    ).read_text(encoding="utf-8"))
    closure = _selected_frontend_pack_closure(lock)
    assert "runtime.tauri.application.default" not in {item["identity"] for item in closure}
    projected, diagnostics, quarantined = frontend.project_selected_declarative_routes(
        closure, [], profile_id="defaults", profile_revision="revision-1",
        activation_id="activation-1", plan_digest="plan-1",
    )
    assert (projected, diagnostics, quarantined) == ([], [], [])


def test_presentation_rejects_stale_activation(tmp_path: Path, monkeypatch) -> None:
    root, digest = _pack(tmp_path / PACK_ID)
    _admit_fixture(monkeypatch, root)
    def stale() -> None:
        raise ValueError("captured activation changed")
    session = SimpleNamespace(
        profile_id="defaults",
        profile_revision="revision-1",
        activation_id="activation-1",
        plan_digest="plan-1",
        selected_pack_closure=({"role": "pack", "identity": PACK_ID, "artifact_digest": digest},),
        assert_current=stale,
    )
    projected, diagnostics, _ = _selected_pack_frontend_routes(session, [])
    assert projected == []
    assert diagnostics[0]["code"] == "v4_frontend_capture_unavailable"


def test_production_ui_catalog_presents_selected_pack_route(
    tmp_path: Path, monkeypatch,
) -> None:
    root, digest = _pack(tmp_path / PACK_ID)
    _admit_fixture(monkeypatch, root)
    session = SimpleNamespace(
        profile_id="defaults", profile_revision="revision-1",
        activation_id="activation-1", plan_digest="plan-1",
        selected_pack_closure=({"role": "pack", "identity": PACK_ID, "artifact_digest": digest},),
        assert_current=lambda: None,
    )
    entries = {
        "default_entry_id": "chat",
        "entries": [{
            "entry_id": "chat", "route": "/chat", "match": "exact",
            "contribution_id": "defaultspack.frontend.chat",
            "implementation": "defaultspack.chat", "label": "Chat",
        }],
    }
    binding = HTTPContractBinding(
        method="GET", path="/api/ui/catalog", presentation="dynamic_pack_catalog",
        targets=(), application_id="defaultspack", route_namespace="defaultspack",
        artifact_digest="sha256:" + "1" * 64,
        frontend_entries=json.dumps(entries).encode("utf-8"),
    )
    result = DefaultspackHTTPPresentation().present_result(
        binding, {}, session=session, routes={},
        capability_snapshot=lambda *args, **kwargs: HTTPCapabilitySnapshot(
            catalog_hash="sha256:" + "2" * 64, targets=(),
        ),
    )["dynamic_host"]
    assert [item["route"] for item in result["contributions"]] == ["/chat", "/qa-route"]
    assert result["catalog_hash"] == canonical_digest({"contributions": []})
    assert result["diagnostics"] == []
