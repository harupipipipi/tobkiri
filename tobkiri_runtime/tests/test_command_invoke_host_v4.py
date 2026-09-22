from __future__ import annotations

from types import SimpleNamespace

import pytest

from ecosystem.rumi_command_protocol_pack.runtime.invoke import (
    CONTRACT_ID,
    FUNCTION_ID,
    OPERATION_ID,
    CommandInvokeHostFactoryV4,
)


def _capture(tmp_path):
    binding = SimpleNamespace(
        function=SimpleNamespace(function_id=FUNCTION_ID, implementation_digest="impl"),
        operation=SimpleNamespace(
            contract_id=CONTRACT_ID,
            operation_id=OPERATION_ID,
            contract_version="1.0.0",
        ),
        principal_ref=SimpleNamespace(value="provider-principal"),
        artifact=SimpleNamespace(digest="artifact"),
    )
    context = SimpleNamespace(
        profile_id="defaults",
        user_data_root=tmp_path,
        provider_bindings=(binding,),
        domain_ids={(CONTRACT_ID, OPERATION_ID, "provider-principal"): "domain"},
    )
    return CommandInvokeHostFactoryV4().capture(context).contributions[0]


class Invocation:
    presentation_owner_principal_id = "application-principal"
    presentation_owner_session_id = "session-1"

    def __init__(self) -> None:
        self.assertions = 0

    def assert_current(self) -> None:
        self.assertions += 1


def _payload(**changes):
    value = {
        "profile_id": "defaults",
        "command_ref": "defaultspack:help",
        "args": {},
        "invocation_id": "help-1",
        "mode": "chat",
    }
    value.update(changes)
    return value


def test_help_is_invoked_and_replayed_from_a_durable_owner_scoped_receipt(tmp_path):
    contribution = _capture(tmp_path)
    invocation = Invocation()
    first = contribution.invoke(OPERATION_ID, _payload(), invocation)
    second = contribution.invoke(OPERATION_ID, _payload(), invocation)

    assert first == second
    assert first["status"] == "succeeded"
    assert first["legacy_result"]["action"] == "open_command_help"
    assert first["legacy_result"]["requires_approval"] is False
    assert invocation.assertions == 2
    database = (
        tmp_path
        / "defaultspack"
        / "shared"
        / "command_protocol"
        / "ordinary_invocations.sqlite3"
    )
    assert database.is_file()


def test_invocation_id_reuse_with_a_different_request_fails_closed(tmp_path):
    contribution = _capture(tmp_path)
    invocation = Invocation()
    contribution.invoke(OPERATION_ID, _payload(), invocation)
    with pytest.raises(PermissionError, match="different request"):
        contribution.invoke(OPERATION_ID, _payload(mode="coding"), invocation)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"command_ref": "defaultspack:terminal"}, "not owned"),
        ({"command_ref": "terminal"}, "not owned"),
        ({"args": {"cmd": "true"}}, "not permitted"),
        ({"idempotency_key": "another-id"}, "must equal"),
        ({"mode": "admin"}, "mode is invalid"),
    ],
)
def test_unowned_commands_and_identity_expansion_are_rejected(tmp_path, changes, message):
    contribution = _capture(tmp_path)
    with pytest.raises((PermissionError, ValueError), match=message):
        contribution.invoke(OPERATION_ID, _payload(**changes), Invocation())


def test_capture_does_not_create_durable_state(tmp_path):
    _capture(tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("command_ref", "mode", "args", "action"),
    [
        ("defaultspack:help", "chat", {}, "open_command_help"),
        ("defaultspack:new", "agent", {}, "new_conversation"),
        ("defaultspack:status", "coding", {}, "show_status"),
        ("defaultspack:tools", "chat", {"query": "git"}, "open_tool_picker"),
        ("defaultspack:settings", "chat", {"section": "general"}, "open_settings"),
        ("defaultspack:diff", "coding", {}, "open_diff_preview"),
        ("defaultspack:files", "coding", {"query": "py"}, "open_file_search"),
        ("defaultspack:hooks", "agent", {}, "open_hooks"),
    ],
)
def test_allowlisted_ordinary_commands_return_only_fixed_actions(
    tmp_path, command_ref, mode, args, action,
):
    result = _capture(tmp_path).invoke(
        OPERATION_ID,
        _payload(
            command_ref=command_ref,
            mode=mode,
            args=args,
            invocation_id=f"ordinary-{command_ref.rsplit(':', 1)[-1]}",
        ),
        Invocation(),
    )
    assert result["legacy_result"]["action"] == action
    assert result["legacy_result"]["args"] == args


def test_coding_only_command_fails_closed_in_chat_mode(tmp_path):
    with pytest.raises(ValueError, match="mode is invalid"):
        _capture(tmp_path).invoke(
            OPERATION_ID,
            _payload(command_ref="defaultspack:diff", mode="chat"),
            Invocation(),
        )
