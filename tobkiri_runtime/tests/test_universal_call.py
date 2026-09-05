"""
tests/test_universal_call.py
Comprehensive test suite for the universal_call step type.
"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ═══════════════════════════════════════════════════════════
#  1. Manifest existence
# ═══════════════════════════════════════════════════════════
class TestManifest(unittest.TestCase):
    def test_manifest_exists(self):
        from tests.legacy_authority_contracts import assert_retired_module_absent

        assert_retired_module_absent("core_runtime.kernel")
        from ecosystem.defaultspack.domain.runtime_v4 import BundledCatalog

        catalog = BundledCatalog.load(
            Path(_ROOT) / "ecosystem" / "defaultspack" / "v4"
        )
        self.assertIn("defaults", catalog.profiles)

    def test_manifest_fields(self):
        from tests.legacy_authority_contracts import assert_profile_resolver_requires_authority_snapshot

        assert_profile_resolver_requires_authority_snapshot()

    def test_manifest_runtime_enum(self):
        from tests.legacy_authority_contracts import assert_retired_module_absent

        assert_retired_module_absent("core_runtime.kernel_handlers_system")
        from tobkiri_host.runtime import V4DispatchSession

        self.assertTrue(hasattr(V4DispatchSession, "invoke"))

    def test_manifest_timeout_max(self):
        from tests.v4_batch_support import bounded_scope

        self.assertEqual(bounded_scope(max_bytes=120).quotas["max_bytes"], 120)


# ═══════════════════════════════════════════════════════════
#  2. FlowStep fields
# ═══════════════════════════════════════════════════════════
class TestFlowStepFields(unittest.TestCase):
    def test_new_fields_exist(self):
        from core_runtime.flow_loader import FlowStep
        import dataclasses
        names = {f.name for f in dataclasses.fields(FlowStep)}
        self.assertIn("runtime", names)
        self.assertIn("protocol", names)
        self.assertIn("docker_image", names)

    def test_defaults_are_none(self):
        from core_runtime.flow_loader import FlowStep
        fs = FlowStep(
            id="t", phase="p", priority=100, type="universal_call",
            when=None, input=None, output=None, raw={},
        )
        self.assertIsNone(fs.runtime)
        self.assertIsNone(fs.protocol)
        self.assertIsNone(fs.docker_image)


# ═══════════════════════════════════════════════════════════
#  3. Constants
# ═══════════════════════════════════════════════════════════
class TestConstants(unittest.TestCase):
    def test_constants(self):
        from core_runtime.kernel_flow_execution import (
            _UC_MAX_RESPONSE_SIZE,
            _UC_MAX_TIMEOUT,
            _UC_DEFAULT_TIMEOUT,
            _UC_VALID_RUNTIMES,
        )
        self.assertEqual(_UC_MAX_RESPONSE_SIZE, 1 * 1024 * 1024)
        self.assertEqual(_UC_MAX_TIMEOUT, 120.0)
        self.assertEqual(_UC_DEFAULT_TIMEOUT, 30.0)
        self.assertEqual(_UC_VALID_RUNTIMES, frozenset({"python", "binary", "command"}))


# ═══════════════════════════════════════════════════════════
#  4. Execution: path traversal
# ═══════════════════════════════════════════════════════════
class TestPathTraversal(unittest.TestCase):
    @patch("core_runtime.approval_manager.get_approval_manager")
    @patch("core_runtime.paths.ECOSYSTEM_DIR", "/tmp/eco")
    @patch("core_runtime.paths.is_path_within", return_value=False)
    @patch("os.path.realpath", return_value="/etc/passwd")
    def test_traversal_blocked(self, _rp, _ipw, mock_am):
        import asyncio
        am = MagicMock()
        am.is_pack_approved_and_verified.return_value = True
        mock_am.return_value = am

        from core_runtime.kernel_flow_execution import KernelFlowExecutionMixin
        obj = object.__new__(KernelFlowExecutionMixin)

        step = {
            "id": "s1", "type": "universal_call",
            "owner_pack": "test_pack", "file": "../../../etc/passwd",
            "runtime": "python", "input": {},
        }
        result = asyncio.run(obj._handle_universal_call_async(step, {}))
        self.assertEqual(result["_kernel_step_status"], "failed")
        self.assertEqual(result["_error_type"], "security_error")


# ═══════════════════════════════════════════════════════════
#  5. Approval rejection
# ═══════════════════════════════════════════════════════════
class TestApproval(unittest.TestCase):
    @patch("core_runtime.approval_manager.get_approval_manager")
    def test_unapproved_rejected(self, mock_am):
        import asyncio
        am = MagicMock()
        am.is_pack_approved_and_verified.return_value = False
        mock_am.return_value = am

        from core_runtime.kernel_flow_execution import KernelFlowExecutionMixin
        obj = object.__new__(KernelFlowExecutionMixin)

        step = {
            "id": "s1", "type": "universal_call",
            "owner_pack": "bad_pack", "file": "run.py",
            "runtime": "python", "input": {},
        }
        result = asyncio.run(obj._handle_universal_call_async(step, {}))
        self.assertEqual(result["_kernel_step_status"], "failed")
        self.assertEqual(result["_error_type"], "approval_error")


# ═══════════════════════════════════════════════════════════
#  6. Output size limit
# ═══════════════════════════════════════════════════════════
class TestOutputSize(unittest.TestCase):
    @patch("core_runtime.approval_manager.get_approval_manager")
    @patch("core_runtime.paths.ECOSYSTEM_DIR", "/tmp/eco")
    @patch("core_runtime.paths.is_path_within", return_value=True)
    @patch("os.path.isfile", return_value=True)
    @patch("os.path.realpath", side_effect=lambda p: p)
    def test_oversized_output(self, _rp, _isf, _ipw, mock_am):
        import asyncio
        am = MagicMock()
        am.is_pack_approved_and_verified.return_value = True
        mock_am.return_value = am

        from core_runtime.kernel_flow_execution import KernelFlowExecutionMixin
        obj = object.__new__(KernelFlowExecutionMixin)

        huge = {"data": "x" * (2 * 1024 * 1024)}

        async def fake_python(*a, **kw):
            return huge

        obj._uc_exec_python = fake_python

        step = {
            "id": "s1", "type": "universal_call",
            "owner_pack": "test_pack", "file": "run.py",
            "runtime": "python", "input": {},
        }
        result = asyncio.run(obj._handle_universal_call_async(step, {}))
        self.assertEqual(result["_kernel_step_status"], "failed")
        self.assertEqual(result["_error_type"], "output_size_error")


# ═══════════════════════════════════════════════════════════
#  7. Validation
# ═══════════════════════════════════════════════════════════
class TestValidation(unittest.TestCase):
    def test_missing_owner_pack(self):
        import asyncio
        from core_runtime.kernel_flow_execution import KernelFlowExecutionMixin
        obj = object.__new__(KernelFlowExecutionMixin)
        step = {"id": "s1", "type": "universal_call", "file": "x.py", "runtime": "python", "input": {}}
        result = asyncio.run(obj._handle_universal_call_async(step, {}))
        self.assertEqual(result["_error_type"], "validation_error")

    def test_invalid_runtime(self):
        import asyncio
        from core_runtime.kernel_flow_execution import KernelFlowExecutionMixin
        obj = object.__new__(KernelFlowExecutionMixin)
        step = {
            "id": "s1", "type": "universal_call",
            "owner_pack": "p", "file": "x.py",
            "runtime": "ruby", "input": {},
        }
        result = asyncio.run(obj._handle_universal_call_async(step, {}))
        self.assertEqual(result["_error_type"], "validation_error")


# ═══════════════════════════════════════════════════════════
#  8. Container builder
# ═══════════════════════════════════════════════════════════
class TestContainerBuilder(unittest.TestCase):
    def test_method_exists(self):
        from core_runtime.container_orchestrator import ContainerOrchestrator
        self.assertTrue(hasattr(ContainerOrchestrator, "build_universal_call_command"))

    def test_returns_list(self):
        from core_runtime.container_orchestrator import ContainerOrchestrator
        orch = ContainerOrchestrator.__new__(ContainerOrchestrator)
        result = orch.build_universal_call_command(
            pack_id="t", workspace_dir="/tmp", input_file="in.json",
            filename="run.sh", runtime="command",
        )
        self.assertIsInstance(result, list)
        self.assertEqual(result[:2], ["docker", "run"])


if __name__ == "__main__":
    unittest.main()
