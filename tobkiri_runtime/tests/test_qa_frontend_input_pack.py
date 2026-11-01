"""Reproducible GUI acceptance sample: signed v4 Pack with a local input route."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

import pytest
from cryptography.hazmat.primitives import serialization

from core_runtime.external_pack_catalog_v4 import admit_signed_external_pack
from core_runtime.pack_artifact_integrity import write_host_install_record
from core_runtime.pack_sdk import validate_pack_manifest
from core_runtime.pack_signature import verify_signed_pack
from ecosystem.defaultspack.defaultspack.v4_frontend_contributions import (
    project_selected_declarative_routes,
)
from tests.qa_frontend_input_pack import (
    FIXTURE,
    PACK_ID,
    PUBLISHER_ID,
    STATIC_PUBLIC_KEY,
    STATIC_SIGNED_PACK,
    VERSION,
    build_signed_qa_pack,
)
from tobkiri_host.artifact_compiler import compile_pack_root


def test_committed_qa_pack_compiles_as_inert_normal_pack() -> None:
    runtime_root = FIXTURE.parents[2]
    manifest = validate_pack_manifest(
        FIXTURE / "pack.v4.json",
        schema_path=(
            runtime_root / "tobkiri_protocol" / "schemas" / "pack_manifest_v4.schema.json"
        ),
    )
    assert manifest["pack"]["kind"] == "normal_sandbox"
    assert manifest["requirements"]["execution_boundary"] == "declarative_only"
    assert manifest["functions"] == []
    assert any(
        item["kind"] == "ui.contribution" and item["path"] == "frontend/contributions/input.json"
        for item in manifest["artifacts"]
    )
    assert compile_pack_root(FIXTURE).artifact.pack_id == PACK_ID


def test_static_signed_pack_contains_exact_committed_source() -> None:
    source_files = {
        path.relative_to(FIXTURE).as_posix(): path.read_bytes()
        for path in FIXTURE.rglob("*")
        if path.is_file()
    }
    signed_files = {
        path.relative_to(STATIC_SIGNED_PACK).as_posix(): path.read_bytes()
        for path in STATIC_SIGNED_PACK.rglob("*")
        if path.is_file()
        and path.relative_to(STATIC_SIGNED_PACK).as_posix() != ".tobkiri/signed-pack.json"
    }
    assert source_files == signed_files
    assert STATIC_PUBLIC_KEY.is_file()
    assert not list(STATIC_SIGNED_PACK.rglob("*.pem"))


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows Python cannot express the Pack signature's POSIX 0644 file mode",
)
def test_static_signed_pack_admits_and_projects_from_cas(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "user-data"))
    pack_root = tmp_path / PACK_ID
    shutil.copytree(STATIC_SIGNED_PACK, pack_root)
    for path in (pack_root, *pack_root.rglob("*")):
        if path.is_dir():
            path.chmod(0o755)
        elif path.is_file():
            path.chmod(0o644)
    signed = json.loads((pack_root / ".tobkiri" / "signed-pack.json").read_text())
    public_pem = STATIC_PUBLIC_KEY.read_text(encoding="utf-8")
    public_key = serialization.load_pem_public_key(public_pem.encode("utf-8"))
    verify_signed_pack(
        pack_root,
        signed,
        public_key,
        expected_publisher_id=PUBLISHER_ID,
        expected_pack_id=PACK_ID,
        expected_version=VERSION,
        expected_contract_versions={},
        expected_capabilities=[],
    )
    trust_dir = tmp_path / "host-policy"
    trust_dir.mkdir(mode=0o700)
    trust_store = trust_dir / "publisher-trust.json"
    write_host_install_record(
        trust_store,
        pack_id=PACK_ID,
        install_path=pack_root,
        record={
            "signature_required": True,
            "publisher_id": PUBLISHER_ID,
            "key_id": signed["signature"]["key_id"],
            "installed_version": VERSION,
            "signed_manifest_path": ".tobkiri/signed-pack.json",
            "contract_versions": {},
            "requested_capabilities": [],
        },
        publisher_record={
            "public_key_pem": public_pem,
            "allowed_pack_namespaces": [PACK_ID],
            "revoked_key_ids": [],
        },
    )
    admitted = admit_signed_external_pack(pack_root, trust_store_path=trust_store)
    projected, diagnostics, quarantined = project_selected_declarative_routes(
        [
            {
                "role": "pack",
                "identity": PACK_ID,
                "artifact_digest": admitted["artifact_digest"],
            }
        ],
        [],
        profile_id="defaults",
        profile_revision="qa-revision",
        activation_id="qa-activation",
        plan_digest="qa-plan",
    )
    assert diagnostics == []
    assert quarantined == []
    assert [item["route"] for item in projected] == ["/qa-frontend-input"]


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows Python cannot express the Pack signature's POSIX 0644 file mode",
)
def test_signed_qa_pack_admits_and_projects_input_from_cas(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path / "user-data"))
    pack_root = tmp_path / PACK_ID
    public_key_path = tmp_path / f"{PACK_ID}.public.pem"
    signed = build_signed_qa_pack(pack_root, public_key_path)
    assert signed["pack_id"] == PACK_ID
    assert signed["publisher_id"] == PUBLISHER_ID
    assert public_key_path.is_file()
    assert not list(pack_root.rglob("*.pem"))

    trust_dir = tmp_path / "host-policy"
    trust_dir.mkdir(mode=0o700)
    trust_store = trust_dir / "publisher-trust.json"
    write_host_install_record(
        trust_store,
        pack_id=PACK_ID,
        install_path=pack_root,
        record={
            "signature_required": True,
            "publisher_id": PUBLISHER_ID,
            "key_id": signed["signature"]["key_id"],
            "installed_version": VERSION,
            "signed_manifest_path": ".tobkiri/signed-pack.json",
            "contract_versions": {},
            "requested_capabilities": [],
        },
        publisher_record={
            "public_key_pem": public_key_path.read_text(encoding="utf-8"),
            "allowed_pack_namespaces": [PACK_ID],
            "revoked_key_ids": [],
        },
    )
    admitted = admit_signed_external_pack(pack_root, trust_store_path=trust_store)
    assert admitted["state"] == "committed"
    assert admitted["pack_id"] == PACK_ID

    projected, diagnostics, quarantined = project_selected_declarative_routes(
        [
            {
                "role": "pack",
                "identity": PACK_ID,
                "artifact_digest": admitted["artifact_digest"],
            }
        ],
        [],
        profile_id="defaults",
        profile_revision="qa-revision",
        activation_id="qa-activation",
        plan_digest="qa-plan",
    )
    assert diagnostics == []
    assert quarantined == []
    assert len(projected) == 1
    assert projected[0]["route"] == "/qa-frontend-input"
    assert projected[0]["view"]["input"] == {
        "label": "QA note",
        "placeholder": "Type a note here",
    }
    assert projected[0]["owner_pack_hash"] == admitted["artifact_digest"]
    assert json.loads((pack_root / ".tobkiri" / "signed-pack.json").read_text()) == signed
