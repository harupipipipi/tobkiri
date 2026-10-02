"""macOS Swift host bridge for Computer Use.

The Swift helper owns macOS desktop primitives. Python stays as the policy,
approval, artifact, and fallback orchestration layer.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


_HELPER_BINARY_NAME = "mac_computer_use_host"
_HELPER_TIMEOUT_SECONDS = 30
_COMPILE_TIMEOUT_SECONDS = 30
_INVENTORY_DIAGNOSTIC_CONTRACT = "rumi.mac.window_inventory.v3"
_LAST_HELPER_PATH: str | None = None
_LAST_HELPER_SIGNATURE_CLASS: str | None = None


@dataclass(frozen=True)
class SwiftHelperResolutionFacts:
    available: bool = False
    invoked: bool = False
    response_contract: str = "not_invoked"
    binary_class: str = "unknown"
    contract_version_class: str = "missing"
    compile_attempted: bool = False
    compile_succeeded: bool = False
    persistence_class: str = "unknown"
    path_stability: str = "unknown"
    signature_stability: str = "unknown"

    def payload(self) -> dict[str, Any]:
        return {
            "selection_swift_helper_available": self.available,
            "selection_swift_helper_invoked": self.invoked,
            "selection_swift_helper_response_contract": self.response_contract,
            "selection_swift_helper_binary_class": self.binary_class,
            "selection_swift_helper_contract_version_class": self.contract_version_class,
            "selection_swift_helper_compile_attempted": self.compile_attempted,
            "selection_swift_helper_compile_succeeded": self.compile_succeeded,
            "selection_swift_helper_persistence_class": self.persistence_class,
            "selection_swift_helper_path_stability": self.path_stability,
            "selection_swift_helper_signature_stability": self.signature_stability,
        }


class MacSwiftHostError(RuntimeError):
    """Raised when the Swift host could not complete a request."""


class MacSwiftComputerHost:
    def __init__(
        self,
        *,
        pack_root: Path | None = None,
        source_path: Path | None = None,
        binary_path: Path | None = None,
    ) -> None:
        self._pack_root = pack_root or Path(__file__).resolve().parents[3]
        self._source_path = source_path or Path(__file__).with_name("ComputerUseHost.swift")
        helper_dir = self._default_helper_dir(self._pack_root)
        self._binary_path = binary_path or helper_dir / _HELPER_BINARY_NAME

    def available(self) -> bool:
        if platform.system() != "Darwin":
            return False
        if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get("RUMI_MAC_COMPUTER_USE_HOST"):
            return False
        override = self._env_binary_path()
        if override is not None:
            return True
        if self._usable_binary(self._binary_path):
            return True
        return self._source_path.exists() and shutil.which("swiftc") is not None

    def run(
        self,
        action: str,
        args: dict[str, Any] | None = None,
        *,
        timeout: float = _HELPER_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        if platform.system() != "Darwin":
            raise MacSwiftHostError("macOS Swift host is only available on macOS.")
        binary = self._ensure_binary()
        if binary is None:
            raise MacSwiftHostError("macOS Swift host binary is unavailable.")
        request = {"action": str(action or ""), "args": dict(args or {})}
        completed = subprocess.run(
            [str(binary)],
            input=json.dumps(request, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip()
            raise MacSwiftHostError(f"macOS Swift host exited with status {completed.returncode}: {stderr}")
        try:
            response = json.loads(completed.stdout.strip() or "{}")
        except json.JSONDecodeError as exc:
            raise MacSwiftHostError("macOS Swift host returned invalid JSON.") from exc
        if not isinstance(response, dict):
            raise MacSwiftHostError("macOS Swift host returned a non-object response.")
        if response.get("ok") is True and isinstance(response.get("result"), dict):
            return dict(response["result"])
        message = str(response.get("error") or "macOS Swift host request failed.")
        code = str(response.get("error_code") or "MAC_SWIFT_HOST_FAILED")
        result = response.get("result") if isinstance(response.get("result"), dict) else {}
        payload = dict(result)
        payload.update({"is_error": True, "error_code": code, "reason": message})
        return payload

    def run_with_facts(
        self,
        action: str,
        args: dict[str, Any] | None = None,
        *,
        timeout: float = _HELPER_TIMEOUT_SECONDS,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Run one helper request and return only closed helper diagnostics on failure."""
        if platform.system() != "Darwin":
            facts = SwiftHelperResolutionFacts(
                binary_class="unavailable", persistence_class="unavailable",
                path_stability="unavailable", signature_stability="unavailable",
            )
            return {}, facts.payload()
        binary, facts = self._ensure_binary_with_facts()
        if binary is None:
            return {}, facts.payload()
        global _LAST_HELPER_PATH
        current_path = str(binary.resolve())
        path_stability = (
            "first_observation" if _LAST_HELPER_PATH is None
            else "same" if _LAST_HELPER_PATH == current_path else "changed"
        )
        _LAST_HELPER_PATH = current_path
        facts = replace(facts, path_stability=path_stability)
        request = {"action": str(action or ""), "args": dict(args or {})}
        facts = replace(facts, invoked=True)
        try:
            completed = subprocess.run(
                [str(binary)],
                input=json.dumps(request, ensure_ascii=False),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {}, replace(facts, response_contract="timeout").payload()
        except Exception:
            return {}, replace(facts, response_contract="process_failure").payload()
        if completed.returncode != 0:
            return {}, replace(facts, response_contract="process_failure").payload()
        try:
            response = json.loads(completed.stdout.strip() or "{}")
        except json.JSONDecodeError:
            return {}, replace(facts, response_contract="invalid_json").payload()
        if not isinstance(response, dict):
            return {}, replace(facts, response_contract="non_object").payload()

        result = response.get("result") if isinstance(response.get("result"), dict) else {}
        marker = result.pop("inventory_diagnostic_contract", None)
        if marker == _INVENTORY_DIAGNOSTIC_CONTRACT:
            version_class = "expected"
        elif marker in (None, ""):
            version_class = "missing"
        else:
            version_class = "mismatch"
        binary_class = facts.binary_class
        if binary_class.startswith("override_"):
            binary_class = "override_expected" if version_class == "expected" else "override_mismatch"
        native_diagnostics = result.get("inventory_diagnostics")
        signing_class = (
            str(native_diagnostics.get("selection_swift_helper_signing_class") or "")
            if isinstance(native_diagnostics, dict) else ""
        )
        private = result.get("inventory_private")
        signature_token = ""
        if isinstance(private, dict):
            signature_token = str(private.pop("helper_signature_token", "") or "")
        signature_identity = signature_token or signing_class
        global _LAST_HELPER_SIGNATURE_CLASS
        if signing_class not in {"signed_stable", "ad_hoc", "unsigned", "unavailable", "unknown"}:
            signature_stability = "unknown"
        else:
            signature_stability = (
                "first_observation" if _LAST_HELPER_SIGNATURE_CLASS is None
                else "same" if _LAST_HELPER_SIGNATURE_CLASS == signature_identity else "changed"
            )
            _LAST_HELPER_SIGNATURE_CLASS = signature_identity
        if response.get("ok") is True and isinstance(response.get("result"), dict):
            facts = replace(
                facts, response_contract="valid_success",
                binary_class=binary_class, contract_version_class=version_class,
                signature_stability=signature_stability,
            )
            return dict(result), facts.payload()
        facts = replace(
            facts, response_contract="valid_error",
            binary_class=binary_class, contract_version_class=version_class,
            signature_stability=signature_stability,
        )
        payload = dict(result)
        payload.update({
            "is_error": True,
            "error_code": str(response.get("error_code") or "MAC_SWIFT_HOST_FAILED"),
            "reason": str(response.get("error") or "macOS Swift host request failed."),
        })
        return payload, facts.payload()

    def _ensure_binary(self) -> Path | None:
        override = self._env_binary_path()
        if override is not None:
            return override
        if self._usable_binary(self._binary_path) and self._binary_is_current():
            return self._binary_path
        swiftc = shutil.which("swiftc")
        if swiftc is None or not self._source_path.exists():
            return self._binary_path if self._usable_binary(self._binary_path) else None
        self._binary_path.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [swiftc, str(self._source_path), "-o", str(self._binary_path)],
            capture_output=True,
            text=True,
            timeout=_COMPILE_TIMEOUT_SECONDS,
            check=False,
        )
        if self._usable_binary(self._binary_path):
            self._binary_path.chmod(self._binary_path.stat().st_mode | 0o755)
        if completed.returncode != 0 or not self._usable_binary(self._binary_path):
            return None
        return self._binary_path

    def _ensure_binary_with_facts(self) -> tuple[Path | None, SwiftHelperResolutionFacts]:
        override = self._env_binary_path()
        if override is not None:
            return override, SwiftHelperResolutionFacts(
                available=True, binary_class="override_mismatch", persistence_class="override"
            )
        prefix = "isolated" if os.environ.get("RUMI_USER_DATA", "").strip() else "pack"
        if self._usable_binary(self._binary_path) and self._binary_is_current():
            return self._binary_path, SwiftHelperResolutionFacts(
                available=True, binary_class=f"{prefix}_reused_current",
                persistence_class="reused_current",
            )
        swiftc = shutil.which("swiftc")
        if swiftc is None or not self._source_path.exists():
            if self._usable_binary(self._binary_path):
                return self._binary_path, SwiftHelperResolutionFacts(
                    available=True, binary_class="stale_fallback", persistence_class="stale_fallback"
                )
            return None, SwiftHelperResolutionFacts(
                binary_class="unavailable", persistence_class="unavailable",
                path_stability="unavailable", signature_stability="unavailable",
            )
        facts = SwiftHelperResolutionFacts(
            compile_attempted=True, binary_class="unavailable", persistence_class="unavailable"
        )
        try:
            self._binary_path.parent.mkdir(parents=True, exist_ok=True)
            completed = subprocess.run(
                [swiftc, str(self._source_path), "-o", str(self._binary_path)],
                capture_output=True,
                text=True,
                timeout=_COMPILE_TIMEOUT_SECONDS,
                check=False,
            )
        except Exception:
            return None, facts
        if self._usable_binary(self._binary_path):
            self._binary_path.chmod(self._binary_path.stat().st_mode | 0o755)
        if completed.returncode != 0 or not self._usable_binary(self._binary_path):
            return None, facts
        return self._binary_path, replace(
            facts, available=True, compile_succeeded=True,
            binary_class=f"{prefix}_compiled_current",
            persistence_class="compiled_current",
        )

    def _binary_is_current(self) -> bool:
        try:
            return self._binary_path.stat().st_mtime >= self._source_path.stat().st_mtime
        except OSError:
            return False

    @staticmethod
    def _default_helper_dir(pack_root: Path) -> Path:
        user_data = os.environ.get("RUMI_USER_DATA", "").strip()
        if user_data:
            return Path(user_data) / "shared" / "helpers" / "mac_computer_use"
        return pack_root / "user_data" / "shared" / "helpers" / "mac_computer_use"

    @staticmethod
    def _usable_binary(path: Path | None) -> bool:
        if path is None:
            return False
        try:
            return path.is_file() and os.access(path, os.X_OK)
        except OSError:
            return False

    def _env_binary_path(self) -> Path | None:
        override = os.environ.get("RUMI_MAC_COMPUTER_USE_HOST", "").strip()
        if not override:
            return None
        path = Path(override).expanduser()
        return path if self._usable_binary(path) else None


def run_swift_host(action: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return MacSwiftComputerHost().run(action, args or {})
