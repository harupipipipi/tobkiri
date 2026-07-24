"""Discovery and registry for Capability Graph profile definitions."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml  # type: ignore[import-untyped]

from .interface_registry import InterfaceRegistry
from .profile_models import (
    ProfileDefinition,
    ProfileValidationError,
    load_profile_document,
)

logger = logging.getLogger(__name__)


class ProfileDiscoveryError(RuntimeError):
    """Raised when profile discovery finds invalid definitions."""


class CapabilityProfileLoader:
    """Load user-shared and approved pack-provided Capability Graph profiles."""

    def __init__(
        self,
        *,
        registry: Any = None,
        interface_registry: Optional[InterfaceRegistry] = None,
        approval_manager: Any = None,
        ecosystem_dir: Optional[str] = None,
        shared_profiles_dir: Optional[str | Path] = None,
        effective_pack_ids: Optional[Iterable[str]] = None,
        continue_on_invalid: bool = False,
    ) -> None:
        base_dir = Path(__file__).resolve().parent.parent
        self.registry = registry
        self.interface_registry = interface_registry
        self.approval_manager = approval_manager
        self.ecosystem_dir = ecosystem_dir
        self.effective_pack_ids = (
            frozenset(str(item) for item in effective_pack_ids)
            if effective_pack_ids is not None
            else None
        )
        self.continue_on_invalid = continue_on_invalid
        self.shared_profiles_dir = (
            Path(shared_profiles_dir)
            if shared_profiles_dir is not None
            else base_dir / "user_data" / "shared" / "profiles"
        )
        self.profiles: Dict[str, ProfileDefinition] = {}
        self.diagnostics: List[Dict[str, Any]] = []

    def load_all_profiles(self, *, register: bool = True) -> Dict[str, ProfileDefinition]:
        self.profiles = {}
        self.diagnostics = []

        for profile_file in self._discover_user_profile_files():
            self._load_profile_file(
                profile_file,
                pack_id=None,
                source_type="user",
                register=register,
                allow_override=True,
            )

        for pack_id, pack_info in self._iter_packs():
            ok, reason = self._is_pack_approved(pack_id)
            if not ok:
                self._diagnose(
                    "warning",
                    "pack_skipped_unapproved",
                    f"Pack '{pack_id}' is not approved or hash-verified: {reason}",
                    pack_id=pack_id,
                    reason=reason,
                )
                continue
            for profile_file in self._discover_pack_profile_files(pack_info):
                self._load_profile_file(
                    profile_file,
                    pack_id=pack_id,
                    source_type="ecosystem",
                    register=register,
                    allow_override=False,
                )

        return dict(self.profiles)

    def list_profiles(self) -> List[ProfileDefinition]:
        if not self.profiles:
            self.load_all_profiles()
        return [self.profiles[profile_id] for profile_id in sorted(self.profiles)]

    def get_profile(self, profile_id: str) -> Optional[ProfileDefinition]:
        if not self.profiles:
            self.load_all_profiles()
        return self.profiles.get(profile_id)

    def to_public_list(self) -> List[Dict[str, Any]]:
        return [profile.to_dict() for profile in self.list_profiles()]

    def _load_profile_file(
        self,
        profile_file: Path,
        *,
        pack_id: Optional[str],
        source_type: str,
        register: bool,
        allow_override: bool,
    ) -> None:
        try:
            with profile_file.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            profile = load_profile_document(
                data,
                source_path=str(profile_file),
                pack_id=pack_id,
                source_type=source_type,
            )
            self._register_profile(profile, register=register, allow_override=allow_override)
        except (OSError, yaml.YAMLError, ProfileValidationError) as exc:
            self._diagnose(
                "error",
                "invalid_profile_file",
                str(exc),
                pack_id=pack_id,
                path=str(profile_file),
            )
            if not self.continue_on_invalid:
                raise ProfileDiscoveryError(f"{profile_file}: {exc}") from exc

    def _register_profile(
        self,
        profile: ProfileDefinition,
        *,
        register: bool,
        allow_override: bool,
    ) -> None:
        if profile.profile_id in self.profiles:
            existing = self.profiles[profile.profile_id]
            if existing.metadata.get("source_type") == "user" and profile.metadata.get("source_type") == "ecosystem":
                self._diagnose(
                    "warning",
                    "profile_skipped_user_override",
                    f"Profile '{profile.profile_id}' from pack is shadowed by a user shared profile",
                    profile_id=profile.profile_id,
                    path=str(profile.metadata.get("source_path") or ""),
                )
                return
            if allow_override and existing.metadata.get("source_type") != profile.metadata.get("source_type"):
                self.profiles[profile.profile_id] = profile
                return
            raise ProfileDiscoveryError(
                "duplicate profile_id '{}': {} conflicts with {}".format(
                    profile.profile_id,
                    profile.metadata.get("source_path") or "new profile",
                    existing.metadata.get("source_path") or "existing profile",
                )
            )
        self.profiles[profile.profile_id] = profile
        if register and self.interface_registry is not None:
            self.interface_registry.register(
                f"profile.{profile.profile_id}",
                profile,
                meta={
                    "source": profile.metadata.get("source_type"),
                    "pack_id": profile.metadata.get("pack_id"),
                    "_system": profile.metadata.get("source_type") == "user",
                },
            )

    def _discover_user_profile_files(self) -> List[Path]:
        if not self.shared_profiles_dir.is_dir():
            return []
        return sorted(self.shared_profiles_dir.glob("*.profile.yaml"))

    def _discover_pack_profile_files(self, pack_info: Any) -> List[Path]:
        pack_subdir = (
            getattr(pack_info, "pack_subdir", None)
            or getattr(pack_info, "subdir", None)
            or getattr(pack_info, "path", None)
        )
        if pack_subdir is None:
            return []
        profiles_dir = Path(pack_subdir) / "profiles"
        if not profiles_dir.is_dir():
            return []
        return sorted(profiles_dir.glob("*.profile.yaml"))

    def _iter_packs(self) -> Iterable[Tuple[str, Any]]:
        registry = self.registry or self._load_registry()
        discovered: Dict[str, Any] = {}
        packs = getattr(registry, "packs", None)
        if isinstance(packs, dict):
            discovered.update(packs)
        for loc in self._discover_installed_packs():
            discovered.setdefault(loc.pack_id, loc)
        return [
            (pack_id, pack_info)
            for pack_id, pack_info in discovered.items()
            if self.effective_pack_ids is None
            or pack_id in self.effective_pack_ids
        ]

    def _discover_installed_packs(self) -> List[Any]:
        from .paths import discover_pack_locations, resolve_pack_locations

        try:
            if self.effective_pack_ids is not None:
                return list(
                    resolve_pack_locations(
                        self.effective_pack_ids,
                        self.ecosystem_dir,
                    )
                )
            return list(discover_pack_locations(self.ecosystem_dir))
        except Exception:
            return []

    def _load_registry(self) -> Any:
        from .paths import discover_pack_locations, resolve_pack_locations

        locations = (
            resolve_pack_locations(self.effective_pack_ids, self.ecosystem_dir)
            if self.effective_pack_ids is not None
            else discover_pack_locations(self.ecosystem_dir)
        )
        registry = type(
            "DiscoveredPackRegistry",
            (),
            {"packs": {loc.pack_id: loc for loc in locations}},
        )()
        self.registry = registry
        return registry

    def _approval_manager(self) -> Any:
        if self.approval_manager is not None:
            return self.approval_manager
        try:
            from .approval_manager import get_approval_manager

            self.approval_manager = get_approval_manager()
        except Exception:
            self.approval_manager = None
        return self.approval_manager

    def _is_pack_approved(self, pack_id: str) -> Tuple[bool, Optional[str]]:
        approval_manager = self._approval_manager()
        if approval_manager is None:
            return False, "approval_manager_unavailable"
        checker = getattr(approval_manager, "is_pack_approved_and_verified", None)
        if not callable(checker):
            return False, "approval_checker_unavailable"
        try:
            result = checker(pack_id)
        except Exception as exc:
            return False, f"approval_check_error:{exc}"
        if isinstance(result, tuple):
            ok = bool(result[0])
            reason = result[1] if len(result) > 1 else None
            return ok, reason
        return bool(result), None

    def _diagnose(self, level: str, code: str, message: str, **meta: Any) -> None:
        self.diagnostics.append(
            {
                "level": level,
                "code": code,
                "message": message,
                **meta,
            }
        )
        if level == "error":
            logger.error("%s: %s", code, message)
        else:
            logger.info("%s: %s", code, message)
