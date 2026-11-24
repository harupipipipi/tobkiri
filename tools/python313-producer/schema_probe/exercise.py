"""Identical observable calls for unchanged native and Wasm JSON Schema packages.

The native oracle imports ordinary extracted wheels. It does not import the
Wasm loader, generate expected values from Wasm output, or replace validation.
"""

import hashlib
import importlib
from importlib import metadata, resources
import json

PURE = ("attrs", "jsonschema", "jsonschema-specifications", "referencing", "packaging")
OPTIONAL = (
    "fqdn",
    "idna",
    "rfc3987",
    "rfc3986_validator",
    "rfc3987_syntax",
    "rfc3339_validator",
    "webcolors",
    "jsonpointer",
    "uri_template",
    "isoduration",
)


def error_record(error):
    """Retain nested diagnostics and causes without unrecorded wildcards."""
    from jsonschema.exceptions import _unset

    result = {
        "type": type(error).__module__ + "." + type(error).__qualname__,
        "message": getattr(error, "message", str(error)),
        "rendered": str(error),
    }
    for name in ("validator", "validator_value", "instance", "schema"):
        if hasattr(error, name):
            value = getattr(error, name)
            result[name] = {"unset": True} if value is _unset else value
    for name in ("path", "absolute_path", "schema_path", "absolute_schema_path"):
        if hasattr(error, name):
            result[name] = list(getattr(error, name))
    result["context"] = [error_record(child) for child in getattr(error, "context", ())]
    cause = getattr(error, "cause", error.__cause__)
    result["cause"] = (
        None
        if cause is None
        else {
            "type": type(cause).__module__ + "." + type(cause).__qualname__,
            "message": str(cause),
        }
    )
    return result


def exercise():
    """Perform validation, late module/resource/metadata IO and public API calls."""
    import sys
    import jsonschema
    from jsonschema import Draft202012Validator, Draft7Validator, FormatChecker
    import rpds
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012
    import jsonschema_specifications

    lazy_before = {
        name: name in sys.modules
        for name in (
            "referencing.retrieval",
            "jsonschema.protocols",
            "packaging.version",
        )
    }
    retrieval = importlib.import_module("referencing.retrieval")
    protocols = importlib.import_module("jsonschema.protocols")
    version = importlib.import_module("packaging.version")
    specifiers = importlib.import_module("packaging.specifiers")
    retrieved = []

    @retrieval.to_cached_resource()
    def retrieve(uri):
        retrieved.append(uri)
        return '{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"integer"}'

    registry = Registry(retrieve=retrieve)
    first = registry.get_or_retrieve("urn:fixture:cached")
    second = first.registry.get_or_retrieve("urn:fixture:cached")
    definitions = {"$id": "urn:fixture:definition", "type": "integer", "minimum": 2}
    registry = Registry().with_resource(
        "urn:fixture:definition",
        Resource.from_contents(definitions, default_specification=DRAFT202012),
    )
    cases = [
        (
            "object-valid",
            {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
            },
            {"name": "Harness"},
        ),
        (
            "object-invalid",
            {"type": "object", "required": ["name"], "additionalProperties": False},
            {"extra": True},
        ),
        (
            "local-ref-valid",
            {"$defs": {"n": {"type": "integer"}}, "$ref": "#/$defs/n"},
            3,
        ),
        (
            "local-ref-invalid",
            {"$defs": {"n": {"type": "integer"}}, "$ref": "#/$defs/n"},
            "3",
        ),
        ("external-ref-valid", {"$ref": "urn:fixture:definition"}, 4),
        ("external-ref-invalid", {"$ref": "urn:fixture:definition"}, 1),
        ("unresolved-ref", {"$ref": "urn:fixture:missing"}, 1),
        (
            "nested-context",
            {
                "type": "object",
                "properties": {
                    "choice": {
                        "anyOf": [
                            {"type": "integer", "minimum": 5},
                            {"type": "string", "minLength": 3},
                        ]
                    }
                },
            },
            {"choice": False},
        ),
        (
            "array",
            {"type": "array", "prefixItems": [{"type": "integer"}], "items": False},
            ["x", 2],
        ),
        (
            "unevaluated",
            {"allOf": [{"properties": {"a": True}}], "unevaluatedProperties": False},
            {"a": 1, "b": 2},
        ),
        ("boolean-schema", False, {"anything": 1}),
    ]
    validations = []
    for name, schema, value in cases:
        try:
            Draft202012Validator.check_schema(schema)
            errors = [
                error_record(e)
                for e in Draft202012Validator(
                    schema, registry=registry, format_checker=FormatChecker()
                ).iter_errors(value)
            ]
            validations.append({"id": name, "errors": errors})
        except Exception as error:
            validations.append({"id": name, "exception": error_record(error)})
    for name, schema in (
        ("invalid-schema-type", {"type": "unknown-type"}),
        ("invalid-schema-required", {"required": "name"}),
    ):
        try:
            Draft202012Validator.check_schema(schema)
            validations.append({"id": name, "errors": []})
        except Exception as error:
            validations.append({"id": name, "exception": error_record(error)})
    legacy_schema = {"$id": "urn:fixture:legacy", "$ref": "urn:fixture:definition"}
    resolver = jsonschema.RefResolver.from_schema(
        legacy_schema, store={"urn:fixture:definition": definitions}
    )
    legacy = [
        error_record(e)
        for e in Draft7Validator(legacy_schema, resolver=resolver).iter_errors("wrong")
    ]

    inputs = {
        "email": ("a@example.com", "missing-at"),
        "idn-email": ("本人@example.com", "missing-at"),
        "ipv4": ("127.0.0.1", "999.1.1.1"),
        "ipv6": ("::1", "not-an-ip"),
        "regex": ("a+", "["),
        "date": ("2024-02-29", "2023-02-29"),
        "time": ("12:34:56", "25:00:00"),
        "uuid": ("123e4567-e89b-12d3-a456-426614174000", "bad"),
    }
    checker = FormatChecker()
    formats = {}
    for name in sorted(checker.checkers):
        if name not in inputs:
            raise ValueError("unexpected optional format helper: " + name)
        outcomes = []
        for value in inputs[name]:
            try:
                checker.check(value, name)
                outcomes.append({"accepted": True})
            except Exception as error:
                outcomes.append({"error": error_record(error)})
        formats[name] = outcomes

    specification_resources = []
    from jsonschema_specifications._core import _schemas

    for resource in _schemas():
        contents = resource.contents
        identifier = resource.id()
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("original schema resource has no declared identifier")
        if jsonschema_specifications.REGISTRY.contents(identifier) != contents:
            raise ValueError("original schema resource registry differs")
        raw = json.dumps(
            contents, sort_keys=True, ensure_ascii=True, separators=(",", ":")
        ).encode()
        specification_resources.append(
            {"id": identifier, "sha256": hashlib.sha256(raw).hexdigest()}
        )
    root = resources.files("jsonschema_specifications")
    missing = root.joinpath("not-a-shipped-resource")
    try:
        missing.read_bytes()
        missing_result = "unexpected-success"
    except Exception as error:
        missing_result = type(error).__module__ + "." + type(error).__qualname__

    distributions = {}
    for name in PURE:
        distribution = metadata.distribution(name)
        files = []
        for path in sorted(distribution.files, key=str):
            raw = path.locate().read_bytes()
            files.append(
                {
                    "path": str(path),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "size": path.size,
                    "record_hash": None
                    if path.hash is None
                    else [path.hash.mode, path.hash.value],
                }
            )
        distributions[name] = {
            "name": distribution.metadata["Name"],
            "version": distribution.version,
            "normalized_query": metadata.version(name.replace("-", ".").upper()),
            "metadata_sha256": hashlib.sha256(
                distribution.read_text("METADATA").encode()
            ).hexdigest(),
            "requires": distribution.requires,
            "files": files,
        }
    persistent = rpds.HashTrieMap({"before": 1})
    inserted = persistent.insert("after", 2)
    removed = inserted.remove("before")
    values = rpds.HashTrieSet(["one"]).insert("two").discard("one")
    return {
        "lazy_before": lazy_before,
        "lazy_protocol": hasattr(protocols, "Validator"),
        "retrieved": retrieved,
        "retrieval_contents": [first.value.contents, second.value.contents],
        "validations": validations,
        "legacy_ref_errors": legacy,
        "formats": formats,
        "optional_helpers_imported": [name for name in OPTIONAL if name in sys.modules],
        "specification_resources": sorted(
            specification_resources, key=lambda x: x["id"]
        ),
        "missing_resource": missing_result,
        "distributions": distributions,
        "rpds_wrapper": {
            "public_names": sorted(n for n in vars(rpds) if not n.startswith("_")),
            "all": getattr(rpds, "__all__", None),
            "doc": rpds.__doc__,
            "delegated_doc": rpds.__doc__ == rpds.rpds.__doc__,
            "delegated_all": getattr(rpds, "__all__", None)
            == getattr(rpds.rpds, "__all__", None),
        },
        "rpds_operations": {
            "original": dict(persistent),
            "inserted": dict(inserted),
            "removed": dict(removed),
            "missing": persistent.get("absent", 7),
            "set_values": sorted(values),
        },
        "packaging": {
            "version": str(version.Version("1.2.3rc1")),
            "contains": version.Version("1.2.3") in specifiers.SpecifierSet(">=1.2,<2"),
        },
    }
