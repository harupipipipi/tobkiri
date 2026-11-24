from __future__ import annotations

from ecosystem.defaultspack.transport.http import DefaultsHttpServer


class _FakeTraceStore:
    events: list[tuple[str, dict]] = []

    def append_blocked_event(self, profile_id, event):
        self.events.append((profile_id, dict(event)))
        return {"profile_id": profile_id, "blocked": [dict(event)]}


def test_api_route_allowlist_blocks_and_records_event(monkeypatch) -> None:
    server = DefaultsHttpServer(None)
    monkeypatch.setattr(
        server,
        "_active_profile_policy",
        lambda: (
            "research-profile",
            {
                "enforce_api_route_allowlist": True,
                "api_route_allowlist": ["GET /api/tools"],
            },
        ),
    )
    monkeypatch.setattr("ecosystem.defaultspack.domain.ai_input.ai_input_trace_store.AiInputTraceStore", _FakeTraceStore)
    _FakeTraceStore.events.clear()

    assert server._route_allowed_by_active_profile("GET", "/api/tools") is True
    assert server._route_allowed_by_active_profile("POST", "/api/tools/invoke") is False

    server._record_profile_blocked_route("POST", "/api/tools/invoke")

    assert _FakeTraceStore.events == [
        (
            "research-profile",
            {
                "event": "api_route_blocked",
                "method": "POST",
                "route": "/api/tools/invoke",
                "reason": "not_in_api_route_allowlist",
                "source": "defaultspack.transport.http",
            },
        )
    ]
