"""A diagnostic improvement must retain the original strict debt comparison."""
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'quality_gate_nonregression.py'
SPEC = importlib.util.spec_from_file_location('quality_gate_nonregression', SCRIPT)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


class NonRegressionDiagnostics(unittest.TestCase):
    def compare(self, head_count):
        out, err = io.StringIO(), io.StringIO()
        with (
            patch('sys.argv', ['quality-gate', '--base-ref', 'pinned-base']),
            patch.object(gate, '_run', return_value=subprocess.CompletedProcess(
                ['git'], 0, stdout='actual-base-sha\n', stderr='')),
            patch.object(gate, '_add_base_worktree', return_value=Path('/base')),
            patch.object(gate, '_remove_base_worktree'),
            patch.object(gate, '_resolve_base_package_dir', return_value=Path('/base/pkg')),
            patch.object(gate, '_ruff_count', side_effect=[(0, '[]'), (0, '[]')]),
            patch.object(gate, '_mypy_count', side_effect=[
                (1, 'old.py:1: error: original debt\n'),
                (head_count, 'old.py:1: error: original debt\nnew.py:7: error: actual new cause\n'),
            ]),
            redirect_stdout(out), redirect_stderr(err),
        ):
            code = gate.main()
        return code, out.getvalue(), err.getvalue()

    def test_regression_still_fails_and_shows_both_measured_outputs(self):
        code, out, err = self.compare(2)
        self.assertEqual(code, 1)
        self.assertIn('pinned-base (sha: actual-base-sha)', out)
        self.assertIn('mypy: base=1 head=2', out)
        self.assertIn('mypy base diagnostics:\nold.py:1: error: original debt', err)
        self.assertIn('mypy head diagnostics:', err)
        self.assertIn('new.py:7: error: actual new cause', err)

    def test_equal_debt_keeps_the_original_pass_without_extra_diagnostics(self):
        code, _, err = self.compare(1)
        self.assertEqual(code, 0)
        self.assertEqual(err, '')

    def test_diagnostic_output_is_bounded(self):
        err = io.StringIO()
        with redirect_stderr(err):
            gate._report_mypy_findings('head', '\n'.join(
                f'a.py:{i}: error: ' + 'x' * 3000 for i in range(40)))
        self.assertEqual(len(err.getvalue().splitlines()), 22)
        self.assertIn('20 further diagnostics omitted', err.getvalue())
        self.assertLess(len(err.getvalue()), 21000)


if __name__ == '__main__':
    unittest.main()
