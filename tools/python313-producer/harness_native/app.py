"""Thin nativeKit WIT adapters; no ordinary Pack lifecycle or authority APIs."""

from componentize_py_types import Err
import wit_world.exports
from wit_world.exports.runtime import Error, ErrorCode

from harness_native.bootstrap import load_original_runtime
from harness_native.dispatcher import (
    AlreadyInitialized,
    Dispatcher,
    MethodRaised,
    NotInitialized,
)
from harness_native.value_graph import InvalidFrame, InvalidReference, UnsupportedValue

# The componentizer creates a separate adapter instance for each exported
# function. All of them must use this one interpreter-local dispatcher.
_dispatcher = Dispatcher(load_original_runtime)


def _invoke(action, *args):
    try:
        return action(*args)
    except NotInitialized:
        raise Err(
            Error(ErrorCode.NOT_INITIALIZED, "native DATA is not initialized", "")
        ) from None
    except AlreadyInitialized:
        raise Err(
            Error(
                ErrorCode.ALREADY_INITIALIZED, "native DATA is already initialized", ""
            )
        ) from None
    except InvalidFrame:
        raise Err(
            Error(ErrorCode.INVALID_FRAME, "invalid native DATA frame", "")
        ) from None
    except InvalidReference:
        raise Err(
            Error(ErrorCode.INVALID_REFERENCE, "invalid native DATA reference", "")
        ) from None
    except UnsupportedValue:
        raise Err(
            Error(ErrorCode.UNSUPPORTED_VALUE, "original DATA is not represented", "")
        ) from None
    except MethodRaised as error:
        # Empty text means undisclosed, not that the original message is empty.
        raise Err(Error(ErrorCode.METHOD_RAISED, "", error.details)) from None


class Runtime(wit_world.exports.Runtime):
    """Fixed calls to the seventeen original method names, with no frame selector."""

    def init(self, payload: str) -> None:
        _invoke(_dispatcher.init, payload)

    def bootstrap_profile_id(self, payload: str) -> str:
        return _invoke(_dispatcher.call, "bootstrap_profile_id", payload)

    def setup_profile_preview(self, payload: str) -> str:
        return _invoke(_dispatcher.call, "setup_profile_preview", payload)

    def confirmation(self, payload: str) -> str:
        return _invoke(_dispatcher.call, "_confirmation", payload)

    def bootstrap_confirmation(self, payload: str) -> str:
        return _invoke(_dispatcher.call, "bootstrap_confirmation", payload)

    def profile_confirmation(self, payload: str) -> str:
        return _invoke(_dispatcher.call, "profile_confirmation", payload)

    def setup_listing(self, payload: str) -> str:
        return _invoke(_dispatcher.call, "setup_listing", payload)

    def setup_preview(self, payload: str) -> str:
        return _invoke(_dispatcher.call, "setup_preview", payload)

    def setup_activation_decision(self, payload: str) -> str:
        return _invoke(_dispatcher.call, "setup_activation_decision", payload)

    def setup_activation_success(self, payload: str) -> str:
        return _invoke(_dispatcher.call, "setup_activation_success", payload)

    def setup_activation_failure(self, payload: str) -> str:
        return _invoke(_dispatcher.call, "setup_activation_failure", payload)

    def retired_setup_response(self, payload: str) -> str:
        return _invoke(_dispatcher.call, "retired_setup_response", payload)

    def load_catalog(self, payload: str) -> str:
        return _invoke(_dispatcher.call, "load_catalog", payload)

    def catalog_with_packs(self, payload: str) -> str:
        return _invoke(_dispatcher.call, "catalog_with_packs", payload)

    def catalog_with_profiles(self, payload: str) -> str:
        return _invoke(_dispatcher.call, "catalog_with_profiles", payload)

    def resolve_profile(self, payload: str) -> str:
        return _invoke(_dispatcher.call, "resolve_profile", payload)

    def dynamic_profile_edges(self, payload: str) -> str:
        return _invoke(_dispatcher.call, "dynamic_profile_edges", payload)

    def denied(self, payload: str) -> str:
        return _invoke(_dispatcher.call, "denied", payload)


class Transport(wit_world.exports.Transport):
    """Closed DATA helpers using the same original registry and scope."""

    def import_values(self, payload: str) -> str:
        return _invoke(_dispatcher.import_values, payload)

    def describe_value(self, payload: str) -> str:
        return _invoke(_dispatcher.describe_value, payload)

    def copy_data_dictionary(self, payload: str) -> str:
        return _invoke(_dispatcher.copy_data_dictionary, payload)

    def release_values(self, payload: str) -> None:
        _invoke(_dispatcher.release_values, payload)

    def append_data_item(self, payload: str) -> None:
        _invoke(_dispatcher.append_data_item, payload)

    def set_data_item(self, payload: str) -> None:
        _invoke(_dispatcher.set_data_item, payload)

    def native_inputs(self, payload: str) -> str:
        return _invoke(_dispatcher.native_inputs, payload)
