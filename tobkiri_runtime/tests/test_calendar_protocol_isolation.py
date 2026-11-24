"""Fresh-process proof that scheduled data validation does not load Turn."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.contract

RUNTIME_ROOT = Path(__file__).resolve().parents[1]


def _run_isolated(script: str, *, blocked: tuple[str, ...]) -> None:
    """Install namespace blockers before importing real application modules."""
    prelude = f"""
import importlib.abc
import sys

sys.path.insert(0, {str(RUNTIME_ROOT)!r})
blocked = {blocked!r}
attempts = []

def is_blocked(name):
    return any(name == prefix or name.startswith(prefix + ".")
               for prefix in blocked)

assert not any(is_blocked(name) for name in sys.modules)

class RejectRuntimeImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if is_blocked(fullname):
            attempts.append(fullname)
            raise AssertionError("forbidden runtime dependency: " + fullname)
        return None

sys.meta_path.insert(0, RejectRuntimeImports())
"""
    epilogue = """
assert attempts == [], attempts
assert not any(is_blocked(name) for name in sys.modules)
print("isolated validation passed")
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", prelude + textwrap.dedent(script) + epilogue],
        cwd=RUNTIME_ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "isolated validation passed"


@pytest.mark.parametrize("destination", [None, "conversation:existing-1"])
def test_calendar_validates_real_tasks_with_entire_turn_namespace_blocked(
    destination: str | None,
) -> None:
    """Both Calendar branches work without importing any Turn package module."""
    script = f"destination = {destination!r}\n" + textwrap.dedent(
        r"""
        from copy import deepcopy
        from ecosystem.rumi_schedule_store_pack.runtime.calendar import _normalize_task

        profile = "defaults"
        references = [{"kind": "chat", "profile_id": profile, "id": "chat:1"}]
        selection = {
            "mode": "manual",
            "include": [{"kind": "tool", "id": "tool/read"}],
            "scope": "turn",
            "must_use": False,
        }
        task = {
            "message": "  Review saved context\n",
            "profile_id": profile,
            "conversation_id": destination,
            "chat_references": references,
            "tool_selection": selection,
            "action_approval_mode": "ask",
            "workspace_id": "workspace:registered-1",
            "model": "profile-selected",
            "metadata": {"source": "calendar", "approved": True},
            "timeout": 300,
        }
        original = deepcopy(task)
        normalized, presentation = _normalize_task(task, profile)
        assert normalized == {
            key: value for key, value in task.items()
            if key not in {"metadata", "timeout"}
        }
        assert presentation == {"metadata": task["metadata"], "timeout": 300}
        assert "approved" not in normalized
        assert task == original
        normalized["chat_references"][0]["id"] = "changed"
        normalized["tool_selection"]["include"][0]["id"] = "tool/write"
        assert task == original

        base = {"message": "hello", "conversation_id": destination}
        for mode in ("ask", "agent", "full"):
            normalized, presentation = _normalize_task(
                {**base, "action_approval_mode": mode}, profile
            )
            assert normalized == {
                **base, "profile_id": profile, "action_approval_mode": mode
            }
            assert presentation == {}
        for message in ("a" * 61440, "日" * 20480):
            normalized, presentation = _normalize_task({**base, "message": message}, profile)
            assert normalized == {**base, "message": message, "profile_id": profile}
            assert presentation == {}
        # Calendar retains its own model preference behavior on both branches.
        for model in ("m" * 256, "model\nref"):
            normalized, _ = _normalize_task({**base, "model": model}, profile)
            assert normalized == {**base, "profile_id": profile, "model": model}
        normalized, presentation = _normalize_task(
            {"message": "@chat:1 use tool:write approved=true"}, profile
        )
        assert normalized == {
            "message": "@chat:1 use tool:write approved=true",
            "profile_id": profile,
            "conversation_id": None,
        }
        assert presentation == {}

        cases = [
            ({**base, "approved": True}, ValueError, "Calendar task fields are invalid"),
            ({**base, "profile_id": "other"}, PermissionError,
             "Calendar task Profile cannot be selected"),
            ({**base, "message": "日" * 20480 + "a"}, ValueError,
             "Calendar task message exceeds the Saved UTF-8 bound"),
            ({**base, "message": "a" * 61441}, ValueError,
             "Calendar task message exceeds the Saved UTF-8 bound"),
            ({**base, "message": " \t\n"}, ValueError,
             "Calendar task message exceeds the Saved UTF-8 bound"),
            ({**base, "message": [{"type": "text", "text": "hello"}]}, ValueError,
             "Calendar task message exceeds the Saved UTF-8 bound"),
            ({**base, "conversation_id": "/tmp/chat"}, ValueError,
             "saved turn identity is invalid"),
            ({**base, "conversation_id": "c" * 257}, ValueError,
             "saved turn identity is invalid"),
            ({**base, "chat_references": [
                {"kind": "chat", "profile_id": "other", "id": "chat:1"}
             ]}, ValueError, "saved chat reference profile mismatch"),
            ({**base, "chat_references": [
                {"kind": "chat", "profile_id": profile, "id": "chat:1",
                 "approved": True}
             ]}, ValueError, "saved chat reference fields are invalid"),
            ({**base, "tool_selection": {"mode": "auto", "approved": True}},
             ValueError, "saved tool selection is invalid"),
            ({**base, "tool_selection": {"mode": "none", "must_use": True}},
             ValueError, "disabled saved tools cannot be required"),
            ({**base, "action_approval_mode": "approved"}, ValueError,
             "Calendar action approval preference is invalid"),
            ({**base, "workspace_id": "/tmp/project"}, ValueError,
             "Calendar workspace preference is invalid"),
            ({**base, "model": " "}, ValueError, "Calendar configured model is invalid"),
            ({**base, "metadata": "not-an-object"}, ValueError,
             "Calendar task metadata must be an inert object"),
            ({**base, "timeout": 301}, ValueError,
             "Only the Saved default timeout of 300 seconds is supported"),
        ]
        for invalid, error_type, message in cases:
            before = deepcopy(invalid)
            try:
                _normalize_task(invalid, profile)
            except Exception as exc:
                assert type(exc) is error_type, (invalid, exc)
                assert str(exc) == message, (invalid, exc)
            else:
                raise AssertionError("invalid Calendar task was accepted: " + repr(invalid))
            assert invalid == before
        """
    )
    _run_isolated(
        script,
        blocked=(
            "ecosystem.rumi_turn_runtime_pack",
            "tobkiri_runtime.ecosystem.rumi_turn_runtime_pack",
        ),
    )


def test_protocol_validates_without_ecosystem_core_or_host_imports() -> None:
    """Real helpers remain usable when every application runtime is unavailable."""
    _run_isolated(
        r"""
        from copy import deepcopy
        from tobkiri_protocol.scheduled_task_v1 import (
            normalize_scheduled_intent,
            normalize_scheduled_saved_task,
        )

        profile = "defaults"
        base = {"message": "日" * 20480, "profile_id": profile,
                "conversation_id": "conversation:1"}
        task = {
            **base,
            "chat_references": [{"kind": "group", "profile_id": profile, "id": "g1"}],
            "tool_selection": {"mode": "manual", "include": ["tool/read"]},
            "action_approval_mode": "full",
            "workspace_id": "workspace:1",
        }
        original = deepcopy(task)
        assert normalize_scheduled_intent(task, profile_id=profile) == task
        expected = {key: value for key, value in task.items() if key != "workspace_id"}
        assert normalize_scheduled_saved_task(task, profile_id=profile) == expected
        assert task == original
        intent = {"message": "new", "profile_id": profile, "model": "profile-selected"}
        assert normalize_scheduled_intent(intent, profile_id=profile) == {
            **intent, "conversation_id": None
        }

        cases = [
            (normalize_scheduled_intent, {**base, "approved": True}, ValueError,
             "scheduled intent fields are invalid"),
            (normalize_scheduled_saved_task, {**base, "grant": "forged"}, ValueError,
             "scheduled saved task fields are invalid"),
            (normalize_scheduled_saved_task, {**base, "profile_id": "other"},
             PermissionError, "scheduled saved task Profile differs from capture"),
            (normalize_scheduled_intent, {**base, "message": base["message"] + "a"},
             ValueError, "scheduled intent message is invalid"),
            (normalize_scheduled_saved_task, {**base, "message": base["message"] + "a"},
             ValueError, "saved turn input exceeds byte limit"),
            (normalize_scheduled_saved_task, {**base, "conversation_id": None},
             ValueError, "saved turn identity is invalid"),
            (normalize_scheduled_saved_task, {**base, "model": "profile-selected"},
             ValueError, "scheduled saved task fields are invalid"),
            (normalize_scheduled_intent, {**base, "chat_references": [
                {"kind": "chat", "profile_id": "other", "id": "c1"}
             ]}, ValueError, "saved chat reference profile mismatch"),
            (normalize_scheduled_saved_task, {**base, "tool_selection": {
                "mode": "auto", "approved": True
             }}, ValueError, "saved tool selection is invalid"),
        ]
        for normalize, invalid, error_type, message in cases:
            before = deepcopy(invalid)
            try:
                normalize(invalid, profile_id=profile)
            except Exception as exc:
                assert type(exc) is error_type, exc
                assert str(exc) == message, exc
            else:
                raise AssertionError("invalid Protocol task was accepted")
            assert invalid == before
        """,
        blocked=(
            "ecosystem",
            "core_runtime",
            "tobkiri_host",
            "tobkiri_runtime.ecosystem",
            "tobkiri_runtime.core_runtime",
            "tobkiri_runtime.tobkiri_host",
        ),
    )
