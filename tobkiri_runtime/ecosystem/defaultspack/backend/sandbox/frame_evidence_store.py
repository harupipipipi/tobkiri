"""Private, bounded storage for explicitly committed desktop frame evidence."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .frame_cache import DesktopFrame


DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_TTL_SECONDS = 30 * 24 * 60 * 60
MAX_EVIDENCE_PER_RUN = 50
MAX_EVIDENCE_BYTES_PER_RUN = 50 * 1024 * 1024
ALLOWED_PURPOSES = frozenset({"visual_qa", "bug_report", "accessibility_qa"})
ALLOWED_CONTENT_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})


class FrameEvidenceError(RuntimeError):
    """Typed failure at the desktop frame evidence storage boundary."""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class FrameEvidenceBinding:
    """Server-derived authority identity bound to one evidence artifact."""

    run_id: str
    conversation_id: str
    workspace_id: str
    seat_id: str
    principal_id: str

    def as_dict(self) -> dict[str, str]:
        """Return the private record representation of the binding."""
        return {
            "run_id": self.run_id,
            "conversation_id": self.conversation_id,
            "workspace_id": self.workspace_id,
            "seat_id": self.seat_id,
            "principal_id": self.principal_id,
        }


class FrameEvidenceStore:
    """Store screenshot evidence with opaque references and strict quotas."""

    def __init__(
        self,
        pack_root: Path,
        *,
        time_fn: Callable[[], float] | None = None,
        max_count_per_run: int = MAX_EVIDENCE_PER_RUN,
        max_bytes_per_run: int = MAX_EVIDENCE_BYTES_PER_RUN,
    ) -> None:
        self.pack_root = Path(pack_root)
        self.root = (
            self.pack_root / "user_data" / "artifacts" / "desktop_frame_evidence"
        )
        self.records_root = self.root / "records"
        self.blobs_root = self.root / "blobs"
        self._time_fn = time_fn or time.time
        self.max_count_per_run = int(max_count_per_run)
        self.max_bytes_per_run = int(max_bytes_per_run)
        self._lock = threading.RLock()

    def persist(
        self,
        frame: DesktopFrame,
        *,
        binding: FrameEvidenceBinding,
        purpose: str,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> dict[str, Any]:
        """Commit an exact cached frame and return a path-free public record."""
        purpose = _purpose(purpose)
        ttl_seconds = _ttl_seconds(ttl_seconds)
        _validate_frame(frame)
        if frame.seat_id != binding.seat_id:
            raise FrameEvidenceError(
                "DESKTOP_FRAME_EVIDENCE_SEAT_MISMATCH",
                "The frame does not belong to the authorized desktop seat.",
                status_code=409,
            )
        digest = hashlib.sha256(frame.data).hexdigest()
        now = float(self._time_fn())
        with self._lock:
            self._ensure_roots()
            records = self._active_records(now)
            duplicate = self._duplicate(
                records,
                binding=binding,
                purpose=purpose,
                frame_seq=frame.frame_seq,
                digest=digest,
            )
            if duplicate is not None:
                return _public_record(duplicate, deduplicated=True)
            self._enforce_quota(
                records,
                binding=binding,
                incoming_bytes=len(frame.data),
            )
            artifact_ref = f"frame_evidence_{uuid.uuid4().hex}"
            extension = _extension(frame.content_type)
            blob_name = f"{artifact_ref}.{extension}"
            record = {
                "version": 1,
                "artifact_ref": artifact_ref,
                "blob_name": blob_name,
                "mime_type": frame.content_type,
                "size": len(frame.data),
                "sha256": digest,
                "width": frame.width,
                "height": frame.height,
                "frame_seq": frame.frame_seq,
                "captured_at": frame.captured_at,
                "created_at": now,
                "expires_at": now + ttl_seconds,
                "ttl_seconds": ttl_seconds,
                "purpose": purpose,
                "binding": binding.as_dict(),
                "storage": {
                    "scope": "local_private",
                    "encrypted_at_rest": False,
                    "file_mode": "0600",
                },
            }
            blob_path = self.blobs_root / blob_name
            record_path = self.records_root / f"{artifact_ref}.json"
            try:
                _write_new_private_file(blob_path, frame.data)
                _write_new_private_file(
                    record_path,
                    json.dumps(record, ensure_ascii=False, sort_keys=True).encode(
                        "utf-8"
                    ),
                )
            except Exception:
                blob_path.unlink(missing_ok=True)
                record_path.unlink(missing_ok=True)
                raise
            return _public_record(record, deduplicated=False)

    def export(
        self,
        artifact_ref: str,
        *,
        binding: FrameEvidenceBinding,
    ) -> tuple[dict[str, Any], bytes]:
        """Read evidence after exact authority-binding verification."""
        with self._lock:
            record = self._bound_record(artifact_ref, binding=binding)
            blob_path = self._blob_path(record)
            if blob_path.is_symlink() or not blob_path.is_file():
                raise FrameEvidenceError(
                    "DESKTOP_FRAME_EVIDENCE_NOT_FOUND",
                    "Desktop frame evidence is unavailable.",
                    status_code=404,
                )
            data = blob_path.read_bytes()
            if hashlib.sha256(data).hexdigest() != record.get("sha256"):
                raise FrameEvidenceError(
                    "DESKTOP_FRAME_EVIDENCE_INTEGRITY_FAILED",
                    "Desktop frame evidence failed integrity verification.",
                    status_code=409,
                )
            return _public_record(record, deduplicated=False), data

    def delete(
        self,
        artifact_ref: str,
        *,
        binding: FrameEvidenceBinding,
    ) -> dict[str, Any]:
        """Delete one evidence artifact after exact binding verification."""
        with self._lock:
            record = self._bound_record(artifact_ref, binding=binding)
            self._blob_path(record).unlink(missing_ok=True)
            record_path = self.records_root / f"{record['artifact_ref']}.json"
            record_path.unlink(missing_ok=True)
            return {
                "artifact_ref": record["artifact_ref"],
                "deleted": True,
            }

    def cleanup_run(self, *, binding: FrameEvidenceBinding) -> int:
        """Delete evidence bound to the exact current run authority."""
        removed = 0
        with self._lock:
            self._ensure_roots()
            for record in self._records():
                if record.get("binding") != binding.as_dict():
                    continue
                self._delete_record(record)
                removed += 1
        return removed

    def _ensure_roots(self) -> None:
        for ancestor in (
            self.pack_root / "user_data",
            self.pack_root / "user_data" / "artifacts",
        ):
            if ancestor.exists() and ancestor.is_symlink():
                raise FrameEvidenceError(
                    "DESKTOP_FRAME_EVIDENCE_STORAGE_UNSAFE",
                    "Desktop frame evidence storage is unsafe.",
                    status_code=409,
                )
            ancestor.mkdir(parents=True, exist_ok=True)
        for path in (self.root, self.records_root, self.blobs_root):
            if path.exists() and path.is_symlink():
                raise FrameEvidenceError(
                    "DESKTOP_FRAME_EVIDENCE_STORAGE_UNSAFE",
                    "Desktop frame evidence storage is unsafe.",
                    status_code=409,
                )
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)

    def _records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in sorted(self.records_root.glob("frame_evidence_*.json")):
            if path.is_symlink():
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if isinstance(value, dict) and value.get("artifact_ref") == path.stem:
                records.append(value)
        return records

    def _active_records(self, now: float) -> list[dict[str, Any]]:
        active: list[dict[str, Any]] = []
        for record in self._records():
            if float(record.get("expires_at") or 0) <= now:
                self._delete_record(record)
            else:
                active.append(record)
        return active

    def _bound_record(
        self,
        artifact_ref: str,
        *,
        binding: FrameEvidenceBinding,
    ) -> dict[str, Any]:
        artifact_ref = _artifact_ref(artifact_ref)
        self._ensure_roots()
        path = self.records_root / f"{artifact_ref}.json"
        if path.is_symlink() or not path.is_file():
            raise FrameEvidenceError(
                "DESKTOP_FRAME_EVIDENCE_NOT_FOUND",
                "Desktop frame evidence was not found.",
                status_code=404,
            )
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise FrameEvidenceError(
                "DESKTOP_FRAME_EVIDENCE_NOT_FOUND",
                "Desktop frame evidence was not found.",
                status_code=404,
            ) from exc
        if not isinstance(record, dict) or record.get("binding") != binding.as_dict():
            raise FrameEvidenceError(
                "DESKTOP_FRAME_EVIDENCE_FORBIDDEN",
                "Desktop frame evidence is bound to a different authority context.",
                status_code=403,
            )
        if float(record.get("expires_at") or 0) <= float(self._time_fn()):
            self._delete_record(record)
            raise FrameEvidenceError(
                "DESKTOP_FRAME_EVIDENCE_EXPIRED",
                "Desktop frame evidence has expired.",
                status_code=410,
            )
        return record

    @staticmethod
    def _duplicate(
        records: list[dict[str, Any]],
        *,
        binding: FrameEvidenceBinding,
        purpose: str,
        frame_seq: int,
        digest: str,
    ) -> dict[str, Any] | None:
        for record in records:
            if (
                record.get("binding") == binding.as_dict()
                and record.get("purpose") == purpose
                and record.get("frame_seq") == frame_seq
                and record.get("sha256") == digest
            ):
                return record
        return None

    def _enforce_quota(
        self,
        records: list[dict[str, Any]],
        *,
        binding: FrameEvidenceBinding,
        incoming_bytes: int,
    ) -> None:
        run_records = [
            record
            for record in records
            if isinstance(record.get("binding"), dict)
            and record["binding"].get("run_id") == binding.run_id
            and record["binding"].get("principal_id") == binding.principal_id
        ]
        used_bytes = sum(int(record.get("size") or 0) for record in run_records)
        if len(run_records) >= self.max_count_per_run:
            raise FrameEvidenceError(
                "DESKTOP_FRAME_EVIDENCE_COUNT_QUOTA",
                "The desktop frame evidence count quota for this run is exhausted.",
                status_code=429,
            )
        if used_bytes + incoming_bytes > self.max_bytes_per_run:
            raise FrameEvidenceError(
                "DESKTOP_FRAME_EVIDENCE_SIZE_QUOTA",
                "The desktop frame evidence size quota for this run is exhausted.",
                status_code=429,
            )

    def _delete_record(self, record: dict[str, Any]) -> None:
        blob_name = str(record.get("blob_name") or "")
        artifact_ref = str(record.get("artifact_ref") or "")
        if blob_name and Path(blob_name).name == blob_name:
            (self.blobs_root / blob_name).unlink(missing_ok=True)
        if artifact_ref.startswith("frame_evidence_"):
            (self.records_root / f"{artifact_ref}.json").unlink(missing_ok=True)

    def _blob_path(self, record: dict[str, Any]) -> Path:
        blob_name = str(record.get("blob_name") or "")
        artifact_ref = str(record.get("artifact_ref") or "")
        if (
            not blob_name
            or Path(blob_name).name != blob_name
            or not blob_name.startswith(f"{artifact_ref}.")
        ):
            raise FrameEvidenceError(
                "DESKTOP_FRAME_EVIDENCE_STORAGE_UNSAFE",
                "Desktop frame evidence storage metadata is unsafe.",
                status_code=409,
            )
        return self.blobs_root / blob_name


def _write_new_private_file(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    path.chmod(0o600)


def _purpose(value: str) -> str:
    purpose = str(value or "").strip()
    if purpose not in ALLOWED_PURPOSES:
        allowed = ", ".join(sorted(ALLOWED_PURPOSES))
        raise FrameEvidenceError(
            "DESKTOP_FRAME_EVIDENCE_PURPOSE_INVALID",
            f"Evidence purpose must be one of: {allowed}.",
        )
    return purpose


def _ttl_seconds(value: int) -> int:
    try:
        ttl = int(value)
    except (TypeError, ValueError) as exc:
        raise FrameEvidenceError(
            "DESKTOP_FRAME_EVIDENCE_TTL_INVALID",
            "Evidence TTL must be an integer number of seconds.",
        ) from exc
    if ttl < 60 or ttl > MAX_TTL_SECONDS:
        raise FrameEvidenceError(
            "DESKTOP_FRAME_EVIDENCE_TTL_INVALID",
            f"Evidence TTL must be between 60 and {MAX_TTL_SECONDS} seconds.",
        )
    return ttl


def _artifact_ref(value: str) -> str:
    artifact_ref = str(value or "").strip()
    prefix = "frame_evidence_"
    suffix = artifact_ref.removeprefix(prefix)
    if not artifact_ref.startswith(prefix) or len(suffix) != 32:
        raise FrameEvidenceError(
            "DESKTOP_FRAME_EVIDENCE_REF_INVALID",
            "Desktop frame evidence reference is invalid.",
        )
    try:
        int(suffix, 16)
    except ValueError as exc:
        raise FrameEvidenceError(
            "DESKTOP_FRAME_EVIDENCE_REF_INVALID",
            "Desktop frame evidence reference is invalid.",
        ) from exc
    return artifact_ref


def _validate_frame(frame: DesktopFrame) -> None:
    if frame.content_type not in ALLOWED_CONTENT_TYPES:
        raise FrameEvidenceError(
            "DESKTOP_FRAME_EVIDENCE_MIME_UNSUPPORTED",
            "Desktop frame evidence uses an unsupported image type.",
            status_code=415,
        )
    signatures = {
        "image/png": frame.data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": frame.data.startswith(b"\xff\xd8\xff"),
        "image/webp": frame.data.startswith(b"RIFF") and frame.data[8:12] == b"WEBP",
    }
    if not signatures[frame.content_type]:
        raise FrameEvidenceError(
            "DESKTOP_FRAME_EVIDENCE_IMAGE_INVALID",
            "Desktop frame evidence bytes do not match the declared image type.",
            status_code=422,
        )


def _extension(content_type: str) -> str:
    return {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
    }[content_type]


def _public_record(record: dict[str, Any], *, deduplicated: bool) -> dict[str, Any]:
    return {
        "artifact_ref": record["artifact_ref"],
        "type": "desktop_frame_evidence",
        "kind": "image",
        "mime_type": record["mime_type"],
        "size": record["size"],
        "sha256": record["sha256"],
        "width": record["width"],
        "height": record["height"],
        "frame_seq": record["frame_seq"],
        "captured_at": record["captured_at"],
        "purpose": record["purpose"],
        "created_at": record["created_at"],
        "expires_at": record["expires_at"],
        "retention": {
            "ttl_seconds": record["ttl_seconds"],
            "cleanup": "expiry_or_explicit_delete_or_run_cleanup",
        },
        "privacy": {
            "scope": "local_private",
            "encrypted_at_rest": False,
        },
        "deduplicated": deduplicated,
    }
