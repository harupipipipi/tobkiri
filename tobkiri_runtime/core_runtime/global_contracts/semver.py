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
_COMPARATORS = (">=", "<=", ">", "<", "=")


@total_ordering
@dataclass(frozen=True)
class Version:
    """Comparable SemVer core plus prerelease marker.

    Build metadata is intentionally excluded because it does not affect SemVer
    precedence or equality.
    """

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
        return _prerelease_key(self.prerelease) < _prerelease_key(
            other.prerelease
        )


def _prerelease_key(value: str) -> tuple[tuple[int, int | str], ...]:
    """Return a key honoring numeric prerelease identifiers."""
    return tuple(
        (0, int(item)) if item.isdigit() else (1, item)
        for item in value.split(".")
    )


def parse_version(value: str) -> Version:
    """Parse a strict semantic version or raise TypeError or ValueError."""
    if not isinstance(value, str):
        raise TypeError("semantic version must be a string")
    match = _SEMVER.fullmatch(value)
    if match is None:
        raise ValueError(f"invalid semantic version: {value!r}")
    prerelease = match.group(4)
    if prerelease is not None:
        for identifier in prerelease.split("."):
            if (
                identifier.isdigit()
                and len(identifier) > 1
                and identifier.startswith("0")
            ):
                raise ValueError(
                    "numeric prerelease identifiers cannot have leading zeros"
                )
    return Version(
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        prerelease,
    )


def validate_version_range(version_range: str) -> None:
    """Validate the exact, caret, tilde, or comparator-list range syntax."""
    if not isinstance(version_range, str) or not version_range:
        raise ValueError("version range must be a non-empty string")
    if version_range != version_range.strip():
        raise ValueError("version range cannot contain surrounding whitespace")
    if version_range.startswith(("^", "~")):
        parse_version(version_range[1:])
        return
    if version_range.startswith((">", "<", "=")):
        for check in version_range.split():
            operator = next(
                (item for item in _COMPARATORS if check.startswith(item)),
                None,
            )
            if operator is None or len(check) == len(operator):
                raise ValueError(f"invalid version comparator: {check!r}")
            parse_version(check[len(operator) :])
        return
    parse_version(version_range)


def is_compatible(version: str, version_range: str) -> bool:
    """Evaluate an exact, caret, tilde, or comparator-list version range."""
    candidate = parse_version(version)
    validate_version_range(version_range)
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
        for check in version_range.split():
            operator = next(
                item for item in _COMPARATORS if check.startswith(item)
            )
            target = parse_version(check[len(operator) :])
            if operator == ">=" and candidate < target:
                return False
            if operator == "<=" and candidate > target:
                return False
            if operator == ">" and candidate <= target:
                return False
            if operator == "<" and candidate >= target:
                return False
            if operator == "=" and candidate != target:
                return False
        return True
    return candidate == parse_version(version_range)
