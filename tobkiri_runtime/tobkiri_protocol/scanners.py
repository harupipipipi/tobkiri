"""Static scanners for duplicate IDs, legacy bypasses, and fallbacks."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .canonical import strict_loads

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is a project dependency.
    yaml = None  # type: ignore[assignment]


SCANNER_VERSION = "io.tobkiri.architecture.scanner.v1"
V4_SCOPE_NAMES = ("tobkiri_protocol", "packs_v4", "profiles_v4", "distributions")
SERIALIZED_SUFFIXES = {".json", ".yaml", ".yml"}
SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".dart", ".rs"}

LEGACY_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("legacy_v3_manifest", re.compile(r"rumi\.pack\.v3")),
    ("legacy_function_registry", re.compile(r"\bFunctionRegistry\b")),
    ("legacy_interface_registry", re.compile(r"\bInterfaceRegistry\b")),
    ("legacy_python_file_call", re.compile(r"\bpython_file_call\b")),
    ("legacy_host_execution", re.compile(r"\bhost_execution\b")),
    ("legacy_host_execution_env", re.compile(r"RUMI_ALLOW_HOST_EXECUTION")),
    ("legacy_direct_pack_route", re.compile(r"pack_api_server|pack-specific host route")),
    ("legacy_permissive_mode", re.compile(r"\bpermissive(?:_production)?\b", re.I)),
    ("legacy_arbitrary_command", re.compile(r"desktop_app\.command|arbitrary command-string")),
)
FALLBACK_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("fallback_field", re.compile(r"[\"']fallback[\"']\s*:")),
    ("legacy_fallback_route", re.compile(r"legacy[_-]?fallback|fallback[_-]?route", re.I)),
    ("allow_on_error_fallback", re.compile(r"fallback.*allow|allow.*fallback", re.I)),
)
IDENTITY_KEYS = {
    "pack_id",
    "profile_id",
    "contract_id",
    "distribution_id",
}
LOCAL_ID_KEYS = {
    "id",
    "function_id",
    "operation_id",
    "provider_id",
    "provider_instance_id",
}


@dataclass(frozen=True, order=True)
class ScannerFinding:
    """One deterministic static-analysis finding."""

    rule_id: str
    path: str
    line: int
    message: str
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe finding."""
        return asdict(self)


def scan_v4_scope(root: Path) -> list[ScannerFinding]:
    """Scan only new v4 directories, where legacy markers are hard errors."""
    files = list(_scope_files(root, V4_SCOPE_NAMES))
    findings = scan_duplicate_ids(root, files)
    findings.extend(scan_legacy_markers(root, files, v4_only=True))
    findings.extend(scan_fallbacks(root, files))
    return sorted(set(findings))


def scan_duplicate_ids(
    root: Path,
    files: Iterable[Path] | None = None,
) -> list[ScannerFinding]:
    """Find duplicate semantic IDs in v4 serialized collections."""
    paths = list(files) if files is not None else list(_scope_files(root, V4_SCOPE_NAMES))
    findings: list[ScannerFinding] = []
    global_seen: dict[tuple[str, str], tuple[str, int]] = {}
    for path in sorted(paths):
        document = _load_serialized(path)
        if document is None:
            continue
        declared_keys = _declared_identity_keys(path)
        for location, key, value in _identity_values(document, declared_keys=declared_keys):
            if not isinstance(value, str):
                continue
            if key in declared_keys:
                namespace = key
            else:
                parent = location.rsplit(".", maxsplit=1)[0]
                # Local IDs are scoped to their containing collection, not to
                # one array element.  Strip indexes before comparing so two
                # functions with the same local ID in one manifest are
                # reported while the same operation in separate manifests is
                # not treated as a global collision.
                collection = re.sub(r"\[\d+\]", "[]", parent)
                namespace = f"{key}:{path.name}:{collection}"
            identity = (namespace, value)
            previous = global_seen.get(identity)
            line = _line_for_path(path, location)
            if previous is not None:
                findings.append(
                    ScannerFinding(
                        "duplicate_semantic_id",
                        _relative(root, path),
                        line,
                        f"duplicate {namespace} {value!r}; first at {previous[0]}:{previous[1]}",
                        f"{location}={value}",
                    )
                )
            else:
                global_seen[identity] = (_relative(root, path), line)
    return sorted(set(findings))


def scan_legacy_markers(
    root: Path,
    files: Iterable[Path] | None = None,
    *,
    v4_only: bool = False,
) -> list[ScannerFinding]:
    """Find legacy execution/bypass terms in the requested source files."""
    paths = list(files) if files is not None else list(_legacy_source_files(root))
    findings: list[ScannerFinding] = []
    for path in sorted(paths):
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError):
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            for rule_id, marker in LEGACY_MARKERS:
                match = marker.search(line)
                if match:
                    findings.append(
                        ScannerFinding(
                            rule_id,
                            _relative(root, path),
                            line_number,
                            "legacy execution or authority path is present",
                            match.group(0),
                        )
                    )
                    if v4_only:
                        break
    return sorted(set(findings))


def scan_fallbacks(
    root: Path,
    files: Iterable[Path] | None = None,
) -> list[ScannerFinding]:
    """Find fallback/allow-on-error markers in new serialized v4 scope."""
    paths = list(files) if files is not None else list(_scope_files(root, V4_SCOPE_NAMES))
    findings: list[ScannerFinding] = []
    for path in sorted(paths):
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError):
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            for rule_id, marker in FALLBACK_MARKERS:
                match = marker.search(line)
                if match:
                    findings.append(
                        ScannerFinding(
                            rule_id,
                            _relative(root, path),
                            line_number,
                            "v4 scope contains an implicit or fail-open fallback",
                            match.group(0),
                        )
                    )
    return sorted(set(findings))


def scan_repository_legacy_inventory(root: Path) -> list[ScannerFinding]:
    """Inventory existing legacy paths without allowing them to become v4 code."""
    return scan_legacy_markers(root, _legacy_source_files(root))


def _scope_files(root: Path, scope_names: Iterable[str]) -> Iterable[Path]:
    for scope_name in scope_names:
        scope = root / "tobkiri_runtime" / scope_name
        if not scope.is_dir():
            continue
        for path in scope.rglob("*"):
            if path.is_file() and path.suffix.lower() in SERIALIZED_SUFFIXES:
                yield path


def _legacy_source_files(root: Path) -> Iterable[Path]:
    for base in (root / "tobkiri_runtime" / "core_runtime", root / "tobkiri_runtime" / "ecosystem"):
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in SOURCE_SUFFIXES | SERIALIZED_SUFFIXES:
                continue
            if any(part in {"tests", "fixtures", "assets", "build", "dist", "node_modules"} for part in path.parts):
                continue
            yield path


def _load_serialized(path: Path) -> Any | None:
    try:
        if path.suffix.lower() == ".json":
            return strict_loads(path.read_bytes())
        if yaml is None:
            return None
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None


def _identity_values(
    value: Any,
    path: str = "$",
    *,
    declared_keys: frozenset[str] = frozenset(),
) -> Iterable[tuple[str, str, Any]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}"
            if key_text in LOCAL_ID_KEYS or key_text in declared_keys:
                semantic_key = "pack_id" if key_text == "id" and path == "$.pack" else key_text
                yield child_path, semantic_key, child
            yield from _identity_values(child, child_path, declared_keys=declared_keys)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _identity_values(
                child,
                f"{path}[{index}]",
                declared_keys=declared_keys,
            )


def _declared_identity_keys(path: Path) -> frozenset[str]:
    """Return IDs declared by a document, excluding reference-only fields."""
    name = path.name.lower()
    if "pack_manifest" in name:
        return frozenset({"pack_id", "contract_id"})
    if name.startswith("contract_") or name.startswith("contract."):
        return frozenset({"contract_id"})
    if "profile" in name and "lock" not in name:
        return frozenset({"profile_id"})
    if "distribution" in name:
        return frozenset({"distribution_id"})
    return frozenset()


def _line_for_path(path: Path, location: str) -> int:
    match = re.search(r"\[(\d+)\]", location)
    if match:
        return int(match.group(1)) + 1
    return 1


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()
