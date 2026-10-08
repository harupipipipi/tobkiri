"""Pure verification of the signed variant's actual Native Kernel capture path."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from core_runtime.authority.v4 import AuthorityStore, AuthorityDenied
from core_runtime.bootstrap import production_v4
from core_runtime.credential_transport import CredentialMaterialStoreBinding
from core_runtime.native_generation_budget import (
    NativeGenerationBudgetTemplate,
    capture_native_generation_budget,
    owner_startup_generation_budget_template,
)
from tobkiri_host.credential_store import HostCredentialMaterialStore, KEY_VERSION


def test_signed_owner_source_always_selects_exact_test_policy():
    policy = owner_startup_generation_budget_template()
    assert policy == NativeGenerationBudgetTemplate("defaults", "provider.main")
    assert policy.max_requests == 3 and policy.max_output_tokens == 2048
    assert policy.model == "google/gemma-4-31b-it:free"


@pytest.mark.parametrize(
    "requested",
    [
        {},
        {"profile_id": "defaults", "max_requests": 999},
        True,
        "approved",
        NativeGenerationBudgetTemplate("other-profile", "provider.main"),
        NativeGenerationBudgetTemplate("defaults", "provider.other"),
    ],
)
def test_signed_owner_source_rejects_client_policy_overrides(requested):
    with pytest.raises(PermissionError):
        owner_startup_generation_budget_template(requested)


@pytest.mark.parametrize("profile", ["other", "defaults"])
def test_common_production_capture_selects_policy_before_catalog_or_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile,
):
    """No-argument capture gates the exact Native snapshot before other work."""
    observed = []
    selected = []

    class StopBeforeCatalogOrNative(Exception):
        pass

    def select(requested):
        policy = owner_startup_generation_budget_template(requested)
        selected.append(policy)
        return policy

    def catalog(*args, **kwargs):
        observed.append("catalog")
        raise StopBeforeCatalogOrNative

    def credential(**kwargs):
        raise AssertionError("credential factory must not run before the policy gate")

    monkeypatch.setattr(
        production_v4, "owner_startup_generation_budget_template", select
    )
    monkeypatch.setattr(production_v4, "_host_profile_catalog", catalog)
    authority = AuthorityStore(tmp_path / "authority" / "v4.sqlite3")
    active = SimpleNamespace(resolved=SimpleNamespace(profile={"profile_id": profile}))
    with pytest.raises(AuthorityDenied):
        production_v4.capture_production_dispatch(
            active,
            authority_store=authority,
            bundle_root=tmp_path / "unused",
            ecosystem_root=tmp_path / "unused",
            credential_store_factory=credential,
        )
    assert selected == [NativeGenerationBudgetTemplate("defaults", "provider.main")]
    assert observed == ([] if profile == "other" else ["catalog"])
    if profile == "defaults":
        material = HostCredentialMaterialStore(user_data_root=tmp_path / "owner")
        binding = capture_native_generation_budget(
            binding=CredentialMaterialStoreBinding(material, KEY_VERSION),
            authority_store=authority,
            user_data_root=tmp_path / "owner",
            profile_id="defaults",
            template=selected[0],
        )
        assert binding.generation_admission.template == selected[0]
        assert authority.get_host_pending_effect(selected[0].effect_id) is not None


def test_native_app_kernel_entry_reaches_fixed_common_capture_with_none_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Execute real app.py and real composition; stop at the pure capture gate.

    Kernel/platform objects are local probes, so no Native, VM, listener, or
    cloud request is run. The production capture and owner selector are real.
    """
    import app
    from core_runtime import bootstrap, di_container, packvm_lifecycle_v4
    from core_runtime.bootstrap import runtime
    from ecosystem.defaultspack.defaultspack import profile_runtime_composition
    from ecosystem.defaultspack.backend.sandbox.isolation import macos_vz_provisioner

    observed = []

    class StopAtVerifiedCatalog(Exception):
        pass

    def catalog(*args, **kwargs):
        observed.append("catalog")
        raise StopAtVerifiedCatalog

    class KernelProbe:
        def __init__(self, **kwargs):
            self.inputs = kwargs
            assert (
                kwargs["runtime_capture_factory"].keywords["generation_budget_template"]
                is None
            )

        def run_startup(self):
            observed.append("kernel-startup")
            production_v4.capture_production_dispatch(
                SimpleNamespace(
                    resolved=SimpleNamespace(profile={"profile_id": "defaults"})
                ),
                authority_store=AuthorityStore(tmp_path / "authority" / "v4.sqlite3"),
                bundle_root=tmp_path / "unused",
                ecosystem_root=tmp_path / "unused",
                generation_budget_template=None,
            )

        def shutdown(self):
            observed.append("shutdown")

    monkeypatch.setattr(runtime, "Kernel", KernelProbe)
    monkeypatch.setattr(
        profile_runtime_composition,
        "install_defaultspack_profile_runtime",
        lambda: None,
    )
    monkeypatch.setattr(
        macos_vz_provisioner, "default_packvm_provisioner", lambda: object()
    )
    monkeypatch.setattr(
        packvm_lifecycle_v4, "PackVMLifecycleV4", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(
        di_container,
        "get_container",
        lambda: SimpleNamespace(register=lambda *args: None),
    )
    monkeypatch.setattr(production_v4, "_host_profile_catalog", catalog)
    selected = []

    def select(requested):
        selected.append(owner_startup_generation_budget_template(requested))
        return selected[-1]

    monkeypatch.setattr(
        production_v4, "owner_startup_generation_budget_template", select
    )
    with pytest.raises(AuthorityDenied):
        app.main(["--headless"])
    assert selected == [NativeGenerationBudgetTemplate("defaults", "provider.main")]
    assert observed == ["kernel-startup", "catalog", "shutdown"]


def test_fresh_kernel_setup_boot_remains_available_then_defaults_binds_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Real empty-state Kernel/activation branches retain Setup and select cap3.

    Presentation/listener and platform fixtures are inert. The real Kernel,
    AppLifecycleManager, common policy gate and private policy persistence run.
    No real profile, Native UI, VM, listener, or cloud connection is touched.
    """
    from core_runtime.bootstrap import runtime, profile_capture
    from core_runtime.pack_api_server import RuntimeCaptureInputs
    from core_runtime.activation_handoff import ActivationCommittedError

    user_data = tmp_path / "owner"
    observed = []

    class Control:
        session_kind = "host_profile_control"

        def __init__(self, **kwargs):
            observed.append("setup-catalog")

        def close(self):
            pass

    class Server:
        port = 0

        def is_running(self):
            return True

        def stop(self):
            pass

    def inputs(active=None):
        return RuntimeCaptureInputs(tmp_path / "unused", tmp_path / "unused", ())

    monkeypatch.setattr(runtime, "active_profile_exists", lambda: False)
    monkeypatch.setattr(runtime, "runtime_user_data_root", lambda: user_data)
    monkeypatch.setattr(runtime, "_prepare_desktop_api_token", lambda *args: None)
    monkeypatch.setattr(runtime, "_bundle_root", lambda: tmp_path / "unused")
    monkeypatch.setattr(runtime, "HostProfileControlSession", Control)
    monkeypatch.setattr(
        runtime, "initialize_pack_api_server", lambda **kwargs: Server()
    )
    monkeypatch.setattr(runtime, "resolve_runtime_port", lambda: 0)
    selected = []

    def select(requested):
        policy = owner_startup_generation_budget_template(requested)
        selected.append(policy)
        return policy

    monkeypatch.setattr(
        production_v4, "owner_startup_generation_budget_template", select
    )
    kernel = runtime.Kernel(
        runtime_capture_factory=inputs,
        host_profile_bindings_factory=lambda: (),
    )
    try:
        result = kernel.run_startup()
        assert result["status"] == "setup_required"
        assert result["runtime_ready"] is False
        assert observed == ["setup-catalog"]
        assert selected == []  # No active Profile means no generation session.
        assert not user_data.exists()
        active = SimpleNamespace(
            resolved=SimpleNamespace(profile={"profile_id": "defaults"})
        )
        monkeypatch.setattr(
            profile_capture, "capture_bootstrap_profile", lambda **kwargs: active
        )
        monkeypatch.setattr(
            profile_capture, "runtime_user_data_root", lambda *args: user_data
        )

        def stop_at_catalog(*args, **kwargs):
            raise RuntimeError("stop before signed catalog/platform fixtures")

        monkeypatch.setattr(production_v4, "_host_profile_catalog", stop_at_catalog)
        with pytest.raises(ActivationCommittedError):
            kernel._lifecycle.activate_bootstrap_profile({})
        assert selected == [NativeGenerationBudgetTemplate("defaults", "provider.main")]
        authority = AuthorityStore(user_data / "authority" / "v4.sqlite3")
        material = HostCredentialMaterialStore(user_data_root=user_data)
        binding = capture_native_generation_budget(
            binding=CredentialMaterialStoreBinding(material, KEY_VERSION),
            authority_store=authority,
            user_data_root=user_data,
            profile_id="defaults",
            template=selected[0],
        )
        assert binding.generation_admission.template == selected[0]
        assert authority.get_host_pending_effect(selected[0].effect_id) is not None
    finally:
        kernel.shutdown()
