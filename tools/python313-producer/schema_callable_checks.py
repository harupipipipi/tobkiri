"""Separate supported callable contracts from Python reflection diagnostics."""

_CALLABLE_CHECK_EVENTS = []
_CALLABLE_CHECK_ERROR = None
_CALLABLE_CHECK_MARKER = None
_CALLABLE_CHECK_FAILURE = None


def _properties(validator: object, properties: object, instance: object,
                schema: object) -> object:
    _CALLABLE_CHECK_EVENTS.append((validator, properties, instance, schema))
    yield _CALLABLE_CHECK_ERROR("live properties code")


def _checker(self: object, instance: object, type: object) -> object:
    _CALLABLE_CHECK_EVENTS.append((self, instance, type))
    if _CALLABLE_CHECK_FAILURE is not None:
        raise _CALLABLE_CHECK_FAILURE
    return _CALLABLE_CHECK_MARKER


def _predicate(checker: object, instance: object) -> object:
    _CALLABLE_CHECK_EVENTS.append((checker, instance))
    if _CALLABLE_CHECK_FAILURE is not None:
        raise _CALLABLE_CHECK_FAILURE
    return _CALLABLE_CHECK_MARKER


def exercise_callable_contracts() -> dict[str, bool]:
    """Require documented callback delegation and opaque FFI table rejection."""
    import sys

    from jsonschema import Draft202012Validator, _keywords, exceptions, validators

    events = []
    value, instance = object(), object()
    schema = {"callable-contract": value}

    def initial(validator: object, value: object, instance: object,
                schema: object) -> object:
        events.append(("initial", validator, value, instance, schema))
        yield exceptions.ValidationError("initial callback")

    def replacement(validator: object, value: object, instance: object,
                    schema: object) -> object:
        events.append(("replacement", validator, value, instance, schema))
        yield exceptions.ValidationError("replacement callback")

    cls = validators.extend(Draft202012Validator, {"callable-contract": initial})
    existing = cls(schema)
    alias = initial

    def check(action: object, validator: object, label: str) -> None:
        events.clear()
        errors = list(action())
        assert len(errors) == 1 and errors[0].message == label + " callback"
        assert type(errors[0]) is exceptions.ValidationError
        assert len(events) == 1 and events[0][0] == label
        assert all(a is b for a, b in zip(
            events[0][1:], (validator, value, instance, schema)
        ))

    try:
        check(lambda: alias(existing, value, instance, schema), existing, "initial")
        check(lambda: existing.iter_errors(instance), existing, "initial")
        cls.VALIDATORS["callable-contract"] = replacement
        fresh = cls(schema)
        check(lambda: alias(existing, value, instance, schema), existing, "initial")
        check(lambda: existing.iter_errors(instance), existing, "initial")
        check(lambda: fresh.iter_errors(instance), fresh, "replacement")
    finally:
        cls.VALIDATORS["callable-contract"] = initial

    parent = Draft202012Validator.VALIDATORS["properties"]
    schema = {"properties": {"x": {"type": "integer"}}}
    instance = {"x": "bad"}

    def delegating(validator: object, value: object, instance: object,
                   schema: object) -> object:
        events.append((validator, value, instance, schema))
        yield from parent(validator, value, instance, schema)

    cls = validators.extend(Draft202012Validator, {"properties": delegating})
    validator = cls(schema)
    events.clear()
    errors = list(validator.iter_errors(instance))
    assert len(events) == len(errors) == 1
    assert all(a is b for a, b in zip(
        events[0], (validator, schema["properties"], instance, schema)
    ))
    assert type(errors[0]) is exceptions.ValidationError
    assert list(errors[0].path) == ["x"]
    assert errors[0].message == "'bad' is not of type 'integer'"

    if sys.modules.get("_tobkiri_schema_kernel") is not None:
        # All prior rejection inputs remain mandatory: mutable module globals
        # must never let a Python value forge C vectorcall keyword metadata.
        saved_names = _keywords._tobkiri_keyword_names
        schema = {"properties": {"x": {"type": "integer"}}}
        instance = {"x": "bad"}
        validator = Draft202012Validator(schema)
        try:
            for bad in (None, (None,) * 8, (("path",) * 20,) * 8):
                _keywords._tobkiri_keyword_names = bad
                try:
                    list(_keywords.properties(
                        validator, schema["properties"], instance, schema
                    ))
                except TypeError:
                    pass
                else:
                    raise AssertionError("forged keyword metadata was accepted")
        finally:
            _keywords._tobkiri_keyword_names = saved_names
    return {"callable_delegation_and_ffi_metadata_safety": True}


def _metadata(function: object) -> tuple:
    return (function.__name__, function.__qualname__, function.__module__,
            function.__doc__, function.__defaults__, function.__kwdefaults__)


def exercise_callable_diagnostics() -> dict:
    """Report optional Python code/reflection observations independently."""
    import sys
    from types import FunctionType

    from jsonschema import Draft202012Validator, _keywords, _types, exceptions

    active, code_reads, audit_counts, cleanup_error = [False], [], [], [None]

    def audit(event: str, args: tuple) -> None:
        if active[0] and event == "object.__getattr__" and args[1] == "__code__":
            code_reads.append(1)

    # Permanent hooks retain only a disabled gate and integer observations.
    sys.addaudithook(audit)

    def invoke(action: object) -> object:
        code_reads.clear()
        active[0] = True
        try:
            return action()
        finally:
            # No assertions here: cleanup must not mask a fatal BaseException.
            active[0] = False
            audit_counts.append(len(code_reads))

    def cleanup(action: object) -> None:
        primary = sys.exc_info()[1]
        try:
            action()
        except BaseException as error:
            if primary is not None and not isinstance(primary, Exception):
                raise primary
            cleanup_error[0] = error
            raise

    def failed(error: Exception) -> dict:
        return {
            "status": "failed",
            "reason": "Python function diagnostic raised an exception",
            "exception_class": (
                type(error).__module__ + "." + type(error).__qualname__
            ),
            "message": str(error),
            "audit_code_reads": audit_counts[:],
        }

    def diagnose(function: object, module: object, observe: object) -> dict:
        if not isinstance(function, FunctionType):
            return {
                "status": "not-python-function",
                "reason": "callable has no mutable Python function code",
                "callable_type": type(function).__name__,
            }
        audit_counts.clear()
        cleanup_error[0] = None
        names = ("_CALLABLE_CHECK_EVENTS", "_CALLABLE_CHECK_ERROR",
                 "_CALLABLE_CHECK_MARKER", "_CALLABLE_CHECK_FAILURE")
        missing, marker = object(), object()
        events, failure = [], ValueError("live checker code")
        try:
            code, metadata = function.__code__, _metadata(function)
            assert code.co_freevars == (), "replacement requires no closure cells"
            namespace = function.__globals__
            assert namespace is vars(module)
            saved = {name: namespace.get(name, missing) for name in names}
        except Exception as error:
            return failed(error)
        observation_error = None
        try:
            try:
                namespace.update(zip(names, (events, exceptions.ValidationError,
                                             marker, None)))
                observe(function, code, namespace, events, marker, failure)
                assert _metadata(function) == metadata, "function metadata changed"
                assert not any(audit_counts), "invocation inspected function code"
            except Exception as error:
                if cleanup_error[0] is error:
                    raise
                observation_error = error
        finally:
            active[0] = False

            def restore() -> None:
                function.__code__ = code
                for name, value in saved.items():
                    if value is missing:
                        namespace.pop(name, None)
                    else:
                        namespace[name] = value

            # Restoration is outside the optional observation catch. Failure
            # must propagate because later cases cannot assume restored state.
            cleanup(restore)
        if observation_error is not None:
            return failed(observation_error)
        return {
            "status": "passed",
            "reason": "Python code mutation, metadata and audit checks passed",
            "audit_code_reads": audit_counts[:],
        }

    def keyword(function: object, code: object, namespace: dict, events: list,
                marker: object, failure: Exception) -> None:
        mapping = Draft202012Validator.VALIDATORS
        mapped = mapping["properties"]
        assert mapped is function
        schema = {"properties": {
            "first": {"type": "integer"}, "second": {"type": "integer"},
        }}
        instance = {"first": "bad first", "second": "bad second"}
        validator = Draft202012Validator(schema)
        selected = validator._validators[0][0]
        alias, started = function, None
        assert selected is alias

        def live_error(action: object) -> None:
            errors = invoke(action)
            assert len(errors) == 1 and errors[0].message == "live properties code"
            assert type(errors[0]) is exceptions.ValidationError
            assert all(a is b for a, b in zip(
                events[-1], (validator, schema["properties"], instance, schema)
            ))

        try:
            started = alias(validator, schema["properties"], instance, schema)
            assert started.gi_code is code and not events
            first = invoke(lambda: next(started))
            assert list(first.path) == ["first"]
            assert first.instance is instance["first"]
            function.__code__ = _properties.__code__
            assert _keywords.properties is alias is selected
            second = invoke(lambda: next(started))
            assert list(second.path) == ["second"]
            assert second.instance is instance["second"] and not events
            invoke(started.close)
            live_error(lambda: list(alias(
                validator, schema["properties"], instance, schema
            )))
            live_error(lambda: list(selected(
                validator, schema["properties"], instance, schema
            )))
            live_error(lambda: list(validator.iter_errors(instance)))

            def mapped_handler(validator: object, properties: object,
                               instance: object, schema: object) -> object:
                yield exceptions.ValidationError("live mapping handler")

            mapping["properties"] = mapped_handler
            fresh = Draft202012Validator(schema)
            assert fresh._validators[0][0] is mapped_handler
            live_error(lambda: list(validator.iter_errors(instance)))
            errors = invoke(lambda: list(fresh.iter_errors(instance)))
            assert len(errors) == 1 and errors[0].message == "live mapping handler"
        finally:
            def restore_keyword() -> None:
                try:
                    mapping["properties"] = mapped
                finally:
                    if started is not None:
                        cleanup(started.close)

            cleanup(restore_keyword)

    def type_calls(function: object, code: object, namespace: dict, events: list,
                   marker: object, failure: Exception, predicate: bool) -> None:
        checker = Draft202012Validator.TYPE_CHECKER
        instance = {"retained": []}
        validator = Draft202012Validator({})
        if predicate:
            assert checker._type_checkers["object"] is function is _types.is_object
            function.__code__ = _predicate.__code__
            calls = [lambda: function(checker, instance)]
        else:
            assert _types.TypeChecker.is_type is function
            function.__code__ = _checker.__code__
            calls = []
        calls.extend((lambda: checker.is_type(instance, "object"),
                      lambda: validator.is_type(instance, "object")))
        for call in calls:
            assert invoke(call) is marker
            assert events[-1][0] is checker and events[-1][1] is instance
        namespace["_CALLABLE_CHECK_FAILURE"] = failure
        for call in calls:
            try:
                invoke(call)
            except ValueError as error:
                assert error is failure
            else:
                raise AssertionError("live checker/predicate exception was lost")

    checker = Draft202012Validator.TYPE_CHECKER
    try:
        return {
            "properties": diagnose(_keywords.properties, _keywords, keyword),
            "type_checker": diagnose(
                _types.TypeChecker.is_type, _types,
                lambda *args: type_calls(*args, predicate=False),
            ),
            "object_predicate": diagnose(
                checker._type_checkers["object"], _types,
                lambda *args: type_calls(*args, predicate=True),
            ),
        }
    finally:
        active[0] = False
