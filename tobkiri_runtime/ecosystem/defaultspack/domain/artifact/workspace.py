from __future__ import annotations

from pathlib import Path
from typing import Any


class ArtifactWorkspace:
    """Resolve user-visible artifact paths inside a fixed artifact workspace."""

    def __init__(self, context: dict[str, Any] | None = None, *, pack_root: Path | None = None) -> None:
        self.context = context if isinstance(context, dict) else {}
        self.pack_root = Path(pack_root) if pack_root is not None else Path(__file__).resolve().parents[2]
        self._root = self._resolve_root()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def resolve(self, user_path: str | None, *, must_exist: bool = False, allow_root: bool = False) -> Path:
        normalized = str(user_path or ".").replace("\\", "/").lstrip("/")
        workspace_prefix = self._conversation_workspace_prefix()
        if workspace_prefix and normalized == workspace_prefix:
            normalized = "."
        elif workspace_prefix and normalized.startswith(workspace_prefix + "/"):
            normalized = normalized[len(workspace_prefix) + 1 :]
        target = (self.root / normalized).resolve()
        root = self.root.resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError("artifact path escapes artifact root") from exc
        if target == root and not allow_root:
            raise ValueError("artifact path must point inside artifact root")
        if must_exist and not target.exists():
            raise FileNotFoundError("artifact path not found: " + str(user_path or "."))
        return target

    def relative(self, path: Path) -> str:
        resolved = Path(path).resolve()
        return resolved.relative_to(self.root.resolve()).as_posix()

    def workspace_relative(self, path: Path) -> str:
        """Return a path suitable for conversation workspace file serving."""
        relative = self.relative(path)
        artifact_prefix = self._conversation_workspace_prefix()
        return f"{artifact_prefix}/{relative}" if artifact_prefix else relative

    def _conversation_workspace_prefix(self) -> str:
        conversation_workspace = self.context.get("conversation_workspace_dir")
        if not isinstance(conversation_workspace, str) or not conversation_workspace.strip():
            return ""
        try:
            artifact_prefix = self.root.resolve().relative_to(Path(conversation_workspace).expanduser().resolve())
        except ValueError:
            return ""
        prefix = artifact_prefix.as_posix()
        return "" if prefix == "." else prefix

    def ensure_dir(self, user_path: str = ".") -> Path:
        path = self.resolve(user_path, allow_root=True)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _resolve_root(self) -> Path:
        for candidate in self._root_candidates():
            try:
                return candidate.expanduser().resolve()
            except Exception:
                continue
        return (self.pack_root / "user_data" / "artifacts").resolve()

    def _root_candidates(self) -> list[Path]:
        candidates: list[Path] = []
        artifact_root = self.context.get("artifact_root")
        if isinstance(artifact_root, str) and artifact_root.strip():
            candidates.append(Path(artifact_root))

        conversation_workspace = self.context.get("conversation_workspace_dir")
        if isinstance(conversation_workspace, str) and conversation_workspace.strip():
            candidates.append(Path(conversation_workspace) / "artifacts")

        candidates.append(self.pack_root / "user_data" / "artifacts")
        return candidates
