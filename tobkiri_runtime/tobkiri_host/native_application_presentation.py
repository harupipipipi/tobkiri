"""Request-scoped backend for the original Application presentation operation."""

from core_runtime.native_provider_prepare_ipc import APPLICATION_PRESENTATION_MINIMUM_MEMORY

from .native_wasm_invocation import NativeWasmInvocationBackend


class NativeApplicationPresentationBackend(NativeWasmInvocationBackend):
    """Run a selected presentation edge through the managed Launcher."""

    backend_id = "tobkiri.native-application-presentation-v1"
    memory_reservation_bytes = APPLICATION_PRESENTATION_MINIMUM_MEMORY
