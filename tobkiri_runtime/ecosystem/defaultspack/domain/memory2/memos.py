"""Deprecated memo facade over the single global memory owner."""

from __future__ import annotations

import re
import uuid
import warnings
from typing import Any, Mapping

from domain.memory.store import MemoryStore

DEFAULT_PERSONALIZATION_FOLDER_ID = "personalization"
DEFAULT_PERSONALIZATION_FOLDER_NAME = "Personalization"
_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


class MemoStore:
    """Project memo folders and notes from scoped memory records."""

    def __init__(self, *_: Any, **__: Any) -> None:
        warnings.warn(
            "domain.memory2.MemoStore is a Wave 7 compatibility facade",
            DeprecationWarning,
            stacklevel=2,
        )
        self.memory = MemoryStore()
        self.ensure_default_folders()

    def ensure_default_folders(self) -> dict[str, Any]:
        """Ensure the default folder exists in the single owner."""
        existing = self.get_folder(DEFAULT_PERSONALIZATION_FOLDER_ID)
        if existing is not None:
            return existing
        return self.create_folder(
            DEFAULT_PERSONALIZATION_FOLDER_NAME,
            folder_id=DEFAULT_PERSONALIZATION_FOLDER_ID,
            slug=DEFAULT_PERSONALIZATION_FOLDER_ID,
            description="Default folder for stable user preferences.",
            metadata={"default": True, "kind": "personalization"},
        )

    def create_folder(
        self,
        name: str,
        *,
        folder_id: str | None = None,
        slug: str | None = None,
        description: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a folder-shaped memory record."""
        clean_name = str(name or "").strip()
        if not clean_name:
            raise ValueError("folder name is required")
        identifier = folder_id or f"memo-folder-{uuid.uuid4()}"
        if self.get_folder(identifier) is not None:
            raise ValueError("memo folder already exists")
        record = self.memory.put_record(
            {
                "id": identifier,
                "content": clean_name,
                "scope": "memo_folder",
                "source": "legacy_memo_facade",
                "metadata": {
                    **dict(metadata or {}),
                    "name": clean_name,
                    "slug": _slug(slug or clean_name),
                    "description": str(description or ""),
                    "archived": False,
                },
            }
        )
        return _folder(record)

    def list_folders(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        """List folder projections from owner records."""
        folders = [
            _folder(item)
            for item in self.memory.list_records()
            if item.get("scope") == "memo_folder"
            and (include_archived or not _metadata(item).get("archived"))
        ]
        return sorted(folders, key=lambda item: item["name"].casefold())

    def get_folder(self, folder_id_or_slug: str) -> dict[str, Any] | None:
        """Resolve one folder by exact ID or slug."""
        key = str(folder_id_or_slug or "").strip()
        return next(
            (
                folder
                for folder in self.list_folders(include_archived=False)
                if folder["id"] == key or folder["slug"] == key
            ),
            None,
        )

    def update_folder(
        self, folder_id_or_slug: str, updates: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """Update one folder record atomically through the owner."""
        current = self.get_folder(folder_id_or_slug)
        if current is None:
            return None
        metadata = dict(current.get("metadata") or {})
        if "name" in updates and str(updates.get("name") or "").strip():
            metadata["name"] = str(updates["name"]).strip()
        if "slug" in updates:
            metadata["slug"] = _slug(str(updates.get("slug") or metadata["name"]))
        if "description" in updates:
            metadata["description"] = str(updates.get("description") or "")
        if isinstance(updates.get("metadata"), Mapping):
            metadata.update(updates["metadata"])
        record = self.memory.put_record(
            {
                "id": current["id"],
                "content": metadata["name"],
                "scope": "memo_folder",
                "source": "legacy_memo_facade",
                "metadata": metadata,
            }
        )
        return _folder(record)

    def delete_folder(
        self, folder_id_or_slug: str, *, archive_notes: bool = True
    ) -> bool:
        """Archive a folder and optionally its notes in the same owner."""
        folder = self.get_folder(folder_id_or_slug)
        if folder is None:
            return False
        if folder["id"] == DEFAULT_PERSONALIZATION_FOLDER_ID:
            raise ValueError("default personalization folder cannot be deleted")
        metadata = dict(folder.get("metadata") or {})
        metadata["archived"] = True
        self.memory.put_record(
            {
                "id": folder["id"], "content": folder["name"],
                "scope": "memo_folder", "source": "legacy_memo_facade",
                "metadata": metadata,
            }
        )
        if archive_notes:
            for note in self.list_notes(folder_id=folder["id"]):
                self.delete_note(note["id"])
        return True

    def create_note(
        self,
        content: str,
        *,
        title: str = "",
        folder_id: str = DEFAULT_PERSONALIZATION_FOLDER_ID,
        metadata: Mapping[str, Any] | None = None,
        source: str = "manual",
        note_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a note-shaped memory record."""
        clean = str(content or "").strip()
        if not clean:
            raise ValueError("note content is required")
        folder = self.get_folder(folder_id)
        if folder is None:
            raise ValueError("memo folder not found")
        identifier = note_id or f"memo-note-{uuid.uuid4()}"
        record = self.memory.put_record(
            {
                "id": identifier,
                "content": clean,
                "scope": "memo_note",
                "source": str(source or "manual"),
                "metadata": {
                    **dict(metadata or {}),
                    "folder_id": folder["id"],
                    "folder_slug": folder["slug"],
                    "title": str(title or "").strip() or _title(clean),
                    "archived": False,
                },
            }
        )
        return _note(record)

    def get_note(self, note_id: str) -> dict[str, Any] | None:
        """Get one nonarchived note by ID."""
        key = str(note_id or "").strip()
        return next(
            (
                _note(item)
                for item in self.memory.list_records()
                if item.get("id") == key
                and item.get("scope") == "memo_note"
                and not _metadata(item).get("archived")
            ),
            None,
        )

    def list_notes(
        self,
        *,
        folder_id: str | None = None,
        limit: int = 50,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """List note projections from owner records."""
        notes = []
        for item in self.memory.list_records():
            metadata = _metadata(item)
            if item.get("scope") != "memo_note":
                continue
            if not include_archived and metadata.get("archived"):
                continue
            if folder_id and metadata.get("folder_id") != folder_id:
                continue
            notes.append(_note(item))
        notes.sort(key=lambda item: int(item.get("updated_at") or 0), reverse=True)
        return notes[: max(0, limit)]

    def search_notes(
        self, query: str, *, folder_id: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Search note projections locally from one owner snapshot."""
        query = str(query or "").casefold()
        return [
            note
            for note in self.list_notes(folder_id=folder_id, limit=10_000)
            if query in f"{note['title']} {note['content']}".casefold()
        ][: max(0, limit)]

    def update_note(
        self, note_id: str, updates: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """Update one note atomically through the memory owner."""
        current = self.get_note(note_id)
        if current is None:
            return None
        metadata = dict(current.get("metadata") or {})
        if "title" in updates:
            metadata["title"] = str(updates.get("title") or "")
        if "folder_id" in updates:
            folder = self.get_folder(str(updates["folder_id"]))
            if folder is None:
                raise ValueError("memo folder not found")
            metadata.update({"folder_id": folder["id"], "folder_slug": folder["slug"]})
        if isinstance(updates.get("metadata"), Mapping):
            metadata.update(updates["metadata"])
        record = self.memory.put_record(
            {
                "id": note_id,
                "content": str(updates.get("content", current["content"])),
                "scope": "memo_note",
                "source": str(updates.get("source", current.get("source") or "manual")),
                "metadata": metadata,
            }
        )
        return _note(record)

    def delete_note(self, note_id: str) -> bool:
        """Delete one note through the memory owner."""
        if self.get_note(note_id) is None:
            return False
        return self.memory.delete(note_id)


def _metadata(item: Mapping[str, Any]) -> dict[str, Any]:
    value = item.get("metadata")
    return dict(value) if isinstance(value, Mapping) else {}


def _folder(item: Mapping[str, Any]) -> dict[str, Any]:
    metadata = _metadata(item)
    return {
        "id": item["id"], "name": metadata.get("name") or item.get("content"),
        "slug": metadata.get("slug") or item["id"],
        "description": metadata.get("description") or "",
        "metadata": metadata, "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "archived_at": item.get("updated_at") if metadata.get("archived") else None,
    }


def _note(item: Mapping[str, Any]) -> dict[str, Any]:
    metadata = _metadata(item)
    return {
        "id": item["id"], "folder_id": metadata.get("folder_id"),
        "folder_slug": metadata.get("folder_slug"),
        "title": metadata.get("title") or _title(str(item.get("content") or "")),
        "content": item.get("content") or "", "metadata": metadata,
        "source": item.get("source"), "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "archived_at": item.get("updated_at") if metadata.get("archived") else None,
    }


def _slug(value: str) -> str:
    return _SLUG_RE.sub("-", str(value).strip().lower()).strip("-") or "memo"


def _title(content: str) -> str:
    return str(content).strip().splitlines()[0][:80] or "Untitled memo"
