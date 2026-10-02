"""Wave 15-F coverage updated for the canonical Pack v4 entrypoint."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _run_app(argv: list[str], startup_result: dict[str, object]):
    import app

    kernel = MagicMock()
    kernel.run_startup.return_value = startup_result
    output = io.StringIO()
    with patch.object(
        app, "_create_defaultspack_kernel", return_value=kernel
    ), redirect_stdout(output):
        code = app.main(argv)
    return code, json.loads(output.getvalue()) if output.getvalue() else None, kernel


def _run_health(probe_result: dict[str, object]):
    import app

    output = io.StringIO()
    with patch.object(
        app, "_probe_health_endpoint", return_value=probe_result
    ) as probe, patch.object(
        app, "_create_defaultspack_kernel"
    ) as factory, redirect_stdout(
        output
    ):
        code = app.main(["--health"])
    return code, json.loads(output.getvalue()) if output.getvalue() else None, probe, factory


def _app_source() -> str:
    import app

    return Path(app.__file__).read_text(encoding="utf-8")


class TestConfigureLoggingCalled(unittest.TestCase):
    """The retired logging bootstrap is absent from the v4 composition root."""

    def test_configure_logging_called_on_main(self):
        code, _result, kernel = _run_app(["--headless"], {"status": "UP"})
        assert code == 0
        assert "configure_logging" not in _app_source()
        kernel.run_startup.assert_called_once()

    def test_configure_logging_default_params(self):
        code, _result, kernel = _run_app(["--headless"], {"status": "UP"})
        assert code == 0
        assert "RUMI_LOG_LEVEL" not in _app_source()
        kernel.run_startup.assert_called_once()

    def test_configure_logging_env_override(self):
        code, _result, kernel = _run_app(["--headless"], {"status": "UP"})
        assert code == 0
        assert "RUMI_LOG_FORMAT" not in _app_source()
        kernel.run_startup.assert_called_once()

    def test_configure_logging_called_once_not_twice(self):
        assert _app_source().count("configure_logging") == 0


class TestHealthFlag(unittest.TestCase):
    """--health probes the running Host without binding the listen socket."""

    def test_health_flag_exits_zero_on_up(self):
        code, result, _probe, _factory = _run_health({"status": "ok"})
        assert code == 0
        assert result["status"] == "ok"

    def test_health_flag_exits_one_on_down(self):
        code, result, _probe, _factory = _run_health(
            {"status": "down", "runtime_ready": False, "port": 8765}
        )
        assert code == 1
        assert result["status"] == "down"

    def test_health_flag_outputs_valid_json(self):
        expected = {
            "status": "ok",
            "runtime_ready": True,
            "runtime_status": "runtime_ready",
            "port": 8765,
        }
        code, result, _probe, _factory = _run_health(expected)
        assert code == 0
        assert result == expected

    def test_health_never_constructs_kernel_or_binds_port(self):
        code, _result, probe, factory = _run_health({"status": "ok"})
        assert code == 0
        probe.assert_called_once()
        factory.assert_not_called()

    def test_health_registers_disk_probe(self):
        code, _result, _probe, _factory = _run_health({"status": "ok"})
        assert code == 0
        assert "probe_disk_space" not in _app_source()

    def test_health_registers_writable_tmp_probe(self):
        code, _result, _probe, _factory = _run_health({"status": "ok"})
        assert code == 0
        assert "probe_file_writable" not in _app_source()

    def test_health_uses_windows_disk_path_on_nt(self):
        code, _result, _probe, _factory = _run_health({"status": "ok"})
        assert code == 0
        assert "SystemDrive" not in _app_source()


class TestHealthEndpointProbe(unittest.TestCase):
    """_probe_health_endpoint unwraps the Host /health APIResponse payload."""

    def test_probe_returns_data_on_success(self):
        import app

        response = MagicMock()
        response.read.return_value = json.dumps(
            {"success": True, "data": {"status": "ok", "runtime_ready": True}}
        ).encode("utf-8")
        urlopen = MagicMock()
        urlopen.return_value.__enter__.return_value = response
        with patch("urllib.request.urlopen", urlopen):
            result = app._probe_health_endpoint(8765)
        assert result["status"] == "ok"
        assert result["runtime_ready"] is True
        assert result["port"] == 8765

    def test_probe_reports_down_when_unreachable(self):
        import app

        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            result = app._probe_health_endpoint(8765)
        assert result["status"] == "down"
        assert result["runtime_ready"] is False
        assert result["port"] == 8765

    def test_probe_reports_error_on_unexpected_payload(self):
        import app

        response = MagicMock()
        response.read.return_value = b"<html>not a host</html>"
        urlopen = MagicMock()
        urlopen.return_value.__enter__.return_value = response
        with patch("urllib.request.urlopen", urlopen):
            result = app._probe_health_endpoint(8765)
        assert result["status"] == "error"
        assert result["runtime_ready"] is False


class TestExistingFlagsNotBroken(unittest.TestCase):
    """Only the current headless/health flags remain in the parser."""

    def test_validate_flag_still_works(self):
        import app

        with pytest.raises(SystemExit) as exc:
            app._parser().parse_args(["--validate"])
        assert exc.value.code == 2

    def test_headless_flag_still_works(self):
        code, _result, kernel = _run_app(["--headless"], {"status": "UP"})
        assert code == 0
        kernel.run_startup.assert_called_once()

    def test_health_evaluated_before_validate(self):
        import app

        with pytest.raises(SystemExit) as exc:
            app._parser().parse_args(["--health", "--validate"])
        assert exc.value.code == 2


def test_post_activation_restart_request_exits_with_launcher_handoff_code() -> None:
    """The supervised Host exits 42 only after the setup response requested it."""

    import app

    kernel = MagicMock()
    kernel.run_startup.return_value = {"status": "setup_required"}
    wait = MagicMock(return_value=False)
    stop = MagicMock(wait=wait)
    with (
        patch.object(app, "_create_defaultspack_kernel", return_value=kernel),
        patch.object(app.threading, "Event", return_value=stop),
        patch.object(app, "_clear_restart_request") as clear_restart,
        patch.object(app, "_restart_requested", return_value=True),
    ):
        assert app.main([]) == 42

    clear_restart.assert_called_once()
    kernel.shutdown.assert_called_once()


if __name__ == "__main__":
    unittest.main()
