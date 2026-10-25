"""Capture bounded exception-frame facts without evaluating error attributes."""

from __future__ import annotations

APP_IMPORT = b"from exercise import exercise\n"
EXCEPT_MARKER = (
    b"            observation = exercise()\n"
    b"        except Exception as error:\n"
)
FRAMES_MARKER = b'                        "frames": frames,\n'
# Emitted strings are capped at 128 characters; type names split that cap
# between module and qualified name. Raw integers are limited to 64 bits.
MAX_DIAGNOSTIC_TEXT_CHARS = 128
MAX_DIAGNOSTIC_FRAMES = 32
MAX_RAW_EXCEPTION_FIELDS = 32
MAX_RAW_INTEGER_BITS = 64
MAX_TYPE_MODULE_CHARS = 63
MAX_TYPE_QUALNAME_CHARS = 64
_EXCEPTION_NAMESPACE = BaseException.__dict__["__dict__"]
_EXCEPTION_TRACEBACK = BaseException.__dict__["__traceback__"]
_TYPE_MODULE = type.__dict__["__module__"]
_TYPE_QUALNAME = type.__dict__["__qualname__"]
_GETSET_DESCRIPTOR_TYPE = type(_EXCEPTION_NAMESPACE)


def _type_name(value: object) -> str:
    """Return a concrete qualified type name without formatting the value."""
    value_type = type(value)
    if any(
        type(descriptor) is not _GETSET_DESCRIPTOR_TYPE
        for descriptor in (_TYPE_MODULE, _TYPE_QUALNAME)
    ):
        return "unknown"
    try:
        module = _TYPE_MODULE.__get__(value_type, type(value_type))
        qualified = _TYPE_QUALNAME.__get__(value_type, type(value_type))
    except BaseException:
        return "unknown"
    if type(module) is not str or type(qualified) is not str:
        return "unknown"
    return (
        module[:MAX_TYPE_MODULE_CHARS]
        + "."
        + qualified[-MAX_TYPE_QUALNAME_CHARS:]
    )


def _raw_instance_fields(error: BaseException) -> dict[str, dict[str, object]]:
    """Copy only bounded primitive facts from the built-in instance namespace."""
    try:
        if type(_EXCEPTION_NAMESPACE) is not _GETSET_DESCRIPTOR_TYPE:
            return {}
        namespace = _EXCEPTION_NAMESPACE.__get__(error, type(error))
    except BaseException:
        return {}
    if type(namespace) is not dict:
        return {}

    fields: dict[str, dict[str, object]] = {}
    for index, (key, value) in enumerate(namespace.items()):
        if index >= MAX_RAW_EXCEPTION_FIELDS:
            break
        if type(key) is not str:
            continue
        field: dict[str, object] = {"type": _type_name(value)}
        if type(value) is str:
            field["value"] = value[:MAX_DIAGNOSTIC_TEXT_CHARS]
        elif type(value) is int:
            if value.bit_length() <= MAX_RAW_INTEGER_BITS:
                field["value"] = value
            else:
                field["value_omitted"] = "integer exceeds the 64-bit diagnostic bound"
        elif type(value) in (bool, type(None)):
            field["value"] = value
        fields[key[:MAX_DIAGNOSTIC_TEXT_CHARS]] = field
    return fields


def _local_facts(locals_map: dict[str, object]) -> dict[str, object]:
    """Record the relevant frame-local presence and safe scalar facts."""
    facts: dict[str, object] = {}
    for name in ("name", "error", "value"):
        if name not in locals_map:
            facts[name] = {"present": False}
            continue
        value = locals_map[name]
        fact: dict[str, object] = {"present": True, "type": _type_name(value)}
        if name == "name" and type(value) is str:
            fact["value"] = value[:MAX_DIAGNOSTIC_TEXT_CHARS]
        if name == "error" and issubclass(type(value), BaseException):
            fact["raw_instance_fields"] = _raw_instance_fields(value)
        facts[name] = fact
    return facts


def capture_exception_diagnostics(error: BaseException) -> dict[str, object]:
    """Capture bounded local facts and raw exception fields without formatting."""
    if type(_EXCEPTION_TRACEBACK) is not _GETSET_DESCRIPTOR_TYPE:
        raise TypeError("built-in exception traceback descriptor required")
    traceback = _EXCEPTION_TRACEBACK.__get__(error, type(error))
    frames: list[dict[str, object]] = []
    current = traceback
    while current is not None and len(frames) < MAX_DIAGNOSTIC_FRAMES:
        frame = current.tb_frame
        module = frame.f_globals.get("__name__")
        frames.append(
            {
                "module": (
                    module[:MAX_DIAGNOSTIC_TEXT_CHARS]
                    if type(module) is str
                    else None
                ),
                "function": frame.f_code.co_name[:MAX_DIAGNOSTIC_TEXT_CHARS],
                "line": current.tb_lineno,
                "locals": _local_facts(frame.f_locals),
            }
        )
        current = current.tb_next
    return {"format": "tobkiri.schema-exception-diagnostics/1", "frames": frames}


def safe_exception_diagnostics(error: BaseException) -> dict[str, object]:
    """Keep the original exception path intact if bounded capture itself fails."""
    try:
        return capture_exception_diagnostics(error)
    except BaseException:
        return {
            "format": "tobkiri.schema-exception-diagnostics/1",
            "capture_failed": True,
        }


def derive_diagnostic_app(
    baseline: bytes, helper: bytes
) -> tuple[bytes, dict[str, object]]:
    """Derive the exception-only app and return hash-bound diff provenance."""
    import difflib
    import hashlib

    if type(baseline) is not bytes or type(helper) is not bytes:
        raise TypeError("baseline app and diagnostic helper bytes required")
    import_anchor = APP_IMPORT
    except_anchor = EXCEPT_MARKER
    frames_anchor = FRAMES_MARKER
    if (
        baseline.count(import_anchor) != 1
        or baseline.count(except_anchor) != 1
        or baseline.count(frames_anchor) != 1
    ):
        raise ValueError("frozen baseline app no longer matches exact derivation anchors")

    derived = baseline.replace(
        import_anchor,
        import_anchor + b"from exception_diagnostics import safe_exception_diagnostics\n",
        1,
    )
    derived = derived.replace(
        except_anchor,
        except_anchor
        + b"            diagnostics = safe_exception_diagnostics(error)\n",
        1,
    )
    derived = derived.replace(
        frames_anchor,
        frames_anchor + b'                        "diagnostics": diagnostics,\n',
        1,
    )
    baseline_text = baseline.decode("utf-8")
    derived_text = derived.decode("utf-8")
    diff = "".join(
        difflib.unified_diff(
            baseline_text.splitlines(keepends=True),
            derived_text.splitlines(keepends=True),
            fromfile="baseline/app.py",
            tofile="diagnostic/app.py",
        )
    )
    provenance: dict[str, object] = {
        "format": "tobkiri.schema-exception-diagnostic-app/1",
        "diagnostic_only": True,
        "baseline_app": {
            "path": "app.py",
            "bytes": len(baseline),
            "sha256": hashlib.sha256(baseline).hexdigest(),
        },
        "derived_app": {
            "path": "app.py",
            "bytes": len(derived),
            "sha256": hashlib.sha256(derived).hexdigest(),
        },
        "helper": {
            "path": "exception_diagnostics.py",
            "bytes": len(helper),
            "sha256": hashlib.sha256(helper).hexdigest(),
        },
        "diff": diff,
        "diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        "original_failure_must_remain_failure": True,
        "production_enabled": False,
    }
    return derived, provenance
