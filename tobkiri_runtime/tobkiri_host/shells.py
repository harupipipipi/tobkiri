"""Orthogonal Base Pack and Shell Provider resolution interfaces."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from .errors import ResolutionError
from .models import require_digest, require_identifier


class PresentationFamily(str, Enum):
    """Broad presentation family without technology-specific privilege."""

    GRAPHICAL = "graphical"
    TERMINAL = "terminal"
    HEADLESS = "headless"


@dataclass(frozen=True)
class BaseDefinition:
    """Capability, policy, and dependency foundation for Profile v4."""

    pack_id: str
    artifact_digest: str
    definition_revision: str
    policy_digest: str
    dependency_artifacts: tuple[tuple[str, str], ...]
    required_shell_capabilities: frozenset[str]
    permitted_families: frozenset[PresentationFamily] = frozenset({PresentationFamily.GRAPHICAL})

    def __post_init__(self) -> None:
        require_identifier(self.pack_id, "base pack_id")
        require_digest(self.artifact_digest, "base artifact")
        require_digest(self.definition_revision, "base definition revision")
        require_digest(self.policy_digest, "base policy")
        dependency_ids: set[str] = set()
        for pack_id, artifact_digest in self.dependency_artifacts:
            require_identifier(pack_id, "base dependency pack_id")
            require_digest(artifact_digest, "base dependency artifact")
            if pack_id in dependency_ids:
                raise ResolutionError("Base dependencies contain a duplicate Pack")
            dependency_ids.add(pack_id)


@dataclass(frozen=True)
class ShellDefinition:
    """Exact presentation-only ``app.shell.v1`` Provider binding."""

    provider_id: str
    pack_id: str
    artifact_digest: str
    definition_revision: str
    contract_id: str
    family: PresentationFamily
    capabilities: frozenset[str]
    local_auth_protocol: str
    local_auth_audience: str
    technology: str | None = None

    def __post_init__(self) -> None:
        require_identifier(self.provider_id, "shell provider_id")
        require_identifier(self.pack_id, "shell pack_id")
        require_digest(self.artifact_digest, "shell artifact")
        require_digest(self.definition_revision, "shell definition revision")
        if self.contract_id != "app.shell.v1":
            raise ResolutionError("Shell must implement app.shell.v1")
        if self.local_auth_protocol != "io.tobkiri.local-auth.v1":
            raise ResolutionError("Shell must use the local-auth v1 handoff")
        if self.local_auth_audience != "runtime-profile":
            raise ResolutionError("Shell local-auth audience must be runtime-profile")


@dataclass(frozen=True)
class PresentationContribution:
    """Pinned UI or CLI contribution metadata, never injected native code."""

    contribution_id: str
    artifact_digest: str
    contract_id: str
    family: PresentationFamily


@dataclass(frozen=True)
class BaseShellBinding:
    """Exact independently pinned Base/Shell pair and selected contributions."""

    base: BaseDefinition
    shell: ShellDefinition
    contributions: tuple[PresentationContribution, ...]
    binding_revision: str


class BaseShellResolver:
    """Resolve compatibility without Runtime technology enum branches."""

    def resolve(
        self,
        base: BaseDefinition,
        shell: ShellDefinition,
        contributions: Sequence[PresentationContribution],
    ) -> BaseShellBinding:
        """Reject incompatible Shells and filter unselected contribution families."""
        if shell.family not in base.permitted_families:
            raise ResolutionError("Shell presentation family is not permitted")
        missing = base.required_shell_capabilities - shell.capabilities
        if missing:
            raise ResolutionError(f"Shell capabilities are missing: {sorted(missing)}")
        selected = tuple(
            sorted(
                (
                    contribution
                    for contribution in contributions
                    if contribution.family is shell.family
                ),
                key=lambda contribution: contribution.contribution_id,
            )
        )
        from tobkiri_protocol.canonical import canonical_digest

        binding_revision = canonical_digest(
            {
                "base": {
                    "pack_id": base.pack_id,
                    "artifact_digest": base.artifact_digest,
                    "definition_revision": base.definition_revision,
                },
                "shell": {
                    "provider_id": shell.provider_id,
                    "pack_id": shell.pack_id,
                    "artifact_digest": shell.artifact_digest,
                    "definition_revision": shell.definition_revision,
                    "contract_id": shell.contract_id,
                },
                "contributions": [
                    {
                        "contribution_id": item.contribution_id,
                        "artifact_digest": item.artifact_digest,
                    }
                    for item in selected
                ],
            }
        )
        return BaseShellBinding(
            base=base,
            shell=shell,
            contributions=selected,
            binding_revision=binding_revision,
        )
