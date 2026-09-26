import os
import sys

_PACK_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_DEFAULTSPACK_ROOT = os.path.join(os.path.dirname(_PACK_ROOT), "defaultspack")
for _path in (_PACK_ROOT, _DEFAULTSPACK_ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from blocks._common import error, ok
from ecosystem.rumi_operations_team_pack.domain.agent.mimo_coding_company import (
    DEFAULT_FAST_MODEL,
    DEFAULT_MAIN_MODEL,
    DEFAULT_VISION_MODEL,
    MimoCodingCompanyRuntime,
    current_model_allowlist,
)


def _as_int(value, default):
    if value in (None, ""):
        return default
    return int(value)


def _as_optional_int(value):
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    if text in {"none", "null", "unlimited", "infinite", "infinity"}:
        return None
    return int(value)


def _as_bool(value, default):
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _as_string_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        raw = value.replace(",", "\n").splitlines()
        return [item.strip() for item in raw if item.strip()]
    return []


def _validated_model(value, *, label, default):
    cleaned = str(value or default).strip()
    if cleaned not in current_model_allowlist():
        raise ValueError(label + " is not allowed for MiMo coding company: " + cleaned)
    return cleaned


def run(input_data, context):
    if not isinstance(input_data, dict):
        return error("input_data must be a dict", "INVALID_INPUT")
    try:
        docker_worker_count = max(0, min(_as_int(input_data.get("docker_worker_count"), 3), 16))
        worker_mode = str(input_data.get("worker_mode") or "").strip().lower()
        docker_enabled_default = worker_mode not in {"non_docker", "non-docker", "managed_desktop", "managed-desktop", "desktop"}
        docker_enabled = _as_bool(input_data.get("docker_enabled"), docker_enabled_default)
        if docker_worker_count <= 0:
            docker_enabled = False
        status = MimoCodingCompanyRuntime().bootstrap(
            start_nonstop=bool(input_data.get("start_nonstop", True)),
            heartbeat_minutes=_as_int(input_data.get("heartbeat_minutes"), 30),
            review_interval_minutes=_as_int(input_data.get("review_interval_minutes"), 180),
            qa_interval_minutes=_as_int(input_data.get("qa_interval_minutes"), 240),
            model=_validated_model(input_data.get("model"), label="model", default=DEFAULT_MAIN_MODEL),
            vision_model=_validated_model(input_data.get("vision_model"), label="vision_model", default=DEFAULT_VISION_MODEL),
            fast_model=_validated_model(input_data.get("fast_model"), label="fast_model", default=DEFAULT_FAST_MODEL),
            qa_targets=_as_string_list(input_data.get("qa_targets")),
            docker_worker_count=docker_worker_count,
            docker_personas=_as_string_list(input_data.get("docker_personas")),
            docker_enabled=docker_enabled,
            max_tool_calls=_as_optional_int(input_data.get("max_tool_calls")),
            workspace_id=str(input_data.get("workspace_id") or "").strip() or None,
            workspace_label=str(input_data.get("workspace_label") or "").strip() or None,
            workspace_root=str(input_data.get("workspace_root") or "").strip() or None,
            seed_tasks=bool(input_data.get("seed_tasks", True)),
            seed_knowledge=bool(input_data.get("seed_knowledge", True)),
            run_initial_review_now=bool(input_data.get("run_initial_review_now", False)),
        )
        return ok(status)
    except ValueError as exc:
        code = "MODEL_NOT_ALLOWED" if "not allowed" in str(exc).lower() else "INVALID_INPUT"
        return error(str(exc), code)
    except Exception as exc:
        return error("MiMo coding company bootstrap failed: " + str(exc), "MIMO_CODING_COMPANY_ERROR")
