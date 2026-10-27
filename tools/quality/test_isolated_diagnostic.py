"""Small process-isolation tests, with no Wasm/native application execution."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

from run_isolated_diagnostic import run


class IsolationTests(unittest.TestCase):
    def test_completed_command_preserves_real_exit_and_output(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory).resolve() / "completed.log"
            with contextlib.redirect_stdout(io.StringIO()):
                code = run([sys.executable, "-I", "-B", "-c",
                            "print('actual-child-output'); raise SystemExit(3)"], log, 2)
            self.assertEqual(code, 3)
            self.assertEqual(log.read_text().strip(), "actual-child-output")
            record = json.loads(log.with_suffix(".log.isolation.json").read_text())
            self.assertFalse(record["forced_process_group_exit"])
            self.assertTrue(record["process_reaped"])
            self.assertFalse(record["individual_task_cancellation_proven"])

    def test_timeout_is_a_forced_process_exit_and_never_a_successful_task_cancel(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory).resolve() / "timeout.log"
            with contextlib.redirect_stdout(io.StringIO()):
                code = run([sys.executable, "-I", "-B", "-c",
                            "import time; print('entered',flush=True); time.sleep(30)"], log, 0.2)
            self.assertEqual(code, 124)
            record = json.loads(log.with_suffix(".log.isolation.json").read_text())
            self.assertTrue(record["forced_process_group_exit"])
            self.assertTrue(record["process_reaped"])
            self.assertFalse(record["individual_task_cancellation_proven"])

    def test_exited_leader_with_inherited_child_pipe_is_still_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory).resolve() / "descendant.log"
            child_code = (
                "import subprocess,sys; "
                "subprocess.Popen([sys.executable,'-I','-B','-c',"
                "'import time; time.sleep(30)']); print('leader-exited',flush=True)"
            )
            with contextlib.redirect_stdout(io.StringIO()):
                code = run([sys.executable, "-I", "-B", "-c", child_code], log, 0.3)
            self.assertEqual(code, 124)
            record = json.loads(log.with_suffix(".log.isolation.json").read_text())
            self.assertTrue(record["forced_process_group_exit"])
            self.assertTrue(record["process_reaped"])


if __name__ == "__main__":
    unittest.main()
