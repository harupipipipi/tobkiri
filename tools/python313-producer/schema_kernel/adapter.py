"""Private binding of original Validator instances to the Rust lazy traversal.

This file imports modules and captures original function/closure objects only.
It does not construct validators, registry resources, schemas or cached results.
Install explicitly in the experimental Wasm App before running its comparisons.
"""

import builtins  # noqa: F401 - genuine module initialization for Rust helpers
import fractions  # noqa: F401
import re  # noqa: F401
import weakref
import urllib.parse as _urlparse
from functools import wraps

import _tobkiri_schema_kernel as kernel
import jsonschema
from jsonschema import _keywords, _types, _utils, exceptions  # noqa: F401

_DEFAULT_CLASS = jsonschema.validators.Draft202012Validator
_DEFAULT_CHECKER = _types.draft202012_type_checker
_DEFAULT_LOOKUP = object.__getattribute__
_DEFAULT_METHOD = _types.TypeChecker.is_type
_EVOLVE_CODE = _DEFAULT_CLASS.evolve.__code__
_POST_INIT_CODE = _DEFAULT_CLASS.__attrs_post_init__.__code__


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
_ORIGINAL_URLJOIN = None
_EXACT_TYPE = type
_EXACT_LEN = len
_STRING_TYPES = (str, bytes)
_TUPLE_TYPE = tuple
_DEFAULT_URLJOIN = _urlparse.urljoin


def _closure(function):
    code = getattr(function, "__code__", None)
    if code is None:
        return {}
    return {
        name: cell.cell_contents
        for name, cell in zip(code.co_freevars, function.__closure__ or ())
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
    original_evolve = validator_class.evolve
    original_post_init = validator_class.__attrs_post_init__
    native_post_init = getattr(original_post_init, "__code__", None) is _POST_INIT_CODE
    post_fields = _closure(original_post_init) if native_post_init else None
    native_evolve = getattr(original_evolve, "__code__", None) is _EVOLVE_CODE
    evolve_fields = _closure(original_evolve).get("evolve_fields") if native_evolve else None
    # These are the actual original create() closure objects. Recomputing the
    # specification from mutable class metadata would change original behavior.
    context = kernel.make_context(
        original["applicable_validators"], original["specification"], _BUILTINS,
        _delegate,
        evolve_fields,
        original_evolve.__globals__ if native_evolve else None,
        original_evolve.__builtins__ if native_evolve else None,
    )

    def iter_errors(self, instance, _schema=None):
        yield from kernel.iter_errors(self, instance, _schema, context)

    def descend(self, instance, schema, path=None, schema_path=None, resolver=None):
        yield from kernel.descend(
            self, instance, schema, path, schema_path, resolver, context
        )

    original_is_type = validator_class.is_type

    @wraps(original_is_type)
    def is_type(self, instance, type):
        # Some out-of-contract keys make the original rpds lookup raise its
        # BaseException-derived PanicException. Preserve this Python boundary
        # instead of transporting that panic through another Rust extension.
        if builtins.type(type) is not builtins.str:
            return original_is_type(self, instance, type)
        return kernel.fast_is_type(
            self, instance, type, _DEFAULT_CLASS, _DEFAULT_CHECKER, _DEFAULT_LOOKUP,
            _DEFAULT_METHOD, original_is_type,
        )

    @wraps(original_evolve)
    def evolve(self, **changes):
        return kernel.evolve(self, changes, context)

    if native_post_init:
        # Keep the original generated attrs constructor and its call/field order.
        # These are live original closure/namespace references, not rebuilt state.
        post_applicable = post_fields["applicable_validators"]
        post_specification = post_fields["specification"]
        post_id_of = post_fields["id_of"]
        post_globals = original_post_init.__globals__
        post_builtins = original_post_init.__builtins__

        @wraps(original_post_init)
        def __attrs_post_init__(self):
            return kernel.post_init(
                self, post_applicable, post_specification, post_id_of,
                post_globals, post_builtins,
            )

        validator_class.__attrs_post_init__ = __attrs_post_init__

    validator_class.iter_errors = iter_errors
    validator_class.descend = descend
    validator_class.is_type = is_type
    if native_evolve and evolve_fields is not None:
        validator_class.evolve = evolve
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

    # Only the original referencing alias is translated. The stdlib function
    # stays available as the independent oracle; no resolver/cache is replaced.
    global _ORIGINAL_URLJOIN
    if _ORIGINAL_URLJOIN is None:
        from urllib import parse
        from referencing import _core

        original_join = _DEFAULT_URLJOIN
        if _core.urljoin is original_join and parse.urljoin is original_join:
            join_context = kernel.make_urljoin_context(original_join)
            join_code = original_join.__code__

            @wraps(original_join)
            def urljoin(*args, **kwargs):
                # Keep arbitrary input protocols/exceptions before the added
                # Rust C boundary. The real function still owns argument binding
                # and changed code/defaults; standard positional URIs are native.
                if (kwargs or _EXACT_LEN(args) not in (2, 3)
                        or original_join.__code__ is not join_code):
                    return original_join(*args, **kwargs)
                base, url = args[:2]
                if (_EXACT_TYPE(base) not in _STRING_TYPES
                        or _EXACT_TYPE(url) not in _STRING_TYPES):
                    return original_join(*args)
                if _EXACT_LEN(args) == 3:
                    allow_fragments = args[2]
                else:
                    defaults = original_join.__defaults__
                    if _EXACT_TYPE(defaults) is not _TUPLE_TYPE or not defaults:
                        return original_join(*args)
                    allow_fragments = defaults[-1]
                return kernel.urljoin(base, url, allow_fragments, join_context)

            _core.urljoin = urljoin
            _ORIGINAL_URLJOIN = original_join

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
