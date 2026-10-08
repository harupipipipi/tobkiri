"""Native-consented, finite development Host migration for Defaults."""

from __future__ import annotations

from copy import copy
import hashlib
import hmac
import os
import plistlib
from pathlib import Path
import secrets
import subprocess
import threading
import time
from typing import Any, Mapping

from core_runtime.development_host_handover_guard import (
    read_handover_journal,
    write_handover_journal,
)
from core_runtime.runtime_locks import NamedLock
from core_runtime.process_identity import process_start_identity
from core_runtime.profile_definition_store_v4 import ProfileDefinitionStore
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.secure_persistence import SecureDirectory

from ecosystem.defaultspack.backend.sandbox.isolation.macos_vz_handover import (
    MacOSVZStorageHandover,
    _identity,
    _ensure_private_parents,
    _move_exclusive,
)
from ecosystem.defaultspack.backend.sandbox.isolation.macos_vz_provisioner import MacOSVZProvisioner
from .development_host_data import prepare_host_data, import_host_data


class DevelopmentHostHandover:
    """Keep old authority private and publish only freshly imported data."""

    def __init__(self, provisioner: MacOSVZProvisioner, *, bootstrap_profile_id: str) -> None:
        self._target = provisioner
        self._root = provisioner.state_path.parent.parent
        self._bootstrap_profile_id = bootstrap_profile_id
        self._pending: dict[str, tuple[float, dict[str, Any], MacOSVZStorageHandover]] = {}
        self._lock = threading.Lock()

    def prepare(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Return the precise native review; export no keys or raw history."""
        if set(payload) != {"source_root", "source_executable"}:
            raise ValueError("Host handover selection fields are invalid")
        with self._lock, NamedLock(self._root / "development-host-locks", "handover"):
            source_root, source_executable = (
                Path(payload["source_root"]),
                Path(payload["source_executable"]),
            )
            source_digest = _verify_previous_host(source_root, source_executable)
            self._target._ensure_state_root()
            SecureDirectory(self._root, create=False)
            _require_inactive(self._root)
            if read_handover_journal(self._root) is not None:
                raise ValueError("Host handover already exists; review exact recovery")
            source = copy(self._target)
            source._requested_state_dir = source_root / "packvm-vz"
            source._state_dir = source._requested_state_dir
            SecureDirectory(source._state_dir, create=False)
            owners = _source_owners(source_root.parent)
            target_digest = "sha256:" + self._root.parent.name
            transfer = MacOSVZStorageHandover(
                source,
                self._target,
                source_host_digest=source_digest,
                target_host_digest=target_digest,
                owners=owners,
            )
            vm_plan = transfer.prepare()
            data = prepare_host_data(source_root, bootstrap_profile_id=self._bootstrap_profile_id)
            target_definitions = ProfileDefinitionStore(self._root).snapshot()
            publications = _owner_publications(self._root, data)
            facts = {
                "schema": "io.tobkiri.development-host-handover.v1",
                "source_root": str(source_root),
                "source_executable": str(source_executable),
                "source_host_digest": source_digest,
                "target_host_digest": target_digest,
                "source_root_identity": _identity(source_root),
                "target_root_identity": _identity(self._root),
                "target_profile_generation": target_definitions["generation"],
                "target_profile_snapshot_digest": canonical_digest(target_definitions),
                "data_digest": data["data_digest"],
                "vm_plan": vm_plan,
                "publications": publications,
                "counts": {
                    "definitions": len(data["definitions"]),
                    "providers": sum(len(row["providers"]) for row in data["profiles"]),
                    "models": sum(len(row["models"]) for row in data["profiles"]),
                    "conversations": sum(len(row["conversations"]) for row in data["profiles"]),
                },
                "history_policy": data["history_policy"],
                "provider_credentials_required": True,
                "fresh_profile_activation_required": True,
            }
            plan = {**facts, "plan_digest": canonical_digest(facts)}
            nonce = secrets.token_urlsafe(32)
            self._pending.clear()
            self._pending[nonce] = (time.monotonic() + 300, plan, transfer)
            return {"plan": plan, "ceremony_nonce": nonce}

    def commit(self, payload: Mapping[str, Any], *, native_secret: str) -> dict[str, Any]:
        """Consume native provenance once, then stage and publish exact data."""
        if set(payload) != {"plan_digest", "ceremony_nonce", "native_consent"}:
            raise ValueError("Host handover commit fields are invalid")
        with self._lock, NamedLock(self._root / "development-host-locks", "handover"):
            pending = self._pending.pop(str(payload["ceremony_nonce"]), None)
            if pending is None or pending[0] < time.monotonic():
                raise ValueError("Host handover review expired or was consumed")
            _deadline, plan, transfer = pending
            if payload["plan_digest"] != plan["plan_digest"]:
                raise ValueError("Host handover plan changed")
            _verify_native_consent(
                payload["native_consent"],
                plan_digest=plan["plan_digest"],
                nonce=str(payload["ceremony_nonce"]),
                native_secret=native_secret,
            )
            _require_inactive(self._root)
            if read_handover_journal(self._root) is not None:
                raise ValueError("Host handover destination publication already exists")
            _verify_previous_host(Path(plan["source_root"]), Path(plan["source_executable"]))
            transfer._require_owners_exited()
            self._require_target_snapshot(plan)
            data = prepare_host_data(
                Path(plan["source_root"]), bootstrap_profile_id=self._bootstrap_profile_id
            )
            if data["data_digest"] != plan["data_digest"] or transfer.prepare() != plan["vm_plan"]:
                raise ValueError("Host handover source changed after review")
            staging = self._root / "development-host-staging" / secrets.token_hex(16) / "user_data"
            _ensure_private_parents(staging, self._root)
            journal = {
                "stage": "staging",
                "plan": plan,
                "staging": str(staging),
                "staged_owners": {},
                "published": [],
                "destination_definition_policy": "Immutable successors retained and fenced on recovery",
            }
            write_handover_journal(self._root, journal)
            imported = import_host_data(staging, data)
            staged_owners = {
                relative: _identity(staging / relative) for relative in plan["publications"]
            }
            journal.update(stage="data-staged", staged_owners=staged_owners, imported=imported)
            write_handover_journal(self._root, journal)
            # The journal now fences all Profile activation/dispatch. Failures
            # retain both the staged data and exact VM recovery evidence.
            self._require_target_snapshot(plan)
            transfer._require_owners_exited()
            if (
                prepare_host_data(
                    Path(plan["source_root"]), bootstrap_profile_id=self._bootstrap_profile_id
                )["data_digest"]
                != plan["data_digest"]
            ):
                raise ValueError("Host handover source changed during staging")
            result = transfer.commit(
                plan["vm_plan"],
                session_digest=canonical_digest({"native_nonce": payload["ceremony_nonce"]}),
                ceremony_nonce=str(payload["ceremony_nonce"]),
            )
            journal.update(stage="vm-transferred", vm_result=result)
            write_handover_journal(self._root, journal)
            for relative in plan["publications"]:
                source, destination = staging / relative, self._root / relative
                if not source.exists():
                    continue
                _require_inactive(self._root)
                _ensure_private_parents(destination.parent, self._root)
                identity = staged_owners[relative]
                _move_exclusive(source, destination, identity)
                journal["published"].append({"relative_path": relative, "identity": identity})
                write_handover_journal(self._root, journal)
            self._require_target_snapshot(plan)
            definitions = ProfileDefinitionStore(self._root)
            transfer._require_owners_exited()
            if (
                prepare_host_data(
                    Path(plan["source_root"]), bootstrap_profile_id=self._bootstrap_profile_id
                )["data_digest"]
                != plan["data_digest"]
            ):
                raise ValueError("Host handover source data changed before publication")

            def complete(snapshot: Mapping[str, Any]) -> None:
                _require_inactive(self._root)
                journal.update(
                    stage="completed",
                    destination_profile_generation=snapshot["generation"],
                    destination_profile_snapshot_digest=canonical_digest(snapshot),
                )
                write_handover_journal(self._root, journal)

            definitions.import_profile_successors(
                data["definitions"],
                expected_store_generation=plan["target_profile_generation"],
                expected_snapshot_digest=plan["target_profile_snapshot_digest"],
                on_committed=complete,
            )
            return {
                "stage": "completed",
                "plan_digest": plan["plan_digest"],
                "vm": result,
                **imported,
            }

    def prepare_recovery(self) -> dict[str, Any]:
        """Describe exact interrupted work; recovery needs fresh Native consent."""
        with self._lock, NamedLock(self._root / "development-host-locks", "handover"):
            journal = read_handover_journal(self._root)
            if journal is None:
                raise ValueError("No unfinished Host handover is available")
            if journal.get("stage") in {"completed", "rolled-back"}:
                return {
                    "completed": True,
                    "result": {
                        "stage": "completed"
                        if journal["stage"] == "completed"
                        else "source-restored",
                        "plan_digest": journal["plan"]["plan_digest"],
                        "activation_required": journal["stage"] == "completed",
                    },
                }
            _require_inactive(self._root)
            plan = journal["plan"]
            source_root = Path(plan["source_root"])
            source_digest = _verify_previous_host(source_root, Path(plan["source_executable"]))
            source = copy(self._target)
            source._requested_state_dir = source_root / "packvm-vz"
            source._state_dir = source._requested_state_dir
            owners = {row["pid"]: row["identity"] for row in plan["vm_plan"]["owners"]}
            transfer = MacOSVZStorageHandover(
                source,
                self._target,
                source_host_digest=source_digest,
                target_host_digest=plan["target_host_digest"],
                owners=owners,
            )
            facts = {
                "schema": "io.tobkiri.development-host-recovery.v1",
                "action": "recover",
                "journal_digest": canonical_digest(journal),
                "handover_plan": plan,
                "stage": journal["stage"],
                "source_data_preserved": True,
                "destination_data_retained_unpublished": True,
            }
            review = {**facts, "plan_digest": canonical_digest(facts)}
            nonce = secrets.token_urlsafe(32)
            self._pending.clear()
            self._pending[nonce] = (time.monotonic() + 300, review, transfer)
            return {"plan": review, "ceremony_nonce": nonce}

    def recover(self, payload: Mapping[str, Any], *, native_secret: str) -> dict[str, Any]:
        """Restore exact old storage; retain all new data and recovery records."""
        if set(payload) != {"plan_digest", "ceremony_nonce", "native_consent"}:
            raise ValueError("Host recovery fields are invalid")
        with self._lock, NamedLock(self._root / "development-host-locks", "handover"):
            pending = self._pending.pop(str(payload["ceremony_nonce"]), None)
            if pending is None or pending[0] < time.monotonic():
                raise ValueError("Host recovery review expired or was consumed")
            _deadline, review, transfer = pending
            if review.get("action") != "recover" or payload["plan_digest"] != review["plan_digest"]:
                raise ValueError("Host recovery plan changed")
            _verify_native_consent(
                payload["native_consent"],
                plan_digest=review["plan_digest"],
                nonce=str(payload["ceremony_nonce"]),
                native_secret=native_secret,
                action="recover",
            )
            _require_inactive(self._root)
            journal = read_handover_journal(self._root)
            if journal is None or canonical_digest(journal) != review["journal_digest"]:
                raise ValueError("Host recovery journal changed after review")
            transfer._require_owners_exited()
            staging = Path(journal["staging"])
            if not staging.is_relative_to(self._root / "development-host-staging"):
                raise ValueError("Host recovery staging path is invalid")
            SecureDirectory(staging, create=False)
            if journal["stage"] == "staging":
                # No owner path or VM was published before staging completed.
                if any(
                    (self._root / path).exists() or (self._root / path).is_symlink()
                    for path in journal["plan"]["publications"]
                ):
                    raise ValueError("Host recovery staging publication changed")
            for relative in reversed(journal["staged_owners"]):
                source, destination = staging / relative, self._root / relative
                identity = journal["staged_owners"][relative]
                if source.exists() and not destination.exists() and _identity(source) == identity:
                    continue
                if source.exists() or source.is_symlink() or not destination.exists():
                    raise ValueError("Host recovery data publication is ambiguous")
                _move_exclusive(destination, source, identity)
            vm_journal = self._target.state_path.parent / "development-host-handover.json"
            if vm_journal.exists() or vm_journal.is_symlink():
                vm_result = transfer.recover_to_source(journal["plan"]["vm_plan"])
            else:
                # The data-only staging phase never took VM custody.
                if transfer.prepare() != journal["plan"]["vm_plan"]:
                    raise ValueError("Host recovery source VM changed")
                vm_result = {"stage": "source-unmodified"}
            journal.update(
                stage="rolled-back",
                recovery_plan_digest=review["plan_digest"],
                recovery_vm=vm_result,
            )
            write_handover_journal(self._root, journal)
            return {
                "stage": "source-restored",
                "plan_digest": review["plan_digest"],
                "destination_fenced": True,
            }

    def _require_target_snapshot(self, plan: Mapping[str, Any]) -> None:
        _require_inactive(self._root)
        if _identity(self._root) != plan["target_root_identity"]:
            raise ValueError("Host handover destination namespace changed")
        snapshot = ProfileDefinitionStore(self._root).snapshot()
        if (
            snapshot["generation"] != plan["target_profile_generation"]
            or canonical_digest(snapshot) != plan["target_profile_snapshot_digest"]
        ):
            raise ValueError("Host handover destination Profile definitions changed")


def _verify_previous_host(root: Path, executable: Path) -> str:
    SecureDirectory(root, create=False)
    if (
        root.name != "user_data"
        or root.parent.parent.name != "builds"
        or not executable.is_absolute()
        or executable.resolve(strict=True) != executable
        or executable.name != "tobkiri-launcher"
        or executable.parent.name != "MacOS"
        or executable.parent.parent.name != "Contents"
        or executable.parent.parent.parent.suffix != ".app"
    ):
        raise ValueError("Select an exact previous development Host and its namespace")
    info = executable.parent.parent / "Info.plist"
    if info.is_symlink() or info.stat().st_size > 1024 * 1024:
        raise ValueError("Previous Host bundle identity is invalid")
    if plistlib.loads(info.read_bytes()).get("CFBundleIdentifier") != "dev.tobkiri.local-launcher":
        raise ValueError("Previous Host must be the development Launcher")
    with executable.open("rb") as stream:
        encoded = stream.read(128 * 1024 * 1024 + 1)
    if len(encoded) > 128 * 1024 * 1024:
        raise ValueError("Previous Host executable exceeds the finite bound")
    digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
    if root.parent.name != digest[7:]:
        raise ValueError("Previous Host executable does not own the selected namespace")
    result = subprocess.run(
        [
            "/usr/bin/codesign",
            "--verify",
            "--deep",
            "--strict",
            str(executable.parent.parent.parent),
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("Previous Host code signature is invalid")
    return digest


def _source_owners(namespace: Path) -> dict[int, str]:
    result = subprocess.run(
        ["/usr/sbin/lsof", "-t", "+D", str(namespace)], capture_output=True, timeout=15, check=False
    )
    if result.returncode not in {0, 1} or result.stderr or len(result.stdout) > 64 * 1024:
        raise ValueError("Previous Host owner evidence is unavailable")
    owners = {}
    for raw in result.stdout.splitlines():
        pid = int(raw)
        if pid == os.getpid():
            continue
        evidence = process_start_identity(pid)
        if evidence.state != "live":
            raise ValueError("Previous Host owner evidence changed")
        owners[pid] = evidence.identity
    return owners


def _require_inactive(root: Path) -> None:
    paths = [root / "profiles" / "active.json"]
    workspaces = root / "workspaces"
    if workspaces.exists() or workspaces.is_symlink():
        SecureDirectory(workspaces, create=False)
        for workspace in workspaces.iterdir():
            SecureDirectory(workspace, create=False)
            paths.append(workspace / "activation" / "active.json")
    if any(path.exists() or path.is_symlink() for path in paths):
        raise ValueError("Host handover requires an inactive destination")


def _owner_publications(target: Path, data: Mapping[str, Any]) -> list[str]:
    paths = []
    for row in data["profiles"]:
        for field, pack in (
            ("providers", "rumi_provider_registry_pack"),
            ("models", "rumi_model_registry_pack"),
            ("conversations", "rumi_conversation_store_pack"),
        ):
            if row[field]:
                relative = f"packs/{pack}/profiles/{row['profile_id']}"
                path = target / relative
                if path.exists() or path.is_symlink():
                    raise ValueError("Host handover destination data owner already exists")
                paths.append(relative)
    return paths


def _verify_native_consent(
    value: Any, *, plan_digest: str, nonce: str, native_secret: str, action: str = "commit"
) -> None:
    if not isinstance(value, Mapping) or set(value) != {
        "version",
        "window_label",
        "action",
        "plan_digest",
        "ceremony_nonce",
        "issued_at",
        "expires_at",
        "signature",
    }:
        raise ValueError("Host handover requires native consent provenance")
    now = int(time.time())
    if (
        type(value["version"]) is not int
        or value["version"] != 1
        or value["window_label"] != "main"
        or value["action"] != action
        or value["plan_digest"] != plan_digest
        or value["ceremony_nonce"] != nonce
        or type(value["issued_at"]) is not int
        or type(value["expires_at"]) is not int
        or not value["issued_at"] <= now < value["expires_at"] <= value["issued_at"] + 180
    ):
        raise ValueError("Host handover native consent expired or changed")
    message = "\n".join(
        (
            "v1",
            "tobkiri.development-host-handover",
            "main",
            action,
            plan_digest,
            nonce,
            str(value["issued_at"]),
            str(value["expires_at"]),
        )
    )
    secret = native_secret.encode()
    expected = hmac.new(secret, message.encode(), hashlib.sha256).hexdigest()
    if (
        not secret
        or not isinstance(value["signature"], str)
        or not hmac.compare_digest(expected, value["signature"])
    ):
        raise ValueError("Host handover native consent authentication failed")
