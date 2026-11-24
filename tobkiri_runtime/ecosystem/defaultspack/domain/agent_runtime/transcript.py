from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from blocks._common import gen_id
from core_runtime.runtime_audit_helpers import redact_sensitive
from core_runtime.runtime_events import utc_now
from core_runtime.runtime_state import append_jsonl, read_tail_jsonl


def default_transcript_dir() -> Path:
    override = os.environ.get("RUMI_DEFAULTSPACK_AGENT_TRANSCRIPT_DIR")
    if override:
        return Path(override)
    runtime_dir = os.environ.get("RUMI_DEFAULTSPACK_AGENT_RUNTIME_DIR")
    if runtime_dir:
        return Path(runtime_dir) / "transcripts"
    user_data = os.environ.get("RUMI_USER_DATA", "").strip()
    if user_data:
        return (
            Path(user_data)
            / "defaultspack"
            / "shared"
            / "agent_runtime"
            / "transcripts"
        )
    return Path(__file__).resolve().parents[2] / "user_data" / "shared" / "agent_runtime" / "transcripts"


class TranscriptStore:
    """Append-only JSONL transcript storage."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_transcript_dir()
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, transcript_id: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in transcript_id)
        return self.root / f"{safe}.jsonl"

    def create(self, run_id: str, *, parent_id: str | None = None, metadata: dict[str, Any] | None = None) -> str:
        transcript_id = gen_id("tr_")
        self.append(
            transcript_id,
            "session_header",
            {
                "run_id": run_id,
                "parent_id": parent_id,
                "metadata": metadata or {},
            },
        )
        return transcript_id

    def append(self, transcript_id: str, entry_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        entry = {
            "id": gen_id("te_"),
            "type": entry_type,
            "payload": redact_sensitive(payload or {}),
            "created_at": utc_now(),
        }
        append_jsonl(self.path_for(transcript_id), entry)
        return entry

    def append_message(self, transcript_id: str, message: dict[str, Any]) -> dict[str, Any]:
        return self.append(transcript_id, "message", message)

    def append_tool_call(self, transcript_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.append(transcript_id, "tool_call", payload)

    def append_tool_result(self, transcript_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.append(transcript_id, "tool_result", payload)

    def append_approval(self, transcript_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.append(transcript_id, "approval", payload)

    def append_compaction(self, transcript_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.append(transcript_id, "compaction", payload)

    def read_tail(self, transcript_id: str, limit: int = 100) -> list[dict[str, Any]]:
        return read_tail_jsonl(self.path_for(transcript_id), limit)

    def read_all(self, transcript_id: str) -> list[dict[str, Any]]:
        path = self.path_for(transcript_id)
        if not path.is_file():
            return []
        return read_tail_jsonl(path, 10**9)

    def create_successor(
        self,
        source_transcript_id: str,
        *,
        run_id: str,
        compact_id: str,
        packet: dict[str, Any],
    ) -> str:
        successor_id = self.create(
            run_id,
            parent_id=source_transcript_id,
            metadata={"successor_of": source_transcript_id, "compact_id": compact_id},
        )
        self.append_compaction(
            successor_id,
            {
                "compact_id": compact_id,
                "source_transcript_id": source_transcript_id,
                "packet": packet,
            },
        )
        return successor_id
