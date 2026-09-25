"""Local-only DeepThink preflight shared by chat and the saved-turn path.

The review-chain gate resolves the effective members through the same
``AIClient``/``select_model_pack`` code paths a real invocation uses, then
verifies member credentials, an explicitly credential-free endpoint, or an
OAuth connection from local metadata — never the network.  Everything here is
a read-only oracle: nothing is sent and nothing is mutated.

``run_request.py`` re-exports these names so existing callers and tests keep
their import surface while the sandboxed saved-turn path imports the same
gate without pulling in the whole chat preparation module.
"""

from __future__ import annotations

from typing import Any

from tobkiri_protocol.settings_state import SettingsOwnerPort

from domain.ai_client import rumi_process
from domain.ai_client.api_key_store import (
    provider_named_api_keys,
    read_provider_api_key,
)
from domain.ai_client.model_pack import ModelPack
from domain.ai_client.model_pack_router import select_model_pack
from domain.ai_client.model_pack_store import ModelPackStore
from domain.ai_client.oauth_store import provider_has_oauth_connection


class DeepThinkPreflightError(ValueError):
    """DeepThink was requested for a selection that cannot run review_chain.

    ``_run_deepthink_chain`` only executes inside the ``review_chain``
    composite; for any other resolved model/composite/provider key the flag
    used to be stripped from provider params and silently ignored (A51).
    ``prepare_chat_run`` raises this before send instead, carrying a
    machine-readable cause and the corrective fix.
    """

    code = "DEEPTHINK_REQUIRES_REVIEW_CHAIN"

    def __init__(
        self,
        *,
        model: str,
        cause: str,
        fix: str,
        original_model: str = "",
    ) -> None:
        self.model = str(model or "")
        self.original_model = str(original_model or "")
        self.cause = str(cause or "")
        self.fix = str(fix or "")
        super().__init__(self._compose_message())

    def _compose_message(self) -> str:
        requested = ""
        if self.original_model and self.original_model != self.model:
            requested = f" (requested {self.original_model!r})"
        return (
            "DeepThink requires the review_chain composite — "
            f"{self.cause}{requested}. {self.fix}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "model": self.model,
            "original_model": self.original_model,
            "cause": self.cause,
            "fix": self.fix,
            "message": str(self),
        }


class DeepThinkReadinessError(ValueError):
    """The resolved DeepThink review chain cannot run credibly.

    ``DeepThinkPreflightError`` gates *which* selections may enter DeepThink;
    this second gate verifies — still before anything is sent — that the
    generator/reviewer members the ``review_chain`` will actually invoke
    resolve to providers with usable credentials (API key, OAuth connection,
    or an explicitly credential-free local endpoint) and that any declared
    chain budget stays inside the bounds ``RumiProcessRunner`` enforces.
    The check is local-only: it reads the secrets-store metadata and the
    provider manifest catalog, never the network.
    """

    default_code = "DEEPTHINK_READINESS_FAILED"

    def __init__(
        self,
        *,
        model: str,
        cause: str,
        fix: str,
        code: str = "",
        original_model: str = "",
        member_models: Any = None,
        details: Any = None,
    ) -> None:
        self.code = str(code or self.default_code)
        self.model = str(model or "")
        self.original_model = str(original_model or "")
        self.cause = str(cause or "")
        self.fix = str(fix or "")
        self.member_models = [
            str(item) for item in (member_models or []) if str(item or "").strip()
        ]
        self.details = dict(details) if isinstance(details, dict) else {}
        super().__init__(self._compose_message())

    def _compose_message(self) -> str:
        requested = ""
        if self.original_model and self.original_model != self.model:
            requested = f" (requested {self.original_model!r})"
        return (
            "DeepThink review chain is not runnable — "
            f"{self.cause}{requested}. {self.fix}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "model": self.model,
            "original_model": self.original_model,
            "member_models": list(self.member_models),
            "cause": self.cause,
            "fix": self.fix,
            "details": dict(self.details),
            "message": str(self),
        }


_DEEPTHINK_READINESS_FIX = (
    "Configure credentials for the member providers (for example via "
    "ai_client set-key <provider>), declare a bounded chain budget, or turn "
    "DeepThink off"
)

_DEEPTHINK_REVIEW_CHAIN_FIX = (
    "Select a review_chain model pack (for example 'modelpack/rumi' or a "
    "'rumi/*' process model) or turn DeepThink off"
)

def _deepthink_model_pack(
    model: str,
    *,
    client: Any,
    settings_owner: SettingsOwnerPort | None,
) -> Any:
    """Resolve the pack through the real ``AIClient._model_pack_for_model``."""
    try:
        return client._model_pack_for_model(model, settings_owner=settings_owner)
    except Exception:
        return None


def _deepthink_composite_for_model(
    model: str,
    *,
    client: Any,
    settings_owner: SettingsOwnerPort | None,
) -> tuple[str, dict[str, Any]] | None:
    """Delegate to ``AIClient._composite_for_model`` under the request owner."""
    try:
        composite = client._composite_for_model(model, settings_owner=settings_owner)
    except Exception:
        return None
    if not isinstance(composite, dict):
        return None
    composite_id = str(composite.get("id") or model or "").strip()
    return composite_id, composite


def _deepthink_is_rumi_process_model(model: str) -> bool:
    """True for ``rumi/*`` selections that RumiProvider maps to the rumi pack."""
    text = str(model or "").strip()
    provider_key, _, tail = text.partition("/")
    if provider_key.strip().lower() != "rumi" or not tail:
        return False
    try:
        from domain.ai_client.providers.rumi_provider import RumiProvider
    except Exception:
        return False
    return bool(RumiProvider._is_rumi_process_model(text))


def _deepthink_resolution_client() -> Any:
    """Return a detached ``AIClient`` holding the providers invocable now.

    The DeepThink review chain resolves members through ``AIClient``
    helpers (``resolve_provider``, ``_resolve_rumi_member_model``,
    ``_routes_for_model`` …), which consult the client's live
    ``_providers``/``_profiles``.  The process-wide singleton can carry a
    stale snapshot — providers registered before a key write, or manifest
    adapters gated out of auto-registration (``RUMI_DEFAULTSPACK_ENABLE_*
    `` flags) but still invocable.  A detached instance re-runs the same
    detection a fresh runtime performs, then merges only the singleton's
    dynamically ``register_provider``-added entries, so the check agrees
    with the registry a real invocation would see.  Nothing is sent and
    nothing is mutated; this is a local-only resolution oracle.
    """
    try:
        from domain.ai_client.client import AIClient
        from domain.ai_client.providers import (
            _provider_manifest_map,
            detect_available_providers,
        )
    except Exception:
        return None
    try:
        client = object.__new__(AIClient)
        client._initialized = False
        AIClient.__init__(client)
    except Exception:
        return None
    try:
        detected = detect_available_providers() or {}
    except Exception:
        detected = {}
    try:
        manifest_ids = set(_provider_manifest_map())
    except Exception:
        manifest_ids = set()
    for provider_id, provider in detected.items():
        client._providers.setdefault(provider_id, provider)
    singleton = getattr(AIClient, "_instance", None)
    if isinstance(singleton, AIClient) and singleton is not client:
        for name, provider in dict(getattr(singleton, "_providers", {})).items():
            # Manifest-declared providers are re-detected against the current
            # credential state; merge only genuinely dynamic registrations.
            if name in detected or name in manifest_ids:
                continue
            client._providers.setdefault(name, provider)
        client._profiles.update(getattr(singleton, "_profiles", {}) or {})
    return client


def _deepthink_pack_spec(pack: Any, *, source: str) -> dict[str, Any]:
    """Carry the resolved ``ModelPack`` plus its budget/metadata forward."""
    metadata = getattr(pack, "metadata", {})
    return {
        "id": str(getattr(pack, "id", "") or ""),
        "source": str(source or ""),
        "pack": pack,
        "composite": None,
        "budget": getattr(pack, "budget", None),
        "metadata": dict(metadata) if isinstance(metadata, dict) else {},
    }


def _deepthink_rumi_pack_spec(
    *,
    client: Any,
    settings_owner: SettingsOwnerPort | None,
) -> dict[str, Any]:
    """Spec for the pack ``rumi/*`` process models dispatch through."""
    pack = _deepthink_model_pack(
        rumi_process.RUMI_MODEL_PACK_REF,
        client=client,
        settings_owner=settings_owner,
    )
    if pack is None:
        pack = ModelPack.from_dict(rumi_process.default_rumi_model_pack())
    return _deepthink_pack_spec(pack, source="rumi_process")


def _deepthink_chain_spec(
    model: str,
    *,
    client: Any,
    settings_owner: SettingsOwnerPort | None,
) -> tuple[dict[str, Any] | None, str]:
    """Resolve the review-chain spec *model* will execute, or why it cannot.

    Uses the same resolution the composite dispatch performs before
    ``RumiProcessRunner._run_deepthink_chain`` can ever run: a model pack or
    composite whose mode is ``review_chain``, or a ``rumi/*`` process model
    that routes to the builtin ``modelpack/rumi`` pack.  Returns
    ``(spec, "")`` for a chain-capable selection and ``(None, cause)``
    otherwise.
    """
    pack = _deepthink_model_pack(
        model, client=client, settings_owner=settings_owner
    )
    if pack is not None:
        mode = str(getattr(pack, "mode", "") or "fallback_chain").strip() or "fallback_chain"
        if mode == "review_chain":
            return _deepthink_pack_spec(pack, source="model_pack"), ""
        pack_id = str(getattr(pack, "id", "") or model)
        return None, (
            f"model pack {pack_id!r} uses mode {mode!r}, "
            "which cannot run the review chain"
        )
    if ModelPackStore.is_model_pack_ref(model):
        return None, f"model pack reference {model!r} has no configured pack"
    composite_match = _deepthink_composite_for_model(
        model, client=client, settings_owner=settings_owner
    )
    if composite_match is not None:
        composite_id, composite = composite_match
        mode = (
            str(composite.get("mode") or composite.get("type") or "fallback_chain").strip()
            or "fallback_chain"
        )
        if mode == "review_chain":
            metadata = composite.get("metadata")
            return {
                "id": str(composite_id or ""),
                "source": "composite",
                "pack": None,
                "composite": dict(composite),
                "budget": composite.get("budget"),
                "metadata": dict(metadata) if isinstance(metadata, dict) else {},
            }, ""
        return None, (
            f"composite {composite_id!r} uses mode {mode!r}, "
            "which cannot run the review chain"
        )
    if _deepthink_is_rumi_process_model(model):
        return _deepthink_rumi_pack_spec(
            client=client, settings_owner=settings_owner
        ), ""
    return None, f"selected model {model!r} is not chain-capable"


def _deepthink_effective_params(
    model: str, params: dict[str, Any]
) -> dict[str, Any]:
    """Apply ``RumiProvider._process_params`` for ``rumi/*`` process models.

    Production routes ``rumi/*`` selections through ``RumiProvider``, which
    injects ``rumi_base_model_override``/``rumi_require_intended_base_model``
    for the MiMo aliases before the pack dispatch sees the params.  Reusing
    the real hook keeps the preflight on the effective params the runner
    would receive (the mirror version never applied it).
    """
    effective = dict(params or {})
    if not _deepthink_is_rumi_process_model(model):
        return effective
    try:
        from domain.ai_client.providers.rumi_provider import RumiProvider

        return dict(RumiProvider._process_params(model, effective))
    except Exception:
        return effective


def _deepthink_ordered_members(
    client: Any,
    spec: dict[str, Any],
    params: dict[str, Any],
    *,
    settings_owner: SettingsOwnerPort | None,
    messages: Any,
    tools: Any,
) -> list[Any]:
    """Members the chain would consider, via the production selection code.

    Pack selections run the real ``select_model_pack`` — which applies
    capability/condition filtering and appends ``pack.fallback`` members —
    so the readiness check sees exactly the ordered members
    ``_complete_model_pack`` would hand to the composite dispatch.
    Composite selections use ``AIClient._composite_member_list`` for the
    ``members``/``models``/``chain`` + string normalization.
    """
    pack = spec.get("pack")
    if pack is not None:
        # ``AIClient._complete_model_pack`` rebuilds the builtin rumi pack on
        # an explicit base-model override before member selection.
        override = str(params.get("rumi_base_model_override") or "").strip()
        if (
            str(getattr(pack, "id", "") or "") == rumi_process.RUMI_MODEL_PACK_ID
            and override
        ):
            pack = ModelPack.from_dict(
                rumi_process.default_rumi_model_pack(base_model=override)
            )
        try:
            models = client._settings_data(settings_owner=settings_owner).get(
                "models"
            )
        except Exception:
            models = None
        try:
            selection = select_model_pack(
                pack,
                {
                    "user_text": client._messages_text(messages),
                    "has_images": client._messages_have_images(messages),
                    "requires_tool_calling": bool(tools),
                    "requested_thinking_level": str(
                        (params or {}).get("thinking_level") or ""
                    ),
                    "task_hints": (params or {}).get("task_hints")
                    if isinstance((params or {}).get("task_hints"), dict)
                    else {},
                },
                settings=models if isinstance(models, dict) else {},
            )
        except Exception:
            return []
        if selection is None:
            return []
        return list(selection.ordered_members or [])
    composite = spec.get("composite")
    if isinstance(composite, dict):
        try:
            return list(client._composite_member_list(composite))
        except Exception:
            return []
    return []


def _deepthink_provider_manifests() -> dict[str, Any]:
    try:
        from domain.ai_client.providers import _provider_manifest_map

        return _provider_manifest_map()
    except Exception:
        return {}


# Namespaces that are never registered provider ids — member models
# referencing them cannot be invoked directly by the runner.  ``rumi`` and
# ``human-operator`` are intentionally absent: both are real registered
# providers, so ``resolve_provider`` remains the oracle for them.
_DEEPTHINK_NON_PROVIDER_IDS = frozenset({"stub", "modelpack", "composite"})


def _deepthink_is_stub_provider(provider: Any) -> bool:
    return provider is None or provider.__class__.__name__ == "StubProvider"


def _deepthink_local_model_inventory(client: Any) -> list[dict[str, Any]]:
    """Local-only equivalent of ``AIClient.list_models``.

    ``list_models`` additionally asks each adapter for its live inventory,
    which may touch the network; the preflight substitutes the declarative
    catalog plus each adapter's static ``KNOWN_MODELS`` so the check stays
    local-only while exercising the same normalization and matching logic.
    """
    try:
        active_ids = set(client._active_provider_ids())
    except Exception:
        return []
    try:
        from domain.ai_client.providers import (
            get_all_known_models,
            get_provider_catalog_map,
        )

        models = [
            item
            for item in get_all_known_models(active_provider_ids=active_ids)
            if isinstance(item, dict)
        ]
        catalog_map = get_provider_catalog_map(active_provider_ids=active_ids)
    except Exception:
        return []
    seen = {item.get("qualified_model_id") for item in models}
    for provider_id in sorted(active_ids):
        provider = client._providers.get(provider_id)
        entry = catalog_map.get(provider_id)
        if provider is None or entry is None:
            continue
        for raw in getattr(provider, "KNOWN_MODELS", None) or []:
            try:
                candidate = client._normalize_runtime_model(provider_id, entry, raw)
            except Exception:
                candidate = None
            if not isinstance(candidate, dict):
                continue
            qualified = candidate.get("qualified_model_id")
            if qualified in seen:
                continue
            seen.add(qualified)
            models.append(candidate)
    return models


def _deepthink_catalog_unique_provider(
    client: Any,
    member_model: str,
    inventory: list[dict[str, Any]],
) -> str:
    """``resolve_provider``'s unique catalog-model match over *inventory*."""
    del client
    ref = str(member_model or "").strip()
    if not ref:
        return ""
    match_keys = (
        "model_id",
        "qualified_model_id",
        "id",
        "name",
        "display_name",
        "disambiguated_name",
    )
    matches = {
        str(item.get("provider_id") or item.get("provider") or "").strip()
        for item in inventory
        if isinstance(item, dict)
        and ref in {str(item.get(key) or "").strip() for key in match_keys}
    }
    matches.discard("")
    return matches.pop() if len(matches) == 1 else ""


def _deepthink_member_credential_cause(
    client: Any,
    member_model: str,
    manifests: dict[str, Any],
    inventory: list[dict[str, Any]],
    *,
    settings_owner: SettingsOwnerPort | None,
) -> str:
    """Return "" when *member_model* resolves to an invocable provider.

    Follows the production dispatch order — an api-bound profile triple
    (``provider/api/model``), then ``api_routes``/``model_api_routes``
    entries — and otherwise lands on ``resolve_provider`` over the
    detection-merged client: a keyed provider that fails to register or a
    reference no provider exposes fails closed instead of passing on a
    configured key alone.
    """
    ref = str(member_model or "").strip()
    if not ref:
        return "a review chain member has no model reference"

    # 1) ``provider/api/model`` bound to a configured named connection.
    try:
        bound = client._api_bound_profile_parts(ref)
    except Exception:
        bound = None
    if bound is not None:
        provider_id, api_id, model_id, _metadata = bound
        provider, _resolved = client.resolve_provider(f"{provider_id}/{model_id}")
        if not _deepthink_is_stub_provider(provider) and read_provider_api_key(
            provider_id, api_id
        ):
            return ""

    # 2) ``api_routes``/``model_api_routes`` entries (``models`` + ``apis``
    #    subtrees, same as ``AIClient._api_routes``).
    try:
        route_refs = client._routes_for_model(ref, settings_owner=settings_owner)
    except Exception:
        route_refs = []
    for route_ref in route_refs or []:
        provider_id, api_id = client._route_parts(route_ref)
        if not provider_id or provider_id in _DEEPTHINK_NON_PROVIDER_IDS:
            continue
        provider, _resolved = client.resolve_provider(
            client._model_for_route(ref, provider_id)
        )
        if _deepthink_is_stub_provider(provider):
            continue
        if not client._provider_api_key_configured(provider_id, api_id):
            continue
        if not read_provider_api_key(provider_id, api_id):
            continue
        return ""

    # 3) Plain ``resolve_provider`` — qualified refs hit the registered
    #    providers directly; unqualified refs go through registered profile
    #    aliases and a unique catalog-model match, both local-only here.
    provider_id = ""
    if "/" in ref:
        provider_id = ref.split("/", 1)[0].strip()
        if provider_id in _DEEPTHINK_NON_PROVIDER_IDS:
            return (
                f"member model {ref!r} targets meta provider {provider_id!r}, "
                "which has no directly invocable model"
            )
        provider, _resolved = client.resolve_provider(ref)
        if not _deepthink_is_stub_provider(provider):
            return ""
    else:
        profile = (
            client._profiles.get(ref)
            if isinstance(getattr(client, "_profiles", None), dict)
            else None
        )
        if isinstance(profile, dict):
            provider_id = str(
                profile.get("provider") or profile.get("provider_id") or ""
            ).strip()
            model_name = str(
                profile.get("model")
                or profile.get("model_id")
                or profile.get("qualified_model_id")
                or ref
            ).strip()
            if "/" in model_name:
                resolved_provider, _, _resolved_model = model_name.partition("/")
                provider_id = provider_id or resolved_provider
            if provider_id and not _deepthink_is_stub_provider(
                client._providers.get(provider_id)
            ):
                return ""
        else:
            provider_id = _deepthink_catalog_unique_provider(
                client, ref, inventory
            )
            if provider_id:
                return ""
        if not provider_id:
            return (
                f"member model {ref!r} is not provider-qualified; "
                "no configured provider can be verified for it"
            )
    if provider_id in _DEEPTHINK_NON_PROVIDER_IDS:
        return (
            f"member model {ref!r} targets meta provider {provider_id!r}, "
            "which has no directly invocable model"
        )
    if provider_id not in manifests:
        # Saved custom-provider connections remain invocable through the
        # captured Pack v4 gateway even without a local adapter manifest —
        # but only when the connection record actually binds an endpoint.
        # A bare key for a provider nothing can invoke (the production
        # StubProvider outcome) must still fail closed.
        try:
            custom_ready = any(
                item.get("configured")
                and str(item.get("base_url") or "").strip()
                for item in provider_named_api_keys(provider_id)
            ) or provider_has_oauth_connection(provider_id)
        except Exception:
            custom_ready = False
        if custom_ready:
            return ""
    return (
        f"member model {ref!r} resolves to provider {provider_id!r}, which "
        "has no configured API key, OAuth connection, or local endpoint"
    )


# Mirrors the bounds ``RumiProcessRunner`` enforces while running the chain.
_DEEPTHINK_BUDGET_BOUNDS = {
    "max_review_rounds": (1, 5),
    "deepthink_max_review_iterations": (1, 8),
    "deepthink_user_rejection_review_cycles": (0, 2),
    "deepthink_max_sections": (1, rumi_process.RUMI_DEEPTHINK_MAX_SECTIONS),
}
_DEEPTHINK_BUDGET_DEFAULTS = {
    "max_review_rounds": 2,
    "deepthink_max_review_iterations": 2,
    "deepthink_user_rejection_review_cycles": 2,
    "deepthink_max_sections": rumi_process.RUMI_DEEPTHINK_MAX_SECTIONS,
}


def _deepthink_explicit_budget_value(
    knob: str, spec: dict[str, Any], params: dict[str, Any]
) -> Any:
    """Return the declared value for *knob*, mirroring runner precedence."""
    budget = spec.get("budget") if isinstance(spec.get("budget"), dict) else {}
    if knob == "deepthink_user_rejection_review_cycles":
        if isinstance(params, dict) and knob in params:
            return params.get(knob)
        return budget.get(knob)
    if isinstance(params, dict) and params.get(knob):
        return params.get(knob)
    if budget.get(knob) not in (None, ""):
        return budget.get(knob)
    if knob == "max_review_rounds":
        metadata = spec.get("metadata") if isinstance(spec.get("metadata"), dict) else {}
        return metadata.get("max_review_rounds")
    return None


def _deepthink_budget_report(
    spec: dict[str, Any], params: dict[str, Any]
) -> tuple[dict[str, int], list[str]]:
    """Return (effective bounded values, problems) for the declared budget.

    Bounds are enforced by the real ``RumiProcessRunner`` clamp helpers so a
    declared value that the runner would silently clamp is reported instead.
    """
    problems: list[str] = []
    effective: dict[str, int] = {}
    budget = spec.get("budget")
    if budget is not None and not isinstance(budget, dict):
        problems.append(
            f"review chain {spec.get('id')!r} declares a non-mapping budget"
        )
    try:
        from domain.ai_client.rumi_process_runner import RumiProcessRunner

        clamp_fns = (
            RumiProcessRunner._positive_int,
            RumiProcessRunner._nonnegative_int,
        )
    except Exception:
        clamp_fns = None
    for knob, (floor, cap) in _DEEPTHINK_BUDGET_BOUNDS.items():
        explicit = _deepthink_explicit_budget_value(knob, spec, params)
        value: int | None = None
        if explicit is not None:
            try:
                value = int(explicit)
            except (TypeError, ValueError):
                problems.append(f"{knob}={explicit!r} is not an integer bound")
        if value is None:
            continue
        if clamp_fns is not None:
            clamp = (
                clamp_fns[1] if floor == 0 else clamp_fns[0]
            )
            bounded = clamp(value, default=max(floor, 1), upper=cap)
        else:
            bounded = max(floor, min(cap, value))
        if bounded != value:
            problems.append(
                f"{knob}={value} is outside the enforced bound [{floor}, {cap}]"
            )
        effective[knob] = bounded
    # Runner default: ``deepthink_max_review_iterations`` falls back to the
    # resolved max_reviews when no explicit bound was declared.
    if "deepthink_max_review_iterations" not in effective:
        effective["deepthink_max_review_iterations"] = effective.get(
            "max_review_rounds", _DEEPTHINK_BUDGET_DEFAULTS["max_review_rounds"]
        )
    for knob, default in _DEEPTHINK_BUDGET_DEFAULTS.items():
        effective.setdefault(knob, default)
    return effective, problems


def _deepthink_readiness_report(
    spec: dict[str, Any],
    params: dict[str, Any],
    *,
    model: str,
    settings_owner: SettingsOwnerPort | None,
    client: Any,
    messages: Any,
    tools: Any,
) -> dict[str, Any]:
    """Resolve effective chain members + verify credentials and budget.

    Every step delegates to the shared production helpers —
    ``select_model_pack`` (capability filtering + pack fallback members),
    ``AIClient._member_conditions_match``, ``AIClient._review_chain_member``
    and ``AIClient._resolve_rumi_member_model`` — so the check cannot drift
    from the resolution the review chain would perform.  Returns
    ``{"ok": bool, "member_models": [...], "budget": {...},
    "problems": [{"code", "cause"}]}`` — local metadata only, never network.
    """
    effective_params = _deepthink_effective_params(
        str(model or ""), params if isinstance(params, dict) else {}
    )
    members = _deepthink_ordered_members(
        client,
        spec,
        effective_params,
        settings_owner=settings_owner,
        messages=messages,
        tools=tools,
    )
    runnable = [
        member
        for member in members
        if client._member_model(member)
        and client._member_conditions_match(member, messages, tools, effective_params)
    ]
    problems: list[dict[str, str]] = []
    resolved_member_models: list[str] = []
    if not runnable:
        problems.append(
            {
                "code": "DEEPTHINK_CHAIN_MEMBERS_EMPTY",
                "cause": (
                    f"review chain {spec.get('id')!r} has no runnable members "
                    "for this request"
                ),
            }
        )
    else:
        generator = client._review_chain_member(
            runnable, {"generator", "primary", "drafter", "planner"}, 0
        )
        reviewer = client._review_chain_member(
            runnable,
            {"reviewer", "judge", "critic"},
            1 if len(runnable) > 1 else 0,
        )
        inventory = _deepthink_local_model_inventory(client)
        for member in (generator, reviewer):
            resolved = str(
                client._resolve_rumi_member_model(
                    client._member_model(member),
                    effective_params,
                    model_inventory=inventory,
                )
                or ""
            ).strip()
            if resolved and resolved not in resolved_member_models:
                resolved_member_models.append(resolved)
        manifests: dict[str, Any] = {}
        manifests_loaded = False
        for resolved in resolved_member_models:
            if not manifests_loaded:
                manifests = _deepthink_provider_manifests()
                manifests_loaded = True
            cause = _deepthink_member_credential_cause(
                client,
                resolved,
                manifests,
                inventory,
                settings_owner=settings_owner,
            )
            if cause:
                problems.append(
                    {"code": "DEEPTHINK_MEMBER_PROVIDER_UNCONFIGURED", "cause": cause}
                )
    effective_budget, budget_problems = _deepthink_budget_report(
        spec, effective_params
    )
    for cause in budget_problems:
        problems.append({"code": "DEEPTHINK_BUDGET_INVALID", "cause": cause})
    return {
        "ok": not problems,
        "chain_id": str(spec.get("id") or ""),
        "chain_source": str(spec.get("source") or ""),
        "member_models": resolved_member_models,
        "budget": effective_budget,
        "problems": problems,
    }


def _enforce_deepthink_preflight(
    model: str,
    params: dict[str, Any],
    model_settings: dict[str, Any],
    *,
    original_model: str = "",
    settings_owner: SettingsOwnerPort | None = None,
    messages: Any = None,
    tools: Any = None,
) -> dict[str, Any] | None:
    """Fail closed when DeepThink is requested for a selection that cannot
    run the review chain, or whose resolved chain lacks credentials/budget.

    Returns the readiness report (for request-context observability) when the
    chain is runnable, ``None`` when DeepThink is off.
    """
    if not rumi_process.deepthink_enabled(params):
        return None
    client = _deepthink_resolution_client()
    if client is None:
        raise DeepThinkPreflightError(
            model=str(model or ""),
            original_model=str(original_model or ""),
            cause="could not inspect the local provider registry",
            fix=_DEEPTHINK_REVIEW_CHAIN_FIX,
        )
    spec, cause = _deepthink_chain_spec(
        str(model or ""), client=client, settings_owner=settings_owner
    )
    if cause:
        raise DeepThinkPreflightError(
            model=str(model or ""),
            original_model=str(original_model or ""),
            cause=cause,
            fix=_DEEPTHINK_REVIEW_CHAIN_FIX,
        )
    if spec is None:
        return None
    report = _deepthink_readiness_report(
        spec,
        params if isinstance(params, dict) else {},
        model=str(model or ""),
        settings_owner=settings_owner,
        client=client,
        messages=messages,
        tools=tools,
    )
    if not report.get("ok"):
        problems = report.get("problems") if isinstance(report.get("problems"), list) else []
        first = problems[0] if problems else {}
        raise DeepThinkReadinessError(
            model=str(model or ""),
            original_model=str(original_model or ""),
            code=str(first.get("code") or ""),
            cause=str(first.get("cause") or "review chain readiness check failed"),
            fix=_DEEPTHINK_READINESS_FIX,
            member_models=report.get("member_models"),
            details={
                "chain_id": report.get("chain_id"),
                "chain_source": report.get("chain_source"),
                "budget": report.get("budget"),
                "problems": problems,
            },
        )
    return {"deepthink": report}


def saved_deepthink_preflight_report(
    model: str,
    *,
    messages: Any = None,
    tools: Any = None,
    settings_owner: SettingsOwnerPort | None = None,
) -> dict[str, Any]:
    """Host-side DeepThink gate for the sandboxed saved-turn path.

    The saved request carries only ``deepthink_enabled``; the chain selection
    is the conversation owner record's ``model_reference``, and the Host — not
    the PackVM guest — owns this check because member credentials and pack
    specifications are visible only here.  Returns the readiness report or
    raises ``DeepThinkPreflightError``/``DeepThinkReadinessError`` with the
    structured ``to_dict()`` payload.
    """
    report_holder = _enforce_deepthink_preflight(
        str(model or ""),
        {"deepthink_enabled": True},
        {},
        settings_owner=settings_owner,
        messages=messages,
        tools=tools,
    )
    report = (report_holder or {}).get("deepthink")
    if not isinstance(report, dict):
        raise DeepThinkPreflightError(
            model=str(model or ""),
            cause="could not resolve the review chain for the saved selection",
            fix=_DEEPTHINK_REVIEW_CHAIN_FIX,
        )
    return dict(report)
