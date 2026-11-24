from __future__ import annotations

from core_runtime.runtime_audit_helpers import audit_event


class _AuditLogger:
    def __init__(self) -> None:
        self.entries: list[object] = []
        self.flushed = False

    def log(self, entry: object) -> None:
        self.entries.append(entry)

    def flush(self) -> None:
        self.flushed = True


def test_audit_event_leaves_owner_empty_when_no_verified_caller_exists() -> None:
    logger = _AuditLogger()

    audit_event({"audit_logger": logger}, "runtime.event")

    assert len(logger.entries) == 1
    assert logger.entries[0].owner_pack is None
    assert logger.flushed is True


def test_audit_event_uses_the_captured_pack_owner() -> None:
    logger = _AuditLogger()

    audit_event({"audit_logger": logger, "pack_id": "contribution_pack"}, "runtime.event")

    assert logger.entries[0].owner_pack == "contribution_pack"
