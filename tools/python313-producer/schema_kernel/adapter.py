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
from jsonschema import _keywords, _types, _utils, exceptions  # noqa: F401

_DEFAULT_CLASS = jsonschema.validators.Draft202012Validator
_EVOLVE_CODE = _DEFAULT_CLASS.evolve.__code__
_POST_INIT_CODE = _DEFAULT_CLASS.__attrs_post_init__.__code__


_INSTALLED = weakref.WeakSet()
_ORIGINAL_CREATE = None


def _closure(function):
    code = getattr(function, "__code__", None)
    if code is None:
        return {}
    return {
        name: cell.cell_contents
        for name, cell in zip(code.co_freevars, function.__closure__ or ())
    }


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
        original["applicable_validators"], original["specification"],
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
    if native_evolve and evolve_fields is not None:
        validator_class.evolve = evolve
    _INSTALLED.add(validator_class)


def install_standard_classes():
    """Use each original class's own closure when evolve() selects a draft.

    The 36 builtin definitions are translated before their first module import.
    Every actual selected callable executes normally; custom and saved aliases
    are never bypassed by an identity check. No class/VALIDATORS map is fabricated.
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
