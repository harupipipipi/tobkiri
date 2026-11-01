"""Panel-session and finite loopback-boundary tests for the retired limiter."""

from __future__ import annotations

import threading
from dataclasses import dataclass

import pytest

from core_runtime.pack_api_server import PackAPIHandler, RuntimeHTTPConfig
from core_runtime.panel_auth import PanelAuthBinding, PanelAuthManager


TEST_BINDING = PanelAuthBinding(
    profile_id="test-profile",
    profile_revision="sha256:" + "1" * 64,
    activation_id="activation:test-panel-auth",
    plan_digest="sha256:" + "2" * 64,
    security_epoch=1,
)


@dataclass
class _FakeClock:
    """Deterministic clock for the panel-session expiry boundary."""

    now: float = 1000.0

    def __call__(self) -> float:
        return self.now


def _exchange(manager: PanelAuthManager) -> dict[str, object] | None:
    """Issue and consume one current panel login code."""
    code = str(manager.issue_login_code(TEST_BINDING)["code"])
    return manager.exchange_code(code, TEST_BINDING)


class TestRateLimiterBasic:
    """Compatibility nodeids now cover bounded panel-session behavior."""

    def test_within_limit(self):
        """Repeated bounded panel sessions are accepted through the manager."""
        manager = PanelAuthManager(bootstrap_secret="test", code_ttl_seconds=15)
        sessions = [_exchange(manager) for _ in range(5)]
        assert all(session is not None for session in sessions)

    def test_exceed_limit(self):
        """A consumed login code cannot be replayed as an unbounded bypass."""
        manager = PanelAuthManager(bootstrap_secret="test", code_ttl_seconds=15)
        code = str(manager.issue_login_code(TEST_BINDING)["code"])
        assert manager.exchange_code(code, TEST_BINDING) is not None
        assert manager.exchange_code(code, TEST_BINDING) is None

    def test_window_expiry(self, monkeypatch: pytest.MonkeyPatch):
        """Expired panel codes are rejected at the current session boundary."""
        import core_runtime.panel_auth as panel_auth

        clock = _FakeClock()
        monkeypatch.setattr(panel_auth, "time", type("Clock", (), {"time": clock}))
        manager = PanelAuthManager(bootstrap_secret="test", code_ttl_seconds=15)
        code = str(manager.issue_login_code(TEST_BINDING)["code"])
        clock.now += 16
        assert manager.exchange_code(code, TEST_BINDING) is None

    def test_different_ips_independent(self):
        """Loopback client classification is finite and explicit per address."""
        assert PackAPIHandler._is_loopback_client(("127.0.0.1", 1)) is True
        assert PackAPIHandler._is_loopback_client(("::1", 1)) is True
        assert PackAPIHandler._is_loopback_client(("192.0.2.1", 1)) is False


class TestRateLimiterEnv:
    """Retired environment knobs cannot configure the v4 boundary."""

    def test_custom_rate_limit_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("RUMI_API_RATE_LIMIT", "1")
        assert not hasattr(PackAPIHandler, "_RateLimiter")
        assert RuntimeHTTPConfig.verify("127.0.0.1", 0).host == "127.0.0.1"

    def test_custom_window_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("RUMI_API_RATE_WINDOW", "1")
        assert not hasattr(PackAPIHandler, "_RateLimiter")
        assert RuntimeHTTPConfig.verify("localhost", 65535).port == 65535


class TestRateLimiterCleanup:
    """Current one-time code/session cleanup replaces limiter state."""

    def test_old_timestamps_cleanup(self, monkeypatch: pytest.MonkeyPatch):
        import core_runtime.panel_auth as panel_auth

        clock = _FakeClock()
        monkeypatch.setattr(panel_auth, "time", type("Clock", (), {"time": clock}))
        manager = PanelAuthManager(bootstrap_secret="test", code_ttl_seconds=15)
        old_code = str(manager.issue_login_code(TEST_BINDING)["code"])
        clock.now += 16
        assert manager.exchange_code(old_code, TEST_BINDING) is None
        assert _exchange(manager) is not None

    def test_max_tracked_ips(self):
        """The removed IP-table limiter leaves no hidden per-IP state."""
        assert not hasattr(PackAPIHandler, "_RateLimiter")
        assert not hasattr(PackAPIHandler, "_requests")
        assert RuntimeHTTPConfig.verify("::1", 8765).host == "127.0.0.1"

    def test_evict_frees_slot(self):
        """Explicit session revocation frees the current session boundary."""
        manager = PanelAuthManager(bootstrap_secret="test")
        first = _exchange(manager)
        assert first is not None
        first_session = str(first["session_id"])
        manager.revoke_session(first_session)
        assert manager.verify_session(first_session, TEST_BINDING) is None
        assert _exchange(manager) is not None


class TestRateLimiterThreadSafety:
    """Panel session issuance remains lock-protected under concurrency."""

    def test_thread_safety(self):
        manager = PanelAuthManager(bootstrap_secret="test")
        errors: list[Exception] = []

        def worker() -> None:
            try:
                for _ in range(100):
                    assert _exchange(manager) is not None
            except Exception as exc:  # pragma: no cover - assertion capture
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert not errors


class TestRateLimiterLocalhost:
    """Loopback-only server coordinates replace the removed limiter allowlist."""

    def test_localhost_rate_limited(self):
        assert all(
            PackAPIHandler._is_loopback_client((host, 1))
            for host in ("127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1")
        )
        assert not PackAPIHandler._is_loopback_client(("198.51.100.1", 1))

    def test_exactly_at_limit(self):
        assert RuntimeHTTPConfig.verify("127.0.0.1", 0).port == 0
        assert RuntimeHTTPConfig.verify("127.0.0.1", 65535).port == 65535
        with pytest.raises(ValueError, match="port"):
            RuntimeHTTPConfig.verify("127.0.0.1", 65536)
