"""App-owned definition DATA from the artifact's lock-pinned dependencies.

Document paths: jsonschema-specifications 2025.9.1. Format registration
definitions: jsonschema 4.26.0/_format.py (MIT, packaged dependency notice).
No App schema, catalog, validation or result is read/prepared here. Callback
bodies and their globals remain explicit shared dependencies in this slice.
"""
from importlib.resources import files
import builtins
import json

from jsonschema import _format
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
