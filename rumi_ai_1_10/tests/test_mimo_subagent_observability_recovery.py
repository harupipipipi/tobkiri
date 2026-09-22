from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ecosystem" / "defaultspack"))
sys.path.insert(0, str(ROOT))


def test_existing_unanswered_signal_beyond_first_page_is_not_posted_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from domain.company import runtime_store
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import (
        MimoCodingCompanyRuntime,
    )

    messages = [{"metadata": {}} for _ in range(500)]
    messages.append({"metadata": {"sync_key": "subagent_gap:child-1"}})
    posted = []

    class RuntimeStore:
        def list_messages(
            self, company_id: str, *, channel_id: str, limit: int, offset: int
        ) -> tuple[list[dict], int]:
            return messages[offset : offset + limit], len(messages)

        def add_message(self, company_id: str, **kwargs: object) -> None:
            posted.append(kwargs)

    monkeypatch.setattr(runtime_store, "CompanyRuntimeStore", RuntimeStore)
    runtime = MimoCodingCompanyRuntime(pack_root=tmp_path)
    monkeypatch.setattr(
        runtime,
        "_subagent_reply_gaps",
        lambda state: {
            "checked_ids": ["child-1"],
            "unanswered": [{"child_conversation_id": "child-1"}],
        },
    )
    observed = runtime._sync_company_observability({"conversation_id": "parent"})
    assert observed["status"] == "ok"
    assert observed["subagents"]["unanswered_count"] == 1
    assert observed["team_workspace"]["synced_messages"] == 0
    assert posted == []


def test_failed_child_read_is_reported_as_observation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from domain.chat import store
    from ecosystem.rumi_operations_company_pack.domain.agent.mimo_coding_company import (
        MimoCodingCompanyRuntime,
    )

    class ChatStore:
        def get_conversation(self, conversation_id: str) -> dict:
            raise OSError("history temporarily unavailable")

    monkeypatch.setattr(store, "ChatStore", ChatStore)
    runtime = MimoCodingCompanyRuntime(pack_root=tmp_path)
    observed = runtime._sync_company_observability({"conversation_id": "parent"})
    assert observed["status"] == "error"
    assert observed["team_workspace"]["synced_messages"] == 0
