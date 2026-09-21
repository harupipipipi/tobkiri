"""The conversation reader cannot widen the captured root or operation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ecosystem.rumi_conversation_store_pack.runtime.host import (
    CONTRACT_ID,
    FUNCTION_ID,
    OPERATION_ID,
    ConversationReadHostFactoryV4,
)
from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore


def _context(root: Path) -> Any:
    binding = SimpleNamespace(
        function=SimpleNamespace(
            function_id=FUNCTION_ID,
            implementation_digest="implementation",
        ),
        operation=SimpleNamespace(
            contract_id=CONTRACT_ID,
            operation_id=OPERATION_ID,
            contract_version="1.0.0",
        ),
        principal_ref=SimpleNamespace(value="reader"),
        artifact=SimpleNamespace(digest="artifact"),
    )
    return SimpleNamespace(
        profile_id="defaults",
        user_data_root=root,
        provider_bindings=(binding,),
        domain_ids={(CONTRACT_ID, OPERATION_ID, "reader"): "domain"},
    )


def test_reader_uses_explicit_root_and_profile(tmp_path: Path) -> None:
    """Read actual owner-store content without ambient data-root selection."""
    store = ConversationStore("defaults", user_data_root=tmp_path)
    store.create({"id": "conversation-1", "title": "Persisted title"}, expected_revision=0)
    reader = ConversationReadHostFactoryV4().capture(_context(tmp_path))
    invoke = reader.contributions[0].invoke
    assert (
        invoke(OPERATION_ID, {"profile_id": "defaults", "operation": "list"}, None)
        == store.snapshot()
    )
    assert invoke(
        OPERATION_ID,
        {
            "profile_id": "defaults",
            "operation": "get",
            "conversation_id": "conversation-1",
        },
        None,
    ) == {"conversation": store.get("conversation-1")}
    reader.close()


@pytest.mark.parametrize(
    "patch",
    [
        {"profile_id": "other"},
        {"operation": "delete"},
        {"operation": "migrate"},
        {"root": "/tmp/other"},
        {"conversation_id": "conversation-1"},
        {"approved": True},
    ],
)
def test_reader_rejects_widening(tmp_path: Path, patch: dict[str, Any]) -> None:
    """No payload flag turns a list request into a cross-profile write."""
    context = _context(tmp_path)
    invoke = ConversationReadHostFactoryV4().capture(context).contributions[0].invoke
    with pytest.raises(PermissionError):
        invoke(
            OPERATION_ID,
            {
                "profile_id": "defaults",
                "operation": "list",
                **patch,
            },
            None,
        )
    assert not (tmp_path / "packs").exists()


def test_reader_rejects_unbound_capture_and_operation(tmp_path: Path) -> None:
    """An absent domain or different operation cannot obtain the reader."""
    context = _context(tmp_path)
    context.domain_ids = {}
    with pytest.raises(PermissionError):
        ConversationReadHostFactoryV4().capture(context)
    context = _context(tmp_path)
    context.provider_bindings[0].operation.operation_id = "other"
    with pytest.raises(PermissionError):
        ConversationReadHostFactoryV4().capture(context)
