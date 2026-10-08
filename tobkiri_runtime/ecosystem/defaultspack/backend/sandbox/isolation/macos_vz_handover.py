"""Finite development-Host transfer of quiescent, immutable VZ storage.

Old keys, registrations and domain storage stay with the old Host. Only the
verified base image and the base instance metadata move. Callers must obtain
native consent for the exact plan; this backend never interprets an approval
flag or stops a process. An interrupted transfer denies normal destination
execution until its exact storage has been recovered.
"""

from __future__ import annotations

from contextlib import ExitStack
import ctypes
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
import sys
import subprocess
import time
from typing import Any, Callable, Mapping, TYPE_CHECKING

from core_runtime.process_identity import process_start_identity
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.durability import flush_directory
from tobkiri_protocol.ids import validate_artifact_digest
from tobkiri_protocol.secure_persistence import SecureDirectory

if TYPE_CHECKING:
    from .macos_vz_provisioner import MacOSVZProvisioner

JOURNAL_NAME = "development-host-handover.json"
SCHEMA = "io.tobkiri.development-vz-handover.v1"
_MAX_JOURNAL = 128 * 1024


def require_finalized_handover(state_root: Path, state: Mapping[str, Any]) -> None:
    """Keep unfinished destination transfers out of ordinary VZ execution."""
    storage = SecureDirectory(state_root, create=False)
    if not storage.exists(JOURNAL_NAME):
        if state.get("host_handover_plan_digest") is not None:
            raise ValueError("PackVM Host handover requires its authenticated recovery journal")
        return
    journal = _read_journal(state_root)
    if (
        not isinstance(journal, dict)
        or journal.get("schema") != SCHEMA
        or journal.get("stage") != "finalized"
        or journal.get("plan", {}).get("plan_digest") != state.get("host_handover_plan_digest")
    ):
        raise ValueError("PackVM Host handover requires exact recovery")
    initial_digest = journal.get("attestation_digest")
    if initial_digest != state.get("attestation_digest"):
        if not isinstance(initial_digest, str):
            raise ValueError("PackVM Host handover predecessor is missing")
        validate_artifact_digest(initial_digest, field="handover_attestation_digest")
        # Normal registration updates retain their authenticated predecessor.
        # The finalized handover remains valid across that existing ceremony.
        retained = _read_authenticated_json(
            state_root, f"registration-history/{initial_digest[7:]}.json"
        )
        if retained.get("attestation_digest") != initial_digest or retained.get(
            "host_handover_plan_digest"
        ) != state.get("host_handover_plan_digest"):
            raise ValueError("PackVM Host handover predecessor changed")


class MacOSVZStorageHandover:
    """Bind two independently authenticated provisioners to finite storage."""

    def __init__(
        self,
        source: MacOSVZProvisioner,
        target: MacOSVZProvisioner,
        *,
        source_host_digest: str,
        target_host_digest: str,
        owners: Mapping[int, str],
        fault: Callable[[str], None] | None = None,
    ) -> None:
        validate_artifact_digest(source_host_digest, field="source_host_digest")
        validate_artifact_digest(target_host_digest, field="target_host_digest")
        if source_host_digest == target_host_digest:
            raise ValueError("Host handover requires different development builds")
        self.source = source
        self.target = target
        self.source_host_digest = source_host_digest
        self.target_host_digest = target_host_digest
        self.owners = dict(owners)
        if any(
            type(pid) is not int or pid <= 0 or not isinstance(identity, str) or not identity
            for pid, identity in owners.items()
        ):
            raise ValueError("Host handover owner evidence is invalid")
        self._fault = fault or (lambda _stage: None)

    def prepare(self) -> dict[str, Any]:
        """Verify the old registration in place; never export its signing key."""
        source_root = self.source.state_path.parent
        target_root = self.target.state_path.parent
        _namespace(source_root, self.source_host_digest)
        _namespace(target_root, self.target_host_digest)
        if source_root.stat().st_dev != target_root.stat().st_dev:
            raise ValueError("Host handover requires one filesystem")
        if self.target.state_path.exists() or self.target.state_path.is_symlink():
            raise ValueError("Host handover destination is already registered")
        if (target_root / JOURNAL_NAME).exists() or (target_root / JOURNAL_NAME).is_symlink():
            raise ValueError("Host handover destination requires recovery")
        if (target_root / "packvm-vz-attestation.key").exists():
            raise ValueError("Host handover destination key already exists")
        retained_domains = _retained_source_domains(source_root)
        if retained_domains["entries"]:
            self._require_owners_exited()
        _empty_domains(target_root)
        state = self.source._load_state()
        manifest = self.target._require_manifest()
        self.source._verify_registration_source(state, manifest)
        instance = source_root / "instances" / str(state["instance"])
        # No EFI, seed, guest key, COW or unknown instance entry is transferred.
        if {child.name for child in instance.iterdir()} != {"base-image.json"}:
            raise ValueError("Host handover base instance contains unreviewed storage")
        instance_metadata = SecureDirectory(instance, create=False).read_bytes_bounded(
            "base-image.json", max_bytes=2 * 1024 * 1024
        )
        base = Path(str(state["base_image_path"]))
        if not base.is_relative_to(source_root / "image-cache"):
            raise ValueError("Host handover image is outside the owned cache")
        relative = base.relative_to(source_root)
        _private_path(base, directory=False)
        if base.stat().st_size != manifest.image_size_bytes:
            raise ValueError("Host handover base image size changed")
        destination_instance = target_root / "instances" / instance.name
        destination_base = target_root / relative
        for destination in (destination_instance, destination_base):
            if destination.exists() or destination.is_symlink():
                raise ValueError("Host handover destination storage already exists")
        facts = {
            "schema": SCHEMA,
            "source_host_digest": self.source_host_digest,
            "target_host_digest": self.target_host_digest,
            "source_root": str(source_root),
            "target_root": str(target_root),
            "source_root_identity": _identity(source_root),
            "target_root_identity": _identity(target_root),
            "source_attestation_digest": str(state["attestation_digest"]),
            "target_manifest_digest": manifest.manifest_digest,
            "previous_helper_digest": state["helper_digest"],
            "target_helper_digest": manifest.helper_digest,
            "previous_guest_runner_digest": state["guest_runner_digest"],
            "target_guest_runner_digest": manifest.agent_digest,
            "previous_config_digest": state["cloud_template_digest"],
            "target_config_digest": manifest.config_digest,
            "image_digest": manifest.image_digest,
            "image_source": manifest.image_source,
            "instance_metadata_digest": "sha256:" + hashlib.sha256(instance_metadata).hexdigest(),
            "owners": [
                {"pid": pid, "identity": identity} for pid, identity in sorted(self.owners.items())
            ],
            "moves": [
                {
                    "source": str(instance),
                    "target": str(destination_instance),
                    "identity": _identity(instance),
                },
                {"source": str(base), "target": str(destination_base), "identity": _identity(base)},
            ],
            "image_download_required": False,
            "fresh_authority_required": True,
            "live_guest_resume": False,
            "retained_source_domain_storage": retained_domains,
        }
        plan = {**facts, "plan_digest": canonical_digest(facts)}
        if len(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()) > 64 * 1024:
            raise ValueError("Host handover storage plan exceeds its review limit")
        return plan

    def commit(
        self, plan: Mapping[str, Any], *, session_digest: str, ceremony_nonce: str
    ) -> dict[str, Any]:
        """Transfer an already consented plan under both normal mutation gates."""
        validate_artifact_digest(session_digest, field="session_digest")
        if not isinstance(ceremony_nonce, str) or not 32 <= len(ceremony_nonce) <= 128:
            raise ValueError("Host handover ceremony nonce is invalid")
        self._require_owners_exited()
        if dict(plan) != self.prepare():
            raise ValueError("Host handover plan changed after native review")
        binding = {"handover_plan_digest": str(plan["plan_digest"])}
        with ExitStack() as stack:
            for provisioner in sorted(
                (self.source, self.target), key=lambda item: str(item.state_path)
            ):
                stack.enter_context(provisioner.operation_gate("prepare", binding))
            self._require_owners_exited()
            if dict(plan) != self.prepare():
                raise ValueError("Host handover plan changed before publication")
            state = self.source._load_state()
            manifest = self.target._require_manifest()
            journal = {"schema": SCHEMA, "stage": "prepared", "plan": dict(plan), "moved": 0}
            self._write_journal(journal)
            self._fault("prepared")
            for index, move in enumerate(plan["moves"]):
                destination = Path(move["target"])
                _ensure_private_parents(destination.parent, self.target.state_path.parent)
                _move_exclusive(Path(move["source"]), destination, move["identity"])
                journal.update(stage="moving", moved=index + 1)
                self._write_journal(journal)
                self._fault(f"moved-{index + 1}")
            instance = Path(plan["moves"][0]["target"])
            # Build a fresh record from an allowlist. Old session, grants,
            # registration MAC, domain/channel/recovery keys never enter it.
            fresh = {
                "version": state["version"],
                "backend_id": state["backend_id"],
                "platform": state["platform"],
                "instance": state["instance"],
                "session_digest": session_digest,
                "plan_digest": plan["plan_digest"],
                "ceremony_nonce_digest": canonical_digest({"nonce": ceremony_nonce}),
                "image_digest": manifest.image_digest,
                "image_source": manifest.image_source,
                "cloud_template_digest": manifest.config_digest,
                "helper_digest": manifest.helper_digest,
                "guest_runner_digest": manifest.agent_digest,
                "bubblewrap_digest": manifest.bubblewrap_digest,
                "host_build_digest": manifest.helper_digest,
                "instance_root": str(instance),
                "instance_root_device": instance.stat().st_dev,
                "instance_root_inode": instance.stat().st_ino,
                "base_image_path": plan["moves"][1]["target"],
                "protocol_ready": True,
                "stopped": False,
                "created_unix": int(time.time()),
                "previous_attestation_digest": state["attestation_digest"],
                "host_handover_plan_digest": plan["plan_digest"],
                **self.target.recovery_identity(),
            }
            registered = self.target._write_attested_state(fresh)
            journal.update(stage="registered", attestation_digest=registered["attestation_digest"])
            self._write_journal(journal)
            self._fault("registered")
            # Recheck identities and byte integrity before the execution fence
            # opens. A publication failure leaves exact recovery evidence.
            for move in plan["moves"]:
                if _identity(Path(move["target"])) != move["identity"]:
                    raise ValueError("Host handover storage changed during publication")
            _require_metadata_digest(instance, str(plan["instance_metadata_digest"]))
            self.target._verify_state_bindings(registered, manifest)
            self._require_owners_exited()
            self._require_retained_domains(plan)
            self.target._audit("host-handover", str(plan["plan_digest"]))
            journal["stage"] = "finalized"
            self._write_journal(journal)
            return {
                "plan_digest": plan["plan_digest"],
                "attestation_digest": registered["attestation_digest"],
                "stage": "finalized",
            }

    def recover_to_source(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        """Restore exact moved storage after separate native rollback consent.

        Never erase destination records or keys. An old Host can authenticate
        its original record again after these same inodes return. Destination
        execution remains fenced permanently for this rejected transaction.
        """
        self._require_owners_exited()
        root = self.target.state_path.parent
        self._validate_recovery_plan(plan)
        journal = _read_journal(root)
        allowed = self._recovery_stages(plan)
        if (
            journal.get("schema") != SCHEMA
            or journal.get("plan") != dict(plan)
            or journal.get("stage") not in allowed
        ):
            raise ValueError("Host handover rollback requires an unfinished exact journal")
        binding = {"handover_plan_digest": str(plan["plan_digest"])}
        with ExitStack() as stack:
            for provisioner in sorted(
                (self.source, self.target), key=lambda item: str(item.state_path)
            ):
                stack.enter_context(provisioner.operation_gate("prepare", binding))
            self._require_owners_exited()
            # A finalized transaction may have won while we waited for its
            # mutation gates. Never act on the earlier journal snapshot.
            journal = _read_journal(root)
            if journal.get("plan") != dict(plan) or journal.get(
                "stage"
            ) not in self._recovery_stages(plan):
                raise ValueError("Host handover rollback journal changed")
            self._validate_recovery_plan(plan)
            _empty_domains(root)
            if journal.get("stage") == "rolled-back":
                for move in plan["moves"]:
                    if (
                        Path(move["target"]).exists()
                        or Path(move["target"]).is_symlink()
                        or _identity(Path(move["source"])) != move["identity"]
                    ):
                        raise ValueError("Host handover recovered storage changed")
                self.source._verify_registration_source(
                    self.source._load_state(), self.target._require_manifest()
                )
                _require_metadata_digest(
                    Path(plan["moves"][0]["source"]), str(plan["instance_metadata_digest"])
                )
                self._require_owners_exited()
                self._require_retained_domains(plan)
                return {"plan_digest": plan["plan_digest"], "stage": "rolled-back"}
            for move in reversed(plan["moves"]):
                source, destination = Path(move["source"]), Path(move["target"])
                # Infer durable rename completion from exact object identities,
                # including a crash between rename and journal publication.
                if (
                    source.exists()
                    and not destination.exists()
                    and _identity(source) == move["identity"]
                ):
                    continue
                if source.exists() or source.is_symlink() or not destination.exists():
                    raise ValueError("Host handover rollback storage is ambiguous")
                _move_exclusive(destination, source, move["identity"])
            _require_metadata_digest(
                Path(plan["moves"][0]["source"]), str(plan["instance_metadata_digest"])
            )
            self.source._verify_registration_source(
                self.source._load_state(), self.target._require_manifest()
            )
            self._require_owners_exited()
            self._require_retained_domains(plan)
            journal["stage"] = "rolled-back"
            self._write_journal(journal)
            return {"plan_digest": plan["plan_digest"], "stage": "rolled-back"}

    def _require_owners_exited(self) -> None:
        for pid in self.owners:
            # PID reuse is deliberately conservative: any live process blocks.
            if process_start_identity(pid).state != "dead":
                raise ValueError("Host handover requires all previous owners to exit")
        _require_storage_quiescent(self.source.state_path.parent.parent.parent)

    def _recovery_stages(self, plan: Mapping[str, Any]) -> set[str]:
        allowed = {"prepared", "moving", "registered", "rolled-back"}
        # A completed VM subtransaction may be restored only while its owning
        # Host/data transaction still fences activation. The old Native
        # consent cannot authorize this recovery: the coordinator obtains a
        # fresh, exact Native rollback approval for the authenticated journal.
        from core_runtime.development_host_handover_guard import KEY, JOURNAL, read_handover_journal

        root = self.target.state_path.parent.parent
        # Standalone unfinished storage transfers may restore their own exact
        # journal. A finalized one needs the authenticated owning transaction.
        witnesses = (root / KEY, root / JOURNAL, root / "development-host-staging")
        transaction = (
            read_handover_journal(root)
            if any(path.exists() or path.is_symlink() for path in witnesses)
            else None
        )
        if (
            transaction is not None
            and transaction.get("stage") not in {"completed", "rolled-back"}
            and transaction.get("plan", {}).get("vm_plan") == dict(plan)
            and not (root / "profiles" / "active.json").exists()
            and not (root / "profiles" / "active.json").is_symlink()
        ):
            allowed.add("finalized")
        return allowed

    def _validate_recovery_plan(self, plan: Mapping[str, Any]) -> None:
        facts = {key: value for key, value in plan.items() if key != "plan_digest"}
        if canonical_digest(facts) != plan.get("plan_digest"):
            raise ValueError("Host handover recovery plan digest changed")
        self._require_retained_domains(plan)
        source_root, target_root = self.source.state_path.parent, self.target.state_path.parent
        for root, digest, prefix in (
            (source_root, self.source_host_digest, "source"),
            (target_root, self.target_host_digest, "target"),
        ):
            _namespace(root, digest)
            if (
                plan.get(f"{prefix}_host_digest") != digest
                or plan.get(f"{prefix}_root") != str(root)
                or plan.get(f"{prefix}_root_identity") != _identity(root)
            ):
                raise ValueError("Host handover recovery namespace changed")
        expected_owners = [
            {"pid": pid, "identity": identity} for pid, identity in sorted(self.owners.items())
        ]
        state = self.source._load_state()
        manifest = self.target._require_manifest()
        base = Path(str(state["base_image_path"]))
        instance = source_root / "instances" / str(state["instance"])
        moves = plan.get("moves")
        if (
            not base.is_relative_to(source_root / "image-cache")
            or not isinstance(moves, list)
            or len(moves) != 2
        ):
            raise ValueError("Host handover recovery storage is invalid")
        expected_paths = [
            (instance, target_root / "instances" / instance.name),
            (base, target_root / base.relative_to(source_root)),
        ]
        if any(
            set(move) != {"source", "target", "identity"}
            or move["source"] != str(source)
            or move["target"] != str(target)
            for move, (source, target) in zip(moves, expected_paths)
        ):
            raise ValueError("Host handover recovery paths changed")
        if (
            plan.get("owners") != expected_owners
            or plan.get("source_attestation_digest") != state["attestation_digest"]
            or plan.get("target_manifest_digest") != manifest.manifest_digest
            or plan.get("image_digest") != manifest.image_digest
            or plan.get("image_source") != manifest.image_source
        ):
            raise ValueError("Host handover recovery authority binding changed")

    def _require_retained_domains(self, plan: Mapping[str, Any]) -> None:
        if plan.get("retained_source_domain_storage") != _retained_source_domains(
            self.source.state_path.parent
        ):
            raise ValueError("Host handover retained source domain storage changed")

    def _write_journal(self, journal: Mapping[str, Any]) -> None:
        signed = {key: value for key, value in journal.items() if key != "authentication"}
        signed["authentication"] = self.target._sign_state(signed)
        raw = json.dumps(signed, sort_keys=True, separators=(",", ":")).encode()
        if len(raw) > _MAX_JOURNAL:
            raise ValueError("Host handover journal is too large")
        SecureDirectory(self.target.state_path.parent, create=False).write_bytes_atomic(
            JOURNAL_NAME, raw
        )


def _namespace(root: Path, host_digest: str) -> None:
    if (
        root.name != "packvm-vz"
        or root.parent.name != "user_data"
        or root.parent.parent.name != host_digest[7:]
        or root.parent.parent.parent.name != "builds"
    ):
        raise ValueError("Host handover requires exact development namespaces")
    _private_path(root, directory=True)


def _private_path(path: Path, *, directory: bool) -> None:
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("Host handover path is redirected")
    metadata = path.lstat()
    correct_type = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if (
        not correct_type
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
        or (not directory and metadata.st_nlink != 1)
    ):
        raise ValueError("Host handover storage is not private")


def _identity(path: Path) -> dict[str, int]:
    metadata = path.lstat()
    _private_path(path, directory=stat.S_ISDIR(metadata.st_mode))
    return {
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "size": metadata.st_size if stat.S_ISREG(metadata.st_mode) else 0,
    }


def _empty_domains(root: Path) -> None:
    domains = root / "domains"
    if domains.exists() or domains.is_symlink():
        _private_path(domains, directory=True)
        if any(domains.iterdir()):
            raise ValueError("Host handover does not transfer guest or domain storage")
    _require_no_domain_recovery(root)


def _require_no_domain_recovery(root: Path) -> None:
    for name in (
        "packvm-vz-recovery.json",
        "packvm-vz-allocation-recovery.json",
        "packvm-vz-orphaned-allocation-claims.json",
    ):
        if (root / name).exists() or (root / name).is_symlink():
            raise ValueError("Host handover requires existing operation recovery first")


def _retained_source_domains(root: Path) -> dict[str, Any]:
    """Inventory closed allocations in place; confer no guest authority."""
    from .macos_vz_provisioner import _validate_allocation_identifier, _valid_process_id

    _require_no_domain_recovery(root)
    domains = root / "domains"
    result: dict[str, Any] = {
        "policy": "retain-in-source-no-resume",
        "domains_root_identity": None,
        "entries": [],
    }
    if not domains.exists() and not domains.is_symlink():
        return result
    result["domains_root_identity"] = _retained_identity(domains, directory=True)
    children = sorted(domains.iterdir())
    if len(children) > 32:
        raise ValueError("Host handover retained domain inventory exceeds its limit")
    names = {
        "allocation.json",
        "boot-cow.raw",
        "efi-variable-store.bin",
        "agent-seed.iso",
        "config-seed.iso",
    }
    fields = {
        "domain_id",
        "reservation_id",
        "lease_id",
        "run_root",
        "cow_disk_path",
        "efi_store_path",
        "owner_pid",
        "owner_identity",
        "cow_disk_digest",
        "efi_variable_store_digest",
        "agent_seed_path",
        "agent_seed_digest",
        "config_seed_path",
        "config_seed_digest",
        "guest_public_key_b64",
        "guest_public_key_digest",
    }
    for directory in children:
        before = _retained_identity(directory, directory=True)
        if before["device"] != root.stat().st_dev or {p.name for p in directory.iterdir()} != names:
            raise ValueError("Host handover retained domain storage is unreviewed")
        allocation = SecureDirectory(directory, create=False).read_bytes_bounded(
            "allocation.json", max_bytes=16 * 1024
        )
        record = json.loads(allocation)
        if not isinstance(record, dict) or set(record) != fields:
            raise ValueError("Host handover retained allocation is invalid")
        identifiers = []
        for key, label in (
            ("domain_id", "domain"),
            ("reservation_id", "reservation"),
            ("lease_id", "lease"),
        ):
            _validate_allocation_identifier(record[key], label)
            identifiers.append(record[key])
        if directory.name != hashlib.sha256("\0".join(identifiers).encode()).hexdigest():
            raise ValueError("Host handover retained allocation namespace changed")
        for key, name in (
            ("run_root", None),
            ("cow_disk_path", "boot-cow.raw"),
            ("efi_store_path", "efi-variable-store.bin"),
            ("agent_seed_path", "agent-seed.iso"),
            ("config_seed_path", "config-seed.iso"),
        ):
            if record[key] != str(directory if name is None else directory / name):
                raise ValueError("Host handover retained allocation path changed")
        if (
            not _valid_process_id(record["owner_pid"])
            or not isinstance(record["owner_identity"], str)
            or not record["owner_identity"]
            or process_start_identity(record["owner_pid"]).state != "dead"
        ):
            raise ValueError("Host handover retained allocation owner must be exited")
        for key in fields:
            if key.endswith("_digest"):
                validate_artifact_digest(record[key], field=key)
        if (
            not isinstance(record["guest_public_key_b64"], str)
            or not record["guest_public_key_b64"]
        ):
            raise ValueError("Host handover retained allocation key binding is invalid")
        # Guest bytes confer no authority and never enter the destination.
        # Bind kernel metadata instead of repeatedly reading multi-GiB COWs
        # or private seeds. Only the bounded allocation claim is hashed.
        files = {
            name: (
                _retained_file(directory / name)
                if name == "allocation.json"
                else _retained_identity(directory / name, directory=False)
            )
            for name in sorted(names)
        }
        if any(item["device"] != before["device"] for item in files.values()):
            raise ValueError("Host handover retained storage crosses filesystems")
        if files["allocation.json"]["digest"] != "sha256:" + hashlib.sha256(allocation).hexdigest():
            raise ValueError("Host handover retained allocation changed during inspection")
        if (
            before != _retained_identity(directory, directory=True)
            or {p.name for p in directory.iterdir()} != names
        ):
            raise ValueError("Host handover retained domain changed during inspection")
        result["entries"].append({"name": directory.name, "identity": before, "files": files})
    if result["domains_root_identity"] != _retained_identity(domains, directory=True) or (
        sorted(domains.iterdir()) != children
    ):
        raise ValueError("Host handover retained inventory changed during inspection")
    return result


def _retained_identity(path: Path, *, directory: bool) -> dict[str, int | str]:
    _private_path(path, directory=directory)
    value = path.lstat()
    return {
        "device": value.st_dev,
        "inode": value.st_ino,
        "size": 0 if directory else value.st_size,
        "mtime_ns": str(value.st_mtime_ns),
        "ctime_ns": str(value.st_ctime_ns),
    }


def _retained_file(path: Path) -> dict[str, Any]:
    before = _retained_identity(path, directory=False)
    if int(before["size"]) > 16 * 1024:
        raise ValueError("Host handover retained allocation exceeds its byte limit")
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        value = os.fstat(descriptor)
        if (
            value.st_dev,
            value.st_ino,
            value.st_size,
            str(value.st_mtime_ns),
            str(value.st_ctime_ns),
        ) != (
            before["device"],
            before["inode"],
            before["size"],
            before["mtime_ns"],
            before["ctime_ns"],
        ):
            raise ValueError("Host handover retained file was redirected")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(16 * 1024 + 1)
            if len(raw) > 16 * 1024:
                raise ValueError("Host handover retained allocation exceeded its byte limit")
            digest.update(raw)
        if before != _retained_identity(path, directory=False):
            raise ValueError("Host handover retained file changed during inspection")
        return {**before, "digest": "sha256:" + digest.hexdigest()}
    finally:
        os.close(descriptor)


def _ensure_private_parents(path: Path, root: Path) -> None:
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if not current.exists() and not current.is_symlink():
            current.mkdir(mode=0o700)
            flush_directory(current.parent)
        _private_path(current, directory=True)


def _move_exclusive(source: Path, destination: Path, identity: Mapping[str, int]) -> None:
    """Use an OS exclusive rename, preserving inode and avoiding overwrite."""
    if _identity(source) != identity:
        raise ValueError("Host handover source storage changed")
    _private_path(source.parent, directory=True)
    _private_path(destination.parent, directory=True)
    if destination.exists() or destination.is_symlink():
        raise ValueError("Host handover destination appeared")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    source_parent = os.open(source.parent, flags)
    try:
        target_parent = os.open(destination.parent, flags)
        try:
            source_info = os.stat(source.name, dir_fd=source_parent, follow_symlinks=False)
            if (
                source_info.st_dev != identity["device"]
                or source_info.st_ino != identity["inode"]
                or stat.S_ISLNK(source_info.st_mode)
            ):
                raise ValueError("Host handover descriptor source changed")
            library = ctypes.CDLL(None, use_errno=True)
            if sys.platform == "darwin":
                rename = library.renameatx_np
                rename.argtypes = (
                    ctypes.c_int,
                    ctypes.c_char_p,
                    ctypes.c_int,
                    ctypes.c_char_p,
                    ctypes.c_uint,
                )
                result = rename(
                    source_parent,
                    os.fsencode(source.name),
                    target_parent,
                    os.fsencode(destination.name),
                    4,
                )
            elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
                rename = library.renameat2
                rename.argtypes = (
                    ctypes.c_int,
                    ctypes.c_char_p,
                    ctypes.c_int,
                    ctypes.c_char_p,
                    ctypes.c_uint,
                )
                result = rename(
                    source_parent,
                    os.fsencode(source.name),
                    target_parent,
                    os.fsencode(destination.name),
                    1,
                )
            else:
                raise ValueError("Host handover exclusive rename is unavailable")
            if result != 0:
                raise OSError(ctypes.get_errno(), "Host handover exclusive storage move failed")
            os.fsync(source_parent)
            os.fsync(target_parent)
        finally:
            os.close(target_parent)
    finally:
        os.close(source_parent)
    flush_directory(source.parent)
    flush_directory(destination.parent)
    if _identity(destination) != identity:
        raise ValueError("Host handover moved storage identity changed")


def _read_journal(root: Path) -> dict[str, Any]:
    return _read_authenticated_json(root, JOURNAL_NAME)


def _read_authenticated_json(root: Path, relative: str) -> dict[str, Any]:
    storage = SecureDirectory(root, create=False)
    journal = json.loads(storage.read_bytes_bounded(relative, max_bytes=_MAX_JOURNAL))
    if not isinstance(journal, dict):
        raise ValueError("Host handover journal is invalid")
    authentication = journal.pop("authentication", None)
    key = storage.read_bytes_bounded("packvm-vz-attestation.key", max_bytes=256)
    from .macos_vz_provisioner import _canonical_bytes

    expected = hmac.new(key, _canonical_bytes(journal), hashlib.sha256).hexdigest()
    if not isinstance(authentication, str) or not hmac.compare_digest(authentication, expected):
        raise ValueError("Host handover journal authentication failed")
    return journal


def _require_metadata_digest(instance: Path, expected: str) -> None:
    raw = SecureDirectory(instance, create=False).read_bytes_bounded(
        "base-image.json", max_bytes=2 * 1024 * 1024
    )
    if "sha256:" + hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("Host handover instance metadata changed")


def _require_storage_quiescent(namespace: Path) -> None:
    """Independently reject open old namespace handles, including new owners."""
    result = subprocess.run(
        ["/usr/sbin/lsof", "-t", "+D", str(namespace)], capture_output=True, timeout=15, check=False
    )
    if result.returncode not in {0, 1} or result.stderr or len(result.stdout) > 64 * 1024:
        raise ValueError("Host handover storage-owner evidence is unavailable")
    try:
        owners = {int(value) for value in result.stdout.splitlines()}
    except ValueError as error:
        raise ValueError("Host handover storage-owner evidence is invalid") from error
    if owners - {os.getpid()}:
        raise ValueError("Host handover storage still has another owner")
