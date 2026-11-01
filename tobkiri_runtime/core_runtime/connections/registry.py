from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

from .models import ConnectionProvider


class ConnectionsRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ConnectionProvider] = {}

    def register(self, provider: ConnectionProvider) -> None:
        if provider.provider_id in self._providers:
            raise ValueError(f"Duplicate connection provider: {provider.provider_id}")
        if not provider.display_name:
            raise ValueError(
                f"Connection provider {provider.provider_id} is missing display name"
            )
        if provider.priority is None:
            raise ValueError(
                f"Connection provider {provider.provider_id} is missing priority"
            )
        self._providers[provider.provider_id] = provider

    def load_manifest(self, path: str | Path) -> ConnectionProvider:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        provider = ConnectionProvider.from_dict(raw)
        self.register(provider)
        return provider

    def load_manifest_dir(self, root: str | Path) -> None:
        for path in discover_connection_manifests(root):
            self.load_manifest(path)

    def get(self, provider_id: str) -> ConnectionProvider:
        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise KeyError(f"Unknown connection provider: {provider_id}") from exc

    def list_providers(self) -> list[dict]:
        return [
            provider.to_dict()
            for provider in sorted(
                self._providers.values(), key=lambda item: item.priority
            )
        ]


def discover_connection_manifests(
    root: str | Path,
    *,
    max_depth: int = 4,
    max_entries: int = 2048,
) -> tuple[Path, ...]:
    """Return bounded connection manifests without following symbolic links.

    Connection metadata is a small configuration tree.  Recursive globbing is
    inappropriate here because a symlinked directory can escape into a whole
    workspace or filesystem.  This walker has stable ordering and explicit
    depth and entry budgets, and ignores every symlink.
    """

    root_path = Path(root)
    if max_depth < 0 or max_entries < 1:
        return ()
    try:
        if root_path.is_symlink() or not root_path.is_dir():
            return ()
    except OSError:
        return ()

    results: list[Path] = []
    visited_entries = 0

    def walk(directory: Path, depth: int) -> Iterator[Path]:
        nonlocal visited_entries
        if depth > max_depth:
            return
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError:
            return
        for entry in entries:
            if visited_entries >= max_entries:
                raise RuntimeError(
                    "connection manifest scan exceeded "
                    f"{max_entries} entries: {root_path}"
                )
            visited_entries += 1
            try:
                if entry.is_symlink():
                    continue
                if entry.is_file(follow_symlinks=False):
                    if entry.name.endswith(".connection.json"):
                        yield Path(entry.path)
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if depth < max_depth:
                        yield from walk(Path(entry.path), depth + 1)
            except OSError:
                continue

    results.extend(walk(root_path, 0))
    return tuple(results)
