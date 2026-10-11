"""Native handover diagnostics expose public source locations only."""

from __future__ import annotations

from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

from core_runtime.native_host_handover_diagnostic import handover_support_code
from core_runtime.native_host_handover_http import handle_native_host_handover
from ecosystem.defaultspack.defaultspack import development_host_handover as owner

_MESSAGE = "Host handover changed or requires recovery; current data is retained"
_SENTINEL = "PRIVATE_SENTINEL_history_key_session_user_data"
_PREFIX = "/api/internal/native-host-handover/"


@pytest.mark.parametrize("action", ["prepare", "commit", "recovery-preview", "recover"])
@pytest.mark.parametrize(
    ("error_type", "category"),
    [
        (OSError, "os"),
        (ValueError, "value"),
        (RuntimeError, "runtime"),
        (KeyError, "key"),
        (TypeError, "type"),
    ],
)
def test_each_caught_failure_retains_409_without_private_details(
    action: str,
    error_type: type[Exception],
    category: str,
) -> None:
    responses = []

    def fail(*args: object, **kwargs: object) -> None:
        private_local = _SENTINEL
        if error_type is OSError:
            raise OSError(13, private_local, f"/private/{private_local}")
        raise error_type(private_local, {private_local: private_local})

    port = SimpleNamespace(prepare=fail, commit=fail, prepare_recovery=fail, recover=fail)
    handler = SimpleNamespace(
        headers={"X-Rumi-Desktop-Bootstrap": _SENTINEL},
        _native_pack_launcher_authenticated=lambda: True,
        _packvm_lifecycle=SimpleNamespace(development_host_handover=port),
        _parse_object_body=lambda: {},
        _send_response=lambda response, status: responses.append((response, status)),
    )
    assert handle_native_host_handover(handler, "POST", _PREFIX + action)
    response, status = responses[0]
    assert status == 409
    assert not response.success
    assert response.data is None
    expected = _MESSAGE
    if action in {"prepare", "recovery-preview"}:
        expected += f" (Support code: NH1.{action}.{category}.unknown)"
    assert response.error == expected
    assert _SENTINEL not in response.to_json()
    assert len(response.error) < 300


def test_broken_diagnostic_metadata_retains_generic_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = sys.modules["ecosystem.defaultspack"]
    monkeypatch.setattr(package, "__path__", None)
    assert handover_support_code("prepare", ValueError(_SENTINEL)) == ("NH1.prepare.value.unknown")


def test_exception_string_and_traceback_overrides_are_not_called() -> None:
    class PrivateFailure(ValueError):
        def __str__(self) -> str:
            raise AssertionError(_SENTINEL)

        def __repr__(self) -> str:
            raise AssertionError(_SENTINEL)

        def __getattribute__(self, name: str) -> object:
            if name == "__traceback__":
                raise AssertionError(_SENTINEL)
            return super().__getattribute__(name)

    assert handover_support_code("prepare", PrivateFailure(_SENTINEL)) == (
        "NH1.prepare.value.unknown"
    )


def test_trusted_host_method_produces_stable_public_location() -> None:
    try:
        owner.DevelopmentHostHandover.prepare(None, {_SENTINEL: _SENTINEL})
    except ValueError as error:
        first = handover_support_code("prepare", error)
        assert first == handover_support_code("prepare", error)
        assert first.startswith("NH1.prepare.value.host.prepare.")
        assert first.rsplit(".", 1)[1].isdigit()
        assert _SENTINEL not in first
        assert str(Path(owner.__file__).parent) not in first
    else:
        pytest.fail("Invalid selection must still be rejected")


@pytest.mark.parametrize("filename", [_SENTINEL, owner.__file__])
def test_spoofed_module_name_or_public_filename_is_not_trusted(filename: str) -> None:
    namespace = {"__name__": owner.__name__, "private": _SENTINEL}
    exec(compile("def fail():\n    raise ValueError(private)\n", filename, "exec"), namespace)
    try:
        namespace["fail"]()
    except ValueError as error:
        assert handover_support_code("prepare", error) == "NH1.prepare.value.unknown"
    else:
        pytest.fail("Synthetic failure must raise")


def test_canonical_pack_origin_can_be_a_sealed_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Compile only public Source into a synthetic namespace; no real app state,
    # stage inventory, user files, VM or handover service is accessed.
    root = tmp_path / "local-authority-test" / "ecosystem" / "defaultspack"
    filename = root / "defaultspack" / "development_host_handover.py"
    module = ModuleType(owner.__name__)
    module.__file__ = str(filename)
    exec(compile(Path(owner.__file__).read_text(), str(filename), "exec"), vars(module))
    package = sys.modules["ecosystem.defaultspack"]
    monkeypatch.setattr(package, "__path__", [str(root)])
    monkeypatch.setitem(sys.modules, owner.__name__, module)
    try:
        module.DevelopmentHostHandover.prepare(None, {})
    except ValueError as error:
        code = handover_support_code("prepare", error)
        assert code.startswith("NH1.prepare.value.host.prepare.")
        assert str(root) not in code
    else:
        pytest.fail("Invalid selection must still be rejected")


def test_trusted_helper_reports_public_location() -> None:
    try:
        owner._verify_native_consent(
            {},
            plan_digest="sha256:" + "a" * 64,
            nonce=_SENTINEL,
            native_secret=_SENTINEL,
        )
    except ValueError as error:
        code = handover_support_code("commit", error)
        assert code.startswith("NH1.commit.value.host._verify_native_consent.")
        assert _SENTINEL not in code
    else:
        pytest.fail("Missing native consent must still be rejected")
