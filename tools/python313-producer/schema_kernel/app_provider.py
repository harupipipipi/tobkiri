"""Explicit experimental App provider; never installed in the product default.

Schema definition checks select the Rust frame engine before evaluation. Document
validation is a separately declared original operation. There is no retry after
an invalid result, callback exception, capacity refusal or trap.
"""
import _tobkiri_schema_kernel as kernel
from jsonschema import Draft202012Validator as _Original, exceptions, validators
from jsonschema.validators import _UNSET
from app_schema_model import DefinitionModel

# Capture canonical compatibility metadata before constructing the App's own
# definition graph. Neither step loads an App input or creates validation results.
kernel.default_reference_policy()
_MODEL = DefinitionModel()
_REFERENCE_POLICY = kernel.app_reference_policy(_MODEL.specifications)
_VALUE_POLICY = kernel.default_value_policy()


class AppSchemaProvider:
    """App-owned callable selected explicitly by the experimental native lane."""

    META_SCHEMA = _MODEL.meta
    FORMAT_CHECKER = _MODEL.checker
    TYPE_CHECKER = _MODEL.type_checker
    _SPECIFICATIONS = _MODEL.specifications
    _REGISTRY = _MODEL.registry

    @classmethod
    def check_schema(cls, schema, format_checker=_UNSET):
        # Original check_schema selects from the META, not schema['$schema'].
        meta = cls.META_SCHEMA
        selected = validators.validator_for(meta, default=_Original)
        checker = ((cls.FORMAT_CHECKER if selected is _Original else selected.FORMAT_CHECKER)
                   if format_checker is _UNSET else format_checker)
        if selected is not _Original:
            # Explicit pre-evaluation compatibility operation, including actual
            # custom selection. It is never reached because native validation failed.
            validator = selected(schema=meta, format_checker=checker)
            for error in validator.iter_errors(schema):
                raise exceptions.SchemaError.create_from(error)
            return
        kernel.app_check_schema(schema, meta, cls._REGISTRY,
                                _REFERENCE_POLICY, cls.TYPE_CHECKER, checker,
                                value_policy=_VALUE_POLICY, type_owner=cls)

    def __new__(cls, *args, **kwargs):
        # Document validation has not been migrated by this check_schema slice.
        # This dispatch occurs before that independent operation starts.
        return _Original(*args, **kwargs)
