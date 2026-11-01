from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
import difflib
import json
import uuid


@dataclass
class KnowledgeEntry:
    knowledge_id: str = ""
    title: str = ""
    content: str = ""
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    entry_id: str = ""
    entry_type: str = "knowledge"
    error_pattern: str = ""
    solution: str = ""
    source: str = ""

    def __post_init__(self) -> None:
        if not self.entry_id and self.knowledge_id:
            self.entry_id = self.knowledge_id
        if not self.entry_id:
            self.entry_id = uuid.uuid4().hex
        self.knowledge_id = self.entry_id

    def to_dict(self) -> Dict[str, Any]:
        return {
            "knowledge_id": self.knowledge_id,
            "entry_id": self.entry_id,
            "id": self.entry_id,
            "title": self.title,
            "content": self.content,
            "tags": list(self.tags),
            "metadata": dict(self.metadata),
            "entry_type": self.entry_type,
            "error_pattern": self.error_pattern,
            "solution": self.solution,
            "source": self.source,
        }


class KnowledgeManager:
    def __init__(self, knowledge_dir: Optional[Path] = None) -> None:
        self._entries: Dict[str, KnowledgeEntry] = {}
        self._dir = Path(knowledge_dir) if knowledge_dir else None

    def create(self, entry: KnowledgeEntry) -> KnowledgeEntry | str:
        self._entries[entry.entry_id] = entry
        self._persist(entry)
        return entry

    def get(self, entry_id: str) -> Optional[KnowledgeEntry]:
        return self._entries.get(entry_id)

    def list_all(self) -> List[KnowledgeEntry]:
        return list(self._entries.values())

    def update(self, entry_id: str, updates: Dict[str, Any]) -> bool:
        entry = self._entries.get(entry_id)
        if entry is None:
            return False
        for key, value in updates.items():
            if hasattr(entry, key):
                setattr(entry, key, value)
        self._persist(entry)
        return True

    def delete(self, entry_id: str) -> bool:
        return self._entries.pop(entry_id, None) is not None

    def _ranked_entries(self, query: str) -> List[KnowledgeEntry]:
        words = {word.lower() for word in query.split() if word}

        def score(entry: KnowledgeEntry) -> float:
            haystack = " ".join([entry.title, entry.content, " ".join(entry.tags)]).lower()
            base = sum(1 for word in words if word in haystack)
            if not words:
                return 0.0
            return base + difflib.SequenceMatcher(None, query.lower(), haystack).ratio()

        return sorted(self._entries.values(), key=score, reverse=True)

    def retrieve_relevant(
        self, query: str
    ) -> List[KnowledgeEntry] | List[Dict[str, Any]]:
        return self._ranked_entries(query)

    def accumulate_error(self, error_pattern: str, solution: str, source: str) -> str:
        entry = KnowledgeEntry(
            knowledge_id=str(uuid.uuid4()),
            title=error_pattern,
            content=solution,
            entry_type="error_solution",
            error_pattern=error_pattern,
            solution=solution,
            source=source,
        )
        self.create(entry)
        return entry.entry_id

    def read(self, entry_id: str) -> Optional[KnowledgeEntry]:
        return self.get(entry_id)

    def _persist(self, entry: KnowledgeEntry) -> None:
        if self._dir is None:
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / f"{entry.entry_id}.json").write_text(
            json.dumps(entry.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )


class KnowledgeStore(KnowledgeManager):
    def create(self, entry: KnowledgeEntry) -> str:
        super().create(entry)
        return entry.entry_id

    def retrieve_relevant(self, query: str) -> List[Dict[str, Any]]:
        results = []
        for entry in self._ranked_entries(query):
            payload = entry.to_dict()
            haystack = " ".join([entry.title, entry.content, " ".join(entry.tags)]).lower()
            payload["relevance_score"] = round(difflib.SequenceMatcher(None, query.lower(), haystack).ratio(), 6)
            results.append(payload)
        return results
