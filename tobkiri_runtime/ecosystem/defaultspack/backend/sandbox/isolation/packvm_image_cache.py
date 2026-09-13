"""Fail-closed, resumable storage for signed PackVM base images."""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import os
import re
import secrets
import shutil
import socket
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

PACKVM_IMAGE_CACHE_VERSION = 1
PACKVM_IMAGE_CHUNK_BYTES = 1024 * 1024
PACKVM_IMAGE_READ_BYTES = 64 * 1024
PACKVM_IMAGE_CONNECT_TIMEOUT_SECONDS = 30.0
PACKVM_IMAGE_INACTIVITY_TIMEOUT_SECONDS = 90.0
PACKVM_IMAGE_OVERALL_TIMEOUT_SECONDS = 24.0 * 60.0 * 60.0
PACKVM_IMAGE_PROGRESS_INTERVAL_BYTES = 8 * 1024 * 1024
PACKVM_IMAGE_DISK_RESERVE_BYTES = 512 * 1024 * 1024
_MAX_METADATA_BYTES = 32 * 1024
_CONTENT_RANGE = re.compile(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)")


@dataclass(frozen=True)
class PackVMImageAuthority:
    """Immutable image facts derived solely from one signed PackVM plan."""

    source_url: str
    digest: str
    size_bytes: int
    platform: str
    architecture: str
    plan_digest: str
    session_digest: str
    operation_id: str


@dataclass(frozen=True)
class PackVMImageProgress:
    """Bounded progress emitted while downloading or validating an image."""

    stage: str
    downloaded_bytes: int
    total_bytes: int
    resumed_bytes: int


@dataclass(frozen=True)
class PackVMVerifiedImage:
    """Descriptor identity for an atomically published verified local image."""

    path: Path
    digest: str
    size_bytes: int
    device: int
    inode: int
    source_url: str


@dataclass(frozen=True)
class PackVMPinnedImage:
    """Verified cache identity with an open descriptor pinned for one consumer."""

    verified: PackVMVerifiedImage
    descriptor: int


class PackVMImageError(RuntimeError):
    """Typed safe diagnostic for PackVM image acquisition failures."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)

    def diagnostic(self) -> dict[str, str]:
        """Return a stable diagnostic without cache paths or response bodies."""

        return {"code": self.code, "stage": "image_prefetch", "kind": "download"}


class PackVMImageCancelled(PackVMImageError):
    """The authenticated provisioning operation requested cancellation."""


@dataclass(frozen=True)
class _PinnedEntry:
    """Open directory descriptors that survive pathname replacement safely."""

    root_descriptor: int
    entry_descriptor: int
    root_device: int
    root_inode: int
    entry_device: int
    entry_inode: int
    entry_name: str


class _StrictRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject every redirect because only the signed source URL is authority."""

    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request:
        del req, fp, code, msg, headers, newurl
        raise PackVMImageError(
            "packvm_image_redirect_rejected", "PackVM image redirects are not authorized"
        )


class PackVMImageCache:
    """Own, resume, verify, and atomically publish PackVM image bytes."""

    def __init__(
        self,
        root: Path,
        *,
        opener: Callable[..., Any] | None = None,
        disk_usage: Callable[[Path], Any] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        connect_timeout_seconds: float = PACKVM_IMAGE_CONNECT_TIMEOUT_SECONDS,
        inactivity_timeout_seconds: float = PACKVM_IMAGE_INACTIVITY_TIMEOUT_SECONDS,
        overall_timeout_seconds: float = PACKVM_IMAGE_OVERALL_TIMEOUT_SECONDS,
    ) -> None:
        if not root.is_absolute():
            raise ValueError("PackVM image cache root must be absolute")
        self._requested_root = root
        self._root = root.resolve()
        if self._root != root:
            raise ValueError("PackVM image cache root must not contain symlinks or traversal")
        # Build the system opener lazily: macOS proxy discovery is not fork-safe,
        # while provisioners are also instantiated in lock-recovery child tests.
        self._opener = opener
        self._disk_usage = disk_usage or shutil.disk_usage
        self._monotonic = monotonic
        self._connect_timeout = _positive_bound(connect_timeout_seconds, 300.0)
        self._inactivity_timeout = _positive_bound(inactivity_timeout_seconds, 900.0)
        self._overall_timeout = _positive_bound(overall_timeout_seconds, 7 * 24 * 3600.0)
        self._signing_key_bytes: bytes | None = None
        self._capacity_lock = threading.Lock()
        self._root_chain_identity = self._capture_root_chain(require_complete=False)

    @property
    def root(self) -> Path:
        """Return the dedicated PackVM-owned image root."""

        return self._root

    def image_path(self, authority: PackVMImageAuthority) -> Path:
        """Return the deterministic official local-source path for authority."""

        self._validate_authority(authority)
        return self._root / self._key(authority) / "image.img"

    def status(self, authority: PackVMImageAuthority) -> tuple[str, str | None]:
        """Classify only an independently verified published image as healthy."""

        self._validate_authority(authority)
        try:
            self._ensure_root()
            entry = self._root / self._key(authority)
            with self._pinned_entry(entry, create=False) as pinned:
                _verified, descriptor = self._published_identity_pinned(
                    authority, entry, pinned
                )
                os.close(descriptor)
        except FileNotFoundError:
            return "absent", None
        except (OSError, PackVMImageError, ValueError) as exc:
            return "unsafe", str(exc)
        return "verified_source", None

    def verified(self, authority: PackVMImageAuthority) -> PackVMVerifiedImage:
        """Revalidate published path, metadata, inode, size, and SHA-256."""

        self._validate_authority(authority)
        self._ensure_root()
        entry = self._root / self._key(authority)
        with self._pinned_entry(entry, create=False) as pinned:
            return self._verified_pinned(authority, entry, pinned)

    def _verified_pinned(
        self,
        authority: PackVMImageAuthority,
        entry: Path,
        pinned: _PinnedEntry,
        cancelled: Callable[[], bool] | None = None,
    ) -> PackVMVerifiedImage:
        """Verify a published image through one pinned entry descriptor."""

        verified, descriptor = self._published_identity_pinned(
            authority, entry, pinned
        )
        try:
            before = os.fstat(descriptor)
            digest = _descriptor_digest(
                descriptor, authority.size_bytes, cancelled=cancelled
            )
            after = os.fstat(descriptor)
            self._require_same_file(
                verified.path, before, after, directory_fd=pinned.entry_descriptor
            )
            if not hmac.compare_digest(digest, authority.digest):
                raise PackVMImageError(
                    "packvm_image_digest_mismatch", "PackVM image digest does not match"
                )
            self._require_pinned_identity(entry, pinned)
            if cancelled is not None and cancelled():
                raise PackVMImageCancelled(
                    "packvm_image_cancelled",
                    "PackVM image verification was cancelled",
                )
            return verified
        finally:
            os.close(descriptor)

    def _published_identity_pinned(
        self,
        authority: PackVMImageAuthority,
        entry: Path,
        pinned: _PinnedEntry,
    ) -> tuple[PackVMVerifiedImage, int]:
        """Authenticate published metadata and return one open exact file identity."""

        image_path = entry / "image.img"
        metadata = self._read_authenticated_json(
            image_path.with_name("published.json"), directory_fd=pinned.entry_descriptor
        )
        expected = self._immutable_metadata(authority)
        if any(metadata.get(key) != value for key, value in expected.items()):
            raise PackVMImageError(
                "packvm_image_binding_mismatch", "PackVM image metadata binding changed"
            )
        descriptor = self._open_regular(
            image_path, writable=False, directory_fd=pinned.entry_descriptor
        )
        try:
            before = os.fstat(descriptor)
            if before.st_size != authority.size_bytes:
                raise PackVMImageError(
                    "packvm_image_size_mismatch", "PackVM image size does not match"
                )
            self._require_same_file(
                image_path, before, before, directory_fd=pinned.entry_descriptor
            )
            if metadata.get("device") != before.st_dev or metadata.get("inode") != before.st_ino:
                raise PackVMImageError(
                    "packvm_image_inode_mismatch", "PackVM image identity changed"
                )
            if metadata.get("ctime_ns") != before.st_ctime_ns:
                raise PackVMImageError(
                    "packvm_image_inode_mismatch", "PackVM image identity changed"
                )
            return (
                PackVMVerifiedImage(
                    path=image_path,
                    digest=authority.digest,
                    size_bytes=before.st_size,
                    device=before.st_dev,
                    inode=before.st_ino,
                    source_url=authority.source_url,
                ),
                descriptor,
            )
        except Exception:
            os.close(descriptor)
            raise

    def prefetch(
        self,
        authority: PackVMImageAuthority,
        *,
        progress: Callable[[PackVMImageProgress], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> PackVMVerifiedImage:
        """Resume and publish one exact signed-plan image under an exclusive lock."""

        self._validate_authority(authority)
        self._ensure_root()
        entry = self._root / self._key(authority)
        with self._pinned_entry(entry, create=True) as pinned:
            with self._exclusive_lock(pinned, "download.lock"):
                try:
                    verified = self._verified_pinned(
                        authority, entry, pinned, cancelled=cancelled
                    )
                except FileNotFoundError:
                    pass
                else:
                    return self._verified_progress_pinned(
                        authority,
                        entry,
                        pinned,
                        verified,
                        progress,
                        cancelled,
                        resumed_bytes=authority.size_bytes,
                    )
                verified = self._download_locked(
                    authority, entry, pinned, progress, cancelled,
                    emit_verified=False,
                )
                return self._verified_progress_pinned(
                    authority,
                    entry,
                    pinned,
                    verified,
                    progress,
                    cancelled,
                    resumed_bytes=0,
                )

    def _verified_progress_pinned(
        self,
        authority: PackVMImageAuthority,
        entry: Path,
        pinned: _PinnedEntry,
        verified: PackVMVerifiedImage,
        progress: Callable[[PackVMImageProgress], None] | None,
        cancelled: Callable[[], bool] | None,
        *,
        resumed_bytes: int,
    ) -> PackVMVerifiedImage:
        """Pin the exact published file across the untrusted progress callback."""

        current, descriptor = self._published_identity_pinned(authority, entry, pinned)
        try:
            before = os.fstat(descriptor)
            if (
                current.device != verified.device
                or current.inode != verified.inode
                or current.size_bytes != verified.size_bytes
            ):
                raise PackVMImageError(
                    "packvm_image_inode_mismatch", "PackVM image identity changed"
                )
            self._require_pinned_identity(entry, pinned)
            if cancelled is not None and cancelled():
                raise PackVMImageCancelled(
                    "packvm_image_cancelled",
                    "PackVM image verification was cancelled",
                )
            if progress is not None:
                progress(PackVMImageProgress(
                    "verified",
                    authority.size_bytes,
                    authority.size_bytes,
                    resumed_bytes,
                ))
            after = os.fstat(descriptor)
            self._require_same_file(
                current.path,
                before,
                after,
                directory_fd=pinned.entry_descriptor,
            )
            self._require_pinned_identity(entry, pinned)
            if cancelled is not None and cancelled():
                raise PackVMImageCancelled(
                    "packvm_image_cancelled",
                    "PackVM image verification was cancelled",
                )
            return current
        finally:
            os.close(descriptor)

    @contextmanager
    def provisioning_image(
        self,
        authority: PackVMImageAuthority,
        *,
        progress: Callable[[PackVMImageProgress], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> Iterator[PackVMPinnedImage]:
        """Pin one published source until its provisioning consumer is finished."""

        self._validate_authority(authority)
        self._ensure_root()
        entry = self._root / self._key(authority)
        with self._pinned_entry(entry, create=True) as pinned:
            with self._exclusive_lock(pinned, "download.lock"):
                try:
                    verified, descriptor = self._published_identity_pinned(
                        authority, entry, pinned
                    )
                except FileNotFoundError:
                    verified = self._download_locked(
                        authority,
                        entry,
                        pinned,
                        progress,
                        cancelled,
                        emit_verified=False,
                    )
                    verified, descriptor = self._published_identity_pinned(
                        authority, entry, pinned
                    )
                try:
                    self._require_pinned_identity(entry, pinned)
                    if cancelled is not None and cancelled():
                        raise PackVMImageCancelled(
                            "packvm_image_cancelled",
                            "PackVM image provisioning was cancelled",
                        )
                    yield PackVMPinnedImage(verified, descriptor)
                finally:
                    os.close(descriptor)

    def remaining_bytes(self, authority: PackVMImageAuthority) -> int:
        """Return authenticated remaining download bytes under the entry lock."""

        self._validate_authority(authority)
        self._ensure_root()
        entry = self._root / self._key(authority)
        try:
            with self._pinned_entry(entry, create=False) as pinned:
                with self._exclusive_lock(pinned, "download.lock"):
                    try:
                        self._verified_pinned(authority, entry, pinned)
                    except FileNotFoundError:
                        offset, _metadata = self._resume_state(
                            authority,
                            entry / "partial.img",
                            entry / "partial.json",
                            pinned,
                        )
                        return authority.size_bytes - offset
                    return 0
        except FileNotFoundError:
            return authority.size_bytes

    def garbage_collect(
        self,
        current: PackVMImageAuthority,
        *,
        maximum_generations: int = 2,
        quota_bytes: int | None = None,
        uncheckpointed_minimum_age_seconds: float = 3600.0,
    ) -> int:
        """Remove only authenticated stale content entries, preserving current."""

        self._validate_authority(current)
        self._ensure_root()
        limit = quota_bytes if quota_bytes is not None else current.size_bytes * 2
        if (
            maximum_generations < 1
            or limit < current.size_bytes
            or uncheckpointed_minimum_age_seconds < 0
        ):
            raise ValueError("PackVM image cache GC policy is invalid")
        current_key = self._key(current)
        candidates: list[tuple[int, int, Path, bool, int, int, str]] = []
        total = 0
        for child in self._root.iterdir():
            if child.name == "image-cache.key" or child.name == current_key:
                continue
            if re.fullmatch(r"[0-9a-f]{64}", child.name) is None:
                continue
            try:
                with self._pinned_entry(child, create=False) as pinned:
                    with self._exclusive_lock(pinned, "download.lock"):
                        metadata = self._owned_entry_metadata(child, pinned)
                        if metadata is None:
                            residue_name, residue = self._uncheckpointed_residue(
                                child, pinned
                            )
                            size = int(residue.st_size)
                            total += size
                            age_ns = time.time_ns() - int(residue.st_mtime_ns)
                            if age_ns < int(
                                uncheckpointed_minimum_age_seconds * 1_000_000_000
                            ):
                                continue
                            candidates.append((
                                int(residue.st_mtime_ns),
                                size,
                                child,
                                True,
                                int(residue.st_dev),
                                int(residue.st_ino),
                                residue_name,
                            ))
                            continue
                        size = int(metadata.get("size_bytes") or 0)
                        timestamp = int(
                            metadata.get("published_unix_ns")
                            or metadata.get("checkpoint_unix_ns")
                            or metadata.get("published_unix")
                            or metadata.get("checkpoint_unix")
                            or 0
                        )
                        total += size
                        candidates.append((timestamp, size, child, False, 0, 0, ""))
            except (FileNotFoundError, OSError, PackVMImageError):
                continue
        total += current.size_bytes
        removed = 0
        generations = len(candidates) + 1
        for (
            _timestamp,
            size,
            child,
            uncheckpointed,
            device,
            inode,
            residue_name,
        ) in sorted(candidates):
            if generations <= maximum_generations and total <= limit:
                break
            try:
                with self._pinned_entry(child, create=False) as pinned:
                    with self._exclusive_lock(pinned, "download.lock") as lock:
                        metadata = self._owned_entry_metadata(child, pinned)
                        if uncheckpointed:
                            if metadata is not None:
                                continue
                            current_name, current_partial = self._uncheckpointed_residue(
                                child, pinned
                            )
                            if (
                                current_name != residue_name
                                or current_partial.st_dev != device
                                or current_partial.st_ino != inode
                                or current_partial.st_nlink != 1
                            ):
                                continue
                            self._unlink_regular_if_present(
                                child / residue_name, pinned
                            )
                        else:
                            if metadata is None:
                                continue
                            for name in (
                                "image.img",
                                "partial.img",
                                "published.json",
                                "partial.json",
                            ):
                                self._unlink_regular_if_present(child / name, pinned)
                        os.fsync(pinned.entry_descriptor)
                        self._remove_locked_entry(child, pinned, lock)
            except (FileNotFoundError, OSError):
                continue
            generations -= 1
            total -= size
            removed += 1
        return removed

    def cleanup(self, authority: PackVMImageAuthority) -> bool:
        """Delete an owned content entry authorized by exact immutable authority."""

        self._validate_authority(authority)
        self._ensure_root()
        entry = self._root / self._key(authority)
        with self._pinned_entry(entry, create=False) as pinned:
            with self._exclusive_lock(pinned, "download.lock"):
                try:
                    metadata = self._read_authenticated_json(
                        entry / "published.json", directory_fd=pinned.entry_descriptor
                    )
                except FileNotFoundError:
                    try:
                        metadata = self._read_authenticated_json(
                            entry / "partial.json", directory_fd=pinned.entry_descriptor
                        )
                    except FileNotFoundError:
                        self._delete_uncheckpointed_residue(entry, pinned)
                        return True
                expected = self._immutable_metadata(authority)
                if any(metadata.get(key) != value for key, value in expected.items()):
                    raise PackVMImageError(
                        "packvm_image_cleanup_binding_mismatch",
                        "PackVM content authority does not match the owned cache entry",
                    )
                for name in ("image.img", "partial.img", "published.json", "partial.json"):
                    self._unlink_regular_if_present(entry / name, pinned)
                os.fsync(pinned.entry_descriptor)
        return True

    def _download_locked(
        self,
        authority: PackVMImageAuthority,
        entry: Path,
        pinned: _PinnedEntry,
        progress: Callable[[PackVMImageProgress], None] | None,
        cancelled: Callable[[], bool] | None,
        *,
        emit_verified: bool,
    ) -> PackVMVerifiedImage:
        """Serialize capacity reservation across every content generation."""

        with self._root_quota_lock(pinned):
            return self._download_under_quota_lock(
                authority,
                entry,
                pinned,
                progress,
                cancelled,
                emit_verified=emit_verified,
            )

    def _download_under_quota_lock(
        self,
        authority: PackVMImageAuthority,
        entry: Path,
        pinned: _PinnedEntry,
        progress: Callable[[PackVMImageProgress], None] | None,
        cancelled: Callable[[], bool] | None,
        *,
        emit_verified: bool,
    ) -> PackVMVerifiedImage:
        partial_path = entry / "partial.img"
        metadata_path = entry / "partial.json"
        offset, metadata = self._resume_state(
            authority, partial_path, metadata_path, pinned
        )
        self._require_capacity(entry, authority.size_bytes - offset)
        resumed = offset
        headers = {"Accept-Encoding": "identity", "User-Agent": "Tobkiri-PackVM/1"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
            validator = metadata.get("etag") or metadata.get("last_modified")
            if not validator:
                raise PackVMImageError(
                    "packvm_image_validator_missing",
                    "PackVM image partial has no server validator",
                )
            headers["If-Range"] = str(validator)
        request = urllib.request.Request(authority.source_url, headers=headers, method="GET")
        started = self._monotonic()
        try:
            opener = self._opener
            if opener is None:
                opener = urllib.request.build_opener(_StrictRedirectHandler()).open
            response = opener(request, timeout=self._connect_timeout)
        except PackVMImageError:
            raise
        except (OSError, urllib.error.URLError) as exc:
            raise PackVMImageError(
                "packvm_image_connect_failed", "PackVM image connection failed"
            ) from exc
        with response:
            status = int(getattr(response, "status", response.getcode()))
            final_url = str(response.geturl())
            self._validate_response_url(authority.source_url, final_url)
            response_headers = response.headers
            encoding = str(response_headers.get("Content-Encoding") or "identity").casefold()
            if encoding != "identity":
                raise PackVMImageError(
                    "packvm_image_compression_rejected",
                    "PackVM image response used ambiguous compression",
                )
            etag = _header(response_headers, "ETag")
            last_modified = _header(response_headers, "Last-Modified")
            if offset and status == 206:
                self._validate_partial_response(
                    authority, offset, response_headers, metadata, final_url
                )
            elif status == 200:
                offset = 0
                resumed = 0
            else:
                raise PackVMImageError(
                    "packvm_image_range_rejected", "PackVM image range response is invalid"
                )
            expected_length = authority.size_bytes - offset
            content_length = _integer_header(response_headers, "Content-Length")
            if content_length is None or content_length != expected_length:
                raise PackVMImageError(
                    "packvm_image_length_mismatch",
                    "PackVM image response length does not match signed authority",
                )
            descriptor = self._open_partial(
                partial_path, offset, directory_fd=pinned.entry_descriptor
            )
            try:
                hasher = hashlib.sha256()
                if offset:
                    _hash_descriptor_prefix(descriptor, offset, hasher)
                else:
                    os.ftruncate(descriptor, 0)
                downloaded = offset
                last_activity = self._monotonic()
                next_checkpoint = downloaded + PACKVM_IMAGE_PROGRESS_INTERVAL_BYTES
                while downloaded < authority.size_bytes:
                    if (
                        downloaded < authority.size_bytes
                        and cancelled is not None
                        and cancelled()
                    ):
                        self._checkpoint(
                            authority, pinned, descriptor, metadata_path, downloaded, hasher,
                            etag, last_modified, final_url,
                        )
                        raise PackVMImageCancelled(
                            "packvm_image_cancelled", "PackVM image download was cancelled"
                        )
                    now = self._monotonic()
                    if now - started > self._overall_timeout:
                        raise PackVMImageError(
                            "packvm_image_overall_timeout",
                            "PackVM image download exceeded its overall time bound",
                        )
                    try:
                        read = getattr(response, "read1", response.read)
                        chunk = read(min(
                            PACKVM_IMAGE_READ_BYTES,
                            PACKVM_IMAGE_CHUNK_BYTES,
                            authority.size_bytes - downloaded,
                        ))
                    except (OSError, socket.timeout) as exc:
                        self._checkpoint(
                            authority, pinned, descriptor, metadata_path, downloaded, hasher,
                            etag, last_modified, final_url,
                        )
                        raise PackVMImageError(
                            "packvm_image_inactivity_timeout",
                            "PackVM image download became inactive",
                        ) from exc
                    current = self._monotonic()
                    if current - started > self._overall_timeout:
                        self._checkpoint(
                            authority, pinned, descriptor, metadata_path, downloaded, hasher,
                            etag, last_modified, final_url,
                        )
                        raise PackVMImageError(
                            "packvm_image_overall_timeout",
                            "PackVM image download exceeded its overall time bound",
                        )
                    if not chunk:
                        self._checkpoint(
                            authority, pinned, descriptor, metadata_path, downloaded, hasher,
                            etag, last_modified, final_url,
                        )
                        raise PackVMImageError(
                            "packvm_image_truncated", "PackVM image response was truncated"
                        )
                    if current - last_activity > self._inactivity_timeout:
                        self._checkpoint(
                            authority, pinned, descriptor, metadata_path, downloaded, hasher,
                            etag, last_modified, final_url,
                        )
                        raise PackVMImageError(
                            "packvm_image_inactivity_timeout",
                            "PackVM image download became inactive",
                        )
                    last_activity = current
                    written = os.write(descriptor, chunk)
                    if written != len(chunk):
                        raise PackVMImageError(
                            "packvm_image_write_failed", "PackVM image write was incomplete"
                        )
                    hasher.update(chunk)
                    downloaded += written
                    if (
                        downloaded < authority.size_bytes
                        and cancelled is not None
                        and cancelled()
                    ):
                        self._checkpoint(
                            authority, pinned, descriptor, metadata_path, downloaded, hasher,
                            etag, last_modified, final_url,
                        )
                        raise PackVMImageCancelled(
                            "packvm_image_cancelled", "PackVM image download was cancelled"
                        )
                    if (
                        downloaded < authority.size_bytes
                        and downloaded >= next_checkpoint
                    ):
                        self._checkpoint(
                            authority, pinned, descriptor, metadata_path, downloaded, hasher,
                            etag, last_modified, final_url,
                        )
                        next_checkpoint = downloaded + PACKVM_IMAGE_PROGRESS_INTERVAL_BYTES
                        if progress is not None:
                            progress(PackVMImageProgress(
                                "downloading", downloaded, authority.size_bytes, resumed
                            ))
                if getattr(response, "read1", response.read)(1):
                    raise PackVMImageError(
                        "packvm_image_overrun", "PackVM image response exceeded signed size"
                    )
                if not hmac.compare_digest("sha256:" + hasher.hexdigest(), authority.digest):
                    raise PackVMImageError(
                        "packvm_image_digest_mismatch", "PackVM image digest does not match"
                    )
                os.fsync(descriptor)
                file_metadata = os.fstat(descriptor)
                _require_non_sparse(file_metadata)
                self._require_same_file(
                    partial_path, file_metadata, file_metadata,
                    directory_fd=pinned.entry_descriptor,
                )
                if file_metadata.st_size != authority.size_bytes:
                    raise PackVMImageError(
                        "packvm_image_size_mismatch", "PackVM image size does not match"
                    )
                os.fchmod(descriptor, 0o400)
                file_metadata = os.fstat(descriptor)
            except OSError as exc:
                if exc.errno in {getattr(os, "ENOSPC", 28), 28}:
                    raise PackVMImageError(
                        "packvm_image_disk_exhausted", "PackVM image storage was exhausted"
                    ) from exc
                raise
            finally:
                os.close(descriptor)
        self._require_pinned_identity(entry, pinned)
        os.replace(
            "partial.img", "image.img",
            src_dir_fd=pinned.entry_descriptor,
            dst_dir_fd=pinned.entry_descriptor,
        )
        file_metadata = os.stat(
            "image.img", dir_fd=pinned.entry_descriptor, follow_symlinks=False
        )
        published = {
            **self._immutable_metadata(authority),
            "acquired_plan_digest": authority.plan_digest,
            "acquired_session_digest": authority.session_digest,
            "acquired_operation_id": authority.operation_id,
            "final_url": final_url,
            "etag": etag,
            "last_modified": last_modified,
            "device": file_metadata.st_dev,
            "inode": file_metadata.st_ino,
            "ctime_ns": file_metadata.st_ctime_ns,
            "published_unix": int(time.time()),
            "published_unix_ns": time.time_ns(),
        }
        self._write_authenticated_json(
            entry / "published.json", published, directory_fd=pinned.entry_descriptor
        )
        try:
            os.unlink("partial.json", dir_fd=pinned.entry_descriptor)
        except FileNotFoundError:
            pass
        os.fsync(pinned.entry_descriptor)
        verified = PackVMVerifiedImage(
            path=entry / "image.img",
            digest=authority.digest,
            size_bytes=file_metadata.st_size,
            device=file_metadata.st_dev,
            inode=file_metadata.st_ino,
            source_url=authority.source_url,
        )
        if cancelled is not None and cancelled():
            raise PackVMImageCancelled(
                "packvm_image_cancelled", "PackVM image download was cancelled"
            )
        self._require_pinned_identity(entry, pinned)
        if cancelled is not None and cancelled():
            raise PackVMImageCancelled(
                "packvm_image_cancelled", "PackVM image download was cancelled"
            )
        if emit_verified and progress is not None:
            progress(PackVMImageProgress(
                "verified", authority.size_bytes, authority.size_bytes, resumed
            ))
        self._require_pinned_identity(entry, pinned)
        return verified

    @contextmanager
    def _root_quota_lock(self, pinned: _PinnedEntry) -> Iterator[int]:
        """Hold the stable cache-wide OS reservation until publication finishes."""

        self._capacity_lock.acquire()
        name = "capacity-reservation.lock"
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=pinned.root_descriptor)
        except Exception:
            self._capacity_lock.release()
            raise
        locked = False
        try:
            metadata = os.fstat(descriptor)
            current = os.stat(
                name, dir_fd=pinned.root_descriptor, follow_symlinks=False
            )
            if (
                not _safe_regular(metadata)
                or metadata.st_nlink != 1
                or (current.st_dev, current.st_ino)
                != (metadata.st_dev, metadata.st_ino)
            ):
                raise PackVMImageError(
                    "packvm_image_reservation_unsafe",
                    "PackVM image capacity reservation is unsafe",
                )
            _acquire_portable_lock(descriptor)
            locked = True
            current = os.stat(
                name, dir_fd=pinned.root_descriptor, follow_symlinks=False
            )
            if (current.st_dev, current.st_ino) != (
                metadata.st_dev,
                metadata.st_ino,
            ):
                raise PackVMImageError(
                    "packvm_image_reservation_unsafe",
                    "PackVM image capacity reservation changed",
                )
            yield descriptor
        finally:
            try:
                if locked:
                    _release_portable_lock(descriptor)
            finally:
                os.close(descriptor)
                self._capacity_lock.release()

    def _resume_state(
        self,
        authority: PackVMImageAuthority,
        partial_path: Path,
        metadata_path: Path,
        pinned: _PinnedEntry,
    ) -> tuple[int, dict[str, Any]]:
        try:
            metadata = self._read_authenticated_json(
                metadata_path, directory_fd=pinned.entry_descriptor
            )
        except FileNotFoundError:
            self._delete_uncheckpointed_residue(partial_path.parent, pinned)
            return 0, {}
        expected = self._immutable_metadata(authority)
        if any(metadata.get(key) != value for key, value in expected.items()):
            raise PackVMImageError(
                "packvm_image_partial_binding_mismatch",
                "PackVM image partial belongs to another signed authority",
            )
        descriptor = self._open_regular(
            partial_path, writable=True, directory_fd=pinned.entry_descriptor
        )
        try:
            file_metadata = os.fstat(descriptor)
            _require_non_sparse(file_metadata)
            downloaded = metadata.get("downloaded_bytes")
            if not isinstance(downloaded, int) or not 0 <= downloaded < authority.size_bytes:
                raise PackVMImageError(
                    "packvm_image_partial_size_invalid", "PackVM image checkpoint is invalid"
                )
            if file_metadata.st_size < downloaded:
                raise PackVMImageError(
                    "packvm_image_partial_truncated", "PackVM image partial was truncated"
                )
            os.ftruncate(descriptor, downloaded)
            digest = _descriptor_digest(descriptor, downloaded)
            if not hmac.compare_digest(digest, str(metadata.get("prefix_digest") or "")):
                raise PackVMImageError(
                    "packvm_image_partial_tampered", "PackVM image partial changed"
                )
            self._require_same_file(
                partial_path, file_metadata, os.fstat(descriptor),
                directory_fd=pinned.entry_descriptor,
            )
            return downloaded, metadata
        finally:
            os.close(descriptor)

    def _checkpoint(
        self,
        authority: PackVMImageAuthority,
        pinned: _PinnedEntry,
        descriptor: int,
        metadata_path: Path,
        downloaded: int,
        hasher: Any,
        etag: str | None,
        last_modified: str | None,
        final_url: str,
    ) -> None:
        os.fsync(descriptor)
        self._require_pinned_identity(metadata_path.parent, pinned)
        if etag is None and last_modified is None:
            os.ftruncate(descriptor, 0)
            os.fsync(descriptor)
            try:
                os.unlink(metadata_path.name, dir_fd=pinned.entry_descriptor)
            except FileNotFoundError:
                pass
            os.fsync(pinned.entry_descriptor)
            return
        self._write_authenticated_json(metadata_path, {
            **self._immutable_metadata(authority),
            "downloaded_bytes": downloaded,
            "prefix_digest": "sha256:" + hasher.hexdigest(),
            "etag": etag,
            "last_modified": last_modified,
            "final_url": final_url,
            "checkpoint_unix": int(time.time()),
            "checkpoint_unix_ns": time.time_ns(),
        }, directory_fd=pinned.entry_descriptor)

    def _validate_partial_response(
        self,
        authority: PackVMImageAuthority,
        offset: int,
        headers: Mapping[str, str],
        metadata: Mapping[str, Any],
        final_url: str,
    ) -> None:
        match = _CONTENT_RANGE.fullmatch(str(headers.get("Content-Range") or ""))
        if match is None:
            raise PackVMImageError(
                "packvm_image_content_range_invalid", "PackVM Content-Range is invalid"
            )
        first, last, total = (int(value) for value in match.groups())
        if first != offset or last != authority.size_bytes - 1 or total != authority.size_bytes:
            raise PackVMImageError(
                "packvm_image_content_range_mismatch", "PackVM Content-Range does not match"
            )
        if metadata.get("final_url") != final_url:
            raise PackVMImageError(
                "packvm_image_final_url_changed", "PackVM image final URL changed"
            )
        for field, header in (("etag", "ETag"), ("last_modified", "Last-Modified")):
            old = metadata.get(field)
            new = _header(headers, header)
            if old is not None and new != old:
                raise PackVMImageError(
                    "packvm_image_validator_changed", "PackVM image validator changed"
                )

    def _validate_authority(self, authority: PackVMImageAuthority) -> None:
        parsed = urllib.parse.urlsplit(authority.source_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise PackVMImageError(
                "packvm_image_source_invalid", "PackVM image source must be an HTTPS URL"
            )
        if re.fullmatch(r"sha256:[0-9a-f]{64}", authority.digest) is None:
            raise PackVMImageError(
                "packvm_image_digest_invalid", "PackVM image digest is invalid"
            )
        if authority.size_bytes <= 0:
            raise PackVMImageError("packvm_image_size_invalid", "PackVM image size is invalid")
        for value in (
            authority.platform, authority.architecture, authority.plan_digest,
            authority.session_digest, authority.operation_id,
        ):
            if not value or len(value) > 512:
                raise PackVMImageError(
                    "packvm_image_authority_invalid", "PackVM image authority is incomplete"
                )

    def _validate_response_url(self, source: str, final: str) -> None:
        source_parts = urllib.parse.urlsplit(source)
        if final != source:
            raise PackVMImageError(
                "packvm_image_redirect_rejected", "PackVM image redirect is not authorized"
            )
        if source_parts.scheme != "https":
            raise PackVMImageError(
                "packvm_image_source_invalid", "PackVM image source must be TLS"
            )

    def _immutable_metadata(self, authority: PackVMImageAuthority) -> dict[str, Any]:
        return {
            "version": PACKVM_IMAGE_CACHE_VERSION,
            "source_url": authority.source_url,
            "digest": authority.digest,
            "size_bytes": authority.size_bytes,
            "platform": authority.platform,
            "architecture": authority.architecture,
        }

    def _key(self, authority: PackVMImageAuthority) -> str:
        payload = json.dumps(
            self._immutable_metadata(authority), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    def _ensure_root(self) -> None:
        if self._requested_root != self._root:
            raise PackVMImageError(
                "packvm_image_root_unsafe", "PackVM image root identity changed"
            )
        self._validate_root_chain_identity()
        descriptors, complete = self._create_root_chain_descriptor_relative()
        try:
            expected = dict(self._root_chain_identity)
            if any(
                expected.get(path) != identity
                for path, identity in complete
                if path in expected
            ):
                raise PackVMImageError(
                    "packvm_image_parent_swap",
                    "PackVM image ancestor identity changed",
                )
            # The descriptors remain continuously pinned from creation through
            # initial trust capture. A pathname replacement can therefore
            # never be adopted as the first trusted chain.
            self._root_chain_identity = complete
            self._validate_root_chain_identity()
            if self._signing_key_bytes is None:
                self._signing_key_bytes = self._load_signing_key_pinned(
                    descriptors[-1]
                )
            self._validate_root_chain_identity()
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _create_root_chain_descriptor_relative(
        self,
    ) -> tuple[list[int], tuple[tuple[Path, tuple[int, int]], ...]]:
        """Create and continuously pin every component through trust capture."""

        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        expected = dict(self._root_chain_identity)
        descriptors: list[int] = []
        identities: list[tuple[Path, tuple[int, int]]] = []
        current_path = Path(self._root.anchor)
        try:
            descriptor = os.open(current_path, flags)
            descriptors.append(descriptor)
            anchor_metadata = os.fstat(descriptor)
            identities.append(
                (current_path, (anchor_metadata.st_dev, anchor_metadata.st_ino))
            )
            if expected.get(current_path) != (
                anchor_metadata.st_dev,
                anchor_metadata.st_ino,
            ):
                raise PackVMImageError(
                    "packvm_image_parent_swap",
                    "PackVM image ancestor identity changed",
                )
            for part in self._root.parts[1:]:
                current_path = current_path / part
                try:
                    descriptor = os.open(part, flags, dir_fd=descriptors[-1])
                except FileNotFoundError:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptors[-1])
                    except FileExistsError:
                        pass
                    descriptor = os.open(part, flags, dir_fd=descriptors[-1])
                except OSError as exc:
                    raise PackVMImageError(
                        "packvm_image_parent_swap",
                        "PackVM image ancestor is unsafe",
                    ) from exc
                descriptors.append(descriptor)
                metadata = os.fstat(descriptor)
                identities.append(
                    (current_path, (metadata.st_dev, metadata.st_ino))
                )
                previous = expected.get(current_path)
                if previous is not None and previous != (
                    metadata.st_dev,
                    metadata.st_ino,
                ):
                    raise PackVMImageError(
                        "packvm_image_parent_swap",
                        "PackVM image ancestor identity changed",
                    )
            root_metadata = os.fstat(descriptors[-1])
            if not _safe_directory(root_metadata):
                raise PackVMImageError(
                    "packvm_image_directory_unsafe",
                    "PackVM image directory is unsafe",
                )
            if os.name == "posix" and root_metadata.st_mode & 0o077:
                os.fchmod(descriptors[-1], 0o700)
            return descriptors, tuple(identities)
        except Exception as exc:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            if isinstance(exc, PackVMImageError):
                raise
            if isinstance(exc, OSError):
                raise PackVMImageError(
                    "packvm_image_parent_swap", "PackVM image ancestor is unsafe"
                ) from exc
            raise

    def _load_signing_key_pinned(self, root_descriptor: int) -> bytes:
        """Load or atomically create the cache key through the pinned root."""

        name = "image-cache.key"
        read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(name, read_flags, dir_fd=root_descriptor)
        except FileNotFoundError:
            temporary = f".image-cache-key-{secrets.token_hex(16)}"
            create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            create_flags |= getattr(os, "O_NOFOLLOW", 0)
            temporary_descriptor = os.open(
                temporary, create_flags, 0o600, dir_fd=root_descriptor
            )
            try:
                material = hashlib.sha256(os.urandom(32)).hexdigest().encode("ascii")
                if os.write(temporary_descriptor, material) != len(material):
                    raise PackVMImageError(
                        "packvm_image_key_unsafe",
                        "PackVM image cache key write was incomplete",
                    )
                os.fchmod(temporary_descriptor, 0o600)
                os.fsync(temporary_descriptor)
                try:
                    os.link(
                        temporary,
                        name,
                        src_dir_fd=root_descriptor,
                        dst_dir_fd=root_descriptor,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    pass
                os.unlink(temporary, dir_fd=root_descriptor)
                os.fsync(root_descriptor)
            finally:
                os.close(temporary_descriptor)
                try:
                    os.unlink(temporary, dir_fd=root_descriptor)
                except FileNotFoundError:
                    pass
            descriptor = os.open(name, read_flags, dir_fd=root_descriptor)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_mode & 0o077
                or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
                or not 32 <= metadata.st_size <= 128
            ):
                raise PackVMImageError(
                    "packvm_image_key_unsafe", "PackVM image cache key is unsafe"
                )
            material = os.read(descriptor, 129)
            if len(material) != metadata.st_size or len(material.strip()) < 32:
                raise PackVMImageError(
                    "packvm_image_key_unsafe", "PackVM image cache key is invalid"
                )
            return material.strip()
        finally:
            os.close(descriptor)

    def _capture_root_chain(
        self, *, require_complete: bool
    ) -> tuple[tuple[Path, tuple[int, int]], ...]:
        """Capture every existing non-symlink ancestor of the cache root."""

        captured: list[tuple[Path, tuple[int, int]]] = []
        current = Path(self._root.anchor)
        paths = [current]
        for part in self._root.parts[1:]:
            current = current / part
            paths.append(current)
        for path in paths:
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                if require_complete:
                    raise PackVMImageError(
                        "packvm_image_parent_swap",
                        "PackVM image ancestor identity is incomplete",
                    )
                break
            if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
                raise PackVMImageError(
                    "packvm_image_parent_swap", "PackVM image ancestor is unsafe"
                )
            captured.append((path, (int(metadata.st_dev), int(metadata.st_ino))))
        return tuple(captured)

    def _validate_root_chain_identity(self) -> None:
        """Reject replacement of any previously captured cache-root ancestor."""

        for path, identity in self._root_chain_identity:
            try:
                metadata = path.lstat()
            except FileNotFoundError as exc:
                raise PackVMImageError(
                    "packvm_image_parent_swap", "PackVM image ancestor identity changed"
                ) from exc
            if (
                path.is_symlink()
                or not stat.S_ISDIR(metadata.st_mode)
                or (metadata.st_dev, metadata.st_ino) != identity
            ):
                raise PackVMImageError(
                    "packvm_image_parent_swap", "PackVM image ancestor identity changed"
                )

    def _open_root_chain(self) -> list[int]:
        """Open the complete root chain descriptor-relatively without following links."""

        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        expected = dict(self._root_chain_identity)
        descriptors: list[int] = []
        current_path = Path(self._root.anchor)
        try:
            descriptor = os.open(current_path, flags)
            descriptors.append(descriptor)
            metadata = os.fstat(descriptor)
            if expected.get(current_path) != (metadata.st_dev, metadata.st_ino):
                raise PackVMImageError(
                    "packvm_image_parent_swap", "PackVM image ancestor identity changed"
                )
            for part in self._root.parts[1:]:
                current_path = current_path / part
                descriptor = os.open(part, flags, dir_fd=descriptors[-1])
                descriptors.append(descriptor)
                metadata = os.fstat(descriptor)
                if expected.get(current_path) != (metadata.st_dev, metadata.st_ino):
                    raise PackVMImageError(
                        "packvm_image_parent_swap",
                        "PackVM image ancestor identity changed",
                    )
            return descriptors
        except Exception:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            raise

    @contextmanager
    def _pinned_entry(self, entry: Path, *, create: bool) -> Iterator[_PinnedEntry]:
        """Pin root and direct-child entry directories against parent swaps."""

        directory_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            directory_flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW
        chain_descriptors = self._open_root_chain()
        root_descriptor = chain_descriptors[-1]
        entry_descriptor = -1
        try:
            root_metadata = os.fstat(root_descriptor)
            if not _safe_directory(root_metadata):
                raise PackVMImageError(
                    "packvm_image_root_unsafe", "PackVM image root is unsafe"
                )
            if create:
                try:
                    os.mkdir(entry.name, mode=0o700, dir_fd=root_descriptor)
                except FileExistsError:
                    pass
            try:
                entry_descriptor = os.open(
                    entry.name, directory_flags, dir_fd=root_descriptor
                )
            except FileNotFoundError:
                raise
            except OSError as exc:
                raise PackVMImageError(
                    "packvm_image_directory_unsafe", "PackVM image entry is unsafe"
                ) from exc
            entry_metadata = os.fstat(entry_descriptor)
            if not _safe_directory(entry_metadata):
                raise PackVMImageError(
                    "packvm_image_directory_unsafe", "PackVM image entry is unsafe"
                )
            pinned = _PinnedEntry(
                root_descriptor=root_descriptor,
                entry_descriptor=entry_descriptor,
                root_device=root_metadata.st_dev,
                root_inode=root_metadata.st_ino,
                entry_device=entry_metadata.st_dev,
                entry_inode=entry_metadata.st_ino,
                entry_name=entry.name,
            )
            self._require_pinned_identity(entry, pinned)
            yield pinned
        finally:
            if entry_descriptor >= 0:
                os.close(entry_descriptor)
            for descriptor in reversed(chain_descriptors):
                os.close(descriptor)

    def _require_pinned_identity(self, entry: Path, pinned: _PinnedEntry) -> None:
        """Revalidate pathname and open-directory identities before publication."""

        self._validate_root_chain_identity()
        try:
            root_metadata = self._root.lstat()
            entry_metadata = entry.lstat()
        except OSError as exc:
            raise PackVMImageError(
                "packvm_image_parent_swap", "PackVM image parent identity changed"
            ) from exc
        if (
            self._root.is_symlink()
            or entry.is_symlink()
            or (root_metadata.st_dev, root_metadata.st_ino)
            != (pinned.root_device, pinned.root_inode)
            or (entry_metadata.st_dev, entry_metadata.st_ino)
            != (pinned.entry_device, pinned.entry_inode)
            or (os.fstat(pinned.root_descriptor).st_dev, os.fstat(pinned.root_descriptor).st_ino)
            != (pinned.root_device, pinned.root_inode)
            or (os.fstat(pinned.entry_descriptor).st_dev, os.fstat(pinned.entry_descriptor).st_ino)
            != (pinned.entry_device, pinned.entry_inode)
        ):
            raise PackVMImageError(
                "packvm_image_parent_swap", "PackVM image parent identity changed"
            )

    @contextmanager
    def _exclusive_lock(self, pinned: _PinnedEntry, name: str) -> Iterator[int]:
        """Hold one stable content-key lock across an entry mutation.

        The lock lives in the root rather than the removable entry, and is never
        deleted.  Different generations can therefore be collected while the
        current image remains pinned, without a split lock inode on recreation.
        """

        del name
        if re.fullmatch(r"[0-9a-f]{64}", pinned.entry_name) is None:
            raise PackVMImageError(
                "packvm_image_lock_unsafe", "PackVM image lock key is unsafe"
            )
        lock_name = f"entry-{pinned.entry_name}.lock"
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(
            lock_name, flags, 0o600, dir_fd=pinned.root_descriptor
        )
        locked = False
        try:
            metadata = os.fstat(descriptor)
            current = os.stat(
                lock_name,
                dir_fd=pinned.root_descriptor,
                follow_symlinks=False,
            )
            if (
                not _safe_regular(metadata)
                or metadata.st_nlink != 1
                or (current.st_dev, current.st_ino)
                != (metadata.st_dev, metadata.st_ino)
            ):
                raise PackVMImageError(
                    "packvm_image_lock_unsafe", "PackVM image lock is unsafe"
                )
            try:
                _try_portable_lock(descriptor)
                locked = True
            except (BlockingIOError, OSError) as exc:
                raise PackVMImageError(
                    "packvm_image_concurrent_writer",
                    "Another PackVM image writer is active",
                ) from exc
            current = os.stat(
                lock_name,
                dir_fd=pinned.root_descriptor,
                follow_symlinks=False,
            )
            if (current.st_dev, current.st_ino) != (
                metadata.st_dev,
                metadata.st_ino,
            ):
                raise PackVMImageError(
                    "packvm_image_lock_unsafe", "PackVM image lock identity changed"
                )
            yield descriptor
        finally:
            try:
                if locked:
                    _release_portable_lock(descriptor)
            finally:
                os.close(descriptor)

    def _remove_locked_entry(
        self, entry: Path, pinned: _PinnedEntry, lock_descriptor: int
    ) -> None:
        """Remove an empty entry while the stable cache-root lock is held."""

        self._require_pinned_identity(entry, pinned)
        lock_metadata = os.fstat(lock_descriptor)
        current = os.stat(
            f"entry-{pinned.entry_name}.lock",
            dir_fd=pinned.root_descriptor,
            follow_symlinks=False,
        )
        if not _safe_regular(lock_metadata) or lock_metadata.st_nlink != 1 or (
            current.st_dev,
            current.st_ino,
        ) != (lock_metadata.st_dev, lock_metadata.st_ino):
            raise PackVMImageError(
                "packvm_image_lock_unsafe", "PackVM image lock identity changed"
            )
        # Remove legacy entry-local locks only under the stable root lock.
        self._unlink_regular_if_present(entry / "download.lock", pinned)
        os.fsync(pinned.entry_descriptor)
        os.rmdir(entry.name, dir_fd=pinned.root_descriptor)
        os.fsync(pinned.root_descriptor)

    def _open_regular(
        self, path: Path, *, writable: bool, directory_fd: int | None = None
    ) -> int:
        flags = os.O_RDWR if writable else os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        target: str | Path = path.name if directory_fd is not None else path
        descriptor = os.open(target, flags, dir_fd=directory_fd)
        metadata = os.fstat(descriptor)
        if not _safe_regular(metadata) or metadata.st_nlink != 1:
            os.close(descriptor)
            raise PackVMImageError(
                "packvm_image_file_unsafe", "PackVM image file is unsafe"
            )
        return descriptor

    def _open_partial(
        self, path: Path, offset: int, *, directory_fd: int | None = None
    ) -> int:
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        target: str | Path = path.name if directory_fd is not None else path
        descriptor = os.open(target, flags, 0o600, dir_fd=directory_fd)
        metadata = os.fstat(descriptor)
        if not _safe_regular(metadata) or metadata.st_nlink != 1:
            os.close(descriptor)
            raise PackVMImageError(
                "packvm_image_partial_unsafe", "PackVM image partial is unsafe"
            )
        os.lseek(descriptor, offset, os.SEEK_SET)
        return descriptor

    def _require_same_file(
        self,
        path: Path,
        before: os.stat_result,
        after: os.stat_result,
        *,
        directory_fd: int | None = None,
    ) -> None:
        current = (
            os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            if directory_fd is not None
            else path.lstat()
        )
        identities = ((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
        if (
            (current.st_dev, current.st_ino) != identities[0]
            or identities[0] != identities[1]
            or current.st_nlink != 1
        ):
            raise PackVMImageError(
                "packvm_image_path_swap", "PackVM image path identity changed"
            )

    def _require_capacity(self, path: Path, size_bytes: int) -> None:
        try:
            free = int(self._disk_usage(path).free)
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            raise PackVMImageError(
                "packvm_image_disk_unknown", "PackVM image disk capacity is unavailable"
            ) from exc
        if free < size_bytes + PACKVM_IMAGE_DISK_RESERVE_BYTES:
            raise PackVMImageError(
                "packvm_image_disk_insufficient",
                "PackVM image cache has insufficient bounded free space",
            )

    def _delete_uncheckpointed_residue(
        self, entry: Path, pinned: _PinnedEntry
    ) -> None:
        """Remove one safe unpublished partial or post-rename image residue."""

        try:
            name, _metadata = self._uncheckpointed_residue(entry, pinned)
        except FileNotFoundError:
            return
        self._unlink_regular_if_present(entry / name, pinned)
        os.fsync(pinned.entry_descriptor)

    def _uncheckpointed_residue(
        self, entry: Path, pinned: _PinnedEntry
    ) -> tuple[str, os.stat_result]:
        """Return exactly one owned uncheckpointed data inode."""

        found: list[tuple[str, os.stat_result]] = []
        for name in ("partial.img", "image.img"):
            try:
                descriptor = self._open_regular(
                    entry / name,
                    writable=False,
                    directory_fd=pinned.entry_descriptor,
                )
            except FileNotFoundError:
                continue
            try:
                metadata = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            found.append((name, metadata))
        if not found:
            raise FileNotFoundError(entry)
        if len(found) != 1:
            raise PackVMImageError(
                "packvm_image_uncheckpointed_unsafe",
                "PackVM uncheckpointed image residue is ambiguous",
            )
        return found[0]

    def _unlink_regular_if_present(self, path: Path, pinned: _PinnedEntry) -> None:
        try:
            descriptor = self._open_regular(
                path, writable=False, directory_fd=pinned.entry_descriptor
            )
        except FileNotFoundError:
            return
        else:
            os.close(descriptor)
        self._require_pinned_identity(path.parent, pinned)
        os.unlink(path.name, dir_fd=pinned.entry_descriptor)

    def _owned_entry_metadata(
        self, entry: Path, pinned: _PinnedEntry
    ) -> dict[str, Any] | None:
        """Return authenticated metadata only when its content key owns entry."""

        metadata: dict[str, Any] | None = None
        for name in ("published.json", "partial.json"):
            try:
                metadata = self._read_authenticated_json(
                    entry / name, directory_fd=pinned.entry_descriptor
                )
                break
            except FileNotFoundError:
                continue
        if metadata is None:
            return None
        immutable = {
            key: metadata.get(key)
            for key in (
                "version", "source_url", "digest", "size_bytes", "platform",
                "architecture",
            )
        }
        encoded = json.dumps(
            immutable, sort_keys=True, separators=(",", ":")
        ).encode()
        if hashlib.sha256(encoded).hexdigest() != entry.name:
            raise PackVMImageError(
                "packvm_image_gc_binding_mismatch",
                "PackVM image entry key does not match authenticated content",
            )
        return metadata

    def _read_authenticated_json(
        self, path: Path, *, directory_fd: int | None = None
    ) -> dict[str, Any]:
        descriptor = self._open_regular(
            path, writable=False, directory_fd=directory_fd
        )
        try:
            metadata = os.fstat(descriptor)
            if metadata.st_size > _MAX_METADATA_BYTES:
                raise PackVMImageError(
                    "packvm_image_metadata_oversize", "PackVM image metadata is too large"
                )
            raw = os.read(descriptor, _MAX_METADATA_BYTES + 1)
        finally:
            os.close(descriptor)
        try:
            payload = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise PackVMImageError(
                "packvm_image_metadata_invalid", "PackVM image metadata is invalid"
            ) from exc
        if not isinstance(payload, dict):
            raise PackVMImageError(
                "packvm_image_metadata_invalid", "PackVM image metadata is invalid"
            )
        authentication = payload.pop("authentication", None)
        expected = self._authenticate(payload)
        if not isinstance(authentication, str) or not hmac.compare_digest(
            authentication, expected
        ):
            raise PackVMImageError(
                "packvm_image_metadata_unauthenticated",
                "PackVM image metadata authentication failed",
            )
        return payload

    def _write_authenticated_json(
        self,
        path: Path,
        payload: Mapping[str, Any],
        *,
        directory_fd: int | None = None,
    ) -> None:
        content = dict(payload)
        content["authentication"] = self._authenticate(content)
        encoded = json.dumps(content, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        temporary = f".packvm-image-{secrets.token_hex(16)}"
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if directory_fd is None:
            descriptor = os.open(path.parent / temporary, flags, 0o600)
        else:
            descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, encoded)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            if directory_fd is None:
                os.replace(path.parent / temporary, path)
                _fsync_directory(path.parent)
            else:
                os.replace(
                    temporary, path.name,
                    src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                )
                os.fsync(directory_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                if directory_fd is None:
                    os.unlink(path.parent / temporary)
                else:
                    os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass

    def _authenticate(self, payload: Mapping[str, Any]) -> str:
        key = self._signing_key_bytes
        if key is None:
            raise PackVMImageError(
                "packvm_image_key_unavailable", "PackVM image key is unavailable"
            )
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hmac.new(key, encoded, hashlib.sha256).hexdigest()


def _positive_bound(value: float, maximum: float) -> float:
    number = float(value)
    if number <= 0 or number > maximum:
        raise ValueError("PackVM image timeout policy is outside its bound")
    return number


def _safe_regular(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and (not hasattr(os, "getuid") or metadata.st_uid == os.getuid())
        and (os.name != "posix" or not metadata.st_mode & 0o022)
    )


def _safe_directory(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(metadata.st_mode)
        and (not hasattr(os, "getuid") or metadata.st_uid == os.getuid())
        and (os.name != "posix" or not metadata.st_mode & 0o022)
    )


def _try_portable_lock(descriptor: int) -> None:
    """Acquire one non-blocking lock without importing a foreign backend."""

    if os.name == "nt":
        backend = importlib.import_module("msvcrt")
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        backend.locking(descriptor, backend.LK_NBLCK, 1)
        return
    backend = importlib.import_module("fcntl")
    backend.flock(descriptor, backend.LOCK_EX | backend.LOCK_NB)


def _acquire_portable_lock(descriptor: int) -> None:
    """Acquire one blocking cache-wide reservation released by process exit."""

    if os.name == "nt":
        backend = importlib.import_module("msvcrt")
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        backend.locking(descriptor, backend.LK_LOCK, 1)
        return
    backend = importlib.import_module("fcntl")
    backend.flock(descriptor, backend.LOCK_EX)


def _release_portable_lock(descriptor: int) -> None:
    """Release the platform lock held by ``_try_portable_lock``."""

    if os.name == "nt":
        backend = importlib.import_module("msvcrt")
        os.lseek(descriptor, 0, os.SEEK_SET)
        backend.locking(descriptor, backend.LK_UNLCK, 1)
        return
    backend = importlib.import_module("fcntl")
    backend.flock(descriptor, backend.LOCK_UN)


def _require_non_sparse(metadata: os.stat_result) -> None:
    """Reject attacker-created sparse partials and allocation ambiguity."""

    blocks = getattr(metadata, "st_blocks", None)
    if isinstance(blocks, int) and metadata.st_size > 0 and blocks * 512 < metadata.st_size:
        raise PackVMImageError(
            "packvm_image_sparse_rejected", "PackVM image file is unexpectedly sparse"
        )


def _descriptor_digest(
    descriptor: int,
    size_bytes: int,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> str:
    hasher = hashlib.sha256()
    _hash_descriptor_prefix(descriptor, size_bytes, hasher, cancelled=cancelled)
    return "sha256:" + hasher.hexdigest()


def _hash_descriptor_prefix(
    descriptor: int,
    size_bytes: int,
    hasher: Any,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    remaining = size_bytes
    while remaining:
        if cancelled is not None and cancelled():
            raise PackVMImageCancelled(
                "packvm_image_cancelled", "PackVM image verification was cancelled"
            )
        chunk = os.read(descriptor, min(PACKVM_IMAGE_CHUNK_BYTES, remaining))
        if not chunk:
            raise PackVMImageError(
                "packvm_image_partial_truncated", "PackVM image data was truncated"
            )
        hasher.update(chunk)
        remaining -= len(chunk)
    os.lseek(descriptor, size_bytes, os.SEEK_SET)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    value = headers.get(name)
    return str(value).strip() if value is not None and str(value).strip() else None


def _integer_header(headers: Mapping[str, str], name: str) -> int | None:
    value = _header(headers, name)
    if value is None or not value.isdigit():
        return None
    return int(value)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "PackVMImageAuthority",
    "PackVMImageCache",
    "PackVMImageCancelled",
    "PackVMImageError",
    "PackVMImageProgress",
    "PackVMVerifiedImage",
]
