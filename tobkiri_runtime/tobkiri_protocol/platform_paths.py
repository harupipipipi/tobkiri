"""Narrow platform-owned path compatibility normalization."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path


def canonical_platform_path(path: Path) -> Path:
    """Normalize only fixed, OS-owned macOS compatibility aliases.

    macOS exposes protected temporary trees through the root-owned ``/var``
    and ``/tmp`` symlinks.  Only those exact aliases are mapped to their
    ``/private`` targets.  Caller-controlled symlinks are intentionally left
    unresolved so descriptor walks can reject them.
    """

    absolute = path.absolute()
    if sys.platform != "darwin":
        return absolute
    aliases = (
        (Path("/var"), Path("/private/var")),
        (Path("/tmp"), Path("/private/tmp")),
    )
    for alias, canonical in aliases:
        if absolute != alias and alias not in absolute.parents:
            continue
        try:
            metadata = alias.lstat()
            link_target = Path(os.readlink(alias))
            accepted_targets = {
                canonical,
                canonical.relative_to("/"),
            }
            if (
                not stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != 0
                or link_target not in accepted_targets
            ):
                return absolute
        except OSError:
            return absolute
        return canonical / absolute.relative_to(alias)
    return absolute
