"""Private binding of original Validator instances to the Rust lazy traversal.

This file imports modules and captures original function/closure objects only.
It does not construct validators, registry resources, schemas or cached results.
Install explicitly in the experimental Wasm App before running its comparisons.
"""

import builtins  # noqa: F401 - genuine module initialization for Rust helpers
import fractions  # noqa: F401
import re  # noqa: F401
import weakref
from functools import wraps

import _tobkiri_schema_kernel as kernel
import jsonschema
from jsonschema import _keywords, _utils, exceptions  # noqa: F401


_BUILTINS = {
    "$ref": _keywords.ref,
    "$dynamicRef": _keywords.dynamicRef,
    "properties": _keywords.properties,
    "patternProperties": _keywords.patternProperties,
    "propertyNames": _keywords.propertyNames,
    "additionalProperties": _keywords.additionalProperties,
    "required": _keywords.required,
    "dependentRequired": _keywords.dependentRequired,
    "dependentSchemas": _keywords.dependentSchemas,
    "minProperties": _keywords.minProperties,
    "maxProperties": _keywords.maxProperties,
    "prefixItems": _keywords.prefixItems,
    "items": _keywords.items,
    "contains": _keywords.contains,
    "uniqueItems": _keywords.uniqueItems,
    "minItems": _keywords.minItems,
    "maxItems": _keywords.maxItems,
    "minimum": _keywords.minimum,
    "maximum": _keywords.maximum,
    "exclusiveMinimum": _keywords.exclusiveMinimum,
    "exclusiveMaximum": _keywords.exclusiveMaximum,
    "multipleOf": _keywords.multipleOf,
    "minLength": _keywords.minLength,
    "maxLength": _keywords.maxLength,
    "pattern": _keywords.pattern,
    "format": _keywords.format,
    "const": _keywords.const,
    "enum": _keywords.enum,
    "type": _keywords.type,
    "allOf": _keywords.allOf,
    "anyOf": _keywords.anyOf,
    "oneOf": _keywords.oneOf,
    "not": _keywords.not_,
    "if": _keywords.if_,
    "unevaluatedItems": _keywords.unevaluatedItems,
    "unevaluatedProperties": _keywords.unevaluatedProperties,
}
_INSTALLED = weakref.WeakSet()
_ORIGINAL_CREATE = None


def _closure(function):
    return {
        name: cell.cell_contents
        for name, cell in zip(function.__code__.co_freevars, function.__closure__ or ())
    }


def _delegate(iterator):
    # Keep CPython's genuine generator finalization/GeneratorExit protocol. The
    # delegated validation work remains entirely in the Rust keyword iterator.
    yield from iterator


def install(validator_class):
    """Keep the real class/state and replace only its traversal method bodies."""
    if validator_class in _INSTALLED:
        return
    original = _closure(validator_class.descend)
    # These are the actual original create() closure objects. Recomputing the
    # specification from mutable class metadata would change original behavior.
    context = kernel.make_context(
        original["applicable_validators"], original["specification"], _BUILTINS,
        _delegate,
    )

    def iter_errors(self, instance, _schema=None):
        yield from kernel.iter_errors(self, instance, _schema, context)

    def descend(self, instance, schema, path=None, schema_path=None, resolver=None):
        yield from kernel.descend(
            self, instance, schema, path, schema_path, resolver, context
        )

    validator_class.iter_errors = iter_errors
    validator_class.descend = descend
    _INSTALLED.add(validator_class)


def install_standard_classes():
    """Use each original class's own closure when evolve() selects a draft.

    The 36 common Draft 2020-12 builtin callables are dispatched in Rust by
    identity. Explicit extension and older-draft keyword callables keep their
    original dispatch contract inside this same Wasm budget. They are not a
    fallback for a failed native builtin. No class/VALIDATORS map is fabricated.
    """
    for validator_class in (
        jsonschema.Draft3Validator,
        jsonschema.Draft4Validator,
        jsonschema.Draft6Validator,
        jsonschema.Draft7Validator,
        jsonschema.Draft201909Validator,
        jsonschema.Draft202012Validator,
    ):
        install(validator_class)

    # create()/extend() may introduce classes after module initialization. Keep
    # their genuine factory/registration behavior and install the same traversal
    # on its actual result. Do not retain those classes in a new strong cache.
    global _ORIGINAL_CREATE
    if _ORIGINAL_CREATE is None:
        from jsonschema import validators

        original_create = validators.create

        @wraps(original_create)
        def create(*args, **kwargs):
            validator_class = original_create(*args, **kwargs)
            install(validator_class)
            return validator_class

        _ORIGINAL_CREATE = original_create
        validators.create = create
