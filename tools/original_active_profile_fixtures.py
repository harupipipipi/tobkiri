"""CI fixture inputs for the next original publication recovery reachpoint.

The original activation fixture helper, pointer writer and codecs create these
inputs. They are data-only test fixtures, not activation authority or an actual
Profile/GUI setup. Keys and activation files stay in CI temporary storage.
"""
from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path


CASES = (
    "current-absent", "current-same-parent", "current-stale", "current-aba",
    "current-immediately-published", "current-same-five-updated-at",
    "current-same-five-snapshot-digest", "current-stale-late-candidate-key-missing",
    "pointer-nonobject", "pointer-invalid-json", "pointer-digest-tamper",
    "pointer-unknown-field", "pointer-symlink", "pointer-dangling-symlink",
    "pointer-directory", "pointer-generation-bool", "snapshot-missing-final",
    "snapshot-parent-symlink", "snapshot-final-symlink", "snapshot-nonobject",
    "snapshot-invalid-json", "snapshot-digest-mismatch", "snapshot-record-missing",
    "snapshot-identity-mismatch", "snapshot-activation-inactive", "snapshot-catalog-mismatch",
    "snapshot-wrapper-envelope", "snapshot-profile-numeric-coercion",
    "current-stale-installed-between-recovery-calls",
)


def original_activation_writer(runtime: Path, protocol):
    """Compile the exact existing original test helper, not a replacement writer."""
    path = runtime / "tests/test_named_profile_store_v4.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    functions = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name == "_write_activation"]
    if len(functions) != 1:
        raise ValueError("one actual original activation fixture helper required")
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, *functions], type_ignores=[]))
    namespace = {"Path": Path, "canonical_digest": protocol.canonical_digest,
                 "canonical_json": protocol.canonical_json}
    exec(compile(module, str(path), "exec"), namespace)
    return namespace["_write_activation"]


def seed_current(name, root, pointer_store, write_activation, protocol, signing):
    """Mutate only the named input after the real original pointer commit."""
    if name == "current-absent":
        return
    activation, snapshot, relative = write_activation(root, "defaults", "current")
    store = pointer_store(root, clock=lambda: 1000.0)
    pointer = store.commit_activation(
        activation, activation_snapshot=snapshot, activation_snapshot_path=relative, expected=None)
    # Establish that the original writer's unmodified fixture really is valid.
    if store.load(verify_snapshot=True) != pointer:
        raise AssertionError("original pointer writer did not verify its fixture")
    current = pointer.to_dict()
    envelope_path = root / relative
    journal = root / "profiles/bootstrap-publication.json"
    envelope = protocol.strict_loads(journal.read_bytes())
    payload = envelope["payload"]

    def save_pointer():
        current["pointer_digest"] = protocol.canonical_digest(
            {key: value for key, value in current.items() if key != "pointer_digest"})
        store.path.write_bytes(protocol.canonical_json(current) + b"\n")

    def save_snapshot(*, rebind=True):
        envelope_path.write_bytes(protocol.canonical_json(snapshot) + b"\n")
        if rebind:
            current["activation_snapshot_digest"] = protocol.canonical_digest(snapshot)
            save_pointer()

    # Most validation cases stay on the current predecessor. Stale cases use
    # predecessor=None so the verified current pointer differs before any later
    # catalog/Authority operation is reached.
    if name not in {"current-stale", "current-stale-installed-between-recovery-calls",
                    "current-immediately-published",
                    "current-stale-late-candidate-key-missing"}:
        payload["predecessor"] = deepcopy(current)
    if name == "current-aba":
        current["generation"] += 2
        save_pointer()
    elif name == "current-same-five-updated-at":
        current["updated_at"] += 1
        save_pointer()
    elif name == "current-same-five-snapshot-digest":
        # Different snapshot bytes with the same identity/state are valid;
        # full dataclass comparison must still notice the new snapshot digest.
        snapshot["fixture_extension"] = "changed"
        save_snapshot()
    elif name == "current-immediately-published":
        for key in ("profile_id", "profile_revision", "activation_id", "plan_digest", "lock_digest"):
            payload["candidate"][key] = current[key]
    elif name == "current-stale-late-candidate-key-missing":
        payload["candidate"]["profile_id"] = "different"
        payload["candidate"].pop("lock_digest")
    elif name == "pointer-nonobject":
        store.path.write_bytes(b"[]")
    elif name == "pointer-invalid-json":
        store.path.write_bytes(b"{")
    elif name == "pointer-digest-tamper":
        current["pointer_digest"] = "sha256:" + "0" * 64
        store.path.write_bytes(protocol.canonical_json(current))
    elif name == "pointer-unknown-field":
        current["unexpected"] = True
        save_pointer()
    elif name in {"pointer-symlink", "pointer-dangling-symlink"}:
        store.path.rename(store.path.with_name("original-active.json"))
        store.path.symlink_to("missing-active.json" if "dangling" in name else "original-active.json")
    elif name == "pointer-directory":
        store.path.unlink(); store.path.mkdir()
    elif name == "pointer-generation-bool":
        current["generation"] = True
        save_pointer()
    elif name == "snapshot-missing-final":
        envelope_path.unlink()
    elif name == "snapshot-parent-symlink":
        parent = envelope_path.parent
        parent.rename(parent.with_name("original-activations"))
        parent.symlink_to("original-activations")
    elif name == "snapshot-final-symlink":
        envelope_path.rename(envelope_path.with_name("original-snapshot.json"))
        envelope_path.symlink_to("original-snapshot.json")
    elif name == "snapshot-nonobject":
        snapshot = []
        save_snapshot()
    elif name == "snapshot-invalid-json":
        envelope_path.write_bytes(b"{")
    elif name == "snapshot-digest-mismatch":
        snapshot["fixture_extension"] = "unbound"
        save_snapshot(rebind=False)
    elif name == "snapshot-record-missing":
        snapshot.pop("plan")
        save_snapshot()
    elif name == "snapshot-identity-mismatch":
        snapshot["profile"]["profile_id"] = "other"
        save_snapshot()
    elif name == "snapshot-activation-inactive":
        snapshot["activation"]["state"] = "prepared"
        save_snapshot()
    elif name == "snapshot-catalog-mismatch":
        current["catalog_revision"] = "sha256:" + "9" * 64
        save_pointer()
    elif name == "snapshot-wrapper-envelope":
        snapshot = {"envelope": snapshot, "fixture": True}
        save_snapshot()
    elif name == "snapshot-profile-numeric-coercion":
        # Numeric profile IDs cannot create a valid original pointer. Keep the
        # valid defaults pointer and observe the real identity-mismatch error
        # after the snapshot's original str(value or '') coercion instead.
        snapshot["profile"]["profile_id"] = 1
        save_snapshot()

    # Semantic snapshot edits should not accidentally test a stale predecessor:
    # keep it equal to the newly rebound pointer unless the case targets that
    # exact dataclass comparison. The invalid pointer still has an independently
    # valid original predecessor, avoiding earlier predecessor-validation errors.
    if name.startswith("snapshot-"):
        payload["predecessor"] = deepcopy(current)
    envelope["hmac"] = signing.compute_data_hmac(
        signing.load_signing_key(root / "profiles/.publication-key"), payload)
    journal.write_bytes(protocol.canonical_json(envelope) + b"\n")
    if name == "current-stale-installed-between-recovery-calls":
        # The outer actual recovery sees no journal. The closed test harness
        # installs this input between the original two calls, allowing the
        # inner discard_superseded=False branch to observe and retain it.
        journal.rename(journal.with_name(".fixture-pending-publication.json"))
