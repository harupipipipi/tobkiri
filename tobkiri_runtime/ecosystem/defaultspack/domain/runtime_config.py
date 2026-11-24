from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any


def _pack_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_runtime_config_path() -> Path:
    return _pack_root() / "config" / "default_runtime_config.json"


def user_runtime_config_path() -> Path:
    override = os.environ.get("RUMI_DEFAULTSPACK_RUNTIME_CONFIG_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    user_data = os.environ.get("RUMI_USER_DATA", "").strip()
    if user_data:
        return (
            Path(user_data).expanduser()
            / "defaultspack"
            / "shared"
            / "runtime_config.json"
        )
    return _pack_root() / "user_data" / "shared" / "runtime_config.json"


def _deep_merge(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def get_runtime_config() -> dict[str, Any]:
    config = _load_json(default_runtime_config_path())
    user_config = _load_json(user_runtime_config_path())
    if user_config:
        config = _deep_merge(config, user_config)
    return config


def section(name: str) -> dict[str, Any]:
    value = get_runtime_config().get(name)
    return value if isinstance(value, dict) else {}


def tool_policy_config() -> dict[str, Any]:
    return section("tool_policy")


def scheduler_config() -> dict[str, Any]:
    return section("scheduler")


def scheduler_jobs_path_override() -> str:
    """Return a user-configured scheduler jobs path, if one was supplied."""
    scheduler = _load_json(user_runtime_config_path()).get("scheduler")
    if not isinstance(scheduler, dict):
        return ""
    return str(scheduler.get("jobs_path") or "").strip()


def gateway_config() -> dict[str, Any]:
    return section("gateway")


def merged_tool_policy(context: dict[str, Any] | None = None) -> dict[str, Any]:
    policy = dict(tool_policy_config())
    if isinstance(context, dict):
        runtime_profile = context.get("runtime_profile")
        if isinstance(runtime_profile, dict) and isinstance(runtime_profile.get("policy"), dict):
            policy = _deep_merge(policy, runtime_profile["policy"])
        profile_policy = context.get("profile_policy")
        if isinstance(profile_policy, dict):
            policy = _deep_merge(policy, profile_policy)
    if _authority_mode_is_off():
        policy = _deep_merge(
            policy,
            {
                "allow_shell": True,
                "allow_network": True,
                "allow_file_write": True,
                "write_actions_require_approval": False,
                "destructive_actions_require_approval": False,
                "full_access": True,
                "max_tool_calls": None,
                "yolo_mode": True,
            },
        )
    return policy


def _authority_mode_is_off() -> bool:
    from core_runtime.host_contract import host_contract_value

    return host_contract_value("authority_mode").strip().lower() == "off"
