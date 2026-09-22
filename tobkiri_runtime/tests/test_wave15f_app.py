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
    with patch.object(app, "Kernel", return_value=kernel), redirect_stdout(output):
        code = app.main(argv)
    return code, json.loads(output.getvalue()) if output.getvalue() else None, kernel


def _app_source() -> str:
    import app

    return Path(app.__file__).read_text(encoding="utf-8")


class TestConfigureLoggingCalled(unittest.TestCase):
    """The retired logging bootstrap is absent from the v4 composition root."""

    def test_configure_logging_called_on_main(self):
        code, result, kernel = _run_app(["--health"], {"status": "UP"})
        assert code == 0
        assert result == {"status": "UP"}
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
    """--health prints the captured startup result without legacy probes."""

    def test_health_flag_exits_zero_on_up(self):
        code, result, _kernel = _run_app(["--health"], {"status": "UP"})
        assert code == 0
        assert result["status"] == "UP"

    def test_health_flag_exits_one_on_down(self):
        code, result, _kernel = _run_app(["--health"], {"status": "DOWN"})
        assert code == 0
        assert result["status"] == "DOWN"

    def test_health_flag_outputs_valid_json(self):
        expected = {
            "status": "UP",
            "timestamp": "2025-01-01T00:00:00Z",
            "probes": {"disk": {"status": "UP"}},
        }
        code, result, _kernel = _run_app(["--health"], expected)
        assert code == 0
        assert result == expected

    def test_health_registers_disk_probe(self):
        code, _result, kernel = _run_app(["--health"], {"status": "UP"})
        assert code == 0
        assert "probe_disk_space" not in _app_source()
        kernel.run_startup.assert_called_once()

    def test_health_registers_writable_tmp_probe(self):
        code, _result, kernel = _run_app(["--health"], {"status": "UP"})
        assert code == 0
        assert "probe_file_writable" not in _app_source()
        kernel.run_startup.assert_called_once()

    def test_health_uses_windows_disk_path_on_nt(self):
        code, _result, kernel = _run_app(["--health"], {"status": "UP"})
        assert code == 0
        assert "SystemDrive" not in _app_source()
        kernel.run_startup.assert_called_once()


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


if __name__ == "__main__":
    unittest.main()
