from __future__ import annotations

import logging
import sys
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

_this_module = sys.modules.get(__name__)
if _this_module is not None:
    if __name__.startswith("ecosystem.defaultspack."):
        sys.modules.setdefault(__name__.removeprefix("ecosystem.defaultspack."), _this_module)
    else:
        sys.modules.setdefault(f"ecosystem.defaultspack.{__name__}", _this_module)

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "compat_aliases.yaml"
_VALID_STAGES = frozenset({"inventory", "warning", "enforcement", "removal"})
_warned_aliases: set[str] = set()
_warning_lock = threading.Lock()


def _read_without_pyyaml(text: str) -> dict[str, Any]:
    aliases: dict[str, dict[str, str]] = {}
    current_alias = ""
    in_aliases = False
    canonical_prefix = "defaultspack."
    compat_prefixes: list[str] = []
    current_stage = "warning"
    in_migration = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if not line.startswith(" "):
            in_aliases = stripped == "aliases:"
            in_migration = stripped == "migration:"
            current_alias = ""
            if stripped.startswith("canonical_prefix:"):
                canonical_prefix = stripped.split(":", 1)[1].strip().strip("\"'")
            continue
        if in_aliases and line.startswith("  ") and not line.startswith("    "):
            current_alias = stripped[:-1] if stripped.endswith(":") else ""
            if current_alias:
                aliases[current_alias] = {}
            continue
        if in_aliases and current_alias and line.startswith("    ") and ":" in stripped:
            key, value = stripped.split(":", 1)
            aliases[current_alias][key.strip()] = value.strip().strip("\"'")
            continue
        if in_migration and stripped.startswith("current_stage:"):
            current_stage = stripped.split(":", 1)[1].strip().strip("\"'")
        if stripped.startswith("- defaults."):
            compat_prefixes.append(stripped[2:].strip())
    return {
        "aliases": aliases,
        "canonical_prefix": canonical_prefix,
        "compat_prefixes": compat_prefixes or ["defaults."],
        "migration": {"current_stage": current_stage},
    }


@lru_cache(maxsize=1)
def load_compat_alias_config() -> dict[str, Any]:
    try:
        text = _CONFIG_PATH.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        import yaml

        data = yaml.safe_load(text)
    except Exception:
        data = _read_without_pyyaml(text)
    return data if isinstance(data, dict) else {}


def compat_alias_metadata(alias: str) -> dict[str, Any] | None:
    config = load_compat_alias_config()
    aliases = config.get("aliases")
    if not isinstance(aliases, dict):
        return None
    raw = aliases.get(str(alias or ""))
    if not isinstance(raw, dict):
        return None
    migration = config.get("migration")
    if not isinstance(migration, dict):
        migration = {}
    metadata = dict(raw)
    metadata["stage"] = str(raw.get("stage") or migration.get("current_stage") or "warning")
    metadata["migration_note"] = str(raw.get("migration_note") or raw.get("reason") or "")
    return metadata


def compatibility_alias_allowed(alias: str) -> bool:
    return compat_alias_metadata(alias) is not None


def compatibility_aliases_for_replacements(replacements: set[str]) -> tuple[str, ...]:
    aliases = load_compat_alias_config().get("aliases")
    if not isinstance(aliases, dict):
        return ()
    return tuple(
        str(alias)
        for alias, metadata in aliases.items()
        if isinstance(metadata, dict)
        and str(metadata.get("replacement") or "") in replacements
    )


def compat_alias_metadata_errors(alias: str, metadata: Any) -> list[str]:
    if not isinstance(metadata, dict):
        return [f"compat alias missing allowlist entry: {alias}"]
    errors: list[str] = []
    for field in ("owner", "replacement", "remove_after"):
        if not str(metadata.get(field) or "").strip():
            errors.append(f"compat alias missing {field}: {alias}")
    if not str(metadata.get("migration_note") or metadata.get("reason") or "").strip():
        errors.append(f"compat alias missing migration note: {alias}")
    stage = str(metadata.get("stage") or "warning").strip()
    if stage not in _VALID_STAGES:
        errors.append(f"compat alias has invalid migration stage: {alias} -> {stage}")
    return errors


def render_compat_alias_reference() -> str:
    config = load_compat_alias_config()
    aliases = config.get("aliases")
    if not isinstance(aliases, dict):
        aliases = {}
    lines = [
        "# Defaultspack Compatibility Alias Reference",
        "",
        "Historical offline projection only; Protocol v4 catalogs are the runtime authority.",
        "Do not use this table to select a Function, artifact, provider, or authority record.",
        "",
        "| Compatibility alias | Canonical replacement | Owner | Stage | Remove after | Migration note |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for alias in sorted(aliases):
        metadata = compat_alias_metadata(alias) or {}
        values = (
            alias,
            str(metadata.get("replacement") or ""),
            str(metadata.get("owner") or ""),
            str(metadata.get("stage") or ""),
            str(metadata.get("remove_after") or ""),
            str(metadata.get("migration_note") or ""),
        )
        escaped = [value.replace("|", "\\|").replace("\n", " ") for value in values]
        lines.append("| " + " | ".join(f"`{value}`" if index < 2 else value for index, value in enumerate(escaped)) + " |")
    return "\n".join(lines) + "\n"


def record_compat_alias_use(alias: str, *, internal_caller: bool) -> dict[str, Any] | None:
    """Audit actual alias resolution without recording arguments or caller identifiers."""
    metadata = compat_alias_metadata(alias)
    if metadata is None:
        return None
    replacement = str(metadata.get("replacement") or "")
    stage = str(metadata.get("stage") or "warning")
    warning_emitted = False
    if not internal_caller and stage in {"warning", "enforcement"}:
        with _warning_lock:
            if alias not in _warned_aliases:
                _warned_aliases.add(alias)
                warning_emitted = True
        if warning_emitted:
            logger.warning(
                "compat_alias_deprecated alias=%s replacement=%s stage=%s remove_after=%s",
                alias,
                replacement,
                stage,
                str(metadata.get("remove_after") or ""),
            )

    details = {
        "schema_version": 1,
        "alias": alias,
        "replacement": replacement,
        "stage": stage,
        "caller_kind": "internal" if internal_caller else "external",
        "warning_emitted": warning_emitted,
    }
    try:
        from core_runtime.audit_logger import get_audit_logger

        get_audit_logger().log_system_event(
            event_type="compat_alias_used",
            success=True,
            details=details,
        )
    except Exception:
        logger.debug("Failed to audit compatibility alias use", exc_info=True)
    return details


def reset_compat_alias_warning_state() -> None:
    with _warning_lock:
        _warned_aliases.clear()
