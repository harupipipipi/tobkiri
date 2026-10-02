"""Confirmed workspace activation and Host selection recover as one operation."""

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from core_runtime.active_profile_store_v4 import ActiveProfileStore
from core_runtime.authority.v4 import AuthorityStore
from core_runtime.bootstrap import profile_capture
from core_runtime.bootstrap.profile_publication import ProfilePublicationJournal
from core_runtime.bootstrap.profile_registry import register_bootstrap_definition
from ecosystem.defaultspack.domain.runtime_v4 import ActivationStore, ProfileResolutionDenied
from tests.test_named_profile_store_v4 import _write_activation
from tests.test_profile_architecture_review_c import _packaged_catalog_revision, _resolve


@pytest.fixture
def upgrade(tmp_path, monkeypatch):
    """Create a normal, verified packaged predecessor and reviewed successor."""
    before = _packaged_catalog_revision(tmp_path / "before", b"before")
    after = _packaged_catalog_revision(tmp_path / "after", b"after")
    user_data = tmp_path / "user-data"
    workspace = user_data / "workspaces" / "defaults"
    workspace.mkdir(parents=True)
    with AuthorityStore(user_data / "authority" / "v4.sqlite3") as authority:
        store = ActivationStore(
            workspace / "activation",
            workspace,
            profile_id="defaults",
            authority=authority,
            catalog=before,
        )
        store.activate(
            _resolve(before),
            activation_id="activation:publication-before",
            created_at="2026-09-30T00:00:00Z",
        )
        previous = store.load_active_snapshot()
    register_bootstrap_definition(user_data, before.profiles["defaults"])
    profile_capture._publish_host_active_pointer(
        previous,
        user_data=user_data,
        replace_existing=False,
    )
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    monkeypatch.setattr(profile_capture, "_bundle_root", lambda _base=None: after.root)
    _, confirmation = profile_capture.prepare_bootstrap_profile_review()
    return user_data, confirmation


def _interrupt_publication(monkeypatch, confirmation):
    def fail(*args, **kwargs):
        raise OSError("interrupted Host publication")

    with monkeypatch.context() as fault:
        fault.setattr(profile_capture, "_publish_host_active_pointer", fail)
        with pytest.raises(OSError, match="interrupted Host publication"):
            profile_capture.capture_bootstrap_profile(confirmation=confirmation)


@pytest.mark.parametrize("restart_entry", ["capture", "review", "reconfirm"])
def test_committed_upgrade_recovers_before_host_publication(upgrade, monkeypatch, restart_entry):
    user_data, confirmation = upgrade
    previous = ActiveProfileStore(user_data).require()
    _interrupt_publication(monkeypatch, confirmation)
    assert ActiveProfileStore(user_data).require() == previous
    journal = ProfilePublicationJournal(user_data)
    assert journal.exists()
    if restart_entry == "capture":
        active = profile_capture.capture_active_profile()
    elif restart_entry == "review":
        profile_capture.prepare_bootstrap_profile_review()
        active = profile_capture.capture_active_profile()
    else:
        active = profile_capture.capture_bootstrap_profile(confirmation=confirmation)
    current = ActiveProfileStore(user_data).require()
    assert current.activation_id == active.activation["activation_id"]
    assert current.generation == previous.generation + 1
    assert not journal.exists()
    assert profile_capture.capture_active_profile() == active
    assert ActiveProfileStore(user_data).require() == current


def test_publication_recovers_after_global_commit_before_cleanup(upgrade, monkeypatch):
    user_data, confirmation = upgrade
    with monkeypatch.context() as fault:

        def fail(_self):
            raise OSError("interrupted journal cleanup")

        fault.setattr(ProfilePublicationJournal, "clear", fail)
        with pytest.raises(OSError, match="interrupted journal cleanup"):
            profile_capture.capture_bootstrap_profile(confirmation=confirmation)
    committed = ActiveProfileStore(user_data).require()
    profile_capture.capture_active_profile()
    assert ActiveProfileStore(user_data).require() == committed
    assert not ProfilePublicationJournal(user_data).exists()


@pytest.mark.parametrize("stage", ["before_authority_commit", "after_authority_commit"])
def test_workspace_commit_faults_keep_confirmation_and_recovery_fences(upgrade, monkeypatch, stage):
    user_data, confirmation = upgrade
    previous = ActiveProfileStore(user_data).require()
    original = ActivationStore.__init__

    def initialize(self, *args, **kwargs):
        def interrupt(actual):
            if actual == stage:
                raise OSError("interrupted workspace commit")

        kwargs["fault"] = interrupt
        original(self, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(ActivationStore, "__init__", initialize)
        with pytest.raises(OSError, match="interrupted workspace commit"):
            profile_capture.capture_bootstrap_profile(confirmation=confirmation)
    if stage == "before_authority_commit":
        _, reviewed = profile_capture.prepare_bootstrap_profile_review()
        assert ActiveProfileStore(user_data).require() == previous
        assert not ProfilePublicationJournal(user_data).exists()
        active = profile_capture.capture_bootstrap_profile(confirmation=reviewed)
    else:
        active = profile_capture.capture_active_profile()
    assert (
        ActiveProfileStore(user_data).require().activation_id == active.activation["activation_id"]
    )


def test_recovery_never_overwrites_a_newer_profile_selection(upgrade, monkeypatch):
    user_data, confirmation = upgrade
    _interrupt_publication(monkeypatch, confirmation)
    pointers = ActiveProfileStore(user_data)
    activation, snapshot, relative = _write_activation(user_data, "profile-b", "new-choice")
    selected = pointers.commit_activation(
        activation,
        activation_snapshot=snapshot,
        activation_snapshot_path=relative,
        expected=pointers.require(),
    )
    assert profile_capture._recover_bootstrap_publication() is None
    assert pointers.require() == selected
    with pytest.raises(ProfileResolutionDenied, match="predecessor is stale"):
        profile_capture.capture_bootstrap_profile(confirmation=confirmation)
    assert pointers.require() == selected
    # Only opening a new review retires the superseded intent; the failed retry
    # above cannot reinterpret its old confirmation against this selection.
    profile_capture.prepare_bootstrap_profile_review()
    assert not ProfilePublicationJournal(user_data).exists()
    assert pointers.require() == selected


def test_publication_cas_loses_to_selection_between_verify_and_commit(upgrade, monkeypatch):
    user_data, confirmation = upgrade
    _interrupt_publication(monkeypatch, confirmation)
    pointers = ActiveProfileStore(user_data)
    activation, snapshot, relative = _write_activation(user_data, "profile-b", "race-choice")
    original = ActiveProfileStore.commit_activation
    winner = []

    def race(self, candidate, **kwargs):
        if candidate["profile_id"] == "defaults" and not winner:
            winner.append(
                original(
                    self,
                    activation,
                    activation_snapshot=snapshot,
                    activation_snapshot_path=relative,
                    expected=self.require(),
                )
            )
        return original(self, candidate, **kwargs)

    monkeypatch.setattr(ActiveProfileStore, "commit_activation", race)
    with pytest.raises(ProfileResolutionDenied, match="recovery failed closed"):
        profile_capture.capture_active_profile()
    assert pointers.require() == winner[0]


def test_corrupt_intent_never_promotes_a_workspace_activation(upgrade, monkeypatch):
    user_data, confirmation = upgrade
    previous = ActiveProfileStore(user_data).require()
    _interrupt_publication(monkeypatch, confirmation)
    journal = ProfilePublicationJournal(user_data)
    journal.path.write_bytes(
        journal.path.read_bytes().replace(b'"security_epoch":1', b'"security_epoch":2')
    )
    with pytest.raises(ProfileResolutionDenied, match="recovery failed closed"):
        profile_capture.capture_active_profile()
    assert ActiveProfileStore(user_data).require() == previous


def test_stale_authority_never_promotes_a_workspace_activation(upgrade, monkeypatch):
    user_data, confirmation = upgrade
    previous = ActiveProfileStore(user_data).require()
    _interrupt_publication(monkeypatch, confirmation)
    with AuthorityStore(user_data / "authority" / "v4.sqlite3") as authority:
        authority.advance_security_epoch(reason="test revocation")
    with pytest.raises(ProfileResolutionDenied):
        profile_capture.capture_active_profile()
    assert ActiveProfileStore(user_data).require() == previous
    # Existing ActivationStore semantics deliberately treat stale Authority as a
    # hard denial, including a fresh review. Publication recovery cannot grant
    # new authority or turn revocation into automatic reconfirmation.
    with pytest.raises(ProfileResolutionDenied):
        profile_capture.prepare_bootstrap_profile_review()
    assert ActiveProfileStore(user_data).require() == previous


def test_unjournaled_historical_gap_is_not_heuristically_adopted(upgrade, monkeypatch):
    user_data, confirmation = upgrade
    previous = ActiveProfileStore(user_data).require()
    _interrupt_publication(monkeypatch, confirmation)
    journal = ProfilePublicationJournal(user_data)
    with journal.locked():
        journal.clear()
    with pytest.raises(ProfileResolutionDenied, match="does not match"):
        profile_capture.capture_active_profile()
    assert ActiveProfileStore(user_data).require() == previous


def test_publication_lock_serializes_separate_processes(tmp_path):
    journal = ProfilePublicationJournal(tmp_path)
    program = """
import sys
from pathlib import Path
from core_runtime.bootstrap.profile_publication import ProfilePublicationJournal
with ProfilePublicationJournal(Path(sys.argv[1])).locked():
    print('locked', flush=True)
    sys.stdin.readline()
"""
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", program, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        },
    )
    acquired = threading.Event()
    started = threading.Event()

    def contend():
        started.set()
        with journal.locked():
            acquired.set()

    try:
        assert process.stdout.readline().strip() == "locked"
        thread = threading.Thread(target=contend)
        thread.start()
        assert started.wait(5)
        assert not acquired.wait(0.1)
        process.communicate("release\n", timeout=5)
        assert process.returncode == 0
        thread.join(timeout=5)
        assert acquired.is_set()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
