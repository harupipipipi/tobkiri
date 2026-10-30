"""Observe live code changes on genuine keyword, checker and predicate objects."""

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


def exercise_callable_changes() -> dict[str, bool]:
    """Mutate original function objects and retain their real binding/state."""
    import sys

    from jsonschema import Draft202012Validator, _keywords, _types, exceptions

    properties = _keywords.properties
    checker_method = _types.TypeChecker.is_type
    checker = Draft202012Validator.TYPE_CHECKER
    predicate = checker._type_checkers["object"]
    assert predicate is _types.is_object
    functions = (properties, checker_method, predicate)
    codes = tuple(function.__code__ for function in functions)
    assert all(code.co_freevars == () for code in codes)
    metadata = tuple(
        (function.__name__, function.__qualname__, function.__module__,
         function.__doc__, function.__defaults__, function.__kwdefaults__)
        for function in functions
    )
    names = (
        "_CALLABLE_CHECK_EVENTS", "_CALLABLE_CHECK_ERROR",
        "_CALLABLE_CHECK_MARKER", "_CALLABLE_CHECK_FAILURE",
    )
    missing, marker, failure = object(), object(), ValueError("live checker code")
    events, code_reads, active = [], [], [False]
    namespaces = (properties.__globals__, checker_method.__globals__)
    assert namespaces[0] is vars(_keywords) and namespaces[1] is vars(_types)
    assert predicate.__globals__ is namespaces[1]
    saved_globals = [
        {name: namespace.get(name, missing) for name in names}
        for namespace in namespaces
    ]
    selected_mapping = Draft202012Validator.VALIDATORS
    mapped_properties = selected_mapping["properties"]
    assert mapped_properties is properties
    schema = {"properties": {
        "first": {"type": "integer"}, "second": {"type": "integer"},
    }}
    instance = {"first": "bad first", "second": "bad second"}
    validator = Draft202012Validator(schema)
    selected = validator._validators[0][0]
    alias = properties
    assert selected is alias
    started = None

    def audit(event: str, args: tuple) -> None:
        if active[0] and event == "object.__getattr__" and args[1] == "__code__":
            code_reads.append(args[0])

    # The hook is permanent, but captures no validator/class/module and remains
    # disabled outside each call, including all intentional code gets/sets.
    sys.addaudithook(audit)

    def invoke(action: object) -> object:
        code_reads.clear()
        active[0] = True
        try:
            return action()
        finally:
            active[0] = False
            assert code_reads == []

    def live_error(action: object) -> None:
        errors = invoke(action)
        assert len(errors) == 1 and errors[0].message == "live properties code"
        assert type(errors[0]) is exceptions.ValidationError
        assert events[-1][0] is validator
        assert events[-1][1] is schema["properties"]
        assert events[-1][2] is instance and events[-1][3] is schema

    try:
        for namespace in namespaces:
            namespace.update(zip(names, (events, exceptions.ValidationError,
                                         marker, None)))
        started = alias(validator, schema["properties"], instance, schema)
        assert started.gi_code is codes[0] and not events
        first = invoke(lambda: next(started))
        assert list(first.path) == ["first"] and first.instance is instance["first"]
        properties.__code__ = _properties.__code__
        assert _keywords.properties is alias is selected
        # A suspended genuine generator keeps the code with which it began.
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

        def mapped(validator: object, properties: object, instance: object,
                   schema: object) -> object:
            yield exceptions.ValidationError("live mapping handler")

        selected_mapping["properties"] = mapped
        fresh = Draft202012Validator(schema)
        assert fresh._validators[0][0] is mapped
        # Existing validators retain their original selected callable object;
        # changing its code is live, replacing a class mapping selects new ones.
        live_error(lambda: list(validator.iter_errors(instance)))
        errors = invoke(lambda: list(fresh.iter_errors(instance)))
        assert len(errors) == 1 and errors[0].message == "live mapping handler"
        selected_mapping["properties"] = mapped_properties
        properties.__code__ = codes[0]

        checker_method.__code__ = _checker.__code__
        assert _types.TypeChecker.is_type is checker_method
        for call in (lambda: checker.is_type(instance, "object"),
                     lambda: validator.is_type(instance, "object")):
            assert invoke(call) is marker
            assert events[-1][0] is checker and events[-1][1] is instance
        namespaces[1]["_CALLABLE_CHECK_FAILURE"] = failure
        for call in (lambda: checker.is_type(instance, "object"),
                     lambda: validator.is_type(instance, "object")):
            try:
                invoke(call)
            except ValueError as error:
                assert error is failure
            else:
                raise AssertionError("live checker exception was lost")
        checker_method.__code__ = codes[1]
        namespaces[1]["_CALLABLE_CHECK_FAILURE"] = None

        predicate.__code__ = _predicate.__code__
        assert checker._type_checkers["object"] is predicate is _types.is_object
        calls = (lambda: predicate(checker, instance),
                 lambda: checker.is_type(instance, "object"),
                 lambda: validator.is_type(instance, "object"))
        for call in calls:
            assert invoke(call) is marker
            assert events[-1][0] is checker and events[-1][1] is instance
        namespaces[1]["_CALLABLE_CHECK_FAILURE"] = failure
        for call in calls:
            try:
                invoke(call)
            except ValueError as error:
                assert error is failure
            else:
                raise AssertionError("live predicate exception was lost")
        assert metadata == tuple(
            (function.__name__, function.__qualname__, function.__module__,
             function.__doc__, function.__defaults__, function.__kwdefaults__)
            for function in functions
        )
    finally:
        active[0] = False
        for function, code in zip(functions, codes):
            function.__code__ = code
        selected_mapping["properties"] = mapped_properties
        for namespace, saved in zip(namespaces, saved_globals):
            for name, value in saved.items():
                if value is missing:
                    namespace.pop(name, None)
                else:
                    namespace[name] = value
        if started is not None:
            started.close()
    kernel = sys.modules.get("_tobkiri_schema_kernel")
    if kernel is not None:
        # Mutable module globals must never let Python forge C vectorcall names.
        saved_names = _keywords._tobkiri_keyword_names
        try:
            for bad in (None, (None,) * 8, (("path",) * 20,) * 8):
                _keywords._tobkiri_keyword_names = bad
                try:
                    list(properties(validator, schema["properties"], instance, schema))
                except TypeError:
                    pass
                else:
                    raise AssertionError("forged keyword metadata was accepted")
        finally:
            _keywords._tobkiri_keyword_names = saved_names
    return {"live_keyword_and_checker_code_changes": True}
