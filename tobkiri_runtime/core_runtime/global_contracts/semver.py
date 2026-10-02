"""Small fail-closed semantic-version compatibility implementation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


@total_ordering
@dataclass(frozen=True)
class Version:
    """Comparable SemVer core plus prerelease marker."""

    major: int
    minor: int
    patch: int
    prerelease: str | None = None

    def __lt__(self, other: object) -> bool:
        """Compare versions using SemVer precedence rules."""
        if not isinstance(other, Version):
            return NotImplemented
        core = (self.major, self.minor, self.patch)
        other_core = (other.major, other.minor, other.patch)
        if core != other_core:
            return core < other_core
        if self.prerelease is None:
            return False
        if other.prerelease is None:
            return True
        return _prerelease_key(self.prerelease) < _prerelease_key(other.prerelease)


def _prerelease_key(value: str) -> tuple[tuple[int, int | str], ...]:
    """Return a comparison key honoring numeric prerelease identifiers."""
    return tuple(
        (0, int(item)) if item.isdigit() else (1, item)
        for item in value.split(".")
    )


def parse_version(value: str) -> Version:
    """Parse a strict semantic version or raise ``ValueError``."""
    match = _SEMVER.fullmatch(value)
    if match is None:
        raise ValueError(f"invalid semantic version: {value!r}")
    return Version(
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        match.group(4),
    )


def is_compatible(version: str, version_range: str) -> bool:
    """Evaluate exact, caret, tilde, or comparator-list version ranges."""
    candidate = parse_version(version)
    if candidate.prerelease and "-" not in version_range:
        return False
    if version_range.startswith("^"):
        minimum = parse_version(version_range[1:])
        if minimum.major:
            maximum = Version(minimum.major + 1, 0, 0)
        elif minimum.minor:
            maximum = Version(0, minimum.minor + 1, 0)
        else:
            maximum = Version(0, 0, minimum.patch + 1)
        return minimum <= candidate < maximum
    if version_range.startswith("~"):
        minimum = parse_version(version_range[1:])
        maximum = Version(minimum.major, minimum.minor + 1, 0)
        return minimum <= candidate < maximum
    if version_range.startswith((">", "<", "=")):
        checks = version_range.split()
        for check in checks:
            operator = next(
                (item for item in (">=", "<=", ">", "<", "=") if check.startswith(item)),
                None,
            )
            if operator is None:
                raise ValueError(f"invalid version comparator: {check!r}")
            target = parse_version(check[len(operator) :])
            if operator == ">=" and not candidate >= target:
                return False
            if operator == "<=" and not candidate <= target:
                return False
            if operator == ">" and not candidate > target:
                return False
            if operator == "<" and not candidate < target:
                return False
            if operator == "=" and candidate != target:
                return False
        return True
    return candidate == parse_version(version_range)

