"""Authority v2 config lattice helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class AuthorityConfigError(ValueError):
    """Raised when an authority v2 config is malformed or widens a parent."""


AUTHORITY_LIST_FACETS: dict[str, str] = {
    "provider_ids": "provider_id",
    "api_ids": "api_id",
    "model_ids": "model_id",
    "function_ids": "function_id",
    "pack_ids": "pack_id",
    "caller_pack_ids": "caller_pack_id",
    "caller_function_ids": "caller_function_id",
    "domains": "domain",
    "host_actions": "host_action",
}
AUTHORITY_PORT_FACET = "ports"
AUTHORITY_BOOL_FACETS: dict[str, str] = {"allow_stream": "stream"}
AUTHORITY_NUMERIC_FACETS: dict[str, str] = {"max_input_tokens": "input_tokens"}
AUTHORITY_CONFIG_FACETS = frozenset(
    set(AUTHORITY_LIST_FACETS)
    | {AUTHORITY_PORT_FACET}
    | set(AUTHORITY_BOOL_FACETS)
    | set(AUTHORITY_NUMERIC_FACETS)
)
AUTHORITY_SUBJECT_FACETS = {
    **{subject: facet for facet, subject in AUTHORITY_LIST_FACETS.items()},
    "operation": "host_actions",
    "port": AUTHORITY_PORT_FACET,
    **{subject: facet for facet, subject in AUTHORITY_BOOL_FACETS.items()},
    **{subject: facet for facet, subject in AUTHORITY_NUMERIC_FACETS.items()},
}
AUTHORITY_RESOURCE_CONFIG_FIELDS: tuple[tuple[str, str], ...] = (
    ("provider_id", "provider_ids"),
    ("api_id", "api_ids"),
    ("model_id", "model_ids"),
    ("function_id", "function_ids"),
    ("pack_id", "pack_ids"),
    ("caller_pack_id", "caller_pack_ids"),
    ("caller_function_id", "caller_function_ids"),
    ("domain", "domains"),
    ("host_action", "host_actions"),
    ("operation", "host_actions"),
)
_ORDERED_FACETS = (
    *AUTHORITY_LIST_FACETS.keys(),
    AUTHORITY_PORT_FACET,
    *AUTHORITY_BOOL_FACETS.keys(),
    *AUTHORITY_NUMERIC_FACETS.keys(),
)


def config_facet_for_subject(subject_key: str) -> str:
    key = str(subject_key or "").strip()
    try:
        return AUTHORITY_SUBJECT_FACETS[key]
    except KeyError as exc:
        raise AuthorityConfigError(f"Unknown authority subject facet: {key}") from exc


def subject_for_config_facet(config_key: str) -> str:
    key = str(config_key or "").strip()
    if key not in AUTHORITY_CONFIG_FACETS:
        raise AuthorityConfigError(f"Unknown authority config facet: {key}")
    if key in AUTHORITY_LIST_FACETS:
        return AUTHORITY_LIST_FACETS[key]
    if key == AUTHORITY_PORT_FACET:
        return "port"
    if key in AUTHORITY_BOOL_FACETS:
        return AUTHORITY_BOOL_FACETS[key]
    return AUTHORITY_NUMERIC_FACETS[key]


def authority_config_from_resource(resource: Mapping[str, Any] | None) -> dict[str, Any]:
    data = resource if isinstance(resource, Mapping) else {}
    config: dict[str, Any] = {}
    for subject_key, facet in AUTHORITY_RESOURCE_CONFIG_FIELDS:
        values = _string_values(data.get(subject_key))
        if values:
            current = config.setdefault(facet, [])
            for value in values:
                if value not in current:
                    current.append(value)
    ports = _port_values(data.get("port"))
    if ports:
        config[AUTHORITY_PORT_FACET] = ports
    if data.get("stream"):
        config["allow_stream"] = True
    input_tokens = _positive_int(data.get("input_tokens"))
    if input_tokens is not None:
        config["max_input_tokens"] = input_tokens
    return config


def resource_within_authority_config(
    config: Mapping[str, Any], resource: Mapping[str, Any]
) -> bool:
    """Return whether a route resource is inside an already-meet grant config."""
    fields: dict[str, list[str]] = {}
    for resource_key, config_key in AUTHORITY_RESOURCE_CONFIG_FIELDS:
        fields.setdefault(config_key, []).append(resource_key)
    for config_key, resource_keys in fields.items():
        if config_key not in config:
            continue
        allowed = set(_string_values(config.get(config_key)))
        actual = {
            value
            for key in resource_keys
            if (value := str(resource.get(key) or "").strip())
        }
        if not allowed or not actual.intersection(allowed):
            return False
    if AUTHORITY_PORT_FACET in config:
        raw_port = resource.get("port")
        if not isinstance(raw_port, (str, int, float, bytes)):
            return False
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            return False
        if port not in set(_port_values(config.get(AUTHORITY_PORT_FACET))):
            return False
    if "allow_stream" in config and resource.get("stream"):
        if not bool(config.get("allow_stream")):
            return False
    if "max_input_tokens" in config and resource.get("input_tokens") is not None:
        raw_max_tokens = config.get("max_input_tokens")
        if not isinstance(raw_max_tokens, (str, int, float, bytes)):
            return False
        try:
            if int(resource.get("input_tokens") or 0) > int(
                raw_max_tokens
            ):
                return False
        except (TypeError, ValueError):
            return False
    return True


def validate_authority_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    if config is None:
        return {}
    if not isinstance(config, Mapping):
        raise AuthorityConfigError("Authority config must be an object")

    unknown = sorted(str(key) for key in config if str(key) not in AUTHORITY_CONFIG_FACETS)
    if unknown:
        raise AuthorityConfigError("Unknown authority config keys: " + ", ".join(unknown))

    normalized: dict[str, Any] = {}
    for facet in AUTHORITY_LIST_FACETS:
        if facet in config:
            normalized[facet] = _string_values(config.get(facet))

    if AUTHORITY_PORT_FACET in config:
        normalized[AUTHORITY_PORT_FACET] = _port_values(config.get(AUTHORITY_PORT_FACET))

    if "allow_stream" in config:
        normalized["allow_stream"] = bool(config.get("allow_stream"))

    if "max_input_tokens" in config:
        value = _positive_int(config.get("max_input_tokens"))
        if value is None:
            raise AuthorityConfigError("max_input_tokens must be a non-negative integer")
        normalized["max_input_tokens"] = value

    return normalized


def authority_constraints_from_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return only Authority v2 constraint facets from a persisted grant config.

    Persisted grants may carry legacy metadata such as ``{"mode": "builtin"}``.
    That metadata must not invalidate the grant, but it also must not
    participate in the Authority v2 lattice.
    """

    if config is None:
        return {}
    if not isinstance(config, Mapping):
        raise AuthorityConfigError("Authority config must be an object")
    return validate_authority_config(
        {key: value for key, value in config.items() if str(key) in AUTHORITY_CONFIG_FACETS}
    )


def meet_authority_configs(*configs: Mapping[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for config in configs:
        result = _meet_two_authority_configs(result, authority_constraints_from_config(config))
    return result


def is_authority_config_subset(
    child_config: Mapping[str, Any] | None,
    parent_config: Mapping[str, Any] | None,
) -> bool:
    return not _non_subset_facets(
        validate_authority_config(child_config),
        validate_authority_config(parent_config),
    )


def assert_authority_config_subset(
    child_config: Mapping[str, Any] | None,
    parent_config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    child = validate_authority_config(child_config)
    parent = validate_authority_config(parent_config)
    widened = _non_subset_facets(child, parent)
    if widened:
        raise AuthorityConfigError("Authority config widens parent facets: " + ", ".join(widened))
    return child


def _meet_two_authority_configs(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for facet in _ORDERED_FACETS:
        parent_has = facet in parent
        child_has = facet in child
        if not parent_has and not child_has:
            continue
        if facet in AUTHORITY_LIST_FACETS or facet == AUTHORITY_PORT_FACET:
            result[facet] = _meet_list_values(
                parent.get(facet) if parent_has else None,
                child.get(facet) if child_has else None,
                parent_has=parent_has,
                child_has=child_has,
            )
        elif facet in AUTHORITY_BOOL_FACETS:
            result[facet] = _meet_bool_values(
                parent.get(facet) if parent_has else None,
                child.get(facet) if child_has else None,
                parent_has=parent_has,
                child_has=child_has,
            )
        elif facet in AUTHORITY_NUMERIC_FACETS:
            result[facet] = _meet_numeric_values(
                parent.get(facet) if parent_has else None,
                child.get(facet) if child_has else None,
                parent_has=parent_has,
                child_has=child_has,
            )
    return result


def _meet_list_values(
    parent_value: Any,
    child_value: Any,
    *,
    parent_has: bool,
    child_has: bool,
) -> list[Any]:
    if parent_has and child_has:
        child_values = set(child_value or [])
        return [item for item in list(parent_value or []) if item in child_values]
    if parent_has:
        return list(parent_value or [])
    return list(child_value or [])


def _meet_bool_values(
    parent_value: Any,
    child_value: Any,
    *,
    parent_has: bool,
    child_has: bool,
) -> bool:
    if parent_has and child_has:
        return bool(parent_value) and bool(child_value)
    return bool(parent_value) if parent_has else bool(child_value)


def _meet_numeric_values(
    parent_value: Any,
    child_value: Any,
    *,
    parent_has: bool,
    child_has: bool,
) -> int:
    if parent_has and child_has:
        return min(int(parent_value), int(child_value))
    return int(parent_value) if parent_has else int(child_value)


def _non_subset_facets(child: dict[str, Any], parent: dict[str, Any]) -> list[str]:
    widened: list[str] = []
    for facet in (*AUTHORITY_LIST_FACETS.keys(), AUTHORITY_PORT_FACET):
        if facet not in child or facet not in parent:
            continue
        child_values = set(child.get(facet) or [])
        parent_values = set(parent.get(facet) or [])
        if child_values and (not parent_values or not child_values.issubset(parent_values)):
            widened.append(facet)

    if child.get("allow_stream") is True and parent.get("allow_stream") is False:
        widened.append("allow_stream")

    if "max_input_tokens" in child and "max_input_tokens" in parent:
        if int(child["max_input_tokens"]) > int(parent["max_input_tokens"]):
            widened.append("max_input_tokens")

    return widened


def _string_values(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    return _unique(str(item).strip() for item in values if str(item or "").strip())


def _port_values(value: Any) -> list[int]:
    values = value if isinstance(value, list) else [value]
    ports: list[int] = []
    for item in values:
        try:
            ports.append(int(item))
        except (TypeError, ValueError):
            continue
    return _unique(ports)


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _unique(values: Any) -> list[Any]:
    seen: set[Any] = set()
    result: list[Any] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
