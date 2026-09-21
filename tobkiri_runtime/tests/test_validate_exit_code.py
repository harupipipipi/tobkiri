"""Regression tests for the `--validate` exit-code contract (issue #1467)."""

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def _report(errors=None, warnings=None):
    from core_runtime.pack_validator import ValidationReport

    return ValidationReport(
        warnings=list(warnings or []),
        errors=list(errors or []),
        pack_count=2,
        valid_count=1,
    )


class TestValidateExitCode(unittest.TestCase):
    """`--validate` must exit non-zero when pack validation errors exist."""

    def _invoke(self, report):
        import app

        buf = io.StringIO()
        with patch(
            "core_runtime.pack_validator.validate_packs", return_value=report
        ), redirect_stdout(buf):
            try:
                app._run_validation()
            except SystemExit as exc:
                return exc.code, buf.getvalue()
        return None, buf.getvalue()

    def test_errors_exit_nonzero(self):
        code, out = self._invoke(_report(errors=["broken pack"]))
        self.assertEqual(code, 1)
        self.assertIn("1 errors", out)

    def test_warnings_only_still_succeed(self):
        code, out = self._invoke(_report(warnings=["advisory"]))
        self.assertIsNone(code)
        self.assertIn("1 warnings", out)

    def test_clean_report_returns_normally(self):
        code, out = self._invoke(_report())
        self.assertIsNone(code)
        self.assertIn("0 errors", out)


if __name__ == "__main__":
    unittest.main()
