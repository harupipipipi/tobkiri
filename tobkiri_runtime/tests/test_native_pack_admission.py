"""Native Pack admission requires Launcher proof before Host policy evaluation."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from core_runtime.pack_api_server import PackAPIHandler


def _handler(body: dict[str, object], *, authorized: bool = True) -> PackAPIHandler:
    class Handler(PackAPIHandler):
        _panel_auth_manager = SimpleNamespace(
            validate_bootstrap_secret=lambda secret: authorized and secret == "launcher"
        )

        def _is_loopback_client(self, address: object) -> bool:
            return address == ("127.0.0.1", 1)

        def _discard_request_body(self) -> None:
            self.discarded = True

        def _parse_object_body(self) -> dict[str, object]:
            self.parsed = True
            return body

        def _send_response(self, response: object, status: int = 200) -> None:
            self.response = response
            self.status = status

    handler = object.__new__(Handler)
    handler.headers = {"X-Rumi-Desktop-Bootstrap": "launcher"}
    handler.client_address = ("127.0.0.1", 1)
    handler.discarded = False
    handler.parsed = False
    return handler


def test_native_pack_admission_rejects_untrusted_callers_before_parsing() -> None:
    handler = _handler({"source_root": "/ignored"}, authorized=False)
    handler._handle_native_external_pack_admission()
    assert handler.status == 401
    assert handler.discarded is True
    assert handler.parsed is False

    handler = _handler({}, authorized=False)
    handler._handle_native_external_pack_status()
    assert handler.status == 401

    handler = _handler({"source_root": "/ignored"})
    handler.client_address = ("192.0.2.1", 1)
    handler._handle_native_external_pack_admission()
    assert handler.status == 401
    assert handler.discarded is True
    assert handler.parsed is False


def test_native_pack_admission_uses_host_policy_and_returns_only_identity(
    monkeypatch, tmp_path: Path
) -> None:
    from core_runtime import external_pack_catalog_v4

    policy = tmp_path / "pack_control" / "publisher_trust.v4.json"
    policy.parent.mkdir()
    policy.write_text("{}", encoding="utf-8")
    source = tmp_path / "selected"
    source.mkdir()
    observed: list[tuple[Path, Path]] = []
    refreshes: list[object] = []
    monkeypatch.setenv("RUMI_PACK_PUBLISHER_TRUST_STORE", str(policy))

    def admit(path: Path, *, trust_store_path: Path) -> dict[str, str]:
        observed.append((path, trust_store_path))
        return {
            "pack_id": "sample.signed",
            "artifact_digest": "sha256:" + "a" * 64,
            "publisher_id": "sample",
            "store_token": "private",
        }

    monkeypatch.setattr(external_pack_catalog_v4, "admit_signed_external_pack", admit)
    handler = _handler({"source_root": str(source)})
    handler._runtime_refresh = lambda session: refreshes.append(session)
    handler._handle_native_external_pack_admission()
    assert handler.status == 200
    assert handler.response.success is True
    assert handler.response.data == {
        "pack_id": "sample.signed",
        "artifact_digest": "sha256:" + "a" * 64,
        "publisher_id": "sample",
        "catalog_refreshed": True,
    }
    assert observed == [(source, policy)]
    assert refreshes == [None]


def test_native_pack_admission_fails_closed_without_policy(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("RUMI_PACK_PUBLISHER_TRUST_STORE", raising=False)
    handler = _handler({"source_root": str(tmp_path)})
    handler._handle_native_external_pack_admission()
    assert handler.status == 409
    assert handler.response.success is False


def test_native_pack_trust_status_requires_verified_policy(
    monkeypatch, tmp_path: Path
) -> None:
    from core_runtime import pack_artifact_integrity

    policy = tmp_path / "publisher-trust.json"
    policy.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("RUMI_PACK_PUBLISHER_TRUST_STORE", str(policy))
    handler = _handler({})
    handler._handle_native_external_pack_status()
    assert handler.response.data["ready"] is False

    monkeypatch.setattr(
        pack_artifact_integrity,
        "read_host_policy_snapshot",
        lambda _path: {"publishers": {"sample": {}}, "install_records": {"pack": {}}},
    )
    handler = _handler({})
    handler._handle_native_external_pack_status()
    assert handler.response.data["ready"] is True


def test_native_pack_admission_preserves_committed_result_when_refresh_fails(
    monkeypatch, tmp_path: Path
) -> None:
    from core_runtime import external_pack_catalog_v4

    policy = tmp_path / "publisher-trust.json"
    policy.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("RUMI_PACK_PUBLISHER_TRUST_STORE", str(policy))
    monkeypatch.setattr(
        external_pack_catalog_v4,
        "admit_signed_external_pack",
        lambda *_args, **_kwargs: {
            "pack_id": "sample.signed",
            "artifact_digest": "sha256:" + "a" * 64,
            "publisher_id": "sample",
        },
    )
    handler = _handler({"source_root": str(tmp_path)})
    handler._runtime_refresh = lambda _session: (_ for _ in ()).throw(RuntimeError("stale"))
    handler._handle_native_external_pack_admission()
    assert handler.status == 200
    assert handler.response.data["catalog_refreshed"] is False


def test_native_pack_onboarding_requires_launcher_before_parsing() -> None:
    handler = _handler({}, authorized=False)
    handler._handle_native_pack_onboarding(commit=False)
    assert handler.status == 401
    assert handler.discarded is True
    assert handler.parsed is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX Host onboarding only")
def test_native_pack_onboarding_rejects_renderer_paths(monkeypatch, tmp_path: Path) -> None:
    from core_runtime import native_pack_onboarding

    called: list[object] = []
    monkeypatch.setattr(
        native_pack_onboarding,
        "preview_signed_pack",
        lambda *_args: called.append("preview"),
    )
    handler = _handler({"source_root": str(tmp_path), "public_key_path": "relative.pem"})
    handler._handle_native_pack_onboarding(commit=False)
    assert handler.status == 400
    assert called == []
