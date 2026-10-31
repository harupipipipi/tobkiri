"""Explicit experimental App provider; never installed in the product default.

Schema definition checks select the Rust frame engine before evaluation. Document
validation is a separately declared original operation. There is no retry after
an invalid result, callback exception, capacity refusal or trap.
"""
import _tobkiri_schema_kernel as kernel
from jsonschema import Draft202012Validator as _Original, exceptions, validators
from jsonschema.validators import _REMOTE_WARNING_REGISTRY, _UNSET

# Capture the original class's actual specification closure, not a reconstructed
# URI-derived replacement. This binds code metadata only, not a workload/resource.
def _closure(function):
    return dict(zip(function.__code__.co_freevars,
                    (cell.cell_contents for cell in function.__closure__ or ())))

_SPECIFICATION = _closure(_Original.__attrs_post_init__)["specification"]


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
                                _SPECIFICATION, selected.TYPE_CHECKER, checker)

    def __new__(cls, *args, **kwargs):
        # Document validation has not been migrated by this check_schema slice.
        # This dispatch occurs before that independent operation starts.
        return _Original(*args, **kwargs)
