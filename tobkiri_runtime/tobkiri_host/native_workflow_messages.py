"""Request-scoped Wasm backend for the original Workflow messages operation."""

from core_runtime.native_provider_prepare_ipc import WORKFLOW_MESSAGES_MINIMUM_MEMORY

from .native_wasm_invocation import NativeWasmInvocationBackend

WORKFLOW_MESSAGES = "tobkiri.service.conversation.messages.build.v1", "messages_build"
WORKFLOW_MESSAGES_FUNCTION = "tobkiri_conversation_orchestration_pack.messages"
WORKFLOW_MESSAGES_ARTIFACT = (
    "sha256:6f739b26adf92428c91bfc93af7aae3dc273907c6f929af120bcf1b349a60624"
)
WORKFLOW_MESSAGES_IMPLEMENTATION = (
    "sha256:ca936271987aa3941dd8b1b3836a23af4601a8905650949a2970332c770cbe31"
)
WORKFLOW_MESSAGES_REVISION = (
    "sha256:7f6e38f70c2ab3a5c0339579f941861720efcd858f597f485e5e434db0359559"
)
WORKFLOW_CALLER_IMPLEMENTATION = (
    "sha256:fa72b1d696ada6653277c341afc34d73098777d382dfe33b51630a461257f3e8"
)
WORKFLOW_CALLER_ARTIFACT = (
    "sha256:554ac10f71b594db331f270d4a08804e96a06ebf1838fe42f81595703acefa5b"
)
WORKFLOW_CALLER_REVISION = (
    "sha256:012b80fe7aa7ce1dfd8a1f138c62b4025324fecaf70c539c80467b9cbbc3a1f2"
)


class NativeWorkflowMessagesBackend(NativeWasmInvocationBackend):
    """Run the signed Workflow child in the managed Launcher."""

    backend_id = "tobkiri.native-workflow-messages-v1"
    memory_reservation_bytes = WORKFLOW_MESSAGES_MINIMUM_MEMORY
