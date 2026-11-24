"""Native onboarding failure stages are finite Host-only diagnostic facts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
from pathlib import Path
from typing import Any

import pytest

from core_runtime import native_pack_onboarding as onboarding
from core_runtime import native_pack_onboarding_http as native_http
from core_runtime.external_pack_catalog_v4 import ExternalPackAdmissionOutcomeUnknown

CANARY = "NEVER_LOG_NATIVE_PATH_KEY_BODY_CHALLENGE_EXCEPTION"
PREVIEW_DIGEST = "sha256:" + "a" * 64


class PrivateFailure(RuntimeError):
    def __str__(self) -> str:
        raise AssertionError("exception text must never be inspected")


class Handler:
    def __init__(self, ceremony: Any, *, commit: bool) -> None:
        self.headers: dict[str, str] = {}
        self.body = {
            "source_root": f"/source/{CANARY}",
            "public_key_path": f"/keys/{CANARY}.pem",
        }
        if commit:
            self.body.update(
                preview_digest=PREVIEW_DIGEST,
                key_version_authorization_challenge=CANARY,
                key_version_authorization_digest=CANARY,
            )
        self._native_pack_key_ceremony = ceremony
        self._runtime_refresh = None
        self.status = 0
        self.response: Any = None
        self.discarded: list[str] = []

    def _native_pack_launcher_authenticated(self) -> bool:
        return True

    def _native_pack_trust_store(self) -> Path:
        return Path(f"/trust/{CANARY}")

    def _parse_object_body(self) -> dict[str, str]:
        return self.body

    def _discard_request_body(self) -> None:
        pass

    def _send_response(self, response: Any, status: int = 200) -> None:
        self.status, self.response = status, response


@pytest.mark.skipif(native_http.os.name == "nt", reason="Native POSIX port")
@pytest.mark.parametrize(
    ("phase", "commit"),
    [
        ("trust", False),
        ("preview", False),
        ("review_parameters", False),
        ("prepare", False),
        ("review_context", False),
        ("authorize", True),
        ("commit", True),
        ("review_context", True),
    ],
)
def test_failure_records_exact_stage_without_any_private_values(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    phase: str,
    commit: bool,
) -> None:
    """The actual handler keeps its response and logs only one fixed stage."""

    def step(name: str) -> None:
        if phase == name:
            error = PrivateFailure(CANARY)
            error.__cause__ = RuntimeError(CANARY)
            raise error

    class Ceremony:
        def prepare(self, result: Any, **arguments: Any) -> Any:
            arguments["context_loader"]()
            step("prepare")
            return result

        def authorize(self, result: Any, **arguments: Any) -> tuple[int, str]:
            arguments["context_loader"]()
            step("authorize")
            return 1, PREVIEW_DIGEST

        def discard(self, challenge: str) -> None:
            handler.discarded.append(challenge)

    def preview(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        step("preview")
        return {"preview_digest": PREVIEW_DIGEST}

    def parameters(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        step("review_parameters")
        return {}

    def context() -> dict[str, Any]:
        step("review_context")
        return {}

    def commit_pack(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        step("commit")
        return {
            "pack_id": "sample",
            "artifact_digest": PREVIEW_DIGEST,
            "publisher_id": "sample",
        }

    handler = Handler(Ceremony(), commit=commit)
    original_trust = handler._native_pack_trust_store

    def trust() -> Path:
        step("trust")
        return original_trust()

    handler._native_pack_trust_store = trust
    monkeypatch.setattr(onboarding, "preview_native_signed_pack", preview)
    monkeypatch.setattr(onboarding, "native_key_version_review_parameters", parameters)
    monkeypatch.setattr(onboarding, "commit_native_signed_pack", commit_pack)
    monkeypatch.setattr(native_http, "_current_review_context", context)
    with caplog.at_level(logging.WARNING, logger=native_http.logger.name):
        native_http.handle_native_pack_onboarding(handler, commit=commit)
    assert handler.status == 409
    assert handler.response.error == "Signed Pack verification was denied"
    records = [r for r in caplog.records if r.name == native_http.logger.name]
    assert len(records) == 1
    assert records[0].getMessage() == (
        f"Native signed Pack verification denied stage={phase}"
    )
    assert records[0].args == (phase,)
    assert records[0].exc_info is None and records[0].stack_info is None
    assert CANARY not in caplog.text
    assert "PrivateFailure" not in caplog.text
    assert handler.discarded == ([CANARY] if commit else [])


class HostileString(str):
    def __hash__(self) -> int:
        raise AssertionError("untrusted stage must not be hashed")

    def __str__(self) -> str:
        raise AssertionError("untrusted stage must not be rendered")


@pytest.mark.parametrize(
    "value",
    [None, True, 123, float("nan"), CANARY * 10000, HostileString("preview"), {}],
    ids=["none", "bool", "integer", "nan", "long", "subclass", "mapping"],
)
def test_invalid_stage_is_bounded_and_never_rendered(
    value: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only exact finite strings can enter the Host record."""
    with caplog.at_level(logging.WARNING, logger=native_http.logger.name):
        native_http._log_failure_stage(value)
    assert caplog.records[-1].args == ("trust",)
    assert caplog.records[-1].getMessage() == (
        "Native signed Pack verification denied stage=trust"
    )
    assert CANARY not in caplog.text


@pytest.mark.skipif(native_http.os.name == "nt", reason="Native POSIX port")
@pytest.mark.parametrize("publication_unknown", [False, True])
def test_broken_diagnostic_sink_cannot_replace_failure_outcome(
    monkeypatch: pytest.MonkeyPatch,
    publication_unknown: bool,
) -> None:
    """Logger errors cannot change ordinary denial or uncertain publication."""
    handler = Handler(None, commit=True)
    handler.body.pop("key_version_authorization_challenge")
    handler.body.pop("key_version_authorization_digest")

    def failed_commit(*_args: Any, **_kwargs: Any) -> Any:
        if publication_unknown:
            raise ExternalPackAdmissionOutcomeUnknown(CANARY)
        raise PrivateFailure(CANARY)

    def broken_sink(*_args: Any, **_kwargs: Any) -> None:
        raise PrivateFailure(CANARY)

    monkeypatch.setattr(onboarding, "commit_native_signed_pack", failed_commit)
    monkeypatch.setattr(native_http.logger, "warning", broken_sink)
    native_http.handle_native_pack_onboarding(handler, commit=True)
    assert handler.status == 409
    assert handler.response.error == (
        "Pack admission outcome requires reconciliation"
        if publication_unknown
        else "Signed Pack verification was denied"
    )


@pytest.mark.skipif(native_http.os.name == "nt", reason="Native POSIX port")
def test_context_failure_stage_is_closed_over_io_thread(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The callback records its failure without a global or borrowed proof."""

    class Ceremony:
        def prepare(self, result: Any, **arguments: Any) -> Any:
            with ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(arguments["context_loader"]).result()

    def failed_context() -> dict[str, Any]:
        raise PrivateFailure(CANARY)

    handler = Handler(Ceremony(), commit=False)
    monkeypatch.setattr(
        onboarding,
        "preview_native_signed_pack",
        lambda *_a, **_k: {"preview_digest": PREVIEW_DIGEST},
    )
    monkeypatch.setattr(
        onboarding, "native_key_version_review_parameters", lambda *_a, **_k: {}
    )
    monkeypatch.setattr(native_http, "_current_review_context", failed_context)
    with caplog.at_level(logging.WARNING, logger=native_http.logger.name):
        native_http.handle_native_pack_onboarding(handler, commit=False)
    assert handler.status == 409
    assert caplog.records[-1].args == ("review_context",)
    assert CANARY not in caplog.text
