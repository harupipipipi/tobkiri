#!/usr/bin/env python3
"""Diff-aware static frontend UI/UX regression guard.

The scanner deliberately targets high-signal implementation patterns that have
caused real rumiai issues. It is not a replacement for browser/device testing,
assistive-technology testing, threat modelling, or visual review.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import fnmatch
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence

SEVERITY_RANK = {"note": 0, "warning": 1, "error": 2}
DEFAULT_POLICY = "scripts/quality/frontend_uiux_policy.json"
DEFAULT_BASELINE = "scripts/quality/frontend_uiux_baseline.json"


class AuditConfigurationError(RuntimeError):
    """Raised when policy, baseline, or diff configuration is unsafe."""


@dataclasses.dataclass(frozen=True)
class AuditPolicy:
    source_roots: tuple[str, ...]
    extensions: frozenset[str]
    exclude_path_parts: frozenset[str]
    exclude_globs: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class RegexRule:
    rule_id: str
    severity: str
    summary: str
    pattern: re.Pattern[str]
    extensions: frozenset[str] | None = None


@dataclasses.dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    summary: str
    path: str
    line: int
    end_line: int
    column: int
    excerpt: str
    fingerprint: str
    source_fragment: str = dataclasses.field(repr=False)
    baselined: bool = False
    baseline_issue: int | None = None

    def public_dict(self) -> dict[str, object]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "summary": self.summary,
            "path": self.path,
            "line": self.line,
            "end_line": self.end_line,
            "column": self.column,
            "excerpt": self.excerpt,
            "fingerprint": self.fingerprint,
            "baselined": self.baselined,
            "baseline_issue": self.baseline_issue,
        }


@dataclasses.dataclass(frozen=True)
class BaselineEntry:
    rule: str
    path: str
    fingerprint: str | None
    contains: str | None
    issue: int
    expires: dt.date
    reason: str


@dataclasses.dataclass(frozen=True)
class ChangedLineMap:
    lines_by_path: Mapping[str, frozenset[int]]

    def includes(self, finding: Finding) -> bool:
        changed = self.lines_by_path.get(finding.path)
        if changed is None:
            return False
        return any(line in changed for line in range(finding.line, finding.end_line + 1))


ALL_SOURCE_EXTENSIONS = frozenset({".ts", ".tsx", ".js", ".jsx", ".html", ".css", ".dart"})
JSX_EXTENSIONS = frozenset({".tsx", ".jsx"})
SCRIPT_EXTENSIONS = frozenset({".ts", ".tsx", ".js", ".jsx"})
QR_EXTENSIONS = SCRIPT_EXTENSIONS | frozenset({".dart"})
_QR_RULE_ID = "security.plaintext-secret-qr"
_QR_RULE_SUMMARY = "Long-lived secrets must not be serialized into display QR payloads."

_QR_MARKER_PATTERN = re.compile(
    r"(?:"
    r"(?P<encoder>\bQRCode\s*\.\s*(?:toDataURL|toString|toCanvas|create)|"
    r"\b(?:QrImage(?:View)?|BarcodeWidget|qrCode|qr_code))\s*\(|"
    r"(?P<payload>\bqr(?:Code|Payload|_payload|_code|Value|Data|Image)\b)"
    r"(?=\s*[:=,()])"
    r")",
    re.IGNORECASE,
)
_QR_SECRET_NAMES = (
    r"(?:"
    r"api[_-]?(?:key|secret)|"
    r"access[_-]?(?:key|token|secret)|"
    r"(?:access|approval|auth|bearer|csrf|device|id|oauth|pairing|refresh|"
    r"session|user)[_-]?token|"
    r"(?:client|encryption|private|server|signing)[_-]?key|"
    r"(?:client|pickup|server)[_-]?secret|"
    r"credential(?:s)?|secret|token"
    r")"
)
_QR_SECRET_IDENTIFIER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])" + _QR_SECRET_NAMES + r"(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_QR_PAIRING_KIND_PATTERN = re.compile(
    r"(?:\bkind\b|[\"']kind[\"'])\s*:\s*[\"']rumi_mobile_pair_v1[\"']",
    re.IGNORECASE,
)
_QR_EXPIRY_FIELD_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:expiresAt|expires_at)\s*(?::|=)",
    re.IGNORECASE,
)
_QR_CONTEXT_RADIUS = 900


def rx(pattern: str, *, flags: int = 0) -> re.Pattern[str]:
    return re.compile(pattern, re.MULTILINE | flags)


REGEX_RULES: tuple[RegexRule, ...] = (
    RegexRule(
        "security.wildcard-postmessage",
        "error",
        "Sensitive data must not be delivered with wildcard postMessage target origin.",
        rx(r"\bpostMessage\s*\([\s\S]{0,900}?,\s*['\"]\*['\"]\s*\)", flags=re.IGNORECASE),
        SCRIPT_EXTENSIONS,
    ),
    RegexRule(
        "security.credential-in-url",
        "error",
        "Credentials and approval capabilities must not be read from or written to URLs.",
        rx(
            r"(?:URLSearchParams|searchParams|query|params)[\s\S]{0,120}?"
            r"(?:\.\s*)?(?:get|set|append)\s*\(\s*['\"][^'\"]*"
            r"(?:token|secret|credential|api[_-]?key|access[_-]?key|approval)[^'\"]*['\"]",
            flags=re.IGNORECASE,
        ),
        SCRIPT_EXTENSIONS,
    ),
    RegexRule(
        "security.secret-browser-storage",
        "error",
        "Reusable credentials must not be persisted in browser or extension storage.",
        rx(
            r"(?:localStorage|sessionStorage|chrome\.storage\.(?:local|sync))[\s\S]{0,180}?"
            r"(?:token|secret|credential|api[_-]?key|access[_-]?key|approval[_-]?token)",
            flags=re.IGNORECASE,
        ),
        SCRIPT_EXTENSIONS,
    ),
    RegexRule(
        "security.unsafe-html",
        "error",
        "Raw HTML insertion requires a reviewed sanitizer and isolated rendering boundary.",
        rx(r"dangerouslySetInnerHTML|\.innerHTML\s*=|document\.write\s*\(", flags=re.IGNORECASE),
        SCRIPT_EXTENSIONS | frozenset({".html"}),
    ),
    RegexRule(
        "security.scripted-inline-document",
        "error",
        "Script-enabled inline documents require a dedicated origin and explicit sandbox policy.",
        rx(
            r"<iframe\b"
            r"(?=[^>]{0,900}?\b(?:srcDoc|srcdoc)\s*=)"
            r"(?=[^>]{0,900}?\bsandbox\s*=\s*['\"][^'\"]*\ballow-scripts\b)"
            r"[^>]{0,900}?>",
            flags=re.IGNORECASE,
        ),
        frozenset({".tsx", ".jsx", ".html"}),
    ),
    RegexRule(
        "privacy.dynamic-image-source",
        "warning",
        "Dynamic image sources require trusted-asset validation or explicit remote-load consent.",
        rx(r"<img\b[\s\S]{0,320}?\bsrc\s*=\s*\{[^}]+\}", flags=re.IGNORECASE),
        JSX_EXTENSIONS,
    ),
    RegexRule(
        "navigation.raw-location",
        "warning",
        "Navigation must pass through the centralized destination and draft-preservation policy.",
        rx(r"(?:window\.)?location\.(?:assign|replace)\s*\(|window\.location\.href\s*=", flags=re.IGNORECASE),
        SCRIPT_EXTENSIONS,
    ),
    RegexRule(
        "a11y.role-application",
        "warning",
        "role=application requires an explicit entry, exit, instruction, and keyboard-capture contract.",
        rx(r"\brole\s*=\s*['\"]application['\"]", flags=re.IGNORECASE),
        frozenset({".tsx", ".jsx", ".html"}),
    ),
    RegexRule(
        "a11y.tiny-tailwind-target",
        "warning",
        "Repeated interactive targets smaller than the product minimum require redesign or an exception.",
        rx(
            r"(?:\bh-(?:5|6|7|8)\b[^\n]{0,180}\bw-(?:5|6|7|8)\b|"
            r"\bw-(?:5|6|7|8)\b[^\n]{0,180}\bh-(?:5|6|7|8)\b)"
        ),
        frozenset({".tsx", ".jsx", ".html"}),
    ),
    RegexRule(
        "a11y.tiny-critical-text",
        "warning",
        "Critical state and action text below the readable product baseline needs explicit review.",
        rx(r"text-\[(?:8|9|10|11)px\]|fontSize\s*:\s*(?:8|9|10|11)(?:\.0)?\b"),
        ALL_SOURCE_EXTENSIONS,
    ),
    RegexRule(
        "reliability.silent-catch",
        "warning",
        "User-affecting failures must not be silently discarded.",
        rx(
            r"\.catch\s*\(\s*\(\s*\)\s*=>\s*(?:undefined|null)\s*\)|"
            r"catch\s*(?:\([^)]*\))?\s*\{\s*(?://[^\n]*)?\s*\}",
            flags=re.IGNORECASE,
        ),
        SCRIPT_EXTENSIONS,
    ),
    RegexRule(
        "reliability.raw-error-surface",
        "warning",
        "Raw exception messages need a redacted user-facing mapping and diagnostic reference.",
        rx(
            r"(?:set[A-Za-z0-9_]*Error|setMessage|setStatus)\s*\(\s*"
            r"(?:err|error|[A-Za-z]+Error)\s+instanceof\s+Error\s*\?\s*"
            r"(?:err|error|[A-Za-z]+Error)\.message"
        ),
        SCRIPT_EXTENSIONS,
    ),
    RegexRule(
        "reliability.raw-browser-storage-write",
        "warning",
        "Browser-storage writes need an acknowledged, versioned, recoverable persistence contract.",
        rx(r"(?:localStorage|sessionStorage)\.setItem\s*\("),
        SCRIPT_EXTENSIONS,
    ),
)


def load_policy(path: Path) -> AuditPolicy:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditConfigurationError(f"Cannot load policy {path}: {exc}") from exc
    if payload.get("version") != 1:
        raise AuditConfigurationError(f"Unsupported policy version in {path}")
    roots = tuple(str(item).strip("/") for item in payload.get("source_roots", []) if str(item).strip())
    extensions = frozenset(str(item) for item in payload.get("extensions", []))
    excluded = frozenset(str(item) for item in payload.get("exclude_path_parts", []))
    excluded_globs = tuple(
        str(item)
        for item in payload.get("exclude_globs", [])
        if str(item).strip()
    )
    if not roots or not extensions:
        raise AuditConfigurationError("Policy must contain source_roots and extensions")
    if not extensions.issubset(ALL_SOURCE_EXTENSIONS):
        unknown = sorted(extensions - ALL_SOURCE_EXTENSIONS)
        raise AuditConfigurationError(f"Unsupported extensions: {unknown}")
    return AuditPolicy(roots, extensions, excluded, excluded_globs)


def load_baseline(path: Path, *, today: dt.date | None = None) -> tuple[BaselineEntry, ...]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ()
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditConfigurationError(f"Cannot load baseline {path}: {exc}") from exc
    if payload.get("version") != 1:
        raise AuditConfigurationError(f"Unsupported baseline version in {path}")
    entries: list[BaselineEntry] = []
    current_date = today or dt.date.today()
    for index, raw in enumerate(payload.get("entries", []), start=1):
        if not isinstance(raw, dict):
            raise AuditConfigurationError(f"Baseline entry {index} must be an object")
        rule = str(raw.get("rule") or "").strip()
        path_glob = str(raw.get("path") or "").strip()
        fingerprint = str(raw.get("fingerprint") or "").strip() or None
        contains = str(raw.get("contains") or "").strip() or None
        reason = str(raw.get("reason") or "").strip()
        issue_raw = raw.get("issue")
        expires_raw = str(raw.get("expires") or "").strip()
        if not rule or not path_glob or not reason:
            raise AuditConfigurationError(f"Baseline entry {index} needs rule, path, and reason")
        if not fingerprint and not contains:
            raise AuditConfigurationError(
                f"Baseline entry {index} is too broad; add fingerprint or contains"
            )
        if not isinstance(issue_raw, int) or issue_raw <= 0:
            raise AuditConfigurationError(f"Baseline entry {index} needs a positive issue number")
        try:
            expires = dt.date.fromisoformat(expires_raw)
        except ValueError as exc:
            raise AuditConfigurationError(
                f"Baseline entry {index} has invalid expiration {expires_raw!r}"
            ) from exc
        if expires < current_date:
            continue
        entries.append(
            BaselineEntry(rule, path_glob, fingerprint, contains, issue_raw, expires, reason)
        )
    return tuple(entries)


def _line_and_column(text: str, offset: int) -> tuple[int, int]:
    line = text.count("\n", 0, offset) + 1
    previous_newline = text.rfind("\n", 0, offset)
    column = offset + 1 if previous_newline < 0 else offset - previous_newline
    return line, column


def _compact_excerpt(fragment: str, limit: int = 260) -> str:
    compact = re.sub(r"\s+", " ", fragment).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def _fingerprint(rule: str, path: str, fragment: str) -> str:
    normalized = re.sub(r"\s+", " ", fragment).strip()
    digest = hashlib.sha256(f"{rule}\0{path}\0{normalized}".encode("utf-8")).hexdigest()
    return digest[:20]


def _finding(rule: str, severity: str, summary: str, path: str, text: str, start: int, end: int) -> Finding:
    line, column = _line_and_column(text, start)
    end_line, _ = _line_and_column(text, max(start, end - 1))
    fragment = text[start:end]
    return Finding(
        rule=rule,
        severity=severity,
        summary=summary,
        path=path,
        line=line,
        end_line=end_line,
        column=column,
        excerpt=_compact_excerpt(fragment),
        fingerprint=_fingerprint(rule, path, fragment),
        source_fragment=fragment,
    )


def _scan_regex_rules(path: str, suffix: str, text: str) -> Iterator[Finding]:
    for rule in REGEX_RULES:
        if rule.extensions is not None and suffix not in rule.extensions:
            continue
        seen_spans: set[tuple[int, int]] = set()
        for match in rule.pattern.finditer(text):
            span = match.span()
            if span in seen_spans:
                continue
            seen_spans.add(span)
            yield _finding(rule.rule_id, rule.severity, rule.summary, path, text, *span)


def _is_short_lived_pairing_secret(
    text: str,
    start: int,
    end: int,
    *,
    context_start: int,
    context_end: int,
) -> bool:
    """Allow only the documented one-time secret in an expiring pairing payload."""
    key = re.sub(r"[_-]", "", text[start:end]).lower()
    if key != "pickupsecret":
        return False
    return bool(
        _QR_PAIRING_KIND_PATTERN.search(text, context_start, start)
        and _QR_EXPIRY_FIELD_PATTERN.search(text, end, context_end)
    )


def _is_import_marker(text: str, position: int) -> bool:
    """Ignore QR-looking names that are part of an import declaration."""
    prefix = text[max(0, position - _QR_CONTEXT_RADIUS) : position]
    boundary = max(prefix.rfind(";"), prefix.rfind("\n\n"))
    return bool(re.match(r"\s*import\b", prefix[boundary + 1 :]))


def _scan_plaintext_secret_qr(path: str, suffix: str, text: str) -> Iterator[Finding]:
    """Find secrets in QR contexts while honoring the pairing contract."""
    if suffix not in QR_EXTENSIONS:
        return
    for marker in _QR_MARKER_PATTERN.finditer(text):
        if _is_import_marker(text, marker.start()):
            continue
        context_start = marker.end() if marker.group("encoder") else marker.start()
        context_end = min(len(text), context_start + _QR_CONTEXT_RADIUS)
        context = text[context_start:context_end]
        for secret in _QR_SECRET_IDENTIFIER_PATTERN.finditer(context):
            absolute_start = context_start + secret.start()
            absolute_end = context_start + secret.end()
            if marker.group("payload") and _is_short_lived_pairing_secret(
                text,
                absolute_start,
                absolute_end,
                context_start=context_start,
                context_end=context_end,
            ):
                continue
            yield _finding(
                _QR_RULE_ID,
                "error",
                _QR_RULE_SUMMARY,
                path,
                text,
                absolute_start,
                absolute_end,
            )


def _iter_opening_tags(text: str, tag: str) -> Iterator[re.Match[str]]:
    pattern = re.compile(rf"<{tag}\b(?P<attrs>[^>]*)>", re.IGNORECASE | re.DOTALL)
    yield from pattern.finditer(text)


def _scan_noop_buttons(path: str, suffix: str, text: str) -> Iterator[Finding]:
    if suffix not in JSX_EXTENSIONS:
        return
    action_markers = (
        "onclick",
        "onpointer",
        "onmouse",
        "onkey",
        "ontouch",
        "formaction",
        'type="submit"',
        "type={'submit'}",
        'type="reset"',
        "{...",
    )
    for match in _iter_opening_tags(text, "button"):
        attrs = match.group("attrs")
        lowered = re.sub(r"\s+", "", attrs.lower())
        if "disabled" in lowered or "aria-hidden" in lowered:
            continue
        if any(marker in lowered for marker in action_markers):
            continue
        yield _finding(
            "ux.enabled-noop-button",
            "error",
            "Enabled production buttons must have an intentional, testable action.",
            path,
            text,
            *match.span(),
        )


def _scan_icon_button_names(path: str, suffix: str, text: str) -> Iterator[Finding]:
    if suffix not in JSX_EXTENSIONS:
        return
    pattern = re.compile(r"<button\b(?P<attrs>[^>]*)>(?P<body>[\s\S]{0,700}?)</button>", re.IGNORECASE)
    for match in pattern.finditer(text):
        attrs = match.group("attrs")
        if re.search(r"aria-label|aria-labelledby|\btitle\s*=", attrs, re.IGNORECASE):
            continue
        body = match.group("body")
        plain = re.sub(r"<[^>]+>", " ", body)
        plain = re.sub(r"\{[^{}]{0,300}\}", " ", plain)
        plain = re.sub(r"\s+", " ", plain).strip()
        has_component_or_svg = bool(re.search(r"<(?:svg|[A-Z][A-Za-z0-9_.]*)\b", body))
        if has_component_or_svg and not plain:
            yield _finding(
                "a11y.icon-button-name",
                "warning",
                "Icon-only controls need a localized accessible name.",
                path,
                text,
                *match.span(),
            )


def _scan_tabs(path: str, suffix: str, text: str) -> Iterator[Finding]:
    if suffix not in JSX_EXTENSIONS | frozenset({".html"}):
        return
    tag_pattern = re.compile(r"<[A-Za-z][^>]*\brole\s*=\s*['\"]tab['\"][^>]*>", re.IGNORECASE | re.DOTALL)
    for match in tag_pattern.finditer(text):
        attrs = match.group(0)
        if "aria-controls" in attrs:
            continue
        yield _finding(
            "a11y.tab-without-controls",
            "warning",
            "Tabs need stable tabpanel relationships and complete keyboard behavior.",
            path,
            text,
            *match.span(),
        )


def _scan_flutter_target_sizes(path: str, suffix: str, text: str) -> Iterator[Finding]:
    if suffix != ".dart":
        return
    pattern = re.compile(
        r"minimumSize\s*:\s*(?:const\s+)?Size\(\s*(?P<w>\d+(?:\.\d+)?)\s*,\s*(?P<h>\d+(?:\.\d+)?)\s*\)"
    )
    for match in pattern.finditer(text):
        width = float(match.group("w"))
        height = float(match.group("h"))
        if width >= 44 and height >= 44:
            continue
        yield _finding(
            "a11y.tiny-flutter-target",
            "warning",
            "Mobile interactive targets should meet the 44×44 logical-pixel product baseline.",
            path,
            text,
            *match.span(),
        )


def scan_text(path: str, text: str) -> list[Finding]:
    suffix = Path(path).suffix.lower()
    findings = [*_scan_regex_rules(path, suffix, text)]
    findings.extend(_scan_plaintext_secret_qr(path, suffix, text) or ())
    findings.extend(_scan_noop_buttons(path, suffix, text) or ())
    findings.extend(_scan_icon_button_names(path, suffix, text) or ())
    findings.extend(_scan_tabs(path, suffix, text) or ())
    findings.extend(_scan_flutter_target_sizes(path, suffix, text) or ())
    unique: dict[tuple[str, str, int, int, str], Finding] = {}
    for item in findings:
        key = (item.rule, item.path, item.line, item.end_line, item.fingerprint)
        unique[key] = item
    return sorted(unique.values(), key=lambda item: (item.path, item.line, item.rule))


def _is_excluded(path: Path, policy: AuditPolicy) -> bool:
    return (
        any(part in policy.exclude_path_parts for part in path.parts)
        or any(
            fnmatch.fnmatch(path.as_posix(), pattern)
            for pattern in policy.exclude_globs
        )
    )


def iter_source_files(root: Path, policy: AuditPolicy, explicit_paths: Sequence[str] | None = None) -> Iterator[Path]:
    if explicit_paths:
        candidates = [root / item for item in explicit_paths]
    else:
        candidates = [root / item for item in policy.source_roots]
    seen: set[Path] = set()
    for candidate in candidates:
        if not candidate.exists():
            continue
        if candidate.is_file():
            paths: Iterable[Path] = (candidate,)
        else:
            paths = candidate.rglob("*")
        for path in paths:
            if path in seen or not path.is_file() or _is_excluded(path.relative_to(root), policy):
                continue
            if path.suffix.lower() not in policy.extensions:
                continue
            seen.add(path)
            yield path


def _parse_diff(diff_text: str) -> ChangedLineMap:
    current_path: str | None = None
    lines_by_path: dict[str, set[int]] = {}
    hunk = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_path = line[6:]
            lines_by_path.setdefault(current_path, set())
            continue
        if line.startswith("+++ /dev/null"):
            current_path = None
            continue
        if not current_path:
            continue
        match = hunk.match(line)
        if not match:
            continue
        start = int(match.group("start"))
        count = int(match.group("count") or "1")
        if count <= 0:
            continue
        lines_by_path[current_path].update(range(start, start + count))
    return ChangedLineMap({path: frozenset(lines) for path, lines in lines_by_path.items()})


def changed_lines_from_git(root: Path, ref: str, policy: AuditPolicy) -> ChangedLineMap:
    command = [
        "git",
        "-C",
        str(root),
        "diff",
        "--unified=0",
        "--diff-filter=ACMR",
        f"{ref}...HEAD",
        "--",
        *policy.source_roots,
    ]
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "git diff failed"
        raise AuditConfigurationError(f"Cannot calculate changed lines from {ref}: {detail}")
    return _parse_diff(completed.stdout)


def _apply_baseline(finding: Finding, entries: Sequence[BaselineEntry]) -> Finding:
    for entry in entries:
        if finding.rule != entry.rule or not fnmatch.fnmatch(finding.path, entry.path):
            continue
        if entry.fingerprint and finding.fingerprint != entry.fingerprint:
            continue
        if entry.contains and entry.contains not in finding.source_fragment:
            continue
        return dataclasses.replace(finding, baselined=True, baseline_issue=entry.issue)
    return finding


def scan_repository(
    root: Path,
    policy: AuditPolicy,
    baseline: Sequence[BaselineEntry],
    *,
    changed_lines: ChangedLineMap | None = None,
    explicit_paths: Sequence[str] | None = None,
) -> list[Finding]:
    findings: list[Finding] = []
    for file_path in iter_source_files(root, policy, explicit_paths):
        relative = file_path.relative_to(root).as_posix()
        try:
            text = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise AuditConfigurationError(f"Source file is not UTF-8: {relative}") from exc
        for finding in scan_text(relative, text):
            if changed_lines is not None and not changed_lines.includes(finding):
                continue
            findings.append(_apply_baseline(finding, baseline))
    return sorted(findings, key=lambda item: (-SEVERITY_RANK[item.severity], item.path, item.line, item.rule))


def _summary(findings: Sequence[Finding]) -> dict[str, int]:
    result = {
        "total": len(findings),
        "new": sum(not item.baselined for item in findings),
        "baselined": sum(item.baselined for item in findings),
        "new_errors": sum(not item.baselined and item.severity == "error" for item in findings),
        "new_warnings": sum(not item.baselined and item.severity == "warning" for item in findings),
        "new_notes": sum(not item.baselined and item.severity == "note" for item in findings),
    }
    return result


def print_text_report(findings: Sequence[Finding], *, scope: str) -> None:
    summary = _summary(findings)
    print(f"Frontend UI/UX static audit ({scope})")
    print(
        f"findings={summary['total']} new={summary['new']} baselined={summary['baselined']} "
        f"errors={summary['new_errors']} warnings={summary['new_warnings']}"
    )
    if not findings:
        print("No matching findings.")
        return
    for finding in findings:
        marker = "BASELINED" if finding.baselined else finding.severity.upper()
        issue = f" issue=#{finding.baseline_issue}" if finding.baseline_issue else ""
        location = f"{finding.path}:{finding.line}"
        if finding.end_line != finding.line:
            location += f"-{finding.end_line}"
        print(f"\n[{marker}] {finding.rule} {location}{issue}")
        print(f"  {finding.summary}")
        print(f"  {finding.excerpt}")
        print(f"  fingerprint={finding.fingerprint}")


def write_json_report(path: Path, findings: Sequence[Finding], *, scope: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "scope": scope,
        "summary": _summary(findings),
        "findings": [item.public_dict() for item in findings],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def should_fail(findings: Sequence[Finding], fail_on: str) -> bool:
    if fail_on == "none":
        return False
    threshold = SEVERITY_RANK[fail_on]
    return any(not item.baselined and SEVERITY_RANK[item.severity] >= threshold for item in findings)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Repository root")
    parser.add_argument("--policy", default=DEFAULT_POLICY, help="Policy JSON path relative to root")
    parser.add_argument("--baseline", default=DEFAULT_BASELINE, help="Baseline JSON path relative to root")
    parser.add_argument("--diff-from", help="Only report findings overlapping lines changed from this git ref")
    parser.add_argument("--path", action="append", dest="paths", help="Explicit file/directory to scan; repeatable")
    parser.add_argument("--json-output", help="Write a machine-readable JSON report")
    parser.add_argument(
        "--fail-on",
        choices=("error", "warning", "none"),
        default="error",
        help="Minimum unbaselined severity that fails the command",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).resolve()
    try:
        policy = load_policy(root / args.policy)
        baseline = load_baseline(root / args.baseline)
        changed = changed_lines_from_git(root, args.diff_from, policy) if args.diff_from else None
        scope = f"diff from {args.diff_from}" if args.diff_from else "full repository"
        findings = scan_repository(
            root,
            policy,
            baseline,
            changed_lines=changed,
            explicit_paths=args.paths,
        )
        print_text_report(findings, scope=scope)
        if args.json_output:
            write_json_report(root / args.json_output, findings, scope=scope)
        return 1 if should_fail(findings, args.fail_on) else 0
    except AuditConfigurationError as exc:
        print(f"audit configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
