"""Explicit experimental App provider; never installed in the product default.

Schema definition checks select the Rust frame engine before evaluation. Document
validation is a separately declared original operation. There is no retry after
an invalid result, callback exception, capacity refusal or trap.
"""
import _tobkiri_schema_kernel as kernel
from jsonschema import Draft202012Validator as _Original, exceptions, validators
from jsonschema.validators import _REMOTE_WARNING_REGISTRY, _UNSET

# Explicit owned-default policy selection. No Resource, Resolver, input or
# validation result is constructed during module initialization.
_REFERENCE_POLICY = kernel.default_reference_policy()


class _Metadata(type):
    def __getattr__(cls, name):
        return getattr(_Original, name)


class AppSchemaProvider(metaclass=_Metadata):
    """App-owned callable selected explicitly by the experimental native lane."""

    @classmethod
    def check_schema(cls, schema, format_checker=_UNSET):
        # Original check_schema selects from the META, not schema['$schema'].
        meta = cls.META_SCHEMA
        selected = validators.validator_for(meta, default=_Original)
        checker = selected.FORMAT_CHECKER if format_checker is _UNSET else format_checker
        if selected is not _Original:
            # Explicit pre-evaluation compatibility operation, including actual
            # custom selection. It is never reached because native validation failed.
            validator = selected(schema=meta, format_checker=checker)
            for error in validator.iter_errors(schema):
                raise exceptions.SchemaError.create_from(error)
            return
        kernel.app_check_schema(schema, meta, _REMOTE_WARNING_REGISTRY,
                                _REFERENCE_POLICY, selected.TYPE_CHECKER, checker)

    def __new__(cls, *args, **kwargs):
        # Document validation has not been migrated by this check_schema slice.
        # This dispatch occurs before that independent operation starts.
        return _Original(*args, **kwargs)
