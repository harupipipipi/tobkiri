from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from core_runtime.paths import resolve_pack_locations

from ..extensions.activation import (
    selected_extension_pack_artifacts,
    selected_extension_pack_ids,
)
from .models import RumiTemplate, TemplateDiagnostic, TemplateTrustLevel
from .validation import parse_template


_EXTRA_TEMPLATE_ROOTS_ENV = "RUMI_DEFAULTSPACK_TEMPLATE_ROOTS"
_APP_ECOSYSTEM_ENVS = ("RUMI_APP_DIR", "RUMI_CORE_DIR")
_PACK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class TemplateRoot:
    path: Path
    trust_level: TemplateTrustLevel | str
    source_pack_id: str | None = None
    source_kind: str = ""
    source_pack_artifact_digest: str = ""


@dataclass
class TemplateDiscoveryResult:
    templates: list[RumiTemplate] = field(default_factory=list)
    diagnostics: list[TemplateDiagnostic] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(diagnostic.is_error for diagnostic in self.diagnostics)


def default_template_roots(defaultspack_root: str | Path | None = None) -> list[TemplateRoot]:
    """Return loader-ordered roots derived from trusted runtime provenance."""
    roots, _ = _default_template_roots_with_diagnostics(defaultspack_root)
    return roots


def _default_template_roots_with_diagnostics(
    defaultspack_root: str | Path | None,
) -> tuple[list[TemplateRoot], list[TemplateDiagnostic]]:
    root = _defaultspack_root(defaultspack_root)
    sibling_roots, diagnostics = _selected_sibling_template_roots(root)
    roots = [
        TemplateRoot(root / "templates", TemplateTrustLevel.BUILTIN),
        *sibling_roots,
        TemplateRoot(root / "user_data" / "shared" / "templates", TemplateTrustLevel.USER),
    ]
    roots.extend(_configured_extra_template_roots())
    return roots, diagnostics


def discover_templates(
    roots: list[str | Path | TemplateRoot] | None = None,
    *,
    defaultspack_root: str | Path | None = None,
    trust_level: TemplateTrustLevel | str | None = None,
) -> TemplateDiscoveryResult:
    result = TemplateDiscoveryResult()
    if roots is None:
        search_roots, root_diagnostics = _default_template_roots_with_diagnostics(defaultspack_root)
        result.diagnostics.extend(root_diagnostics)
        if trust_level is not None:
            forced_trust = _coerce_trust(trust_level)
            search_roots = [
                TemplateRoot(
                    root.path,
                    forced_trust,
                    source_pack_id=root.source_pack_id,
                    source_kind=root.source_kind,
                    source_pack_artifact_digest=root.source_pack_artifact_digest,
                )
                for root in search_roots
            ]
    else:
        search_roots = _normalize_roots(
            roots, defaultspack_root=defaultspack_root, trust_level=trust_level
        )
    for template_root in search_roots:
        root_trust = _coerce_trust(template_root.trust_level)
        for template_path in _iter_template_files(template_root.path):
            loaded = load_template_file(
                template_path,
                trust_level=root_trust,
                source_pack_id=template_root.source_pack_id,
                source_root=template_root.path,
                source_kind=template_root.source_kind,
                source_pack_artifact_digest=template_root.source_pack_artifact_digest,
            )
            result.diagnostics.extend(loaded.diagnostics)
            result.templates.extend(loaded.templates)
    return result


def load_template_file(
    template_path: str | Path,
    *,
    trust_level: TemplateTrustLevel | str | None = None,
    source_pack_id: str | None = None,
    source_root: str | Path | None = None,
    source_kind: str = "",
    source_pack_artifact_digest: str = "",
) -> TemplateDiscoveryResult:
    path = Path(template_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return TemplateDiscoveryResult(
            diagnostics=[
                TemplateDiagnostic(
                    code="template.discovery.json_parse_error",
                    message=f"failed to parse template JSON: {exc.msg}",
                    path=f"line {exc.lineno}, column {exc.colno}",
                    source_path=str(path),
                )
            ]
        )
    except OSError as exc:
        return TemplateDiscoveryResult(
            diagnostics=[
                TemplateDiagnostic(
                    code="template.discovery.read_error",
                    message=f"failed to read template JSON: {exc}",
                    source_path=str(path),
                )
            ]
        )

    # Trust is assigned by the loader based on where the template came from.
    # Do not let a user-writable template self-declare ``trust_level: builtin``
    # and bypass path / shell diagnostics. Direct parse_template() remains a
    # lower-level parser for tests and in-memory builtin construction.
    effective_trust = (
        _coerce_trust(trust_level) if trust_level is not None else TemplateTrustLevel.USER
    )
    parsed = parse_template(
        raw,
        source_path=str(path),
        trust_level=effective_trust.value,
    )
    if parsed.template is not None:
        # These fields are loader-owned provenance. Template JSON cannot spoof
        # or promote them because they are overwritten after parsing.
        for key in (
            "source_pack_id",
            "source_root",
            "source_kind",
            "source_pack_artifact_digest",
        ):
            parsed.template.metadata.pop(key, None)
        if source_pack_id:
            parsed.template.metadata["source_pack_id"] = source_pack_id
        if source_root is not None:
            parsed.template.metadata["source_root"] = str(Path(source_root).resolve())
        if source_kind:
            parsed.template.metadata["source_kind"] = source_kind
        if source_pack_artifact_digest:
            parsed.template.metadata["source_pack_artifact_digest"] = source_pack_artifact_digest
    return TemplateDiscoveryResult(
        templates=[parsed.template] if parsed.template is not None else [],
        diagnostics=parsed.diagnostics,
    )


def _iter_template_files(root: Path) -> list[Path]:
    if root.is_file():
        return (
            [root]
            if root.name == "template.json"
            and not root.is_symlink()
            and _is_relative_to(root, root.parent)
            else []
        )
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.rglob("template.json")
        if path.is_file() and not path.is_symlink() and _is_relative_to(path, root)
    )


def _normalize_roots(
    roots: list[str | Path | TemplateRoot] | None,
    *,
    defaultspack_root: str | Path | None,
    trust_level: TemplateTrustLevel | str | None,
) -> list[TemplateRoot]:
    if roots is None:
        configured_roots = default_template_roots(defaultspack_root)
    else:
        configured_roots = [
            root
            if isinstance(root, TemplateRoot)
            else TemplateRoot(Path(root), _infer_root_trust(Path(root), defaultspack_root))
            for root in roots
        ]
    if trust_level is None:
        return configured_roots
    forced_trust = _coerce_trust(trust_level)
    return [
        TemplateRoot(
            root.path,
            forced_trust,
            source_pack_id=root.source_pack_id,
            source_kind=root.source_kind,
            source_pack_artifact_digest=root.source_pack_artifact_digest,
        )
        for root in configured_roots
    ]


def _coerce_trust(value: TemplateTrustLevel | str) -> TemplateTrustLevel:
    if isinstance(value, TemplateTrustLevel):
        return value
    try:
        return TemplateTrustLevel(str(value))
    except ValueError:
        return TemplateTrustLevel.USER


def _infer_root_trust(root: Path, defaultspack_root: str | Path | None) -> TemplateTrustLevel:
    pack_root = _defaultspack_root(defaultspack_root)
    if _is_relative_to(root, pack_root / "templates"):
        return TemplateTrustLevel.BUILTIN
    if _is_relative_to(root, pack_root / "user_data" / "shared" / "templates"):
        return TemplateTrustLevel.USER
    return TemplateTrustLevel.USER


def _defaultspack_root(defaultspack_root: str | Path | None) -> Path:
    return (
        Path(defaultspack_root).resolve()
        if defaultspack_root is not None
        else Path(__file__).resolve().parents[2]
    )


def _selected_sibling_template_roots(
    defaultspack_root: Path,
) -> tuple[list[TemplateRoot], list[TemplateDiagnostic]]:
    selected_pack_ids = selected_extension_pack_ids(defaultspack_root)
    selected_artifacts = selected_extension_pack_artifacts(defaultspack_root)
    roots: list[TemplateRoot] = []
    diagnostics: list[TemplateDiagnostic] = []
    for pack_id in sorted(selected_pack_ids):
        if not _PACK_ID_PATTERN.fullmatch(pack_id) or pack_id in {".", ".."}:
            diagnostics.append(
                TemplateDiagnostic(
                    code="template.discovery.selected_pack_invalid_id",
                    message=f"selected template pack ID is unsafe: {pack_id}",
                    details={"source_pack_id": pack_id},
                )
            )
            continue
        expected_artifact_digest = selected_artifacts.get(pack_id, "")
        if not expected_artifact_digest:
            diagnostics.append(
                TemplateDiagnostic(
                    code="template.discovery.selected_pack_artifact_unbound",
                    message=(
                        f"selected sibling Pack is not bound to a verified v4 artifact: {pack_id}"
                    ),
                    details={"source_pack_id": pack_id},
                )
            )
            continue
        located, mismatch = _locate_selected_pack(
            defaultspack_root,
            pack_id,
            expected_artifact_digest,
        )
        if located is None:
            if mismatch:
                diagnostics.append(
                    TemplateDiagnostic(
                        code="template.discovery.selected_pack_manifest_mismatch",
                        message=(f"selected sibling Pack has a mismatched v4 identity: {pack_id}"),
                        details={"source_pack_id": pack_id},
                    )
                )
            continue
        template_root = located / "templates"
        if template_root.exists() and not _is_relative_to(template_root, located):
            diagnostics.append(
                TemplateDiagnostic(
                    code="template.discovery.selected_pack_template_root_escape",
                    message=f"selected sibling Pack template root escapes its Pack: {pack_id}",
                    source_path=str(template_root),
                    details={"source_pack_id": pack_id},
                )
            )
            continue
        artifact_diagnostics = _validate_template_artifacts(
            located,
            pack_id=pack_id,
        )
        diagnostics.extend(artifact_diagnostics)
        if any(item.is_error for item in artifact_diagnostics):
            continue
        roots.append(
            TemplateRoot(
                template_root,
                TemplateTrustLevel.LOCAL,
                source_pack_id=pack_id,
                source_kind="selected_sibling_pack",
                source_pack_artifact_digest=expected_artifact_digest,
            )
        )
    return roots, diagnostics


def _locate_selected_pack(
    defaultspack_root: Path,
    pack_id: str,
    expected_artifact_digest: str,
) -> tuple[Path | None, bool]:
    mismatch = False
    for ecosystem_root in _candidate_ecosystem_roots(defaultspack_root):
        locations = resolve_pack_locations([pack_id], str(ecosystem_root))
        for location in locations:
            candidate = location.pack_dir.resolve()
            if (
                location.is_legacy
                or candidate.parent != ecosystem_root.resolve()
                or candidate == defaultspack_root.resolve()
            ):
                continue
            manifest_pack_id, artifact_digest = _manifest_identity(candidate)
            if manifest_pack_id != pack_id or artifact_digest != expected_artifact_digest:
                mismatch = True
                continue
            return candidate, mismatch
    return None, mismatch


def _candidate_ecosystem_roots(defaultspack_root: Path) -> list[Path]:
    roots: list[Path] = []
    if defaultspack_root.parent.name == "ecosystem":
        roots.append(defaultspack_root.parent.resolve())
    for env_name in _APP_ECOSYSTEM_ENVS:
        raw = str(os.environ.get(env_name, "") or "").strip()
        if not raw:
            continue
        app_dir = Path(raw).expanduser()
        candidates = [app_dir] if app_dir.name == "ecosystem" else [app_dir / "ecosystem"]
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved.is_dir() and resolved not in roots:
                roots.append(resolved)
    return roots


def _manifest_identity(pack_root: Path) -> tuple[str, str]:
    manifest_path = pack_root / "pack.v4.json"
    if not manifest_path.is_file():
        return "", ""
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", ""
    if not isinstance(raw, dict) or not isinstance(raw.get("pack"), dict):
        return "", ""
    if raw.get("pack_api_version") != "io.tobkiri.pack.v4":
        return "", ""
    return (
        str(raw["pack"].get("id") or "").strip(),
        str(raw["pack"].get("artifact_digest") or "").strip(),
    )


def _validate_template_artifacts(
    pack_root: Path,
    *,
    pack_id: str,
) -> list[TemplateDiagnostic]:
    """Require every sibling template to be an exact declared Pack v4 artifact."""

    manifest_path = pack_root / "pack.v4.json"
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    artifacts = raw.get("artifacts")
    declared = {
        str(item.get("path") or ""): str(item.get("digest") or "")
        for item in (artifacts if isinstance(artifacts, list) else [])
        if isinstance(item, dict)
    }
    diagnostics: list[TemplateDiagnostic] = []
    template_root = pack_root / "templates"
    actual = {
        path.relative_to(pack_root).as_posix(): path for path in _iter_template_files(template_root)
    }
    declared_templates = {
        relative_path: digest
        for relative_path, digest in declared.items()
        if relative_path.startswith("templates/") and relative_path.endswith("/template.json")
    }
    for relative_path in sorted(set(actual) | set(declared_templates)):
        path = actual.get(relative_path, pack_root / relative_path)
        expected_digest = declared_templates.get(relative_path, "")
        try:
            actual_digest = (
                "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
                if path.is_file() and not path.is_symlink() and _is_relative_to(path, pack_root)
                else ""
            )
        except OSError:
            actual_digest = ""
        if expected_digest == actual_digest:
            continue
        diagnostics.append(
            TemplateDiagnostic(
                code="template.discovery.selected_pack_template_artifact_mismatch",
                message=(
                    "selected sibling template is not an exact declared Pack v4 "
                    f"artifact: {pack_id}:{relative_path}"
                ),
                source_path=str(path),
                details={
                    "source_pack_id": pack_id,
                    "artifact_path": relative_path,
                },
            )
        )
    return diagnostics


def _configured_extra_template_roots() -> list[TemplateRoot]:
    roots: list[TemplateRoot] = []
    seen: set[Path] = set()
    for item in str(os.environ.get(_EXTRA_TEMPLATE_ROOTS_ENV, "") or "").split(os.pathsep):
        item = item.strip()
        if not item:
            continue
        path = Path(item).expanduser().resolve()
        if path in seen:
            continue
        seen.add(path)
        # Environment-configured roots remain user trust regardless of any
        # trust declaration in their JSON files.
        roots.append(
            TemplateRoot(
                path,
                TemplateTrustLevel.USER,
                source_kind="configured_extra_root",
            )
        )
    return roots


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False
