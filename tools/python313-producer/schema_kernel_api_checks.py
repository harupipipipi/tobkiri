"""Actual guest observations for the port's lazy public API, not product setup."""


def exercise_api():
    """Exercise real validators/callbacks and retain their original objects."""
    from jsonschema import Draft202012Validator, exceptions, validators

    events = []
    children = []

    def callback(validator, value, instance, schema):
        def errors():
            try:
                events.append("first")
                yield exceptions.ValidationError("first")
                events.append("second")
                yield exceptions.ValidationError("second")
            finally:
                events.append("closed")

        child = errors()
        children.append(child)
        return child

    extended = validators.extend(Draft202012Validator, {"native-test": callback})
    schema, instance = {"native-test": True}, {"actual": []}
    validator = extended(schema)
    cursor = validator.iter_errors(instance)
    assert events == [] and children == []
    first = next(cursor)
    assert events == ["first"]
    assert type(first) is exceptions.ValidationError
    assert first.instance is instance and first.schema is schema
    assert first.validator_value is schema["native-test"]
    second = cursor.send("ignored by the original traversal's for loop")
    assert events == ["first", "second"] and second.message == "second"
    assert second.schema is schema and second.instance is instance
    try:
        next(cursor)
    except StopIteration:
        pass
    else:
        raise AssertionError("native cursor did not finish")
    assert events == ["first", "second", "closed"]

    # Original iter_errors uses for, not yield-from: closing its outer generator
    # must not close a callback iterator retained elsewhere.
    events.clear()
    cursor = validator.iter_errors(instance)
    next(cursor)
    child = children[-1]
    cursor.close()
    assert events == ["first"]
    assert next(child).message == "second"
    child.close()
    assert events == ["first", "second", "closed"]

    # Independent iterators retain the same real Validator but separate progress.
    events.clear()
    left, right = validator.iter_errors(instance), validator.iter_errors(instance)
    assert next(left).message == "first" and next(right).message == "first"
    assert next(left).message == "second" and next(right).message == "second"
    left.close()
    right.close()
    for child in children:
        child.close()

    import gc
    import weakref

    delegated = []
    custom = validators.extend(Draft202012Validator, {})
    original_descend = custom.descend

    def retain_descend(self, *args, **kwargs):
        child = original_descend(self, *args, **kwargs)
        delegated.append(child)
        return child

    custom.descend = retain_descend
    cursor = custom({"properties": {"x": {"type": "integer"}}}).iter_errors({"x": "bad"})
    del cursor
    assert delegated == []
    cursor = custom({"properties": {"x": {"type": "integer"}}}).iter_errors({"x": "bad"})
    next(cursor)
    assert len(delegated) == 1 and delegated[0].gi_frame is not None
    cursor.close()
    assert delegated[0].gi_frame is None

    cursor = custom({"properties": {"x": {"type": "integer"}}}).iter_errors({"x": "bad"})
    next(cursor)
    retained_child = delegated[-1]
    del cursor
    assert retained_child.gi_frame is None

    cyclic_instance = {"x": "bad"}
    cursor = custom({"properties": {"x": {"type": "integer"}}}).iter_errors(cyclic_instance)
    next(cursor)
    retained_child = delegated[-1]
    cyclic_instance["cycle"] = cursor
    del cyclic_instance, cursor
    gc.collect()
    assert retained_child.gi_frame is None

    list(custom({"properties": {"x": {"type": "integer"}}}).iter_errors({"x": "bad"}))
    assert delegated[-1].gi_frame is None

    def stopped(validator, value, instance, schema):
        raise StopIteration("callback exception")

    exceptional = validators.extend(Draft202012Validator, {"native-stop": stopped})
    cursor = exceptional({"native-stop": True}).iter_errors(instance)
    try:
        next(cursor)
    except RuntimeError as error:
        assert str(error) == "generator raised StopIteration"
        assert type(error.__cause__) is StopIteration
        assert error.__context__ is error.__cause__
    else:
        raise AssertionError("callback StopIteration was swallowed")
    try:
        next(cursor)
    except StopIteration:
        pass
    else:
        raise AssertionError("failed generator resumed")

    error = next(validator.descend(instance, False, path="unused", schema_path="unused"))
    assert error.schema is False and error.instance is instance
    assert list(error.path) == [] and list(error.schema_path) == []
    evolved_schema = {"type": "integer"}
    evolved = validator.evolve(schema=evolved_schema)
    assert type(evolved) is extended and evolved.schema is evolved_schema

    ephemeral = validators.extend(Draft202012Validator, {})
    original_class = weakref.ref(ephemeral)
    del ephemeral
    gc.collect()
    assert original_class() is None

    exercise_types()
    return {
        "lazy_start_and_resume": True,
        "actual_error_objects_and_references": True,
        "outer_close_keeps_retained_callback_open": True,
        "interleaved_iterators": True,
        "pep479_and_failed_iterator_closed": True,
        "false_descend_preserves_original_early_paths": True,
        "evolve_preserves_actual_class_and_schema": True,
        "unregistered_extension_class_not_cached": True,
        "keyword_delegation_closes_retained_child": True,
        "keyword_deallocation_closes_retained_child": True,
        "keyword_cycle_collection_closes_retained_child": True,
        "unstarted_keyword_does_not_acquire_child": True,
        "default_type_dispatch_matches_original": True,
    }


def exercise_types():
    """Compare real return objects/exceptions with the original type method."""
    import numbers
    from fractions import Fraction
    from jsonschema import Draft202012Validator, exceptions

    schema = {}
    validator = Draft202012Validator(schema)
    method = Draft202012Validator.is_type
    original = getattr(method, "__wrapped__", method)

    class ListSubclass(list):
        pass

    class DictSubclass(dict):
        pass

    class StrSubclass(str):
        pass

    values = [None, True, False, 1, 10 ** 300, 1.0, 1.2, float("nan"),
              float("inf"), -float("inf"), complex(1, 2), Fraction(1, 2),
              [], {}, "x", (), ListSubclass(), DictSubclass(), StrSubclass("x")]
    for value in values:
        for kind in ("array", "object", "string", "boolean", "null", "integer", "number"):
            assert validator.is_type(value, kind) is original(validator, value, kind)

    # The original integral-float predicate may return a subclass's arbitrary
    # result object. Integer fast dispatch must not normalize that to bool.
    marker, calls = object(), []

    class FloatSubclass(float):
        def is_integer(self):
            calls.append("integer")
            return marker

    value = FloatSubclass(1)
    assert validator.is_type(value, "integer") is marker
    assert original(validator, value, "integer") is marker
    assert calls == ["integer", "integer"]

    raised = ValueError("integer override")

    class RaisingFloat(float):
        def is_integer(self):
            raise raised

    for check in (validator.is_type, lambda value, kind: original(validator, value, kind)):
        try:
            check(RaisingFloat(1), "integer")
        except ValueError as error:
            assert error is raised
        else:
            raise AssertionError("integer subclass exception was lost")

    class RegisteredNumber:
        pass

    value = RegisteredNumber()
    assert validator.is_type(value, "number") is original(validator, value, "number") is False
    numbers.Number.register(RegisteredNumber)
    assert validator.is_type(value, "number") is original(validator, value, "number") is True
    numbers.Number._abc_caches_clear()
    assert validator.is_type(value, "number") is original(validator, value, "number") is True

    def outcome(check, kind):
        try:
            result = check(1, kind)
        except Exception as error:
            if isinstance(error, exceptions.UnknownType):
                assert error.instance is not None and error.schema is schema
            return type(error), str(error), error.__suppress_context__
        return result

    for kind in ("unknown", "\ud800", StrSubclass("integer")):
        assert outcome(validator.is_type, kind) == outcome(
            lambda value, name: original(validator, value, name), kind
        )

    saved = Draft202012Validator.TYPE_CHECKER
    custom_calls = []

    def custom(checker, value):
        assert checker is Draft202012Validator.TYPE_CHECKER
        custom_calls.append(value)
        return value is marker

    Draft202012Validator.TYPE_CHECKER = saved.redefine("integer", custom)
    try:
        assert validator.is_type(marker, "integer") is True
        assert original(validator, marker, "integer") is True
        assert custom_calls == [marker, marker]
    finally:
        Draft202012Validator.TYPE_CHECKER = saved

    reads = []
    lookup_missing = "__getattribute__" not in Draft202012Validator.__dict__
    original_lookup = Draft202012Validator.__getattribute__

    def lookup(self, name):
        if name == "TYPE_CHECKER":
            reads.append(name)
        return original_lookup(self, name)

    Draft202012Validator.__getattribute__ = lookup
    Draft202012Validator.TYPE_CHECKER = saved.redefine("integer", custom)
    try:
        assert validator.is_type(marker, "integer") is True
        assert reads == ["TYPE_CHECKER"]
        reads.clear()
        assert original(validator, marker, "integer") is True
        assert reads == ["TYPE_CHECKER"]
    finally:
        Draft202012Validator.TYPE_CHECKER = saved
        if lookup_missing:
            del Draft202012Validator.__getattribute__
        else:
            Draft202012Validator.__getattribute__ = original_lookup

    fail_lookup = [False]

    class CheckerDescriptor:
        def __get__(self, instance, owner):
            reads.append("descriptor")
            if fail_lookup[0]:
                raise raised
            return saved

    Draft202012Validator.TYPE_CHECKER = CheckerDescriptor()
    try:
        reads.clear()
        assert validator.is_type(1, "integer") is True
        assert reads == ["descriptor"]
        reads.clear()
        assert original(validator, 1, "integer") is True
        assert reads == ["descriptor"]
        fail_lookup[0] = True
        reads.clear()
        try:
            validator.is_type(1, "integer")
        except ValueError as error:
            assert error is raised
        else:
            raise AssertionError("checker descriptor exception was lost")
        assert reads == ["descriptor"]
    finally:
        Draft202012Validator.TYPE_CHECKER = saved

    checker_class = type(saved)
    checker_method = checker_class.is_type
    checker_class.is_type = lambda checker, value, kind: marker
    try:
        assert validator.is_type(1, "integer") is marker
        assert original(validator, 1, "integer") is marker
    finally:
        checker_class.is_type = checker_method

    override_calls = []

    def override(self, instance, kind):
        override_calls.append((instance, kind))
        return True

    Draft202012Validator.is_type = override
    try:
        assert list(Draft202012Validator({"type": "string"}).iter_errors(1)) == []
        assert override_calls == [(1, "string")]
    finally:
        Draft202012Validator.is_type = method

    import jsonschema

    for cls in (jsonschema.Draft3Validator, jsonschema.Draft4Validator,
                jsonschema.Draft6Validator, jsonschema.Draft7Validator,
                jsonschema.Draft201909Validator):
        other = cls({})
        reference = getattr(cls.is_type, "__wrapped__", cls.is_type)
        assert other.is_type(1.0, "integer") is reference(other, 1.0, "integer")


def exercise_unsupported_native():
    """Observe out-of-contract non-str keys separately from guest APIs.

    The declared type-name parameter is str. Original rpds panics for this
    key collisions; an uncatchable Wasm trap cannot be placed in the same
    successful return as the normal API suite. No schema case is removed.
    """
    import sys
    from jsonschema import Draft202012Validator

    if sys.platform != "linux":
        raise RuntimeError("native unsupported-key observation requires Linux CI")
    validator = Draft202012Validator({})
    original = getattr(Draft202012Validator.is_type, "__wrapped__", Draft202012Validator.is_type)
    cases = []
    for kind in (None, [], b"integer"):
        outcomes = []
        for check in (validator.is_type, lambda value, kind: original(validator, value, kind)):
            try:
                check(1, kind)
            except BaseException as error:
                name = type(error).__module__ + "." + type(error).__qualname__
                if not isinstance(error, Exception) and name != "pyo3_runtime.PanicException":
                    raise
                outcomes.append({"type": name, "message": str(error),
                                 "suppress_context": error.__suppress_context__})
            else:
                raise AssertionError("original unsupported-key failure disappeared")
        assert outcomes[0] == outcomes[1]
        cases.append({"kind": repr(kind), "outcome": outcomes[0]})
    return {
        "same_original_outcome": True,
        "cases": cases,
        "scope": "out-of-contract native non-str keys only; no Wasm negative-probe proof",
    }


if __name__ == "__main__":
    import json
    import os
    import sys

    if (sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true"
            or sys.version_info[:3] != (3, 13, 16)):
        raise RuntimeError("actual original API execution is restricted to pinned Linux CI")
    print(json.dumps({
        "format": "tobkiri.schema-port-original-api/1",
        "checks": exercise_api(),
        "wasm_executed": False, "production_enabled": False,
    }, sort_keys=True))
    print(json.dumps({
        "format": "tobkiri.schema-port-original-unsupported/1",
        "observation": exercise_unsupported_native(),
        "wasm_executed": False, "production_enabled": False,
    }, sort_keys=True))
