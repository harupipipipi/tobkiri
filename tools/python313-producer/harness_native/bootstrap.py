"""Fixed original class references for the inactive whole nativeKit candidate.

This loader does not fill missing native facades or initialize Host state. The
assembled original module closure and native binding bootstrap must be complete
before this candidate can execute; an import failure is not domain success.
"""

from .dispatcher import OriginalRuntime
from .native_bootstrap import installed_inputs
from .native_path import NativePath
from .value_graph import RecordType, Scope


def load_original_runtime(scope: Scope) -> OriginalRuntime:
    """Use the original qualified classes; never select them from a frame."""
    inputs = installed_inputs(scope)
    inputs.modules.install()
    try:
        return _load_bound_runtime(inputs)
    except BaseException:
        inputs.modules.close()
        raise


def _load_bound_runtime(inputs) -> OriginalRuntime:
    """Original code executes only after its actual inputs were installed."""
    from core_runtime.profile_runtime_port import SetupActivationDecision
    from ecosystem.defaultspack.defaultspack.profile_runtime_composition import (
        DefaultspackProfileRuntime,
    )
    from ecosystem.defaultspack.domain.profile_resolution import (
        ProfileResolutionDenied,
        ResolvedDefaultProfile,
    )
    from ecosystem.defaultspack.domain._profile_runtime_records import (
        ActiveDefaultProfile,
    )
    from tobkiri_protocol.bundle_catalog import (
        BundleIntegrityError,
        BundledCatalog,
        DefaultProfileV4Error,
    )

    from .exception_graph import _FIXED_EXCEPTION_CLASSES

    errors = (
        ("type-error", TypeError),
        ("key-error", KeyError),
        ("index-error", IndexError),
        ("attribute-error", AttributeError),
        *_FIXED_EXCEPTION_CLASSES.items(),
        ("default-profile-v4-error", DefaultProfileV4Error),
        ("bundle-integrity-error", BundleIntegrityError),
        ("profile-resolution-denied", ProfileResolutionDenied),
    )
    records = (
        RecordType("native-path", NativePath, native_binding=True),
        RecordType(
            "bundled-catalog",
            BundledCatalog,
            (
                "root",
                "packs",
                "bases",
                "shells",
                "profiles",
                "artifact_root",
                "executable_catalogs",
            ),
        ),
        RecordType(
            "resolved-default-profile",
            ResolvedDefaultProfile,
            ("profile", "lock", "plan"),
        ),
        RecordType(
            "setup-activation-decision",
            SetupActivationDecision,
            ("confirmation", "response"),
        ),
        RecordType(
            "active-default-profile", ActiveDefaultProfile, ("resolved", "activation")
        ),
        # Retain the actual exception object privately. Its fields, message and
        # cause/context disclosure require a separate proven extraction policy.
        *(RecordType(kind, cls) for kind, cls in errors),
    )
    return OriginalRuntime(
        DefaultspackProfileRuntime(), records, tuple(cls for _, cls in errors),
        inputs.inputs,
    )
