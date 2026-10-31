"""App-owned definition DATA from the artifact's lock-pinned dependencies.

Document paths: jsonschema-specifications 2025.9.1. Format registration
definitions: jsonschema 4.26.0/_format.py (MIT, packaged dependency notice).
No App schema, catalog, validation or result is read/prepared here. Callback
bodies and their globals remain explicit shared dependencies in this slice.
"""
from collections.abc import Mapping as _EqualityMapping, Sequence as _EqualitySequence
from importlib.resources import files
import builtins
import json

from jsonschema import _format, exceptions
from jsonschema._types import draft202012_type_checker
from jsonschema.validators import _warn_for_remote_retrieve
from referencing import Registry
from referencing.jsonschema import DRAFT202012

ROOT_URI = "https://json-schema.org/draft/2020-12/schema"
SOURCE_DOCUMENTS = ("metaschema.json", *("vocabularies/" + name for name in (
    "core", "applicator", "unevaluated", "validation", "meta-data",
    "format-annotation", "content")))

# Explicit source definitions, not a copy of a mutable checker singleton.
# Optional callbacks are admitted only when that dependency defined them.
FORMAT_DEFINITIONS = (
    ("email", "is_email", ()), ("idn-email", "is_email", ()),
    ("ipv4", "is_ipv4", ("ipaddress.AddressValueError",)),
    ("ipv6", "is_ipv6", ("ipaddress.AddressValueError",)),
    ("hostname", "is_host_name", ("ValueError",)),
    ("idn-hostname", "is_idn_host_name", ("idna.IDNAError", "UnicodeError")),
    ("uri", "is_uri", None),
    ("uri-reference", "is_uri_reference", ("ValueError",)),
    ("iri", "is_iri", ("ValueError",)),
    ("iri-reference", "is_iri_reference", ("ValueError",)),
    ("date-time", "is_datetime", ()), ("time", "is_time", ()),
    ("regex", "is_regex", ("re.error",)), ("date", "is_date", ("ValueError",)),
    ("json-pointer", "is_json_pointer", ("jsonpointer.JsonPointerException",)),
    ("relative-json-pointer", "is_relative_json_pointer", ("jsonpointer.JsonPointerException",)),
    ("uri-template", "is_uri_template", ()),
    ("duration", "is_duration", ("isoduration.DurationParsingException",)),
    ("uuid", "is_uuid", ("ValueError",)),
)


def _exception(name: str) -> type[Exception]:
    parts = name.split(".")
    value = vars(_format).get(parts[0], vars(builtins).get(parts[0]))
    for part in parts[1:]:
        value = getattr(value, part)
    return value


class DefinitionModel:
    """Own the selected default contents, resources and registration table."""

    def __init__(self) -> None:
        self.type_checker = draft202012_type_checker
        root = files("jsonschema_specifications").joinpath("schemas", "draft202012")
        documents = [json.loads(root.joinpath(name).read_bytes()) for name in SOURCE_DOCUMENTS]
        resources = [(document["$id"], DRAFT202012.create_resource(document))
                     for document in documents]
        self.specifications = Registry().with_resources(resources).crawl()
        # Match the original class-root view: a distinct root dict with the
        # same actual children as this model's registry root, not Original's.
        self.meta = dict(self.specifications.contents(ROOT_URI))
        self.registry = self.specifications.combine(Registry(retrieve=_warn_for_remote_retrieve))
        self.checker = _format.FormatChecker(formats=[])
        for name, callback_name, raises in FORMAT_DEFINITIONS:
            callback = vars(_format).get(callback_name)
            if callback is None:
                continue
            if raises is None:
                raises = ("ValueError",) if "rfc3987" in vars(_format) else ()
            exceptions = tuple(_exception(item) for item in raises)
            # Preserve the actual exception-class / tuple representation.
            caught = exceptions[0] if len(exceptions) == 1 else exceptions
            self.checker.checkers[name] = (callback, caught)


def type_boundary(checker, instance, kind, schema):
    """Run the actual selected custom operation with its original error context."""
    try:
        return checker.is_type(instance, kind)
    except exceptions.UndefinedTypeCheck:
        raise exceptions.UnknownType(kind, instance, schema) from None


# Fixed keyword definitions from jsonschema 4.26.0 validators.py:767-804.
# Only dispatch names are stored; no schema, input, permission or result is kept.
_KEYWORD_NAMES = {name: name for name in (
    "$dynamicRef", "$ref", "additionalProperties", "allOf", "anyOf", "const",
    "contains", "dependentRequired", "dependentSchemas", "enum",
    "exclusiveMaximum", "exclusiveMinimum", "format", "if", "items",
    "maxItems", "maxLength", "maxProperties", "maximum", "minItems",
    "minLength", "minProperties", "minimum", "multipleOf", "not", "oneOf",
    "pattern", "patternProperties", "prefixItems", "properties",
    "propertyNames", "required", "type", "unevaluatedItems",
    "unevaluatedProperties", "uniqueItems",
)}


def unpack_keyword_pair(pair):
    """Preserve the actual foreign pair's sequence-unpack operations/errors."""
    key, value = pair
    return key, value


def select_keyword(key, root):
    """Dispatch a foreign key using source membership/getitem or descend get."""
    if root:
        return _KEYWORD_NAMES[key] if key in _KEYWORD_NAMES else None
    return _KEYWORD_NAMES.get(key)


def keyword_has_schema_path(key):
    """Preserve foreign-key hash/equality at the error-decoration boundary."""
    return key not in {"if", "$ref"}


# Owned operation boundaries derived from jsonschema 4.26.0 _utils.py:99-143
# and _keywords.py:262-266; MIT attribution is in COPYING.jsonschema. These
# helpers retain no input/result and do not consult the generic helper globals.


def _equality_unbool(value, true=object(), false=object()):
    if value is True:
        return true
    if value is False:
        return false
    return value


def _equality_sequence(one, two):
    if len(one) != len(two):
        return False
    return all(equality_boundary(left, right) for left, right in zip(one, two))


def _equality_mapping(one, two):
    if len(one) != len(two):
        return False
    return all(key in two and equality_boundary(value, two[key])
               for key, value in one.items())


def equality_boundary(one, two):
    """Run actual container/foreign equality, retaining its truth-result object."""
    if one is two:
        return True
    if isinstance(one, str) or isinstance(two, str):
        return one == two
    if isinstance(one, _EqualitySequence) and isinstance(two, _EqualitySequence):
        return _equality_sequence(one, two)
    if isinstance(one, _EqualityMapping) and isinstance(two, _EqualityMapping):
        return _equality_mapping(one, two)
    return _equality_unbool(one) == _equality_unbool(two)


def type_is_single(value):
    """Preserve foreign __class__ observations at the type-specification check."""
    return isinstance(value, str)


def type_error(instance, types, single):
    """Build a type error after the actual second-pass kind representations."""
    # join also normalizes a str-subclass returned by a custom __repr__;
    # passing that subclass straight to the f-string adds a __format__ call.
    if single:
        reprs = ", ".join((repr(types),))
    else:
        reprs = ", ".join(repr(kind) for kind in types)
    return exceptions.ValidationError(f"{instance!r} is not of type {reprs}")


def additional_properties_error(extras, schema):
    """Build the error using live schema and the source operation order."""
    if "patternProperties" in schema:
        verb = "does" if len(extras) == 1 else "do"
        joined = ", ".join(repr(each) for each in sorted(extras))
        patterns = ", ".join(repr(each) for each in sorted(schema["patternProperties"]))
        error = f"{joined} {verb} not match any of the regexes: {patterns}"
        return exceptions.ValidationError(error)
    error = "Additional properties are not allowed (%s %s unexpected)"
    constructor = exceptions.ValidationError
    ordered = sorted(extras, key=str)
    verb = "was" if len(ordered) == 1 else "were"
    joined = ", ".join(repr(extra) for extra in ordered)
    return constructor(error % (joined, verb))
