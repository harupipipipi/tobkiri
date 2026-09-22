"""
core_runtime package

外部公開 API は小さく保ち、旧来の top-level re-export は
互換 shim と deprecation warning で段階的に縮小する。
"""

from __future__ import annotations

import importlib
import warnings
from types import ModuleType
from typing import Dict, Tuple, Union


_ExportTarget = Tuple[str, str]
_ModuleExportTarget = str
_LegacyExportTarget = Union[_ExportTarget, _ModuleExportTarget]


_PUBLIC_EXPORTS: Dict[str, _ExportTarget] = {
    "Kernel": (".bootstrap.runtime", "Kernel"),
    "Diagnostics": (".diagnostics", "Diagnostics"),
    "DIContainer": (".di_container", "DIContainer"),
    "get_container": (".di_container", "get_container"),
    "reset_container": (".di_container", "reset_container"),
    "ApprovalManager": (".approval_manager", "ApprovalManager"),
    "get_approval_manager": (".approval_manager", "get_approval_manager"),
    "AuditLogger": (".audit_logger", "AuditLogger"),
    "get_audit_logger": (".audit_logger", "get_audit_logger"),
    "WorktreeContractError": (".worktree_team_contract", "WorktreeContractError"),
    "WorktreeTeamLedger": (".worktree_team_contract", "WorktreeTeamLedger"),
    "normalize_task_request": (".worktree_team_contract", "normalize_task_request"),
    "worktree_task_preset": (".worktree_team_contract", "worktree_task_preset"),
    "L": (".lang", "L"),
}


_LEGACY_EXPORTS: Dict[str, _LegacyExportTarget] = {
    "pip_installer": ".pip_installer",
    "ds_container": ".ds_container",
    "InstallJournal": (".install_journal", "InstallJournal"),
    "InstallJournalConfig": (".install_journal", "InstallJournalConfig"),
    "EventBus": (".event_bus", "EventBus"),
    "FunctionAliasRegistry": (".function_alias", "FunctionAliasRegistry"),
    "get_function_alias_registry": (".function_alias", "get_function_alias_registry"),
    "PackStatus": (".approval_manager", "PackStatus"),
    "PackApproval": (".approval_manager", "PackApproval"),
    "ApprovalResult": (".approval_manager", "ApprovalResult"),
    "initialize_approval_manager": (".approval_manager", "initialize_approval_manager"),
    "VocabRegistry": (".vocab_registry", "VocabRegistry"),
    "VocabGroup": (".vocab_registry", "VocabGroup"),
    "ConverterInfo": (".vocab_registry", "ConverterInfo"),
    "get_vocab_registry": (".vocab_registry", "get_vocab_registry"),
    "reset_vocab_registry": (".vocab_registry", "reset_vocab_registry"),
    "VOCAB_FILENAME": (".vocab_registry", "VOCAB_FILENAME"),
    "CONVERTERS_DIRNAME": (".vocab_registry", "CONVERTERS_DIRNAME"),
    "LangRegistry": (".lang", "LangRegistry"),
    "LangManager": (".lang", "LangManager"),
    "get_lang_registry": (".lang", "get_lang_registry"),
    "get_lang_manager": (".lang", "get_lang_manager"),
    "Lp": (".lang", "Lp"),
    "set_locale": (".lang", "set_locale"),
    "get_locale": (".lang", "get_locale"),
    "reload_lang": (".lang", "reload_lang"),
    "AuditEntry": (".audit_logger", "AuditEntry"),
    "AuditCategory": (".audit_logger", "AuditCategory"),
    "AuditSeverity": (".audit_logger", "AuditSeverity"),
    "reset_audit_logger": (".audit_logger", "reset_audit_logger"),
    "NetworkGrantManager": (".network_grant_manager", "NetworkGrantManager"),
    "NetworkGrant": (".network_grant_manager", "NetworkGrant"),
    "NetworkCheckResult": (".network_grant_manager", "NetworkCheckResult"),
    "get_network_grant_manager": (".network_grant_manager", "get_network_grant_manager"),
    "reset_network_grant_manager": (".network_grant_manager", "reset_network_grant_manager"),
}


__all__ = [*_PUBLIC_EXPORTS.keys(), "rumi_syscall", "syscall"]


def _load_export(name: str, target: _ExportTarget):
    module_name, attr_name = target
    module = importlib.import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def _load_module_export(name: str, module_name: str) -> ModuleType:
    module = importlib.import_module(module_name, __name__)
    globals()[name] = module
    return module


def _deprecation_message(name: str, target: _LegacyExportTarget) -> str:
    if isinstance(target, str):
        clean_module = target.removeprefix(".")
        return (
            f"core_runtime.{name} is deprecated as a top-level module alias. "
            f"Use import core_runtime.{clean_module} instead."
        )
    module_name, attr_name = target
    clean_module = module_name.removeprefix(".")
    return (
        f"core_runtime.{name} is deprecated as a top-level import. "
        f"Use from core_runtime.{clean_module} import {attr_name} instead."
    )


def __getattr__(name: str):
    if name in _PUBLIC_EXPORTS:
        return _load_export(name, _PUBLIC_EXPORTS[name])
    if name in _LEGACY_EXPORTS:
        warnings.warn(
            _deprecation_message(name, _LEGACY_EXPORTS[name]),
            DeprecationWarning,
            stacklevel=2,
        )
        target = _LEGACY_EXPORTS[name]
        if isinstance(target, str):
            return _load_module_export(name, target)
        return _load_export(name, target)
    if name in {"rumi_syscall", "syscall"}:
        module = importlib.import_module(f".{name}", __name__)
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(__all__) | set(_LEGACY_EXPORTS))
