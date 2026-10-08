from __future__ import annotations

import copy
import json
import math
import re
from typing import Any, Callable

from .ai_input_token_estimator import estimate_tokens


TokenCounter = Callable[[str], int]

DEFAULT_TOKENIZER_ID = "defaultspack.approximate"
MISSING_TOKENIZER_WARNING = (
    "No tokenizer was found for this model profile. Defaultspack used its "
    "approximate tokenizer, so counts may differ significantly from the model."
)


def count_text_tokens(
    text: Any,
    *,
    model_profile_id: str = "",
    model: str = "",
    profiles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    resolver = _resolve_tokenizer(model_profile_id=model_profile_id, model=model, profiles=profiles)
    rendered = str(text or "")
    try:
        tokens = int(resolver["counter"](rendered))
    except Exception:
        tokens = int(estimate_tokens(rendered))
        resolver = _default_tokenizer(
            selected_profile=resolver.get("selected_profile"),
            model_profile_id=model_profile_id,
            model=model,
            warning_code="tokenizer_failed",
        )
    return {
        "tokens": max(0, tokens),
        "tokenizer": _public_tokenizer_metadata(resolver),
    }


def count_json_tokens(
    value: Any,
    *,
    model_profile_id: str = "",
    model: str = "",
    profiles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = json.dumps(str(value), ensure_ascii=False)
    return count_text_tokens(text, model_profile_id=model_profile_id, model=model, profiles=profiles)


def tokenizer_metadata(
    *,
    model_profile_id: str = "",
    model: str = "",
    profiles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return _public_tokenizer_metadata(
        _resolve_tokenizer(model_profile_id=model_profile_id, model=model, profiles=profiles)
    )


def apply_tokenizer_to_ai_input_response(
    response: dict[str, Any],
    *,
    model_profile_id: str = "",
    model: str = "",
    profiles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not model_profile_id and not model:
        return response
    payload = copy.deepcopy(response)
    effective = _dict_value(payload.get("effective_input"))
    profile_catalog = profiles if isinstance(profiles, list) else _profile_catalog()
    summary_metadata = tokenizer_metadata(model_profile_id=model_profile_id, model=model, profiles=profile_catalog)
    by_port: dict[str, int] = {}
    by_node: dict[str, int] = {}

    for port, key in (
        ("system", "system_segments"),
        ("developer", "developer_segments"),
        ("context", "context_segments"),
        ("tools", "tool_schemas"),
    ):
        total = 0
        for segment in _list_value(effective.get(key)):
            if not isinstance(segment, dict):
                continue
            counted = _count_segment_tokens(
                segment,
                model_profile_id=model_profile_id,
                model=model,
                profiles=profile_catalog,
            )
            if counted is not None:
                segment["tokens"] = counted["tokens"]
                segment["tokenizer"] = counted["tokenizer"]
            else:
                segment["tokenizer"] = summary_metadata
            tokens = _int(segment.get("tokens"))
            total += tokens
            segment_id = str(segment.get("id") or "")
            if segment_id:
                by_node[segment_id] = tokens
        by_port[port] = total

    policy = _dict_value(effective.get("policy"))
    policy_total = 0
    for segment in _list_value(policy.get("segments")):
        if not isinstance(segment, dict):
            continue
        counted = _count_segment_tokens(
            segment,
            model_profile_id=model_profile_id,
            model=model,
            profiles=profile_catalog,
        )
        if counted is not None:
            segment["tokens"] = counted["tokens"]
            segment["tokenizer"] = counted["tokenizer"]
        else:
            segment["tokenizer"] = summary_metadata
        tokens = _int(segment.get("tokens"))
        policy_total += tokens
        segment_id = str(segment.get("id") or "")
        if segment_id:
            by_node[segment_id] = tokens
    by_port["policy"] = policy_total

    for segment in _list_value(effective.get("disabled_segments")):
        if not isinstance(segment, dict):
            continue
        counted = _count_segment_tokens(
            segment,
            model_profile_id=model_profile_id,
            model=model,
            profiles=profile_catalog,
        )
        if counted is not None:
            segment["tokens"] = counted["tokens"]
            segment["tokenizer"] = counted["tokenizer"]
        else:
            segment["tokenizer"] = summary_metadata

    token_estimate = _dict_value(payload.get("token_estimate"))
    token_estimate["by_port"] = by_port
    token_estimate["by_node"] = dict(sorted(by_node.items(), key=lambda item: item[1], reverse=True))
    token_estimate["total"] = sum(by_port.values())
    token_estimate["tokenizer"] = summary_metadata
    payload["token_estimate"] = token_estimate
    return payload


def apply_tokenizer_to_prompt_usage(
    usage: dict[str, Any],
    *,
    model_profile_id: str = "",
    model: str = "",
    profiles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not model_profile_id and not model:
        return usage
    payload = copy.deepcopy(usage)
    profile_catalog = profiles if isinstance(profiles, list) else _profile_catalog()
    summary_metadata = tokenizer_metadata(model_profile_id=model_profile_id, model=model, profiles=profile_catalog)
    by_port: dict[str, int] = {}
    for segment in _list_value(payload.get("segments")):
        if not isinstance(segment, dict):
            continue
        counted = _count_segment_tokens(
            segment,
            model_profile_id=model_profile_id,
            model=model,
            profiles=profile_catalog,
        )
        if counted is not None:
            segment["tokens"] = counted["tokens"]
            segment["tokenizer"] = counted["tokenizer"]
        else:
            segment["tokenizer"] = summary_metadata
        if segment.get("status") == "active":
            port = str(segment.get("port") or "system")
            by_port[port] = by_port.get(port, 0) + _int(segment.get("tokens"))
    token_estimate = _dict_value(payload.get("token_estimate"))
    if by_port:
        token_estimate["by_port"] = by_port
        token_estimate["total"] = sum(by_port.values())
    token_estimate["tokenizer"] = summary_metadata
    payload["token_estimate"] = token_estimate
    payload["active_segments"] = [
        segment for segment in payload.get("segments", [])
        if isinstance(segment, dict) and segment.get("status") == "active"
    ]
    payload["disabled_segments"] = [
        segment for segment in payload.get("segments", [])
        if isinstance(segment, dict) and segment.get("status") != "active"
    ]
    return payload


def _count_segment_tokens(
    segment: dict[str, Any],
    *,
    model_profile_id: str,
    model: str,
    profiles: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if isinstance(segment.get("text"), str):
        return count_text_tokens(segment.get("text"), model_profile_id=model_profile_id, model=model, profiles=profiles)
    if "schema" in segment:
        return count_json_tokens(segment.get("schema"), model_profile_id=model_profile_id, model=model, profiles=profiles)
    return None


def _resolve_tokenizer(
    *,
    model_profile_id: str,
    model: str,
    profiles: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    catalog = profiles if isinstance(profiles, list) else _profile_catalog()
    selected = _select_profile(catalog, model_profile_id=model_profile_id, model=model)
    direct = _tokenizer_for_profile(selected, catalog, seen=set())
    if direct is not None:
        return {
            **direct,
            "status": direct.get("status") or "configured",
            "source": direct.get("source") or "profile",
            "selected_profile": selected,
        }
    borrowed = _borrow_tokenizer_for_same_model(selected, catalog)
    if borrowed is not None:
        return {
            **borrowed,
            "status": "borrowed",
            "source": "same_model_provider",
            "selected_profile": selected,
        }
    return _default_tokenizer(selected_profile=selected, model_profile_id=model_profile_id, model=model)


def _tokenizer_for_profile(
    profile: dict[str, Any] | None,
    catalog: list[dict[str, Any]],
    *,
    seen: set[str],
) -> dict[str, Any] | None:
    if not isinstance(profile, dict):
        return None
    profile_id = _profile_id(profile)
    if profile_id in seen:
        return None
    seen.add(profile_id)
    config = _tokenizer_config(profile)
    if config is None:
        return None
    ref = _config_ref_profile_id(config)
    if ref:
        target = _select_profile(catalog, model_profile_id=ref, model="")
        resolved = _tokenizer_for_profile(target, catalog, seen=seen)
        if resolved is not None:
            return {
                **resolved,
                "source": "profile_reference",
                "tokenizer_profile_id": _profile_id(target) or resolved.get("tokenizer_profile_id"),
            }
    counter = _counter_from_config(config)
    if counter is None:
        return None
    tokenizer_id = _tokenizer_id(config)
    return {
        "available": True,
        "fallback": False,
        "warning": "",
        "warning_code": "",
        "counter": counter,
        "tokenizer_id": tokenizer_id,
        "tokenizer_profile_id": profile_id,
        "tokenizer_provider_id": str(profile.get("provider_id") or ""),
        "tokenizer_model": str(profile.get("model_id") or profile.get("qualified_model_id") or ""),
        "provider_id": str(profile.get("provider_id") or ""),
        "model_profile_id": profile_id,
        "model": str(profile.get("model_id") or profile.get("qualified_model_id") or profile_id),
    }


def _borrow_tokenizer_for_same_model(
    selected: dict[str, Any] | None,
    catalog: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not isinstance(selected, dict):
        return None
    selected_id = _profile_id(selected)
    key = _same_model_key(selected)
    if not key:
        return None
    for candidate in catalog:
        if not isinstance(candidate, dict) or _profile_id(candidate) == selected_id:
            continue
        if _same_model_key(candidate) != key:
            continue
        resolved = _tokenizer_for_profile(candidate, catalog, seen=set())
        if resolved is not None:
            return resolved
    return None


def _counter_from_config(config: Any) -> TokenCounter | None:
    if callable(config):
        return _callable_counter(config)
    if isinstance(config, str):
        config = {"tokenizer_id": config}
    if not isinstance(config, dict):
        return None
    for key in ("count_tokens", "token_count", "counter"):
        counter = config.get(key)
        if callable(counter):
            return _callable_counter(counter)
    kind = str(config.get("kind") or config.get("type") or "").strip().lower()
    chars_per_token = _float_value(config.get("characters_per_token") or config.get("chars_per_token"))
    if chars_per_token and chars_per_token > 0:
        return _characters_counter(chars_per_token)
    bytes_per_token = _float_value(config.get("bytes_per_token"))
    if bytes_per_token and bytes_per_token > 0:
        return _bytes_counter(bytes_per_token)
    if kind in {"whitespace", "word", "words"}:
        return lambda text: len(re.findall(r"\S+", text))
    encoding_name = str(config.get("encoding") or config.get("tokenizer_id") or "").strip()
    if encoding_name:
        counter = _tiktoken_counter(encoding_name)
        if counter is not None:
            return counter
    if kind in {"defaultspack", "approximate", "default"}:
        return lambda text: int(estimate_tokens(text))
    return None


def _tiktoken_counter(encoding_name: str) -> TokenCounter | None:
    try:
        import tiktoken  # type: ignore
    except Exception:
        return None
    try:
        encoding = tiktoken.get_encoding(encoding_name)
    except Exception:
        try:
            encoding = tiktoken.encoding_for_model(encoding_name)
        except Exception:
            return None
    return lambda text: len(encoding.encode(text or ""))


def _tokenizer_config(profile: dict[str, Any]) -> Any:
    metadata = profile.get("metadata") if isinstance(profile.get("metadata"), dict) else {}
    defaults = profile.get("defaults") if isinstance(profile.get("defaults"), dict) else {}
    for container in (profile, metadata, defaults):
        if not isinstance(container, dict):
            continue
        if "tokenizer" in container:
            return container.get("tokenizer")
        for key in ("tokenizer_profile_id", "tokenizer_model_profile_id", "token_counter_profile_id"):
            value = str(container.get(key) or "").strip()
            if value:
                return {"profile_id": value}
        for key in ("count_tokens", "token_count"):
            if callable(container.get(key)):
                return container.get(key)
    return None


def _config_ref_profile_id(config: Any) -> str:
    if not isinstance(config, dict):
        return ""
    for key in ("profile_id", "model_profile_id", "source_profile_id", "tokenizer_profile_id", "tokenizer_model_profile_id"):
        value = str(config.get(key) or "").strip()
        if value:
            return value
    return ""


def _tokenizer_id(config: Any) -> str:
    if isinstance(config, str):
        return config or DEFAULT_TOKENIZER_ID
    if isinstance(config, dict):
        return str(config.get("tokenizer_id") or config.get("encoding") or config.get("kind") or DEFAULT_TOKENIZER_ID)
    return DEFAULT_TOKENIZER_ID


def _default_tokenizer(
    *,
    selected_profile: dict[str, Any] | None,
    model_profile_id: str,
    model: str,
    warning_code: str = "missing_tokenizer",
) -> dict[str, Any]:
    return {
        "available": False,
        "fallback": True,
        "status": "default",
        "source": "default",
        "warning": MISSING_TOKENIZER_WARNING,
        "warning_code": warning_code,
        "counter": lambda text: int(estimate_tokens(text)),
        "tokenizer_id": DEFAULT_TOKENIZER_ID,
        "tokenizer_profile_id": "",
        "tokenizer_provider_id": "",
        "tokenizer_model": "",
        "provider_id": str((selected_profile or {}).get("provider_id") or ""),
        "model_profile_id": _profile_id(selected_profile) or model_profile_id,
        "model": str((selected_profile or {}).get("model_id") or (selected_profile or {}).get("qualified_model_id") or model or model_profile_id),
        "selected_profile": selected_profile,
    }


def _public_tokenizer_metadata(value: dict[str, Any]) -> dict[str, Any]:
    metadata = {key: copy.deepcopy(item) for key, item in value.items() if key not in {"counter", "selected_profile"}}
    metadata.setdefault("available", False)
    metadata.setdefault("fallback", not bool(metadata.get("available")))
    metadata.setdefault("status", "default" if metadata.get("fallback") else "configured")
    metadata.setdefault("source", "default" if metadata.get("fallback") else "profile")
    metadata.setdefault("warning", MISSING_TOKENIZER_WARNING if metadata.get("fallback") else "")
    metadata.setdefault("warning_code", "missing_tokenizer" if metadata.get("fallback") else "")
    metadata.setdefault("tokenizer_id", DEFAULT_TOKENIZER_ID if metadata.get("fallback") else "")
    return metadata


def _select_profile(
    profiles: list[dict[str, Any]],
    *,
    model_profile_id: str,
    model: str,
) -> dict[str, Any] | None:
    identifiers = [model_profile_id, model]
    normalized = [_normalize_identifier(item) for item in identifiers if str(item or "").strip()]
    if not normalized:
        return None
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        profile_ids = _profile_identifiers(profile)
        if any(item in profile_ids for item in normalized):
            return profile
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        model_id = _normalize_identifier(profile.get("model_id"))
        if model_id and model_id in normalized:
            return profile
    return None


def _profile_identifiers(profile: dict[str, Any]) -> set[str]:
    provider_id = str(profile.get("provider_id") or "").strip()
    model_id = str(profile.get("model_id") or "").strip()
    values = {
        profile.get("profile_id"),
        profile.get("qualified_model_id"),
        profile.get("id"),
        profile.get("display_name"),
        profile.get("disambiguated_name"),
        model_id,
        f"{provider_id}/{model_id}" if provider_id and model_id else "",
    }
    return {_normalize_identifier(value) for value in values if str(value or "").strip()}


def _same_model_key(profile: dict[str, Any]) -> str:
    return _normalize_identifier(
        profile.get("same_model_across_providers_key")
        or profile.get("canonical_model_id")
        or profile.get("model_id")
        or profile.get("qualified_model_id")
    )


def _profile_id(profile: dict[str, Any] | None) -> str:
    if not isinstance(profile, dict):
        return ""
    return str(profile.get("profile_id") or profile.get("qualified_model_id") or profile.get("id") or "").strip()


def _normalize_identifier(value: Any) -> str:
    return str(value or "").strip().lower()


def _float_value(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dict_value(value: Any) -> dict[str, Any]:
    """Return a JSON object value, or an empty object for another shape."""
    return value if isinstance(value, dict) else {}


def _list_value(value: Any) -> list[Any]:
    """Return a JSON array value, or an empty array for another shape."""
    return value if isinstance(value, list) else []


def _callable_counter(callback: Callable[[str], object]) -> TokenCounter:
    def count(text: str) -> int:
        value = callback(text)
        if not isinstance(value, (str, bytes, bytearray, int, float)):
            raise TypeError("token counter must return a numeric value")
        return max(0, int(value))

    return count


def _characters_counter(divisor: float) -> TokenCounter:
    def count(text: str) -> int:
        return math.ceil(len(text) / divisor) if text else 0

    return count


def _bytes_counter(divisor: float) -> TokenCounter:
    def count(text: str) -> int:
        return math.ceil(len(text.encode("utf-8")) / divisor) if text else 0

    return count


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _profile_catalog() -> list[dict[str, Any]]:
    return []
