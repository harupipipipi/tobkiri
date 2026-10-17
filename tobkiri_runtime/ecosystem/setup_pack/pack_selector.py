from __future__ import annotations

import json
import platform
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from core_runtime.setup_pack_metadata import (
    as_dict,
    has_signing_proof,
    normalize_dependency_specs,
    normalize_pack_ref_specs,
    validate_setup_pack_metadata,
    validate_setup_pack_schema,
)


@dataclass
class PackCandidate:
    pack_id: str = ""
    target_pack_id: str = ""
    pack_identity: str = ""
    display_name: str = ""
    description: str = ""
    version: str = ""
    recommended: bool = False
    risk_level: str = "normal"
    all_ok_eligible: bool = False
    depends_on: List[Dict[str, str]] = field(default_factory=list)
    conflicts_with: List[Dict[str, str]] = field(default_factory=list)
    overlap_policy: Dict[str, Any] = field(default_factory=dict)
    base_pack_promotion: Dict[str, Any] = field(default_factory=dict)
    compatibility: Dict[str, Any] = field(default_factory=dict)
    marketplace: Dict[str, Any] = field(default_factory=dict)
    signing: Dict[str, Any] = field(default_factory=dict)
    schema_issues: List[Dict[str, Any]] = field(default_factory=list)
    required_permissions: List[str] = field(default_factory=list)
    install_surfaces: List[str] = field(default_factory=list)
    install_prompt: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pack_id": self.pack_id,
            "target_pack_id": self.target_pack_id,
            "pack_identity": self.pack_identity,
            "display_name": self.display_name,
            "description": self.description,
            "version": self.version,
            "recommended": self.recommended,
            "risk_level": self.risk_level,
            "all_ok_eligible": self.all_ok_eligible,
            "depends_on": list(self.depends_on or []),
            "conflicts_with": list(self.conflicts_with or []),
            "overlap_policy": dict(self.overlap_policy or {}),
            "base_pack_promotion": dict(self.base_pack_promotion or {}),
            "compatibility": dict(self.compatibility or {}),
            "marketplace": dict(self.marketplace or {}),
            "signing": dict(self.signing or {}),
            "schema_issues": list(self.schema_issues or []),
            "required_permissions": list(self.required_permissions or []),
            "install_surfaces": list(self.install_surfaces or []),
            "install_prompt": dict(self.install_prompt or {}),
        }


class PackSelector:
    def __init__(self, setup_pack_dir: Optional[Path] = None) -> None:
        self._setup_pack_dir = setup_pack_dir
        self._audit_log: List[Dict[str, Any]] = []

    def _resolve_setup_pack_root(self) -> Optional[Path]:
        if not self._setup_pack_dir:
            return None
        if not self._setup_pack_dir.exists():
            return None
        # 互換: ecosystem/ を渡された場合は ecosystem/setup_pack を優先
        nested = self._setup_pack_dir / "setup_pack"
        if nested.is_dir():
            return nested
        return self._setup_pack_dir

    @staticmethod
    def _read_pack_identity(ecosystem_root: Path, target_pack_id: str) -> str:
        if not target_pack_id:
            return ""
        ecosystem_json = ecosystem_root / target_pack_id / "ecosystem.json"
        if not ecosystem_json.is_file():
            return ""
        try:
            data = json.loads(ecosystem_json.read_text(encoding="utf-8"))
        except Exception:
            return ""
        return str(data.get("pack_identity", ""))

    @staticmethod
    def _as_dict(value: Any) -> Dict[str, Any]:
        return as_dict(value)

    @staticmethod
    def _normalize_dependency_specs(value: Any) -> List[Dict[str, str]]:
        return normalize_dependency_specs(value)

    @staticmethod
    def _normalize_pack_ref_specs(value: Any) -> List[Dict[str, str]]:
        return normalize_pack_ref_specs(value)

    @staticmethod
    def _parse_version(value: Any) -> tuple[int, ...]:
        return tuple(int(part) for part in re.findall(r"\d+", str(value or "")))

    @classmethod
    def _version_satisfies(cls, actual: Any, constraint: str) -> bool:
        actual_version = cls._parse_version(actual)
        if not actual_version:
            return False
        for raw_part in str(constraint or "").split(","):
            part = raw_part.strip()
            if not part:
                continue
            match = re.match(r"^(>=|<=|==|>|<)?\s*([0-9][0-9A-Za-z.+-]*)$", part)
            if not match:
                return False
            op = match.group(1) or "=="
            required = cls._parse_version(match.group(2))
            if not required:
                return False
            size = max(len(actual_version), len(required))
            left = actual_version + (0,) * (size - len(actual_version))
            right = required + (0,) * (size - len(required))
            if op == "==" and left != right:
                return False
            if op == ">=" and left < right:
                return False
            if op == ">" and left <= right:
                return False
            if op == "<=" and left > right:
                return False
            if op == "<" and left >= right:
                return False
        return True

    @staticmethod
    def _platform_aliases(platform_name: str) -> set[str]:
        normalized = str(platform_name or sys.platform).lower()
        aliases = {normalized}
        if normalized.startswith("win"):
            aliases.update({"windows", "win", "win32", "x86_64-pc-windows-msvc"})
        elif normalized == "darwin" or "mac" in normalized:
            aliases.update({"macos", "mac", "darwin", "apple", "aarch64-apple-darwin", "x86_64-apple-darwin"})
        elif normalized.startswith("linux"):
            aliases.update({"linux", "ubuntu", "x86_64-unknown-linux-gnu"})
        return aliases

    def scan_candidates(self) -> List[PackCandidate]:
        candidates: List[PackCandidate] = []
        setup_pack_root = self._resolve_setup_pack_root()
        if setup_pack_root is None:
            return candidates
        ecosystem_root = setup_pack_root.parent
        for pack_json in sorted(setup_pack_root.glob("*/pack.json")):
            try:
                loaded = json.loads(pack_json.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(loaded, dict):
                data = loaded
                fallback_pack_id = pack_json.parent.name
                setup_pack_id = str(data.get("pack_id") or fallback_pack_id)
                target_pack_id = str(data.get("target_pack_id") or setup_pack_id)
            else:
                data = {}
                setup_pack_id = pack_json.parent.name
                target_pack_id = setup_pack_id
            schema_issues = validate_setup_pack_schema(
                loaded,
                fallback_pack_id=setup_pack_id,
                fallback_target_pack_id=target_pack_id,
            )
            identity = self._read_pack_identity(ecosystem_root, target_pack_id)
            compatibility = self._as_dict(data.get("compatibility"))
            for source_key, compatibility_key in (
                ("target_pack_version", "target_pack_version"),
                ("target_version", "target_pack_version"),
                ("python_requires", "python"),
                ("platforms", "platforms"),
            ):
                if source_key in data and compatibility_key not in compatibility:
                    compatibility[compatibility_key] = data[source_key]
            candidates.append(
                PackCandidate(
                    pack_id=setup_pack_id,
                    target_pack_id=target_pack_id,
                    pack_identity=identity,
                    display_name=str(data.get("display_name", setup_pack_id)),
                    description=str(data.get("description", "")),
                    version=str(data.get("version", "")),
                    recommended=bool(data.get("recommended", False)),
                    risk_level=str(data.get("risk_level", "medium")),
                    all_ok_eligible=bool(
                        data.get(
                            "supports_all_ok",
                            data.get("all_ok_eligible", False),
                        )
                    ),
                    depends_on=self._normalize_dependency_specs(
                        data.get("depends_on", data.get("dependencies", []))
                    ),
                    conflicts_with=self._normalize_pack_ref_specs(data.get("conflicts_with", [])),
                    overlap_policy=self._as_dict(data.get("overlap_policy")),
                    base_pack_promotion=self._as_dict(data.get("base_pack_promotion")),
                    compatibility=compatibility,
                    marketplace=self._as_dict(data.get("marketplace")),
                    signing=self._as_dict(data.get("signing")),
                    schema_issues=schema_issues,
                    required_permissions=[
                        str(item)
                        for item in data.get("required_permissions", [])
                        if str(item).strip()
                    ] if isinstance(data.get("required_permissions"), list) else [],
                    install_surfaces=[
                        str(item)
                        for item in data.get("install_surfaces", [])
                        if str(item).strip()
                    ] if isinstance(data.get("install_surfaces"), list) else [],
                    install_prompt=self._as_dict(data.get("install_prompt")),
                )
            )
        return candidates

    @staticmethod
    def _contract_issue_type(issue: Dict[str, Any]) -> str:
        reason = str(issue.get("reason") or "")
        return {
            "invalid_setup_pack_schema": "invalid_setup_pack_metadata",
            "invalid_marketplace_metadata": "invalid_marketplace_metadata",
            "invalid_marketplace_status": "invalid_marketplace_metadata",
            "marketplace_blacklisted": "marketplace_blacklisted",
            "invalid_signing_mode": "invalid_signature_algorithm",
            "missing_required_signature": "unsigned_pack",
        }.get(reason, reason or "invalid_setup_pack_metadata")

    @classmethod
    def _contract_issue_to_selector(
        cls,
        candidate: PackCandidate,
        issue: Dict[str, Any],
    ) -> Dict[str, Any]:
        result = {
            "type": cls._contract_issue_type(issue),
            "pack_id": candidate.pack_id,
            "severity": issue.get("severity") or "error",
            "reason": issue.get("reason") or "",
            "message": issue.get("error") or "",
        }
        if issue.get("field"):
            result["field"] = issue["field"]
        return result

    def validate_candidates(
        self,
        *,
        installed_packs: Optional[Dict[str, Dict[str, Any]]] = None,
        platform_name: Optional[str] = None,
        python_version: Optional[str] = None,
        require_signed: bool = False,
    ) -> List[Dict[str, Any]]:
        """Validate against an explicitly supplied, Authority-resolved Pack set."""
        installed_packs = dict(installed_packs or {})
        platform_aliases = self._platform_aliases(platform_name or sys.platform)
        python_version = python_version or platform.python_version()
        issues: List[Dict[str, Any]] = []
        candidates = self.scan_candidates()
        candidate_ids = {candidate.pack_id for candidate in candidates}
        installed_ids = set(installed_packs)

        for candidate in candidates:
            for issue in candidate.schema_issues:
                issues.append(self._contract_issue_to_selector(candidate, issue))
            for issue in validate_setup_pack_metadata(
                pack_id=candidate.pack_id,
                target_pack_id=candidate.target_pack_id,
                marketplace=candidate.marketplace,
                signing=candidate.signing,
            ):
                issues.append(self._contract_issue_to_selector(candidate, issue))

            for dep in candidate.depends_on or []:
                dep_id = dep.get("pack_id", "")
                installed = installed_packs.get(dep_id)
                if installed is None:
                    issues.append({
                        "type": "missing_dependency",
                        "pack_id": candidate.pack_id,
                        "depends_on": dep_id,
                        "severity": "error",
                    })
                    continue
                constraint = dep.get("version")
                if constraint and not self._version_satisfies(installed.get("version"), constraint):
                    issues.append({
                        "type": "version_mismatch",
                        "pack_id": candidate.pack_id,
                        "depends_on": dep_id,
                        "required": constraint,
                        "actual": installed.get("version"),
                        "severity": "error",
                    })

            compatibility = candidate.compatibility or {}
            platforms = compatibility.get("platforms")
            if isinstance(platforms, list) and platforms:
                supported = {str(item).lower() for item in platforms if isinstance(item, str)}
                if supported and platform_aliases.isdisjoint(supported):
                    issues.append({
                        "type": "unsupported_platform",
                        "pack_id": candidate.pack_id,
                        "supported": sorted(supported),
                        "actual": platform_name or sys.platform,
                        "severity": "error",
                    })

            python_requires = compatibility.get("python") or compatibility.get("python_requires")
            if isinstance(python_requires, str) and python_requires.strip():
                if not self._version_satisfies(python_version, python_requires):
                    issues.append({
                        "type": "python_version_mismatch",
                        "pack_id": candidate.pack_id,
                        "required": python_requires,
                        "actual": python_version,
                        "severity": "error",
                    })

            signing = candidate.signing or {}
            if require_signed and not has_signing_proof(signing):
                issues.append({
                    "type": "unsigned_pack",
                    "pack_id": candidate.pack_id,
                    "severity": "error",
                })

            for conflict in candidate.conflicts_with or []:
                conflict_id = str(conflict.get("pack_id") or "")
                if not conflict_id:
                    continue
                if conflict_id not in candidate_ids and conflict_id not in installed_ids:
                    continue
                issues.append({
                    "type": "pack_conflict",
                    "pack_id": candidate.pack_id,
                    "conflicts_with": conflict_id,
                    "resolution": conflict.get("resolution") or "choose_one_pack",
                    "reason": conflict.get("reason") or "",
                    "severity": "error" if conflict_id in installed_ids else "warning",
                })

        return issues

    def select_and_grant(self, pack_id: str) -> Dict[str, Any]:
        candidates = {c.pack_id: c for c in self.scan_candidates()}
        candidate = candidates.get(pack_id)
        if candidate is None:
            return {"error": f"pack {pack_id} not found", "granted": False}
        result = {
            "pack_id": pack_id,
            "granted": True,
            "all_ok": bool(candidate.all_ok_eligible),
        }
        self._audit_log.append(
            {
                "action": "select_and_grant",
                "pack_id": pack_id,
                "all_ok": result["all_ok"],
                "timestamp": time.time(),
            }
        )
        return result

    def revoke(self, pack_id: str) -> Dict[str, Any]:
        self._audit_log.append({"action": "revoke", "pack_id": pack_id, "timestamp": time.time()})
        return {"revoked": True, "pack_id": pack_id}

    def get_audit_log(self) -> List[Dict[str, Any]]:
        return list(self._audit_log)
