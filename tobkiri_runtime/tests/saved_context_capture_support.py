"""Real indexed owner-code captures for the fixed saved-context composition."""

from pathlib import Path
from types import SimpleNamespace

from core_runtime.bootstrap.production_v4 import _borrow_selected_saved_context
from core_runtime.host_provider_hooks_v4 import load_host_provider_factory
from core_runtime.host_provider_backend_v4 import HostProviderCaptureContextV4
from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_host.contracts import OperationCatalog, OperationRoute
from tobkiri_host.models import OpaqueAuthorityRef

RUNTIME = Path(__file__).resolve().parents[1]
CONVERSATION = ("tobkiri.resource.conversation.v1", "rumi_conversation_store_pack.conversation-resource")
PROJECT = ("tobkiri.resource.project.state.v1", "tobkiri_ui_settings_pack.projects-read")


def binding(pack, function, target, *, root=None):
    root = root or RUNTIME / "ecosystem" / pack
    compiled = compile_pack_root(root)
    metadata = compiled.routes[target]
    route = OperationRoute(
        contract_id=target[0], operation_id=target[1], artifact_digest=compiled.artifact.digest,
        function_id=function, variant_id=metadata["variant_id"], catalog_digest=compiled.artifact.catalog_digest,
        platform=metadata["platform"], architecture=metadata["architecture"],
        runtime_abi=metadata["runtime_abi"], backend=metadata["backend"],
        execution_kind=metadata["execution_kind"], domain_kind=metadata["domain_kind"],
        execution_domain_profile=metadata["execution_domain_profile"], materialization_mode=metadata["materialization_mode"],
        target_principal_ref=OpaqueAuthorityRef("authority:saved-context-capture." + function),
    )
    return OperationCatalog((compiled.artifact,), (route,)).resolve(*target, ">=1,<2")


def owners(*, conversation_root=None, project_root=None, project_capture=True):
    conversation = binding("rumi_conversation_store_pack", "rumi_conversation_store_pack.conversation-store.resource",
                           CONVERSATION, root=conversation_root)
    project = binding("tobkiri_ui_settings_pack", "tobkiri.project.state.read", PROJECT, root=project_root) if project_capture else None
    factories = [
        (conversation.function.function_id, (conversation,), load_host_provider_factory(
            conversation_root or RUNTIME / "ecosystem/rumi_conversation_store_pack", conversation), "test-adapter"),
    ]
    if project is not None:
        factories.append((project.function.function_id, (project,), load_host_provider_factory(
            project_root or RUNTIME / "ecosystem/tobkiri_ui_settings_pack", project), "test-adapter"))
    consumer = binding("rumi_turn_runtime_pack", "rumi_turn_runtime_pack.turn-runtime.saved",
                       ("tobkiri.action.turn.saved.v1", "rumi_turn_runtime_pack.turn-saved"))
    return conversation, project, consumer, factories


def context(binding, root):
    return HostProviderCaptureContextV4(
        profile_id="defaults", plan_digest="test-adapter", security_epoch=1, activation={},
        state_root=root / "state", user_data_root=root, provider_bindings=(binding,),
        catalog_bindings=(binding,), domain_ids={(binding.operation.contract_id,
            binding.operation.operation_id, binding.principal_ref.value): "test-domain"},
    )


def borrow(selected, *, operation, profile_id="defaults", owner=lambda: ("shell.tauri.default", "session-one"),
           read_project, guard=lambda: None, project_bound=True):
    conversation, project, consumer, factories = selected
    return _borrow_selected_saved_context(
        operation=operation, consumer=consumer, conversation_owner=conversation,
        project_owner=project if project_bound else None, factories=factories,
        profile_id=profile_id, presentation_owner=owner, read_project=read_project, guard=guard,
    )


def operation(selected, profile_id="defaults"):
    consumer = selected[2]
    return SimpleNamespace(context=SimpleNamespace(profile_id=profile_id),
        target_principal=consumer.principal_ref, contract_id=consumer.operation.contract_id,
        operation_id=consumer.operation.operation_id)
