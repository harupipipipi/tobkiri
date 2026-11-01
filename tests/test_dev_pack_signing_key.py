"""Isolated CLI signer continuity tests; no Host, Native, or real key store."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

RUNTIME = Path(__file__).resolve().parents[1] / "tobkiri_runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

verify_signed_pack = importlib.import_module(
    "core_runtime.pack_signature"
).verify_signed_pack
keys = importlib.import_module("scripts.dev_pack_signing_key")
cli = importlib.import_module("scripts.tobkiri_pack")

PUBLISHER = "publisher.fixture"


@pytest.fixture
def signer(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    store = tmp_path.resolve() / "dev-signer"
    public = keys.initialize_dev_signer_store(store, publisher_id=PUBLISHER)
    return store, public


def opened(store: Path, public: dict[str, str]):
    return keys.open_dev_signer(
        store,
        publisher_id=PUBLISHER,
        expected_key_id=public["key_id"],
    )


def test_initialization_is_private_durable_and_create_once(signer) -> None:
    store, public = signer
    assert stat.S_IMODE(store.stat().st_mode) == 0o700
    assert set(p.name for p in store.iterdir()) == {"private.pem", "identity.json"}
    before = {p.name: (p.read_bytes(), p.stat().st_ino) for p in store.iterdir()}
    for child in store.iterdir():
        assert child.stat().st_uid == os.geteuid()
        assert child.stat().st_nlink == 1
        assert stat.S_IMODE(child.stat().st_mode) == 0o600
    with pytest.raises(keys.DevSignerStoreError, match="no replacement"):
        keys.initialize_dev_signer_store(store, publisher_id=PUBLISHER)
    assert before == {
        p.name: (p.read_bytes(), p.stat().st_ino) for p in store.iterdir()
    }
    with opened(store, public) as loaded:
        assert loaded.key_id == public["key_id"]
        assert loaded.fingerprint == public["fingerprint"]
        assert "PRIVATE KEY" not in repr(loaded)


def test_two_pack_versions_use_one_key_and_no_authority(signer, tmp_path, capsys):
    store, public = signer
    pack = tmp_path / "pack"
    pack.mkdir()
    source = pack / "handler.py"
    source.write_text("VALUE = 1\n")
    args = [
        "sign",
        str(pack),
        "--dev-signer-store",
        str(store),
        "--dev-signer-key-id",
        public["key_id"],
        "--pack-id",
        "fixture.pack",
        "--publisher-id",
        PUBLISHER,
        "--core-compatibility",
        ">=1,<9",
    ]
    signatures = []
    for version in ("1.0.0", "1.0.1"):
        source.write_text(f"VALUE = '{version}'\n")
        assert cli.main([*args, "--version", version]) == 0
        output = json.loads(capsys.readouterr().out)
        assert output["key_id"] == public["key_id"]
        manifest = json.loads((pack / ".tobkiri/signed-pack.json").read_bytes())
        with opened(store, public) as loaded:
            result = verify_signed_pack(
                pack,
                manifest,
                loaded.private_key.public_key(),
                expected_publisher_id=PUBLISHER,
                expected_key_id=public["key_id"],
                expected_version=version,
            )
        assert result["verified"] and result["authority_granted"] is False
        signatures.append(manifest["signature"]["value"])
    assert signatures[0] != signatures[1]


@pytest.mark.parametrize("missing", ["private.pem", "identity.json", "both"])
def test_missing_existing_files_never_regenerate(signer, missing):
    store, public = signer
    for name in ("private.pem", "identity.json"):
        if missing in (name, "both"):
            (store / name).unlink()
    before = {p.name: p.read_bytes() for p in store.iterdir()}
    with pytest.raises(keys.DevSignerStoreError, match="incomplete"):
        with opened(store, public):
            pass
    assert before == {p.name: p.read_bytes() for p in store.iterdir()}
    with pytest.raises(keys.DevSignerStoreError):
        keys.initialize_dev_signer_store(store, publisher_id=PUBLISHER)


def test_absent_store_is_not_initialized(tmp_path):
    store = tmp_path.resolve() / "absent"
    with pytest.raises((keys.DevSignerStoreError, OSError)):
        with keys.open_dev_signer(
            store, publisher_id=PUBLISHER, expected_key_id="0" * 32
        ):
            pass
    assert not store.exists()


@pytest.mark.parametrize(
    "target,mode",
    [
        ("store", 0o755),
        ("private.pem", 0o644),
        ("private.pem", 0o400),
        ("identity.json", 0o640),
        ("identity.json", 0o666),
    ],
)
def test_insecure_or_drifted_permissions_are_rejected(signer, target, mode):
    store, public = signer
    path = store if target == "store" else store / target
    path.chmod(mode)
    with pytest.raises(keys.DevSignerStoreError, match="mode"):
        with opened(store, public):
            pass
    assert stat.S_IMODE(path.stat().st_mode) == mode


@pytest.mark.parametrize("name", ["private.pem", "identity.json"])
def test_hardlinks_are_rejected(signer, tmp_path, name):
    store, public = signer
    os.link(store / name, tmp_path / "extra-link")
    with pytest.raises(keys.DevSignerStoreError, match="single-link"):
        with opened(store, public):
            pass


@pytest.mark.parametrize("name", ["private.pem", "identity.json"])
def test_file_symlinks_are_rejected_without_reading_targets(signer, tmp_path, name):
    store, public = signer
    target = tmp_path / "outside"
    (store / name).rename(target)
    (store / name).symlink_to(target)
    before = target.read_bytes()
    with pytest.raises(keys.DevSignerStoreError):
        with opened(store, public):
            pass
    assert target.read_bytes() == before


def test_symlinked_ancestor_and_store_are_rejected(signer, tmp_path):
    store, public = signer
    alias = tmp_path / "alias"
    alias.symlink_to(store, target_is_directory=True)
    with pytest.raises((keys.DevSignerStoreError, OSError)):
        with opened(alias, public):
            pass
    parent_alias = tmp_path / "parent-alias"
    parent_alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises((keys.DevSignerStoreError, OSError)):
        keys.initialize_dev_signer_store(parent_alias / "new", publisher_id=PUBLISHER)
    assert not (tmp_path / "new").exists()


def test_foreign_file_owner_is_rejected(signer, monkeypatch):
    store, public = signer
    real = keys.os.fstat

    def foreign_file(descriptor):
        value = real(descriptor)
        if not stat.S_ISREG(value.st_mode):
            return value
        fields = {
            name: getattr(value, name) for name in dir(value) if name.startswith("st_")
        }
        fields["st_uid"] = os.geteuid() + 1
        return SimpleNamespace(**fields)

    monkeypatch.setattr(keys.os, "fstat", foreign_file)
    with pytest.raises(keys.DevSignerStoreError, match="owned"):
        with opened(store, public):
            pass


@pytest.mark.parametrize(
    "mutation", ["key", "metadata", "publisher", "extra", "duplicate"]
)
def test_corrupt_or_tampered_store_is_rejected(signer, mutation):
    store, public = signer
    if mutation == "key":
        (store / "private.pem").write_bytes(b"corrupt key")
    elif mutation == "metadata":
        (store / "identity.json").write_text("{}")
    elif mutation == "publisher":
        data = json.loads((store / "identity.json").read_bytes())
        data["publisher_id"] = "different.publisher"
        (store / "identity.json").write_text(json.dumps(data))
    elif mutation == "extra":
        (store / "unexpected").write_text("extra")
    else:
        (store / "identity.json").write_text('{"schema":"one","schema":"two"}')
    with pytest.raises(keys.DevSignerStoreError):
        with opened(store, public):
            pass


def test_whole_store_key_replacement_fails_external_pin(signer, tmp_path):
    store, public = signer
    replacement = tmp_path.resolve() / "replacement"
    other = keys.initialize_dev_signer_store(replacement, publisher_id=PUBLISHER)
    assert other["key_id"] != public["key_id"]
    for path in replacement.iterdir():
        shutil.copy2(path, store / path.name)
    with pytest.raises(keys.DevSignerStoreError, match="previously recorded"):
        with opened(store, public):
            pass


def test_publisher_cannot_silently_change(signer):
    store, public = signer
    with pytest.raises(keys.DevSignerStoreError, match="identity"):
        with keys.open_dev_signer(
            store, publisher_id="another.publisher", expected_key_id=public["key_id"]
        ):
            pass


@pytest.mark.parametrize(
    "part",
    ["user_data", "credentials", "cas", "packvm-vz", "authority", "generation-budget"],
)
def test_known_runtime_state_locations_are_denied(tmp_path, part):
    location = tmp_path / part
    location.mkdir()
    with pytest.raises(keys.DevSignerStoreError, match="runtime"):
        keys.initialize_dev_signer_store(location / "signer", publisher_id=PUBLISHER)
    assert not (location / "signer").exists()


def test_repository_and_explicit_pack_roots_are_denied(tmp_path):
    with pytest.raises(keys.DevSignerStoreError, match="outside"):
        keys.initialize_dev_signer_store(
            keys.REPOSITORY_ROOT / "must-not-create-dev-signer",
            publisher_id=PUBLISHER,
        )
    with pytest.raises(keys.DevSignerStoreError, match="outside"):
        keys.initialize_dev_signer_store(
            tmp_path.resolve() / "signer",
            publisher_id=PUBLISHER,
            forbidden_roots=(tmp_path,),
        )
    assert not (keys.REPOSITORY_ROOT / "must-not-create-dev-signer").exists()


def test_writable_parent_cannot_initialize_a_store(tmp_path):
    parent = tmp_path / "shared-parent"
    parent.mkdir(mode=0o700)
    parent.chmod(0o777)
    with pytest.raises(keys.DevSignerStoreError, match="writable"):
        keys.initialize_dev_signer_store(parent / "signer", publisher_id=PUBLISHER)
    assert not (parent / "signer").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS extended ACL fixture")
def test_extended_acl_is_rejected_even_with_mode_0600(signer):
    store, public = signer
    private = store / "private.pem"
    subprocess.run(
        ["/bin/chmod", "+a", "everyone allow read", str(private)],
        check=True,
        capture_output=True,
        timeout=5,
    )
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    with pytest.raises(keys.DevSignerStoreError, match="ACL"):
        with opened(store, public):
            pass


def test_initialization_path_swap_cannot_write_to_replacement(tmp_path, monkeypatch):
    store = tmp_path.resolve() / "signer"
    replacement = tmp_path / "replacement"
    replacement.mkdir(mode=0o700)
    generate = keys.Ed25519PrivateKey.generate

    def swap():
        store.rename(tmp_path / "original-partial")
        store.symlink_to(replacement, target_is_directory=True)
        return generate()

    monkeypatch.setattr(keys.Ed25519PrivateKey, "generate", swap)
    with pytest.raises(keys.DevSignerStoreError, match="path changed"):
        keys.initialize_dev_signer_store(store, publisher_id=PUBLISHER)
    assert list(replacement.iterdir()) == []
    assert list((tmp_path / "original-partial").iterdir()) == []


@pytest.mark.parametrize(
    "mutation",
    ["key_replace", "key_remove", "key_modify", "root_replace", "chmod", "hardlink"],
)
def test_loaded_signer_detects_operation_races(signer, tmp_path, mutation):
    store, public = signer
    with pytest.raises(keys.DevSignerStoreError):
        with opened(store, public) as loaded:
            path = store / "private.pem"
            if mutation == "key_replace":
                old = path.read_bytes()
                path.unlink()
                path.write_bytes(old)
                path.chmod(0o600)
            elif mutation == "key_remove":
                path.unlink()
            elif mutation == "key_modify":
                path.write_bytes(path.read_bytes() + b"\n")
            elif mutation == "root_replace":
                store.rename(tmp_path / "moved")
                store.mkdir(mode=0o700)
            elif mutation == "chmod":
                path.chmod(0o644)
            else:
                os.link(path, tmp_path / "extra")
            loaded.assert_current()


def test_signer_scope_is_closed_after_use(signer):
    store, public = signer
    with opened(store, public) as loaded:
        loaded.assert_current()
    with pytest.raises(keys.DevSignerStoreError, match="closed"):
        loaded.assert_current()


def test_initializer_refuses_an_injected_file_without_overwriting(
    tmp_path, monkeypatch
):
    store = tmp_path.resolve() / "new"
    original = keys._write_once

    def inject(directories, name, raw):
        descriptor = os.open(
            name,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
            dir_fd=directories.descriptor,
        )
        os.write(descriptor, b"do not overwrite")
        os.close(descriptor)
        original(directories, name, raw)

    monkeypatch.setattr(keys, "_write_once", inject)
    with pytest.raises(keys.DevSignerStoreError):
        keys.initialize_dev_signer_store(store, publisher_id=PUBLISHER)
    assert (store / "private.pem").read_bytes() == b"do not overwrite"
    assert not (store / "identity.json").exists()


def test_concurrent_initialization_has_one_winner(tmp_path):
    store = tmp_path.resolve() / "one-store"

    def initialize():
        try:
            return keys.initialize_dev_signer_store(store, publisher_id=PUBLISHER)
        except keys.DevSignerStoreError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: initialize(), range(2)))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    with opened(store, winners[0]) as loaded:
        loaded.assert_current()


def test_cli_init_outputs_only_public_identity(tmp_path, capsys):
    store = tmp_path.resolve() / "cli-store"
    assert cli.main(["dev-signer-init", str(store), "--publisher-id", PUBLISHER]) == 0
    raw = capsys.readouterr().out
    public = json.loads(raw)
    assert set(public) == {"initialized", "publisher_id", "key_id", "fingerprint"}
    assert "PRIVATE" not in raw and "private_key" not in raw
    assert len(public["key_id"]) == 32
    assert public["fingerprint"].startswith("sha256:")


def test_cli_requires_store_pin_and_preserves_private_key_route(
    signer, tmp_path, capsys
):
    store, public = signer
    pack = tmp_path / "cli-pack"
    pack.mkdir()
    (pack / "handler.py").write_text("VALUE = 1\n")
    common = [
        "sign",
        str(pack),
        "--pack-id",
        "fixture.pack",
        "--version",
        "1.0.0",
        "--publisher-id",
        PUBLISHER,
        "--core-compatibility",
        ">=1,<9",
    ]
    with pytest.raises(ValueError, match="key-id"):
        cli.main([*common, "--dev-signer-store", str(store)])
    assert not (pack / ".tobkiri/signed-pack.json").exists()
    assert cli.main([*common, "--private-key", str(store / "private.pem")]) == 0
    manifest = json.loads((pack / ".tobkiri/signed-pack.json").read_bytes())
    assert manifest["signature"]["key_id"] == public["key_id"]
    with pytest.raises(SystemExit):
        cli.main(
            [
                *common,
                "--private-key",
                str(store / "private.pem"),
                "--dev-signer-store",
                str(store),
            ]
        )
    capsys.readouterr()


def test_cli_key_drift_during_signing_prevents_manifest_output(
    signer,
    tmp_path,
    monkeypatch,
):
    store, public = signer
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "handler.py").write_text("VALUE = 1\n")
    original = cli.sign_manifest

    def change_key(manifest, private):
        result = original(manifest, private)
        (store / "private.pem").unlink()
        return result

    monkeypatch.setattr(cli, "sign_manifest", change_key)
    with pytest.raises(keys.DevSignerStoreError):
        cli.main(
            [
                "sign",
                str(pack),
                "--dev-signer-store",
                str(store),
                "--dev-signer-key-id",
                public["key_id"],
                "--pack-id",
                "fixture.pack",
                "--version",
                "1.0.0",
                "--publisher-id",
                PUBLISHER,
                "--core-compatibility",
                ">=1,<9",
            ]
        )
    assert not (pack / ".tobkiri/signed-pack.json").exists()
