"""Renderer-neutral Surface Template contracts and resolution.

Surface Templates are deliberately a small semantic boundary between Pack v4
operations and a renderer.  This module never imports React, Tauri, a DOM
implementation, a shell command, or an HTTP client.  It validates a bounded
selector language, resolves only selected and Host-pinned Packs, and projects
typed operation events into canonical intents.  ``RecordingSurface`` is the
conformance renderer used by tests; a renderer adapter can consume the same
``SurfaceIntent`` values without changing a template.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.errors import ProtocolError, SchemaValidationError
from tobkiri_protocol.validation import validate_document

try:
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover - the runtime dependency is declared.
    Draft202012Validator = None  # type: ignore[assignment,misc]


SURFACE_TEMPLATE_API_VERSION = "io.tobkiri.surface-template.v1"
SURFACE_RENDERER_API_VERSION = "io.tobkiri.surface-renderer.v1"

PATTERNS = (
    "content",
    "problem",
    "notice",
    "progress",
    "resource_input",
    "choice",
    "form",
    "confirmation",
    "collection",
    "detail",
)
RESOURCE_KINDS = ("file", "image", "conversation", "artifact", "text", "uri")
RESOURCE_EFFECTS = (
    "attach",
    "inspect",
    "import",
    "reference",
    "compare",
    "open",
    "invoke",
)
_INTERACTIVE_PATTERNS = frozenset({"resource_input", "choice", "form", "confirmation"})
_SECURITY_SENSITIVE_PATTERNS = frozenset({"confirmation"})
_FALLBACK_PATTERNS = frozenset({"content", "problem", "notice", "progress", "collection", "detail"})


class SurfaceTemplateError(ValueError):
    """Base class for fail-closed Surface Template errors."""


class SurfaceTemplateValidationError(SurfaceTemplateError):
    """A Surface Template document is malformed or unsafe."""


class SurfaceResolutionError(SurfaceTemplateError):
    """A Surface Template resolution cannot be used safely."""


class SurfaceActionDenied(SurfaceResolutionError):
    """A Surface action was not admitted by the Host operation boundary."""


class SurfaceAuthorityDenied(SurfaceActionDenied):
    """The Host denied an operation requiring Authority."""


class SurfaceResourceDenied(SurfaceActionDenied):
    """A resource input was not a Host-issued opaque reference."""


@dataclass(frozen=True)
class SurfaceDiagnostic:
    """A deterministic diagnostic emitted while resolving a surface."""

    code: str
    severity: str
    message: str
    subject: str | None = None
    pack_id: str | None = None
    template_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return the JSON-safe diagnostic projection."""

        return asdict(self)


@dataclass(frozen=True)
class PatternCapability:
    """Semantic pattern metadata advertised to renderer providers."""

    pattern: str
    interactive: bool
    security_sensitive: bool
    fallback_allowed: bool


DEFAULT_PATTERN_REGISTRY: Mapping[str, PatternCapability] = {
    pattern: PatternCapability(
        pattern=pattern,
        interactive=pattern in _INTERACTIVE_PATTERNS,
        security_sensitive=pattern in _SECURITY_SENSITIVE_PATTERNS,
        fallback_allowed=pattern in _FALLBACK_PATTERNS,
    )
    for pattern in PATTERNS
}


class PatternRegistry:
    """Registry of the finite semantic patterns supported by the platform."""

    def __init__(
        self,
        capabilities: Mapping[str, PatternCapability] = DEFAULT_PATTERN_REGISTRY,
    ) -> None:
        self._capabilities = dict(capabilities)

    def get(self, pattern: str) -> PatternCapability | None:
        """Return a pattern capability, or ``None`` for an unknown pattern."""

        return self._capabilities.get(str(pattern))

    def require(self, pattern: str) -> PatternCapability:
        """Return a known pattern or raise a deterministic error."""

        capability = self.get(pattern)
        if capability is None:
            raise SurfaceTemplateValidationError(f"unknown surface pattern: {pattern}")
        return capability

    def supported(self) -> tuple[str, ...]:
        """Return the sorted finite pattern set."""

        return tuple(sorted(self._capabilities))


@dataclass(frozen=True)
class OperationBinding:
    """Host-registered identity and safety metadata for one operation."""

    contract_id: str
    operation_id: str
    contract_revision_digest: str | None = None
    input_schema_digest: str | None = None
    output_schema_digest: str | None = None
    error_schema_digest: str | None = None
    progress_schema_digest: str | None = None
    input_schema: Mapping[str, Any] | None = None
    effect_ceiling: tuple[str, ...] = ()
    authority_required: bool = False
    resource_kinds: tuple[str, ...] = ()
    source_pack_id: str | None = None

    @property
    def identity(self) -> tuple[str, str]:
        """Return the exact Contract/Operation identity."""

        return self.contract_id, self.operation_id


class OperationRegistry:
    """Finite Host registry used to authenticate template bindings/actions."""

    def __init__(self, operations: Iterable[OperationBinding] = ()) -> None:
        self._operations: dict[tuple[str, str], OperationBinding] = {}
        for operation in operations:
            self.register(operation)

    def register(self, operation: OperationBinding) -> None:
        """Register one operation and reject identity collisions."""

        key = operation.identity
        if key in self._operations:
            raise SurfaceResolutionError(
                f"operation collision: {operation.contract_id}/{operation.operation_id}"
            )
        self._operations[key] = operation

    def get(self, contract_id: str, operation_id: str) -> OperationBinding | None:
        """Return one exact registered operation."""

        return self._operations.get((str(contract_id), str(operation_id)))

    def require(self, contract_id: str, operation_id: str) -> OperationBinding:
        """Return one exact operation or fail closed."""

        operation = self.get(contract_id, operation_id)
        if operation is None:
            raise SurfaceActionDenied(
                "operation is not registered by the selected Logic Pack: "
                f"{contract_id}/{operation_id}"
            )
        return operation

    def identities(self) -> tuple[tuple[str, str], ...]:
        """Return registered identities in deterministic order."""

        return tuple(sorted(self._operations))

    @classmethod
    def from_packs(cls, packs: Iterable["SurfacePack"]) -> "OperationRegistry":
        """Build an operation registry from selected Logic Pack metadata."""

        registry = cls()
        for pack in packs:
            if pack.role not in {"logic", "mixed"}:
                continue
            for operation in _operations_from_pack(pack):
                registry.register(operation)
        return registry


@dataclass(frozen=True)
class SurfaceTemplate:
    """Validated canonical template plus Host-attested source identity."""

    document: Mapping[str, Any]
    source_pack_id: str
    source_pack_digest: str
    template_digest: str
    trust_class: str = "untrusted"

    @property
    def template_id(self) -> str:
        """Return the public template identity."""

        return str(self.document["template_id"])

    @property
    def version(self) -> str:
        """Return the template semantic version."""

        return str(self.document["version"])

    @property
    def contract_id(self) -> str:
        """Return the bound Contract identity."""

        return str(self.document["binds"]["contract_id"])

    @property
    def operation_id(self) -> str:
        """Return the bound operation identity."""

        return str(self.document["binds"]["operation_id"])

    @property
    def patterns(self) -> tuple[str, ...]:
        """Return all semantic patterns used by this template."""

        input_pattern = str(self.document["input"]["pattern"])
        outcomes = self.document.get("outcomes", {})
        outcome_patterns = (
            str(value["pattern"])
            for value in outcomes.values()
            if isinstance(value, Mapping) and "pattern" in value
        )
        return tuple(dict.fromkeys((input_pattern, *outcome_patterns)))

    @property
    def actions(self) -> tuple[Mapping[str, Any], ...]:
        """Return declarative operation actions."""

        raw = self.document.get("actions", ())
        return tuple(raw) if isinstance(raw, list) else ()

    def to_dict(self) -> dict[str, Any]:
        """Return a canonical document projection with source provenance."""

        return {
            **dict(self.document),
            "source": {
                "pack_id": self.source_pack_id,
                "pack_artifact_digest": self.source_pack_digest,
                "template_digest": self.template_digest,
                "trust_class": self.trust_class,
            },
        }


@dataclass(frozen=True)
class RendererProvider:
    """A renderer capability provider selected by pattern support."""

    renderer_id: str
    renderer_api_version: str
    supported_patterns: tuple[str, ...]
    renderer_digest: str
    source_pack_id: str
    trusted: bool = False
    enabled: bool = True
    version: str = "0.0.0"

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        source_pack_id: str | None = None,
        source_pack_digest: str | None = None,
    ) -> "RendererProvider":
        """Parse a renderer provider descriptor without executable fields."""

        renderer_id = _required_text(value.get("renderer_id") or value.get("id"), "renderer_id")
        api_version = str(value.get("renderer_api_version") or SURFACE_RENDERER_API_VERSION)
        patterns = value.get("supported_patterns") or value.get("patterns")
        if not isinstance(patterns, (list, tuple)):
            raise SurfaceResolutionError("renderer supported_patterns must be an array")
        digest = str(
            value.get("renderer_digest") or value.get("artifact_digest") or source_pack_digest or ""
        )
        if not _is_digest(digest):
            raise SurfaceResolutionError("renderer_digest must be a sha256 digest")
        return cls(
            renderer_id=renderer_id,
            renderer_api_version=api_version,
            supported_patterns=tuple(dict.fromkeys(str(item) for item in patterns)),
            renderer_digest=digest,
            source_pack_id=str(source_pack_id or value.get("source_pack_id") or ""),
            trusted=bool(value.get("trusted", value.get("trust_class") == "system")),
            enabled=bool(value.get("enabled", True)),
            version=str(value.get("version") or "0.0.0"),
        )


@dataclass(frozen=True)
class SurfacePack:
    """Selected Pack metadata projected for surface resolution."""

    pack_id: str
    artifact_digest: str
    role: str
    templates: tuple[Mapping[str, Any], ...] = ()
    renderers: tuple[Mapping[str, Any], ...] = ()
    operations: tuple[Mapping[str, Any], ...] = ()
    enabled: bool = True
    installed: bool = True
    approved: bool = True
    trust_class: str = "untrusted"
    version: str = "0.0.0"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SurfacePack":
        """Parse a v4 Pack projection or a test fixture Pack mapping."""

        nested_pack = value.get("pack")
        pack_data = nested_pack if isinstance(nested_pack, Mapping) else value
        pack_id = _required_text(value.get("pack_id") or pack_data.get("id"), "pack_id")
        digest = str(value.get("artifact_digest") or pack_data.get("artifact_digest") or "")
        if not _is_digest(digest):
            raise SurfaceResolutionError(f"{pack_id}: artifact_digest is invalid")
        raw_templates = value.get("templates") or value.get("surface_templates") or ()
        raw_renderers = value.get("renderers") or value.get("renderer_providers") or ()
        raw_operations = value.get("operations") or value.get("operation_catalog") or ()
        templates = _mapping_items(raw_templates, "templates", pack_id)
        renderers = _mapping_items(raw_renderers, "renderers", pack_id)
        operations = _mapping_items(raw_operations, "operations", pack_id)
        declared_role = (
            str(value.get("role") or value.get("surface_role") or value.get("pack_role") or "")
            .strip()
            .lower()
        )
        if not declared_role:
            if templates:
                declared_role = "surface"
            elif renderers:
                declared_role = "renderer"
            elif operations:
                declared_role = "logic"
            else:
                declared_role = "logic"
        if declared_role not in {"logic", "surface", "renderer", "mixed"}:
            raise SurfaceResolutionError(f"{pack_id}: unsupported Surface Pack role")
        lifecycle = value.get("lifecycle")
        lifecycle = lifecycle if isinstance(lifecycle, Mapping) else {}
        state = str(lifecycle.get("state") or value.get("state") or "enabled").lower()
        approval = value.get("approval_state")
        if approval is None:
            approval = value.get("approval")
        if isinstance(approval, Mapping):
            approval = approval.get("state")
        approved = bool(value.get("approved", approval in {None, "verified", "approved"}))
        return cls(
            pack_id=pack_id,
            artifact_digest=digest,
            role=declared_role,
            templates=templates,
            renderers=renderers,
            operations=operations,
            enabled=bool(value.get("enabled", True)) and state not in {"disabled", "uninstalled"},
            installed=bool(value.get("installed", True)) and state != "uninstalled",
            approved=approved,
            trust_class=str(value.get("trust_class") or value.get("trust") or "untrusted"),
            version=str(value.get("version") or pack_data.get("version") or "0.0.0"),
        )


@dataclass(frozen=True)
class SurfaceTemplatePin:
    """Exact template identity projected into a resolved surface lock."""

    template_id: str
    version: str
    template_digest: str
    source_pack_id: str
    source_pack_digest: str


@dataclass(frozen=True)
class RendererPin:
    """Exact renderer identity projected into a resolved surface lock."""

    renderer_id: str
    renderer_api_version: str
    renderer_digest: str
    source_pack_id: str
    supported_patterns: tuple[str, ...]


@dataclass(frozen=True)
class SurfaceResolution:
    """Immutable selected template/renderer projection for one Profile."""

    surface_api_version: str
    profile_id: str
    profile_revision: str
    plan_digest: str
    template_pins: tuple[SurfaceTemplatePin, ...]
    renderer_pins: tuple[RendererPin, ...]
    templates: tuple[SurfaceTemplate, ...]
    renderers: tuple[RendererProvider, ...]
    diagnostics: tuple[SurfaceDiagnostic, ...] = ()
    fallback_patterns: tuple[str, ...] = ()

    @property
    def activation_allowed(self) -> bool:
        """Return whether this projection is safe to activate."""

        return not any(item.severity == "error" for item in self.diagnostics)

    @property
    def resolution_digest(self) -> str:
        """Return a digest of all identity-bearing resolution fields."""

        return canonical_digest(
            {
                "surface_api_version": self.surface_api_version,
                "profile_id": self.profile_id,
                "profile_revision": self.profile_revision,
                "plan_digest": self.plan_digest,
                "template_pins": [asdict(item) for item in self.template_pins],
                "renderer_pins": [
                    {
                        **asdict(item),
                        "supported_patterns": list(item.supported_patterns),
                    }
                    for item in self.renderer_pins
                ],
                "fallback_patterns": list(self.fallback_patterns),
            }
        )

    def require_active(self) -> None:
        """Raise when diagnostics make this resolution unsafe to use."""

        if not self.activation_allowed:
            first = next(item for item in self.diagnostics if item.severity == "error")
            raise SurfaceResolutionError(f"{first.code}: {first.message}")

    def require_binding(self, *, profile_revision: str, plan_digest: str) -> None:
        """Reject a renderer that is using a stale Profile/ResolvedPlan lock."""

        if self.profile_revision != str(profile_revision) or self.plan_digest != str(plan_digest):
            raise SurfaceResolutionError("STALE_PROFILE_SURFACE_BINDING")

    def template(self, template_id: str) -> SurfaceTemplate:
        """Return one exact template from the resolved projection."""

        matches = [item for item in self.templates if item.template_id == template_id]
        if len(matches) != 1:
            raise SurfaceResolutionError(f"template is absent or ambiguous: {template_id}")
        return matches[0]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe profile/lock projection."""

        return {
            "surface_api_version": self.surface_api_version,
            "profile": {
                "profile_id": self.profile_id,
                "profile_revision": self.profile_revision,
                "plan_digest": self.plan_digest,
            },
            "profile_lock": {
                "plan_digest": self.plan_digest,
                "surface_resolution_digest": self.resolution_digest,
                "template_pins": [asdict(item) for item in self.template_pins],
                "renderer_pins": [
                    {
                        **asdict(item),
                        "supported_patterns": list(item.supported_patterns),
                    }
                    for item in self.renderer_pins
                ],
            },
            "templates": [item.to_dict() for item in self.templates],
            "renderers": [_json_safe(asdict(item)) for item in self.renderers],
            "fallback_patterns": list(self.fallback_patterns),
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "activation_allowed": self.activation_allowed,
        }


@dataclass(frozen=True)
class SurfaceIntent:
    """One renderer-neutral semantic intent."""

    template_id: str
    pattern: str
    payload: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical transcript shape."""

        return {
            "template_id": self.template_id,
            "pattern": self.pattern,
            "payload": _json_safe(dict(self.payload)),
        }


AuthorityChecker = Callable[[OperationBinding, Mapping[str, Any]], bool]
ResourceExchange = Callable[[Any], Any]


class SurfaceRenderer:
    """Common renderer adapter that consumes canonical intents."""

    def __init__(
        self,
        resolution: SurfaceResolution,
        *,
        operation_registry: OperationRegistry | None = None,
        authority_checker: AuthorityChecker | None = None,
        resource_exchange: ResourceExchange | None = None,
    ) -> None:
        self.resolution = resolution
        self.operation_registry = operation_registry or OperationRegistry()
        self.authority_checker = authority_checker
        self.resource_exchange = resource_exchange
        self._notice_keys: set[str] = set()

    def process(self, template_id: str, event: Mapping[str, Any]) -> tuple[SurfaceIntent, ...]:
        """Map one typed operation event into semantic intents."""

        self.resolution.require_active()
        template = self.resolution.template(template_id)
        if not isinstance(event, Mapping):
            raise SurfaceResolutionError("surface event must be an object")
        event_kind = _event_kind(event)
        outcome_key = _outcome_key(event_kind)
        outcome = template.document.get("outcomes", {}).get(outcome_key)
        if not isinstance(outcome, Mapping):
            raise SurfaceResolutionError(
                f"template {template_id} does not map operation event {event_kind!r}"
            )
        pattern = str(outcome["pattern"])
        payload = _project_outcome(outcome, event)
        if pattern == "notice":
            dedupe = payload.get("dedupe_key")
            if isinstance(dedupe, str) and dedupe:
                if dedupe in self._notice_keys:
                    return ()
                self._notice_keys.add(dedupe)
        return (SurfaceIntent(template_id, pattern, payload),)

    def render(self, template_id: str, event: Mapping[str, Any]) -> tuple[SurfaceIntent, ...]:
        """Render one event; alias shared by conformance adapters."""

        return self.process(template_id, event)

    def input_intent(
        self,
        template_id: str,
        value: Mapping[str, Any],
    ) -> SurfaceIntent:
        """Project a semantic input pattern after Host resource validation."""

        self.resolution.require_active()
        template = self.resolution.template(template_id)
        input_spec = template.document["input"]
        pattern = str(input_spec["pattern"])
        if pattern == "resource_input":
            resource_selector = str(input_spec.get("bind_to") or "$")
            resource = resolve_selector(resource_selector, value)
            accepts = input_spec.get("accepts")
            accepted_kinds = (
                tuple(str(item) for item in accepts.get("resource_kinds", ()))
                if isinstance(accepts, Mapping)
                else ()
            )
            payload = {"resource": self._normalize_input_resource(resource, accepted_kinds)}
        else:
            payload = {"value": _json_safe(dict(value))}
        return SurfaceIntent(template_id, pattern, payload)

    def _normalize_input_resource(
        self,
        value: Any,
        accepted_kinds: Sequence[str],
    ) -> str:
        """Exchange renderer input with the Host before accepting a handle."""

        if isinstance(value, str) and _RESOURCE_HANDLE_RE.fullmatch(value):
            return normalize_resource_reference(value, accepted_kinds)
        if self.resource_exchange is None:
            return normalize_resource_reference(value, accepted_kinds)
        return normalize_resource_reference(
            self.resource_exchange(value),
            accepted_kinds,
        )

    def invoke_action(
        self,
        template_id: str,
        action_index: int,
        event: Mapping[str, Any],
    ) -> SurfaceIntent:
        """Resolve one registered action through the Host authority boundary."""

        self.resolution.require_active()
        template = self.resolution.template(template_id)
        if action_index < 0 or action_index >= len(template.actions):
            raise SurfaceActionDenied("surface action index is unavailable")
        action = template.actions[action_index]
        contract_id = str(action["contract_id"])
        operation_id = str(action["operation_id"])
        operation = self.operation_registry.require(contract_id, operation_id)
        _match_operation_binding(template, operation)
        binding = action.get("payload_binding")
        if not isinstance(binding, Mapping):
            raise SurfaceActionDenied("surface action payload binding is invalid")
        payload = {
            str(key): resolve_selector(str(selector), event) for key, selector in binding.items()
        }
        _validate_action_payload(payload, operation.input_schema)
        _validate_action_resources(payload, operation.resource_kinds)
        if operation.authority_required:
            if self.authority_checker is None or not self.authority_checker(operation, payload):
                raise SurfaceAuthorityDenied(f"Host Authority denied {contract_id}/{operation_id}")
        return SurfaceIntent(
            template_id,
            "action",
            {
                "contract_id": contract_id,
                "operation_id": operation_id,
                "payload": payload,
            },
        )


class RecordingSurface(SurfaceRenderer):
    """Test-only renderer that records a canonical semantic transcript."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._transcript: list[SurfaceIntent] = []

    def process(self, template_id: str, event: Mapping[str, Any]) -> tuple[SurfaceIntent, ...]:
        """Record operation projections and return them to the caller."""

        intents = super().process(template_id, event)
        self._transcript.extend(intents)
        return intents

    def input_intent(self, template_id: str, value: Mapping[str, Any]) -> SurfaceIntent:
        """Record a semantic input projection."""

        intent = super().input_intent(template_id, value)
        self._transcript.append(intent)
        return intent

    def invoke_action(
        self,
        template_id: str,
        action_index: int,
        event: Mapping[str, Any],
    ) -> SurfaceIntent:
        """Record a Host-authorized action projection."""

        intent = super().invoke_action(template_id, action_index, event)
        self._transcript.append(intent)
        return intent

    @property
    def transcript(self) -> tuple[dict[str, Any], ...]:
        """Return immutable transcript entries."""

        return tuple(item.to_dict() for item in self._transcript)

    @property
    def transcript_digest(self) -> str:
        """Return a canonical digest of the transcript."""

        return canonical_digest(list(self.transcript))


class ReactSurface(SurfaceRenderer):
    """Renderer-neutral React adapter model.

    The actual defaultspack React layer can map ``render_model`` to components;
    this adapter only emits semantic props, ensuring it cannot introduce a
    React component name, DOM event, CSS selector, or endpoint into a template.
    """

    def render_model(
        self,
        template_id: str,
        event: Mapping[str, Any],
    ) -> tuple[dict[str, Any], ...]:
        """Return a generic model suitable for the current React renderer."""

        return tuple(
            {
                "type": intent.pattern,
                "template_id": intent.template_id,
                "props": dict(intent.payload),
            }
            for intent in self.process(template_id, event)
        )


def validate_surface_template(document: Mapping[str, Any] | str | bytes) -> dict[str, Any]:
    """Validate one canonical Surface Template and its semantic invariants."""

    try:
        validated = validate_document(document, "surface_template")
    except (ProtocolError, SchemaValidationError) as error:
        raise SurfaceTemplateValidationError(str(error)) from error
    _validate_surface_semantics(validated)
    return validated


def resolve_surface_templates(
    packs: Iterable[SurfacePack | Mapping[str, Any]],
    *,
    profile: Mapping[str, Any] | Any | None = None,
    profile_lock: Mapping[str, Any] | Any | None = None,
    selected_pack_ids: Iterable[str] | None = None,
    operation_registry: OperationRegistry | None = None,
    renderer_providers: Iterable[RendererProvider | Mapping[str, Any]] = (),
    pattern_registry: PatternRegistry | None = None,
) -> SurfaceResolution:
    """Resolve selected Surface Template/Renderer Packs fail closed.

    ``profile`` and ``profile_lock`` are intentionally read-only mappings. If
    a Profile Lock is supplied, every selected Pack must have an exact
    artifact digest in its effective set; this prevents stale templates or
    renderers from surviving a disable/update/uninstall operation.
    """

    pattern_registry = pattern_registry or PatternRegistry()
    normalized = tuple(
        item if isinstance(item, SurfacePack) else SurfacePack.from_mapping(item) for item in packs
    )
    profile_id, profile_revision, plan_digest, selected, lock_digests = _profile_binding(
        profile,
        profile_lock,
        selected_pack_ids,
    )
    diagnostics: list[SurfaceDiagnostic] = []
    by_pack: dict[str, SurfacePack] = {}
    for pack in normalized:
        if pack.pack_id in by_pack:
            diagnostics.append(
                _diag(
                    "SURFACE_PACK_COLLISION",
                    "error",
                    "Pack identity is duplicated in the selected catalog",
                    pack_id=pack.pack_id,
                )
            )
            continue
        by_pack[pack.pack_id] = pack
    selected_set = set(selected) if selected is not None else set(by_pack)
    selected_packs: list[SurfacePack] = []
    for pack_id in sorted(selected_set):
        selected_pack = by_pack.get(pack_id)
        if selected_pack is None:
            diagnostics.append(
                _diag(
                    "SURFACE_PACK_MISSING",
                    "error",
                    "Selected Pack is unavailable",
                    pack_id=pack_id,
                )
            )
            continue
        if lock_digests is not None and lock_digests.get(pack_id) != selected_pack.artifact_digest:
            diagnostics.append(
                _diag(
                    "SURFACE_PACK_DIGEST_MISMATCH",
                    "error",
                    "Selected Pack does not match the exact Profile Lock digest",
                    pack_id=pack_id,
                )
            )
            continue
        if not selected_pack.installed:
            diagnostics.append(
                _diag(
                    "SURFACE_PACK_UNINSTALLED",
                    "error",
                    "Surface Pack is not installed",
                    pack_id=pack_id,
                )
            )
            continue
        if not selected_pack.enabled:
            diagnostics.append(
                _diag(
                    "SURFACE_PACK_DISABLED",
                    "error",
                    "Surface Pack is disabled",
                    pack_id=pack_id,
                )
            )
            continue
        if not selected_pack.approved:
            diagnostics.append(
                _diag(
                    "SURFACE_PACK_UNAPPROVED",
                    "error",
                    "Surface Pack lacks Host approval",
                    pack_id=pack_id,
                )
            )
            continue
        selected_packs.append(selected_pack)

    logic_packs = tuple(item for item in selected_packs if item.role in {"logic", "mixed"})
    registry = operation_registry or OperationRegistry.from_packs(logic_packs)
    templates: list[SurfaceTemplate] = []
    for pack in selected_packs:
        if pack.role not in {"surface", "mixed"}:
            continue
        if pack.trust_class not in {"system", "local", "verified"}:
            diagnostics.append(
                _diag(
                    "SURFACE_TEMPLATE_UNTRUSTED",
                    "error",
                    "Surface Template Pack is not trusted for projection",
                    pack_id=pack.pack_id,
                )
            )
            continue
        for raw_template in pack.templates:
            try:
                document = validate_surface_template(raw_template)
                template = SurfaceTemplate(
                    document=document,
                    source_pack_id=pack.pack_id,
                    source_pack_digest=pack.artifact_digest,
                    template_digest=canonical_digest(document),
                    trust_class=pack.trust_class,
                )
                _validate_template_operation(template, registry)
                for pattern in template.patterns:
                    pattern_registry.require(pattern)
                if (
                    (
                        str(document.get("security", {}).get("class", "ordinary"))
                        in {"trusted", "sensitive"}
                        or any(pattern in _SECURITY_SENSITIVE_PATTERNS for pattern in template.patterns)
                        or any(bool(action.get("sensitive")) for action in template.actions)
                    )
                    and pack.trust_class != "system"
                ):
                    raise SurfaceTemplateValidationError(
                        "trusted or security-sensitive surfaces require a system Surface Pack"
                    )
                templates.append(template)
            except SurfaceTemplateError as error:
                diagnostics.append(
                    _diag(
                        "SURFACE_TEMPLATE_INVALID",
                        "error",
                        str(error),
                        pack_id=pack.pack_id,
                        template_id=_candidate_template_id(raw_template),
                    )
                )

    accepted_templates: list[SurfaceTemplate] = []
    by_template_id: dict[str, list[SurfaceTemplate]] = {}
    for template in templates:
        by_template_id.setdefault(template.template_id, []).append(template)
    for template_id, candidates in sorted(by_template_id.items()):
        if len(candidates) != 1:
            diagnostics.append(
                _diag(
                    "SURFACE_TEMPLATE_COLLISION",
                    "error",
                    "Public template identity is duplicated; selection is ambiguous",
                    subject=template_id,
                    template_id=template_id,
                )
            )
            continue
        accepted_templates.append(candidates[0])

    renderers = _normalize_renderers(selected_packs, renderer_providers, diagnostics)
    for renderer in renderers:
        if renderer.renderer_api_version != SURFACE_RENDERER_API_VERSION:
            diagnostics.append(
                _diag(
                    "SURFACE_RENDERER_API_VERSION_MISMATCH",
                    "warning",
                    "Renderer API version is unsupported by this Host",
                    subject=renderer.renderer_id,
                    pack_id=renderer.source_pack_id or None,
                )
            )
    required_patterns = tuple(
        sorted({pattern for template in accepted_templates for pattern in template.patterns})
    )
    selected_renderers: list[RendererProvider] = []
    fallback: list[str] = []
    renderer_pins: list[RendererPin] = []
    for pattern in required_patterns:
        renderer_candidates = [
            item
            for item in renderers
            if item.enabled
            and item.renderer_api_version == SURFACE_RENDERER_API_VERSION
            and pattern in item.supported_patterns
        ]
        if len(renderer_candidates) > 1:
            diagnostics.append(
                _diag(
                    "SURFACE_RENDERER_COLLISION",
                    "error",
                    "More than one renderer claims the same semantic capability",
                    subject=pattern,
                )
            )
            continue
        if len(renderer_candidates) == 1:
            candidate = renderer_candidates[0]
            if not candidate.trusted and pattern in _SECURITY_SENSITIVE_PATTERNS:
                diagnostics.append(
                    _diag(
                        "SURFACE_RENDERER_UNTRUSTED",
                        "error",
                        "Security-sensitive pattern requires a trusted renderer",
                        subject=pattern,
                    )
                )
                continue
            if candidate not in selected_renderers:
                selected_renderers.append(candidate)
                renderer_pins.append(
                    RendererPin(
                        renderer_id=candidate.renderer_id,
                        renderer_api_version=candidate.renderer_api_version,
                        renderer_digest=candidate.renderer_digest,
                        source_pack_id=candidate.source_pack_id,
                        supported_patterns=candidate.supported_patterns,
                    )
                )
            continue
        capability = pattern_registry.require(pattern)
        if capability.fallback_allowed:
            fallback.append(pattern)
        else:
            diagnostics.append(
                _diag(
                    "SURFACE_RENDERER_MISSING",
                    "error",
                    "No renderer supports the required interactive or security-sensitive pattern",
                    subject=pattern,
                )
            )

    template_pins = tuple(
        SurfaceTemplatePin(
            template_id=item.template_id,
            version=item.version,
            template_digest=item.template_digest,
            source_pack_id=item.source_pack_id,
            source_pack_digest=item.source_pack_digest,
        )
        for item in sorted(accepted_templates, key=lambda value: value.template_id)
    )
    return SurfaceResolution(
        surface_api_version=SURFACE_TEMPLATE_API_VERSION,
        profile_id=profile_id,
        profile_revision=profile_revision,
        plan_digest=plan_digest,
        template_pins=template_pins,
        renderer_pins=tuple(renderer_pins),
        templates=tuple(sorted(accepted_templates, key=lambda item: item.template_id)),
        renderers=tuple(selected_renderers),
        diagnostics=tuple(diagnostics),
        fallback_patterns=tuple(sorted(set(fallback))),
    )


def discover_surface_templates(
    pack_roots: Mapping[str, Path | str],
    *,
    profile: Mapping[str, Any] | Any | None = None,
    profile_lock: Mapping[str, Any] | Any | None = None,
    selected_pack_ids: Iterable[str] | None = None,
    renderer_providers: Iterable[RendererProvider | Mapping[str, Any]] = (),
) -> SurfaceResolution:
    """Discover sidecar Surface Template documents from selected Pack roots."""

    packs: list[SurfacePack] = []
    for pack_id, raw_root in sorted(pack_roots.items()):
        root = Path(raw_root)
        manifest_path = root / "pack.v4.json"
        try:
            manifest = validate_document(manifest_path.read_bytes(), "pack")
            pack_data: dict[str, Any] = {
                "pack": manifest["pack"],
                "pack_id": pack_id,
                "artifact_digest": manifest["pack"]["artifact_digest"],
                "role": "logic",
                "trust_class": "local",
                "operations": manifest.get("operation_catalog", ()),
                "installed": True,
                "enabled": True,
                "approved": True,
            }
            template_documents = _read_surface_sidecars(root)
            if template_documents:
                pack_data["role"] = "mixed" if pack_data["operations"] else "surface"
                pack_data["templates"] = template_documents
            packs.append(SurfacePack.from_mapping(pack_data))
        except (OSError, ProtocolError, SurfaceTemplateError) as error:
            packs.append(
                SurfacePack(
                    pack_id=str(pack_id),
                    artifact_digest="sha256:" + "0" * 64,
                    role="surface",
                    templates=(),
                    enabled=False,
                    installed=False,
                    approved=False,
                )
            )
            # The resolver emits the lifecycle diagnostic for this placeholder;
            # discovery itself remains deterministic and side-effect free.
            del error
    return resolve_surface_templates(
        packs,
        profile=profile,
        profile_lock=profile_lock,
        selected_pack_ids=selected_pack_ids,
        renderer_providers=renderer_providers,
    )


def resolve_selector(selector: str, value: Mapping[str, Any] | Any) -> Any:
    """Resolve a bounded non-executable selector against one event object."""

    tokens = _parse_selector(selector)
    current: Any = value
    for token in tokens:
        if isinstance(token, int):
            if not isinstance(current, (list, tuple)) or token >= len(current):
                return None
            current = current[token]
        else:
            if not isinstance(current, Mapping):
                return None
            if token in {"__proto__", "prototype", "constructor"}:
                raise SurfaceTemplateValidationError("unsafe selector member")
            current = current.get(token)
    return current


def normalize_resource_reference(
    value: Any,
    accepted_kinds: Sequence[str] = (),
) -> str:
    """Accept only an opaque Host-issued resource handle.

    Ambient filesystem paths, ``file://`` URLs, and GUI drop metadata are
    rejected even when they are supplied by a trusted renderer. The Host must
    first exchange them for a ``handle:...`` reference.
    """

    kind: str | None = None
    if isinstance(value, Mapping):
        if set(value) - {"handle", "kind", "media_type"}:
            raise SurfaceResourceDenied("resource input contains unsupported metadata")
        kind = str(value.get("kind") or "") or None
        value = value.get("handle")
    if not isinstance(value, str) or not _RESOURCE_HANDLE_RE.fullmatch(value):
        raise SurfaceResourceDenied("resource input must be a Host-issued handle reference")
    handle_kind = value.removeprefix("handle:").split("/", 1)[0].split(":", 1)[0]
    kind = kind or handle_kind
    if kind and accepted_kinds and kind not in set(accepted_kinds):
        raise SurfaceResourceDenied("resource kind is not accepted by the template")
    return value


def normalize_resource_input(
    value: Any,
    *,
    host_exchange: ResourceExchange | None = None,
    accepted_kinds: Sequence[str] = (),
) -> str:
    """Normalize a renderer resource through a Host-owned handle exchange.

    ``host_exchange`` is intentionally called before a Pack sees the value.
    Its return value is still validated as an opaque ``handle:...`` reference,
    so a compromised Host adapter cannot accidentally pass a local path into a
    Logic Pack.
    """

    if isinstance(value, str) and _RESOURCE_HANDLE_RE.fullmatch(value):
        return normalize_resource_reference(value, accepted_kinds)
    if host_exchange is None:
        return normalize_resource_reference(value, accepted_kinds)
    return normalize_resource_reference(host_exchange(value), accepted_kinds)


def _validate_surface_semantics(document: Mapping[str, Any]) -> None:
    forbidden = {
        "react",
        "component",
        "module",
        "import",
        "callback",
        "expression",
        "html",
        "css",
        "svg",
        "endpoint",
        "http_method",
        "placement",
        "screen_top",
        "stderr",
        "toast",
        "url",
    }
    for key, _value in _walk_keys(document):
        normalized = key.casefold().replace("-", "_")
        if normalized in forbidden or any(
            fragment in normalized
            for fragment in ("callback", "endpoint", "javascript", "component_name")
        ):
            raise SurfaceTemplateValidationError(
                f"canonical Surface Template contains forbidden executable or renderer field: {key}"
            )
    input_spec = document["input"]
    pattern = str(input_spec["pattern"])
    if pattern == "resource_input":
        if "resource_kind" not in input_spec and not (
            isinstance(input_spec.get("accepts"), Mapping)
            and input_spec["accepts"].get("resource_kinds")
        ):
            raise SurfaceTemplateValidationError(
                "resource_input requires a resource kind or accepted resource kinds"
            )
        effect = input_spec.get("effect")
        if effect not in RESOURCE_EFFECTS:
            raise SurfaceTemplateValidationError("resource_input requires a semantic effect")
    for outcome_key, outcome in document.get("outcomes", {}).items():
        expected = {"error": "problem", "progress": "progress", "notice": "notice"}.get(
            str(outcome_key)
        )
        if expected and str(outcome["pattern"]) != expected:
            raise SurfaceTemplateValidationError(
                f"{outcome_key} outcome must use the {expected} pattern"
            )
    required = set(str(item) for item in document.get("required_patterns", ()))
    declared = {str(pattern) for pattern in _document_patterns(document)}
    if not required.issubset(declared):
        raise SurfaceTemplateValidationError(
            "required_patterns must be used by the template document"
        )
    _validate_selector_count(document)


def _validate_template_operation(
    template: SurfaceTemplate,
    registry: OperationRegistry,
) -> None:
    operation = registry.require(template.contract_id, template.operation_id)
    binds = template.document["binds"]
    for key in (
        "contract_revision_digest",
        "input_schema_digest",
        "output_schema_digest",
        "error_schema_digest",
        "progress_schema_digest",
    ):
        supplied = binds.get(key)
        actual = getattr(operation, key)
        if supplied is not None and actual is not None and supplied != actual:
            raise SurfaceTemplateValidationError(f"template binding {key} is stale")
    effect = template.document["input"].get("effect")
    if effect and operation.effect_ceiling:
        allowed_effects = set(operation.effect_ceiling)
        if effect not in allowed_effects and f"effect:{effect}" not in allowed_effects:
            raise SurfaceTemplateValidationError(
                "template input effect exceeds the registered operation effect ceiling"
            )


def _match_operation_binding(
    template: SurfaceTemplate,
    operation: OperationBinding,
) -> None:
    if (
        template.contract_id != operation.contract_id
        or template.operation_id != operation.operation_id
    ):
        raise SurfaceActionDenied("action operation does not match the template binding")


def _project_outcome(outcome: Mapping[str, Any], event: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in (
        "data",
        "items",
        "code",
        "message",
        "current",
        "total",
        "level",
        "dedupe_key",
        "title",
        "details",
    ):
        selector = outcome.get(key)
        if selector is not None:
            payload[key] = _json_safe(resolve_selector(str(selector), event))
    return payload


def _validate_action_resources(
    payload: Mapping[str, Any],
    resource_kinds: Sequence[str],
) -> None:
    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for raw_key, child in value.items():
                key = str(raw_key).casefold()
                if key in {"path", "local_path", "file_path"}:
                    raise SurfaceResourceDenied(
                        "action payload cannot contain local filesystem paths"
                    )
                if key in {"resource", "resource_handle", "handle"}:
                    normalize_resource_reference(child, resource_kinds)
                else:
                    visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)

    visit(payload)


def _validate_action_payload(
    payload: Mapping[str, Any],
    input_schema: Mapping[str, Any] | None,
) -> None:
    """Validate an action payload against the Host-registered input schema."""

    if input_schema is None:
        return
    if Draft202012Validator is None:  # pragma: no cover - dependency is required.
        raise SurfaceActionDenied("registered action schema validation is unavailable")
    validator = Draft202012Validator(dict(input_schema))
    errors = sorted(
        validator.iter_errors(dict(payload)),
        key=lambda error: tuple(str(item) for item in error.absolute_path),
    )
    if errors:
        first = errors[0]
        location = ".".join(str(item) for item in first.absolute_path) or "$"
        raise SurfaceActionDenied(f"action payload does not match registered schema at {location}")


def _event_kind(event: Mapping[str, Any]) -> str:
    value = event.get("kind") or event.get("status") or event.get("type")
    return str(value or "success").strip().casefold()


def _outcome_key(event_kind: str) -> str:
    if event_kind in {"success", "ok", "complete", "completed", "content"}:
        return "success"
    if event_kind in {"error", "failure", "failed", "problem"}:
        return "error"
    if event_kind in {"progress", "working", "pending"}:
        return "progress"
    if event_kind in {"notice", "info", "warning", "success_notice"}:
        return "notice"
    return event_kind


def _profile_binding(
    profile: Mapping[str, Any] | Any | None,
    profile_lock: Mapping[str, Any] | Any | None,
    selected_pack_ids: Iterable[str] | None,
) -> tuple[str, str, str, tuple[str, ...] | None, dict[str, str] | None]:
    profile_data = _as_mapping(profile)
    lock_data = _as_mapping(profile_lock)
    profile_id = str(profile_data.get("profile_id") or "unbound")
    profile_revision = str(profile_data.get("profile_revision") or "")
    plan_digest = str(
        profile_data.get("plan_digest")
        or (lock_data or {}).get("plan_digest")
        or (
            (profile_data.get("resolved_plan") or {}).get("plan_digest")
            if isinstance(profile_data.get("resolved_plan"), Mapping)
            else ""
        )
        or ""
    )
    if lock_data:
        profile_revision = str(lock_data.get("profile_revision") or profile_revision)
    explicit = tuple(
        dict.fromkeys(str(item).strip() for item in selected_pack_ids or () if str(item).strip())
    )
    selected: tuple[str, ...] | None = explicit or None
    if selected is None and profile_data:
        selected = _pack_ids_from(profile_data.get("selected_pack_ids"))
        if selected is None:
            selected = _pack_ids_from(profile_data.get("packs"))
        if selected is None:
            selected = _pack_ids_from(profile_data.get("effective_pack_set"))
    lock_digests: dict[str, str] | None = None
    if lock_data:
        lock_rows = lock_data.get("effective_set") or lock_data.get("packs")
        lock_digests = {}
        if isinstance(lock_rows, (list, tuple)):
            for row in lock_rows:
                row_map = _as_mapping(row)
                pack_id = str(row_map.get("pack_id") or row_map.get("identity") or "").strip()
                digest = str(row_map.get("artifact_digest") or "").strip()
                if pack_id and digest:
                    lock_digests[pack_id] = digest
            if selected is None:
                selected = tuple(sorted(lock_digests))
    return profile_id, profile_revision, plan_digest, selected, lock_digests


def _operations_from_pack(pack: SurfacePack) -> tuple[OperationBinding, ...]:
    operations: list[OperationBinding] = []
    for raw in pack.operations:
        contract_id = str(
            raw.get("contract_id") or raw.get("contract_reference") or raw.get("contract") or ""
        )
        operation_id = str(raw.get("operation_id") or raw.get("id") or "")
        if not contract_id or not operation_id:
            continue
        effect_ceiling = raw.get("effect_ceiling") or raw.get("effects") or ()
        resource_kinds = raw.get("resource_kinds") or ()
        operations.append(
            OperationBinding(
                contract_id=contract_id,
                operation_id=operation_id,
                contract_revision_digest=_optional_digest(
                    raw.get("contract_revision_digest") or raw.get("revision_digest")
                ),
                input_schema_digest=_optional_digest(raw.get("input_schema_digest")),
                output_schema_digest=_optional_digest(raw.get("output_schema_digest")),
                error_schema_digest=_optional_digest(raw.get("error_schema_digest")),
                progress_schema_digest=_optional_digest(raw.get("progress_schema_digest")),
                input_schema=(
                    dict(raw["input_schema"])
                    if isinstance(raw.get("input_schema"), Mapping)
                    else None
                ),
                effect_ceiling=tuple(str(item) for item in effect_ceiling),
                authority_required=bool(
                    raw.get("authority_required", raw.get("requires_authority", False))
                ),
                resource_kinds=tuple(str(item) for item in resource_kinds),
                source_pack_id=pack.pack_id,
            )
        )
    return tuple(operations)


def _normalize_renderers(
    selected_packs: Sequence[SurfacePack],
    explicit: Iterable[RendererProvider | Mapping[str, Any]],
    diagnostics: list[SurfaceDiagnostic],
) -> tuple[RendererProvider, ...]:
    values: list[RendererProvider] = []
    for pack in selected_packs:
        if pack.role not in {"renderer", "mixed"}:
            continue
        if pack.trust_class not in {"system", "local", "verified"}:
            diagnostics.append(
                _diag(
                    "SURFACE_RENDERER_UNTRUSTED",
                    "error",
                    "Renderer Pack is not trusted for projection",
                    pack_id=pack.pack_id,
                )
            )
            continue
        for raw in pack.renderers:
            try:
                values.append(
                    RendererProvider.from_mapping(
                        raw,
                        source_pack_id=pack.pack_id,
                        source_pack_digest=pack.artifact_digest,
                    )
                )
            except SurfaceTemplateError as error:
                diagnostics.append(
                    _diag(
                        "SURFACE_RENDERER_INVALID",
                        "error",
                        str(error),
                        pack_id=pack.pack_id,
                    )
                )
    for renderer_raw in explicit:
        try:
            values.append(
                renderer_raw
                if isinstance(renderer_raw, RendererProvider)
                else RendererProvider.from_mapping(renderer_raw)
            )
        except SurfaceTemplateError as error:
            diagnostics.append(_diag("SURFACE_RENDERER_INVALID", "error", str(error)))
    return tuple(values)


def _read_surface_sidecars(root: Path) -> list[Mapping[str, Any]]:
    candidates = [root / "surface_templates.v1.json", root / "surface-templates.v1.json"]
    candidates.extend(sorted((root / "surface_templates").glob("*.json")))
    documents: list[Mapping[str, Any]] = []
    for path in candidates:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, Mapping):
            rows = value.get("templates")
            if isinstance(rows, list):
                documents.extend(item for item in rows if isinstance(item, Mapping))
            elif "template_id" in value:
                documents.append(value)
        elif isinstance(value, list):
            documents.extend(item for item in value if isinstance(item, Mapping))
    return documents


def _mapping_items(value: Any, field_name: str, pack_id: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        raise SurfaceResolutionError(f"{pack_id}: {field_name} must be an array")
    return tuple(item for item in value if isinstance(item, Mapping))


def _validate_selector_count(document: Mapping[str, Any]) -> None:
    if sum(1 for _ in _walk_selectors(document)) > 256:
        raise SurfaceTemplateValidationError("Surface Template contains too many selectors")


def _document_patterns(document: Mapping[str, Any]) -> tuple[str, ...]:
    values = [str(document["input"]["pattern"])]
    values.extend(
        str(item["pattern"])
        for item in document.get("outcomes", {}).values()
        if isinstance(item, Mapping) and "pattern" in item
    )
    return tuple(values)


def _candidate_template_id(value: Any) -> str | None:
    return (
        str(value.get("template_id"))
        if isinstance(value, Mapping) and value.get("template_id")
        else None
    )


def _diag(
    code: str,
    severity: str,
    message: str,
    *,
    subject: str | None = None,
    pack_id: str | None = None,
    template_id: str | None = None,
) -> SurfaceDiagnostic:
    return SurfaceDiagnostic(code, severity, message, subject, pack_id, template_id)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        return result if isinstance(result, Mapping) else {}
    return {}


def _pack_ids_from(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, (list, tuple)):
        return None
    values: list[str] = []
    for item in value:
        mapping = _as_mapping(item)
        pack_id = str(mapping.get("pack_id") or mapping.get("identity") or item or "").strip()
        if pack_id:
            values.append(pack_id)
    return tuple(dict.fromkeys(values))


def _required_text(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise SurfaceResolutionError(f"{field_name} is required")
    return normalized


def _is_digest(value: str) -> bool:
    return bool(re.fullmatch(r"sha256:[0-9a-f]{64}", str(value)))


def _optional_digest(value: Any) -> str | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    return normalized if _is_digest(normalized) else None


_RESOURCE_HANDLE_RE = re.compile(r"^handle:[A-Za-z0-9][A-Za-z0-9._:/~-]{0,255}$")
_SELECTOR_RE = re.compile(
    r"^\$(?:(?:[A-Za-z][A-Za-z0-9_]*)|(?:\.[A-Za-z][A-Za-z0-9_]*)|(?:\[(?:0|[1-9][0-9]*)\])){1,16}$|^\$$"
)


def _parse_selector(selector: str) -> tuple[str | int, ...]:
    if not isinstance(selector, str) or not _SELECTOR_RE.fullmatch(selector):
        raise SurfaceTemplateValidationError(
            f"selector is outside the bounded path language: {selector!r}"
        )
    if selector == "$":
        return ()
    body = selector[1:]
    tokens: list[str | int] = []
    index = 0
    while index < len(body):
        if body[index] == ".":
            index += 1
            start = index
            while index < len(body) and (body[index].isalnum() or body[index] == "_"):
                index += 1
            if start == index:
                raise SurfaceTemplateValidationError("selector contains an empty member")
            tokens.append(body[start:index])
        elif body[index] == "[":
            end = body.find("]", index + 1)
            if end < 0:
                raise SurfaceTemplateValidationError("selector contains an unterminated index")
            tokens.append(int(body[index + 1 : end]))
            index = end + 1
        else:
            start = index
            while index < len(body) and (body[index].isalnum() or body[index] == "_"):
                index += 1
            if start == index:
                raise SurfaceTemplateValidationError("selector contains an invalid member")
            tokens.append(body[start:index])
    if len(tokens) > 16:
        raise SurfaceTemplateValidationError("selector exceeds the maximum depth")
    if any(
        token in {"__proto__", "prototype", "constructor"}
        for token in tokens
        if isinstance(token, str)
    ):
        raise SurfaceTemplateValidationError("selector contains a forbidden member")
    return tuple(tokens)


def _walk_keys(value: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key), child
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def _walk_selectors(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for child in value.values():
            if isinstance(child, str) and child.startswith("$"):
                yield child
            yield from _walk_selectors(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_selectors(child)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    return str(value)


class SurfaceTemplateResolver:
    """Reusable resolver facade for Host/Profile lifecycle integrations."""

    def __init__(self, packs: Iterable[SurfacePack | Mapping[str, Any]], **kwargs: Any) -> None:
        self._packs = tuple(packs)
        self._kwargs = dict(kwargs)

    def resolve(self, **overrides: Any) -> SurfaceResolution:
        """Resolve the current selected Pack snapshot."""

        options = {**self._kwargs, **overrides}
        return resolve_surface_templates(self._packs, **options)


SurfaceTemplatePack = SurfacePack
RendererPack = SurfacePack
resolve_surface = resolve_surface_templates
validate_surface_template_document = validate_surface_template


__all__ = [
    "DEFAULT_PATTERN_REGISTRY",
    "OperationBinding",
    "OperationRegistry",
    "PATTERNS",
    "PatternCapability",
    "PatternRegistry",
    "ReactSurface",
    "RecordingSurface",
    "RendererPack",
    "RendererPin",
    "RendererProvider",
    "RESOURCE_EFFECTS",
    "RESOURCE_KINDS",
    "SURFACE_RENDERER_API_VERSION",
    "SURFACE_TEMPLATE_API_VERSION",
    "SurfaceActionDenied",
    "SurfaceAuthorityDenied",
    "SurfaceDiagnostic",
    "SurfaceIntent",
    "SurfacePack",
    "SurfaceRenderer",
    "SurfaceResolution",
    "SurfaceResolutionError",
    "SurfaceResourceDenied",
    "SurfaceTemplate",
    "SurfaceTemplateError",
    "SurfaceTemplatePack",
    "SurfaceTemplatePin",
    "SurfaceTemplateResolver",
    "SurfaceTemplateValidationError",
    "discover_surface_templates",
    "normalize_resource_input",
    "normalize_resource_reference",
    "resolve_selector",
    "resolve_surface_templates",
    "resolve_surface",
    "validate_surface_template",
    "validate_surface_template_document",
]
