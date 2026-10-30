"""Bounded observations of the original keyword-to-descend Python boundary."""


def exercise_descend() -> dict[str, bool]:
    """Compare real original handlers with the installed keyword traversal."""
    import sys
    from types import GeneratorType, MethodType

    from jsonschema import Draft202012Validator, _keywords, exceptions, validators

    factory = getattr(validators.create, "__wrapped__", validators.create)
    cls = factory(
        Draft202012Validator.META_SCHEMA,
        validators=Draft202012Validator.VALIDATORS,
    )
    adapter = sys.modules.get("schema_adapter")
    if adapter is not None:
        adapter.install(cls)
    saved_descend = cls.descend
    child, payload = {}, {"retained": []}
    cases = []
    for name, value, instance in (
        ("properties", {"field": child}, {"field": payload}),
        ("propertyNames", child, {"field": payload}),
        ("items", child, [payload]),
        ("additionalProperties", child, {"extra": payload}),
        ("prefixItems", [child], [payload]),
        ("allOf", [child], payload),
        ("properties", {None: child}, {None: payload}),
        ("additionalProperties", child, {None: payload}),
    ):
        schema = {name: value}
        cases.append((name, value, instance, schema, cls(schema)))
    shapes = (
        (2, ("path", "schema_path")),
        (0, ("instance", "schema")),
        (0, ("instance", "schema", "path")),
        (2, ("path",)),
        (0, ("instance", "schema", "schema_path", "path")),
        (2, ("schema_path",)),
        (2, ("path", "schema_path")),
        (2, ("path",)),
    )
    events, calls, code_reads = [], [], []
    active = [False]

    def audit(event: str, args: tuple) -> None:
        if active[0] and event == "object.__getattr__" and args[1] == "__code__":
            code_reads.append(args[0])

    # Hooks cannot be removed. This one captures only these small containers,
    # stays disabled outside the observations and is disabled in finally.
    sys.addaudithook(audit)

    def cursor(case: tuple, native: bool) -> object:
        name, value, instance, schema, validator = case
        if native:
            return validator.iter_errors(instance)
        return getattr(_keywords, name)(validator, value, instance, schema)

    def same_calls(left: list, right: list) -> None:
        assert len(left) == len(right)
        for expected, actual in zip(left, right):
            assert len(expected) == len(actual)
            assert all(a is b for a, b in zip(expected, actual))

    def compare(case: tuple) -> list:
        observed = []
        for native in (False, True):
            events.clear()
            calls.clear()
            code_reads.clear()
            active[0] = True
            try:
                assert list(cursor(case, native)) == []
            finally:
                active[0] = False
            assert code_reads == []
            observed.append((events[:], calls[:]))
        assert observed[0][0] == observed[1][0]
        same_calls(observed[0][1], observed[1][1])
        return observed[1][0]

    def record(*args: object, **kwargs: object) -> object:
        events.append((len(args), tuple(kwargs)))
        calls.append(args + tuple(kwargs.values()))
        return iter(())

    class Descriptor:
        def __get__(self, instance: object, owner: type) -> object:
            assert instance is target[0] and owner is cls
            events.append("get")
            return record

    class Callable:
        @property
        def __code__(self) -> object:
            raise AssertionError("custom callable metadata was inspected")

        def __call__(self, *args: object, **kwargs: object) -> object:
            return record(*args, **kwargs)

    callable_descend = Callable()

    class MethodDescriptor:
        def __get__(self, instance: object, owner: type) -> object:
            events.append("get")
            # MethodType accepts a callable object as __func__, so even a bound
            # method must not cause inspection of arbitrary callable metadata.
            return MethodType(callable_descend, instance)

    target = [None]
    try:
        cls.descend = Descriptor()
        for case, shape in zip(cases, shapes):
            target[0] = case[-1]
            assert compare(case) == ["get", shape]
        cls.descend = callable_descend
        assert compare(cases[1]) == [shapes[1]]
        cls.descend = MethodDescriptor()
        for index in (0, 1, 5):
            arity, names = shapes[index]
            assert compare(cases[index]) == ["get", (arity + 1, names)]

        def first(self: object, *args: object, **kwargs: object) -> object:
            events.append("first body")
            return record(*args, **kwargs)

        def second(self: object, *args: object, **kwargs: object) -> object:
            events.append("second body")
            return record(*args, **kwargs)

        cls.descend = first
        assert compare(cases[0]) == ["first body", shapes[0]]
        first.__code__ = second.__code__
        assert compare(cases[0]) == ["second body", shapes[0]]

        children = []

        def lazy(self: object, *args: object, **kwargs: object) -> object:
            assert self is cases[0][-1]
            calls.append(args + tuple(kwargs.values()))
            try:
                events.append("first")
                yield exceptions.ValidationError("first")
                events.append("second")
                yield exceptions.ValidationError("second")
            finally:
                events.append("closed")

        def descend(self: object, *args: object, **kwargs: object) -> object:
            events.append("acquire")
            result = lazy(self, *args, **kwargs)
            assert type(result) is GeneratorType
            children.append(result)
            return result

        cls.descend = descend
        observations = []
        for native in (False, True):
            events.clear()
            calls.clear()
            children.clear()
            code_reads.clear()
            active[0] = True
            try:
                outer = cursor(cases[0], native)
                assert type(outer) is GeneratorType and not events and not children
                outer.close()
                assert not events and not children
                outer = cursor(cases[0], native)
                assert next(outer).message == "first"
                assert events == ["acquire", "first"]
                outer.close()
                assert events[-1] == "closed" and children[-1].gi_frame is None
                outer = cursor(cases[0], native)
                assert next(outer).message == "first"
                retained = children[-1]
                del outer
                assert retained.gi_frame is None and events[-1] == "closed"
                left, right = cursor(cases[0], native), cursor(cases[0], native)
                assert next(left).message == next(right).message == "first"
                assert children[-2] is not children[-1]
                assert next(left).message == next(right).message == "second"
                left.close()
                assert children[-2].gi_frame is None
                assert children[-1].gi_frame is not None
                right.close()
                assert children[-1].gi_frame is None
                assert events[-2:] == ["closed", "closed"]
            finally:
                active[0] = False
            assert code_reads == []
            observations.append((events[:], calls[:]))
        assert observations[0][0] == observations[1][0]
        same_calls(observations[0][1], observations[1][1])
    finally:
        active[0] = False
        cls.descend = saved_descend
    return {"descend_keyword_binding_and_generator_protocol": True}
