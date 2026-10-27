"""Pure projections of existing owner identities; no reference ledger."""

from __future__ import annotations

import base64
import re
import time
from typing import Any, Mapping

from tobkiri_protocol.canonical import canonical_digest

VERSION = "tobkiri.chat-reference/v1"
MAX_REFERENCES = 16
MAX_MEMBERS = 256
MAX_LIST_LIMIT = 100
MAX_CONVERSATIONS = 4096
MAX_PROJECTS = 256
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


def identifier(value: object) -> str:
    """Validate an existing opaque identity without inventing a new one."""
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("chat reference identity is invalid")
    return value


def validate_request(payload: Mapping[str, Any], profile_id: str) -> None:
    """Reject profile selection, unknown fields and unbounded references."""
    if payload.get("profile_id") != profile_id:
        raise PermissionError("chat reference Profile is invalid")
    operation = payload.get("operation")
    required = {"profile_id", "operation"}
    if operation == "resolve":
        required.add("references")
    elif operation != "list":
        raise PermissionError("chat reference operation is invalid")
    optional = {"_session_id"} | ({"limit", "cursor"} if operation == "list" else set())
    if not required <= set(payload) or set(payload) - required - optional:
        raise PermissionError("chat reference fields are invalid")
    if "_session_id" in payload and not isinstance(payload["_session_id"], str):
        raise ValueError("chat reference session field is invalid")
    if operation == "list":
        limit = payload.get("limit", MAX_LIST_LIMIT)
        if type(limit) is not int or not 1 <= limit <= MAX_LIST_LIMIT:
            raise ValueError("chat reference list limit is invalid")
        cursor = payload.get("cursor")
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 96):
            raise ValueError("chat reference cursor is invalid")
    if operation == "resolve":
        references = payload["references"]
        if (
            not isinstance(references, list)
            or not 1 <= len(references) <= MAX_REFERENCES
        ):
            raise ValueError("chat reference count exceeds its bound")
        seen = set()
        for reference in references:
            if not isinstance(reference, Mapping) or set(reference) != {"kind", "id"}:
                raise ValueError("chat reference fields are invalid")
            if not isinstance(reference["kind"], str) or reference["kind"] not in {
                "chat",
                "group",
            }:
                raise ValueError("chat reference kind is invalid")
            key = (reference["kind"], identifier(reference["id"]))
            if key in seen:
                raise ValueError("duplicate chat reference")
            seen.add(key)


def _revision(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("chat reference owner revision is invalid")
    return value


def _records(value: object, maximum: int) -> dict[str, Mapping[str, Any]]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError("chat reference owner collection exceeds its bound")
    result = {}
    for record in value:
        if not isinstance(record, Mapping):
            raise ValueError("chat reference owner record is invalid")
        key = identifier(record.get("id"))
        if key in result:
            raise ValueError("duplicate owner identity")
        result[key] = record
    return result


def _membership(record: Mapping[str, Any], projects: Mapping[str, Any]) -> str | None:
    metadata = record.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        raise ValueError("chat reference metadata is invalid")
    values = [record.get("group_id"), metadata.get("group_id"), metadata.get("groupId")]
    identities = [identifier(value) for value in values if value is not None]
    if len(set(identities)) > 1:
        raise ValueError("conversation group membership conflicts")
    # A compatibility hint is used only from the signed, persisted owner record
    # and only if its identity exists in the caller's canonical Project owner.
    candidate = identities[0] if identities else None
    return candidate if candidate in projects else None


def tag_group_id(tag: str) -> str:
    """Encode normalized first-40-codepoint tags as collision-free opaque IDs."""
    normalized = re.sub(r"\s+", "-", tag.strip().lower())[:40]
    encoded = (
        base64.urlsafe_b64encode(normalized.encode("utf-8")).decode("ascii").rstrip("=")
    )
    return identifier(f"group-tag-{encoded}")


def _display_label(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("chat reference label is invalid")
    # Existing conversation owners permit longer titles. Trim display only;
    # never replace identity or reject the whole owner snapshot for its title.
    return (
        value[:256]
        .encode("utf-8", errors="replace")[:1024]
        .decode("utf-8", errors="ignore")
    )


def _history_graph(
    conversations: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Mapping[str, Any]], dict[str, set[str]]]:
    """Mirror History visible roots and union explicit/inverse child edges."""
    visible = {
        cid: record
        for cid, record in conversations.items()
        if (record.get("metadata") or {}).get("is_hidden") is not True
    }
    children = {cid: set() for cid in visible}
    child_ids = set()
    for cid, record in visible.items():
        explicit = record.get("child_conversation_ids") or []
        if not isinstance(explicit, list) or len(explicit) > MAX_CONVERSATIONS:
            raise ValueError("conversation child collection is invalid")
        for child in explicit:
            if isinstance(child, str) and child in visible:
                children[cid].add(child)
                child_ids.add(child)
        parent = record.get("parent_conversation_id")
        if isinstance(parent, str) and parent in visible:
            children[parent].add(cid)
            child_ids.add(cid)
    return {
        cid: record for cid, record in visible.items() if cid not in child_ids
    }, children


def _expand_children(seeds: list[str], children: Mapping[str, set[str]]) -> list[str]:
    """Expand visible nested children once, bounded by the owner collection."""
    found = set()
    pending = list(seeds)
    while pending:
        cid = pending.pop()
        if cid in found:
            continue
        found.add(cid)
        pending.extend(children.get(cid, set()) - found)
    return sorted(found)


def _builtin_groups(
    conversations: Mapping[str, Mapping[str, Any]],
    membership: Mapping[str, str | None],
    now_ms: int,
) -> dict[str, tuple[str, list[str]]]:
    """Derive stable History buckets from persisted records, never UI layout.

    Date buckets use rolling age (24 hours / 7 days), matching the intent of
    App.formatBoardDate without its localized-string comparison bug. Timezone
    is irrelevant for elapsed milliseconds. Metadata mode follows HistoryBoard.
    """
    day = 86_400_000
    special_tags = {"company", "operations-company", "mimo-coding-company", "coding"}

    def features(record: Mapping[str, Any]) -> tuple[list[str], bool, bool]:
        metadata = record.get("metadata") or {}
        raw_tags = record.get("tags") or []
        if not isinstance(raw_tags, list) or len(raw_tags) > 256:
            raise ValueError("conversation tags exceed their bound")
        tags = list(
            dict.fromkeys(
                re.sub(r"\s+", "-", str(tag).strip().lower())[:40]
                for tag in raw_tags
                if str(tag).strip()
            )
        )
        kind = record.get("conversation_kind")
        company = bool(
            metadata.get("company_id")
            or metadata.get("companyId")
            or str(record.get("group_id") or metadata.get("group_id") or "").startswith(
                "company:"
            )
            or kind in {"operations_company", "mimo_coding_company"}
            or set(tags) & (special_tags - {"coding"})
        )
        coding = bool(
            metadata.get("workspace_id")
            or metadata.get("workspaceId")
            or kind == "coding"
            or metadata.get("mode") == "coding"
            or "coding" in tags
        )
        return tags, company, coding

    details = {cid: features(record) for cid, record in conversations.items()}
    metadata_mode = any(
        record.get("is_pinned") or record.get("is_starred") or tags or company or coding
        for cid, record in conversations.items()
        for tags, company, coding in [details[cid]]
    )
    labels = (
        {
            "group-pinned": "Pinned",
            "group-company": "Team",
            "group-coding": "Coding",
            "group-tags": "Tags",
            "group-recent": "Recent",
        }
        if metadata_mode
        else {"group-today": "Today", "group-recent": "Recent", "group-older": "Older"}
    )
    groups = {key: (label, []) for key, label in labels.items()}
    for cid, record in conversations.items():
        metadata = record.get("metadata") or {}
        # Persisted integration sections have their own identity and are not
        # silently reinterpreted as a Project or a default date/metadata bucket.
        if membership[cid] or metadata.get("external_provider"):
            continue
        tags, company, coding = details[cid]
        if not metadata_mode:
            updated = record.get("updated_at")
            if type(updated) is not int or updated < 0:
                raise ValueError("conversation timestamp is invalid")
            age = now_ms - updated
            key = (
                "group-today"
                if age < day
                else "group-recent"
                if age < 7 * day
                else "group-older"
            )
        elif record.get("is_pinned"):
            key = "group-pinned"
        elif company:
            key = "group-company"
        elif coding:
            key = "group-coding"
        else:
            primary = next((tag for tag in tags if tag not in special_tags), None)
            if primary:
                key = tag_group_id(primary)
                groups.setdefault(key, (f"#{primary}", []))
                groups["group-tags"][1].append(cid)
            else:
                key = "group-recent"
        groups[key][1].append(cid)
    return groups


def project_references(
    payload: Mapping[str, Any],
    conversation_snapshot: Mapping[str, Any],
    project_snapshot: Mapping[str, Any],
    *,
    profile_id: str,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Resolve bounded references from two authenticated owner projections."""
    validate_request(payload, profile_id)
    if conversation_snapshot.get("profile_id") != profile_id:
        raise PermissionError("conversation snapshot Profile is invalid")
    if project_snapshot.get("namespace") != "defaultspack.projects.v1":
        raise PermissionError("Project snapshot namespace is invalid")
    conversations = _records(
        conversation_snapshot.get("conversations"), MAX_CONVERSATIONS
    )
    projects = _records(project_snapshot.get("projects"), MAX_PROJECTS)
    membership = {
        key: _membership(record, projects) for key, record in conversations.items()
    }
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if type(now_ms) is not int or now_ms < 0:
        raise ValueError("chat reference clock is invalid")
    visible_roots, children = _history_graph(conversations)
    builtins = _builtin_groups(visible_roots, membership, now_ms)
    builtins = {
        key: (label, _expand_children(seeds, children))
        for key, (label, seeds) in builtins.items()
    }
    if set(projects) & set(builtins):
        raise ValueError("Project identity conflicts with a reserved History bucket")
    references = payload.get("references")
    if payload["operation"] == "list":
        references = [
            {"kind": "group", "id": key} for key in sorted({**projects, **builtins})
        ] + [{"kind": "chat", "id": key} for key in sorted(conversations)]
    result = []
    fanout = set()
    for reference in references:
        kind, key = reference["kind"], reference["id"]
        records = conversations if kind == "chat" else projects
        if key not in records and not (kind == "group" and key in builtins):
            raise KeyError("chat reference is unavailable")
        members = (
            [key]
            if kind == "chat"
            else sorted(builtins[key][1])
            if key in builtins
            else _expand_children(
                [cid for cid, group_id in membership.items() if group_id == key],
                children,
            )
        )
        complete = len(members) <= MAX_MEMBERS
        if payload["operation"] == "resolve" and not complete:
            raise ValueError("chat reference group read fanout exceeds its bound")
        fanout.update(members)
        if payload["operation"] == "resolve" and len(fanout) > MAX_MEMBERS:
            raise ValueError("chat reference combined fanout exceeds its bound")
        title = (
            builtins[key][0]
            if kind == "group" and key in builtins
            else records[key].get("title")
        )
        title = _display_label(title)
        result.append(
            {
                "kind": kind,
                "id": key,
                "label": title,
                "conversation_ids": members if complete else [],
                "member_count": len(members),
                "membership_complete": complete,
                "snapshot_digest": canonical_digest(
                    {
                        "version": VERSION,
                        "profile_id": profile_id,
                        "kind": kind,
                        "id": key,
                        "conversation_ids": members,
                    }
                ),
            }
        )
    next_cursor = None
    if payload["operation"] == "list":
        listing_digest = canonical_digest(
            {"profile_id": profile_id, "references": result}
        )
        offset = 0
        cursor = payload.get("cursor")
        if cursor is not None:
            offset_text, separator, expected_digest = cursor.partition(":")
            if (
                not separator
                or not offset_text.isascii()
                or not offset_text.isdecimal()
            ):
                raise ValueError("chat reference cursor is invalid")
            offset = int(offset_text)
            if expected_digest != listing_digest or offset >= len(result):
                raise ValueError("chat reference cursor is stale or invalid")
        limit = payload.get("limit", MAX_LIST_LIMIT)
        if offset + limit < len(result):
            next_cursor = f"{offset + limit}:{listing_digest}"
        result = result[offset : offset + limit]
    return {
        "kind": "tobkiri.chat.reference.snapshot.v1",
        "profile_id": profile_id,
        "snapshot_time": now_ms,
        "expires_at": now_ms + 600_000,
        "store_revision": _revision(conversation_snapshot.get("revision")),
        "project_revision": _revision(project_snapshot.get("revision")),
        "references": result,
        "next_cursor": next_cursor,
        "truncated": next_cursor is not None,
    }
