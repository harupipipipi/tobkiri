from __future__ import annotations

import time
import uuid
from typing import Any


SCHEMA_VERSION = 3
CHECK_LOG_TAIL_CHARS = 12000
STATUS_VALUES = {"draft", "open", "changes_requested", "approved", "committed", "closed", "archived"}
DECISION_VALUES = {"none", "commented", "changes_requested", "approved"}
COMMENT_KIND_VALUES = {"comment", "change_request", "suggestion"}
CHECK_STATUS_VALUES = {"queued", "running", "passed", "failed", "skipped", "timed_out"}


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_change_request_id() -> str:
    return "cr_" + uuid.uuid4().hex


def new_review_thread_id() -> str:
    return "crt_" + uuid.uuid4().hex


def new_review_comment_id() -> str:
    return "crc_" + uuid.uuid4().hex


def new_review_check_id() -> str:
    return "chk_" + uuid.uuid4().hex


def sanitize_change_request_id(value: Any) -> str:
    text = str(value or "").strip()
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if not text or any(ch not in allowed for ch in text):
        raise ValueError("change request id is invalid")
    return text


def normalize_record(raw: dict[str, Any]) -> dict[str, Any]:
    record = dict(raw or {})
    record["id"] = sanitize_change_request_id(record.get("id"))
    record["title"] = str(record.get("title") or "Untitled review")
    record["description"] = str(record.get("description") or "")
    record["status"] = str(record.get("status") or "open")
    if record["status"] not in STATUS_VALUES:
        record["status"] = "open"
    record["decision"] = str(record.get("decision") or "none")
    if record["decision"] not in DECISION_VALUES:
        record["decision"] = "none"
    record["workspace_root"] = str(record.get("workspace_root") or "")
    record["workspace_id"] = record.get("workspace_id") or None
    record["created_at"] = str(record.get("created_at") or utc_now())
    record["updated_at"] = str(record.get("updated_at") or record["created_at"])
    revision = record.get("revision")
    record["revision"] = revision if isinstance(revision, int) and revision > 0 else 1
    receipts = record.get("mutation_receipts")
    record["mutation_receipts"] = receipts if isinstance(receipts, dict) else {}
    record["initial_snapshot"] = (
        record.get("initial_snapshot") if isinstance(record.get("initial_snapshot"), dict) else {}
    )
    record["latest_snapshot"] = (
        record.get("latest_snapshot") if isinstance(record.get("latest_snapshot"), dict) else {}
    )
    history = record.get("snapshot_history")
    record["snapshot_history"] = history if isinstance(history, list) else []
    metadata = record.get("metadata")
    record["metadata"] = metadata if isinstance(metadata, dict) else {}
    comments = record.get("comments")
    record["comments"] = [
        normalize_comment(item)
        for item in (comments if isinstance(comments, list) else [])
        if isinstance(item, dict)
    ]
    threads = record.get("review_threads")
    record["review_threads"] = [
        normalize_thread(item)
        for item in (threads if isinstance(threads, list) else [])
        if isinstance(item, dict)
    ]
    record["viewed_files"] = normalize_viewed_files(record.get("viewed_files"))
    checks = record.get("checks")
    record["checks"] = [
        normalize_check(item)
        for item in (checks if isinstance(checks, list) else [])
        if isinstance(item, dict)
    ]
    decisions = record.get("review_decisions")
    record["review_decisions"] = [
        item for item in (decisions if isinstance(decisions, list) else []) if isinstance(item, dict)
    ]
    commit = record.get("commit")
    record["commit"] = commit if isinstance(commit, dict) else {}
    seal = record.get("commit_seal")
    record["commit_seal"] = seal if isinstance(seal, dict) else {}
    refresh_review_counts(record)
    return record


def normalize_comment(raw: dict[str, Any]) -> dict[str, Any]:
    comment = dict(raw or {})
    comment["id"] = str(comment.get("id") or new_review_comment_id())
    comment["thread_id"] = str(comment.get("thread_id") or new_review_thread_id())
    kind = str(comment.get("kind") or "comment").strip()
    comment["kind"] = kind if kind in COMMENT_KIND_VALUES else "comment"
    comment["body"] = str(comment.get("body") or comment.get("text") or "")
    comment["path"] = str(comment.get("path") or comment.get("file_path") or "")
    line = comment.get("line")
    comment["line"] = line if isinstance(line, int) and line > 0 else None
    comment["side"] = str(comment.get("side") or "new")
    comment["author"] = str(comment.get("author") or "local")
    comment["suggested_patch"] = str(comment.get("suggested_patch") or "")
    comment["resolved"] = bool(comment.get("resolved"))
    comment["created_at"] = str(comment.get("created_at") or utc_now())
    comment["updated_at"] = str(comment.get("updated_at") or comment["created_at"])
    return comment


def normalize_thread(raw: dict[str, Any]) -> dict[str, Any]:
    thread = dict(raw or {})
    thread["id"] = str(thread.get("id") or thread.get("thread_id") or new_review_thread_id())
    thread["path"] = str(thread.get("path") or "")
    line = thread.get("line")
    thread["line"] = line if isinstance(line, int) and line > 0 else None
    thread["resolved"] = bool(thread.get("resolved"))
    thread["created_at"] = str(thread.get("created_at") or utc_now())
    thread["updated_at"] = str(thread.get("updated_at") or thread["created_at"])
    return thread


def normalize_viewed_files(raw: Any) -> dict[str, dict[str, Any]]:
    if isinstance(raw, list):
        raw = {str(item.get("path") or ""): item for item in raw if isinstance(item, dict)}
    if not isinstance(raw, dict):
        return {}
    viewed: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        path = str(key or "").strip()
        if isinstance(value, dict):
            path = str(value.get("path") or path).strip()
            entry = dict(value)
        else:
            entry = {"viewed": bool(value)}
        if not path:
            continue
        entry["path"] = path
        entry["viewed"] = bool(entry.get("viewed"))
        entry["updated_at"] = str(entry.get("updated_at") or utc_now())
        viewed[path] = entry
    return viewed


def normalize_check(raw: dict[str, Any]) -> dict[str, Any]:
    check = dict(raw or {})
    legacy_full_log = str(check.get("full_log") or "")
    check["id"] = str(check.get("id") or new_review_check_id())
    check["name"] = str(check.get("name") or check.get("command") or "check")
    check["command"] = str(check.get("command") or "")
    status = str(check.get("status") or "queued").strip()
    check["status"] = status if status in CHECK_STATUS_VALUES else "queued"
    exit_code = check.get("exit_code")
    check["exit_code"] = exit_code if isinstance(exit_code, int) else None
    check["stdout_tail"] = bounded_log_tail(check.get("stdout_tail"))
    check["stderr_tail"] = bounded_log_tail(check.get("stderr_tail"))
    check["log_tail"] = bounded_log_tail(check.get("log_tail") or legacy_full_log)
    check.pop("full_log", None)
    check.pop("_full_log", None)
    check["log_ref"] = str(check.get("log_ref") or "")
    check["full_log_ref"] = str(check.get("full_log_ref") or check.get("log_ref") or "")
    check["started_at"] = str(check.get("started_at") or "")
    check["completed_at"] = str(check.get("completed_at") or "")
    duration = check.get("duration_ms")
    check["duration_ms"] = duration if isinstance(duration, int) else None
    return check


def bounded_log_tail(value: Any) -> str:
    return str(value or "")[-CHECK_LOG_TAIL_CHARS:]


def refresh_review_counts(record: dict[str, Any]) -> None:
    comments = record.get("comments") if isinstance(record.get("comments"), list) else []
    unresolved = [
        item
        for item in comments
        if isinstance(item, dict)
        and item.get("kind") in {"comment", "suggestion"}
        and not bool(item.get("resolved"))
    ]
    suggestions = [
        item
        for item in comments
        if isinstance(item, dict)
        and item.get("kind") == "suggestion"
        and str(item.get("suggested_patch") or "").strip()
    ]
    checks = record.get("checks") if isinstance(record.get("checks"), list) else []
    record["unresolved_count"] = len(unresolved)
    record["unresolved_comment_count"] = len(unresolved)
    record["suggestion_count"] = len(suggestions)
    record["viewed_file_count"] = sum(
        1
        for item in (record.get("viewed_files") or {}).values()
        if isinstance(item, dict) and item.get("viewed")
    )
    record["check_summary"] = check_summary(checks)


def check_summary(checks: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {"total": len(checks), "passed": 0, "failed": 0, "pending": 0, "skipped": 0, "label": "checks pending"}
    for check in checks:
        status = str(check.get("status") or "")
        if status == "passed":
            summary["passed"] += 1
        elif status in {"failed", "timed_out"}:
            summary["failed"] += 1
        elif status == "skipped":
            summary["skipped"] += 1
        elif status in {"queued", "running"}:
            summary["pending"] += 1
    if summary["failed"]:
        summary["label"] = f"{summary['failed']} failing"
    elif summary["pending"]:
        summary["label"] = f"{summary['pending']} pending"
    elif summary["total"]:
        summary["label"] = f"{summary['passed']} passing"
    return summary
