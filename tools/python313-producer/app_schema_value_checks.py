"""Differential observations of live App values and numeric boundaries."""


def value_checks(provider, kernel) -> dict[str, bool]:
    """Keep the original oracle, actual objects, and operation-time callbacks."""
    import gc
    import sys
    import weakref
    from decimal import Decimal
    from fractions import Fraction
    from numbers import Number

    from jsonschema import Draft202012Validator as original, FormatChecker, exceptions
    from schema_kernel_observation import first_difference

    fields = ("native_type_checks", "python_type_checks",
              "native_numeric_comparisons", "python_numeric_comparisons")
    operators = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum")
    checks = {}

    def counts():
        values = kernel.app_value_counts()
        assert type(values) is dict and all(
            type(values.get(name)) is int and values[name] >= 0 for name in fields)
        return {name: values[name] for name in fields}

    def tag(value):
        return type(value).__module__ + "." + type(value).__qualname__

    def failure_graph(error, supplied=None, seen=None):
        if error is None:
            return None
        seen = {} if seen is None else seen
        if id(error) in seen:
            return {"ref": seen[id(error)]}
        seen[id(error)] = len(seen)
        return {"type": tag(error), "message": str(error),
                "is_supplied": error is supplied,
                "suppressed": error.__suppress_context__,
                "cause": failure_graph(error.__cause__, supplied, seen),
                "context": failure_graph(error.__context__, supplied, seen)}

    def run(selected, make):
        state = make()
        state["owner"] = selected
        meta, actual = state["meta"], state["actual"]
        events = state.setdefault("events", [])
        known = {}

        def index(value, path, active=()):
            known.setdefault(id(value), []).append(path)
            if id(value) in active:
                return
            assert len(active) < 16, "bounded value fixture required"
            children = (value.items() if type(value) is dict else
                        enumerate(value) if type(value) is list else ())
            for key, child in children:
                index(child, path + "/" + repr(key), (*active, id(value)))

        def describe(value):
            return {"type": tag(value), "repr": repr(value),
                    "identity": sorted(set(known.get(id(value), [])))}

        def graph(error, seen=None):
            return failure_graph(error, state.get("fault", state.get("unknown_context")), seen)

        def observe(error, root=True):
            assert type(error) is (exceptions.SchemaError if root else exceptions.ValidationError)
            assert id(error.schema) in known, "error lost the actual META node"
            assert id(error.instance) in known, "error lost the actual input node"
            return {"type": tag(error), "message": error.message,
                    "validator": describe(error.validator),
                    "validator_value": describe(error.validator_value),
                    "schema": describe(error.schema), "instance": describe(error.instance),
                    "path": list(error.path), "schema_path": list(error.schema_path),
                    "absolute_path": list(error.absolute_path),
                    "absolute_schema_path": list(error.absolute_schema_path),
                    "cause": graph(error.cause),
                    "exception_cause": graph(error.__cause__),
                    "exception_context": graph(error.__context__),
                    "suppressed": error.__suppress_context__,
                    "context": [observe(child, False) for child in error.context],
                    "children_reparented": all(child.parent is error for child in error.context)}

        index(meta, "meta")
        index(actual, "actual")
        for label, value in state.get("refs", {}).items():
            index(value, "retained/" + label)
        saved_meta, saved_type = selected.META_SCHEMA, selected.TYPE_CHECKER
        before = counts()
        selected.META_SCHEMA = meta
        if "type_checker" in state:
            selected.TYPE_CHECKER = state["type_checker"]
        try:
            try:
                selected.check_schema(actual, format_checker=state.get("checker"))
            except exceptions.SchemaError as error:
                index(meta, "meta-after")
                index(actual, "actual-after")
                outcome = {"kind": "schema-error", "error": observe(error)}
                if "error_schema" in state:
                    assert error.schema is state["error_schema"]
            except Exception as error:
                outcome = {"kind": "exception", "error": graph(error)}
                if "fault" in state:
                    assert error is state["fault"], "callback exception identity changed"
                if "unknown_context" in state:
                    assert type(error) is exceptions.UnknownType
                    assert error.__context__ is state["unknown_context"]
                    assert error.__cause__ is None and error.__suppress_context__
                    assert error.schema is meta and error.instance is actual
                    assert error.type is meta["type"]
            else:
                outcome = {"kind": "valid"}
        finally:
            selected.META_SCHEMA, selected.TYPE_CHECKER = saved_meta, saved_type
        after = counts()
        delta = {name: after[name] - before[name] for name in fields}
        assert all(value >= 0 for value in delta.values())
        if selected is original:
            assert not any(delta.values()), "original oracle entered native values"
        if "fault" in state:
            assert outcome["kind"] == "exception"
        return {"outcome": outcome, "events": events,
                "actual": describe(actual), "meta": describe(meta)}, delta

    def compare(make, positive=()):
        expected, _ = run(original, make)
        actual, delta = run(provider, make)
        difference = first_difference(expected, actual)
        assert difference is None, difference
        assert all(delta[name] > 0 for name in positive), (positive, delta)

    primitive_values = (lambda: None, lambda: True, lambda: False, lambda: 1,
                        lambda: 1.0, lambda: 1.5, lambda: 2 ** 240,
                        lambda: float("nan"), lambda: float("inf"), lambda: -float("inf"))
    for kind in ("integer", "number"):
        for value in primitive_values:
            compare(lambda: {"meta": {"type": kind}, "actual": value()},
                    ("native_type_checks",))
    for kind, value in (("object", dict), ("array", list), ("string", lambda: "plain"),
                        ("boolean", lambda: True), ("null", lambda: None)):
        compare(lambda: {"meta": {"type": kind}, "actual": value()},
                ("native_type_checks",))
    checks["exact_primitive_type_distinctions_and_nonfinite_values"] = True

    for operator in operators:
        for actual, bound in ((1, 2), (2, 1), (2, 2), (1.5, 2), (2, 1.5), (1.5, 1.5),
                              (2 ** 63 - 1, -(2 ** 63)),
                              (2 ** 53, float(2 ** 53)), (-(2 ** 53), -float(2 ** 53))):
            compare(lambda: {"meta": {operator: bound}, "actual": actual},
                    ("native_type_checks", "native_numeric_comparisons"))
        for actual, bound in ((2 ** 240, 2 ** 239),
                              (2 ** 63, 2 ** 63 - 1),
                              (2 ** 53 + 1, float(2 ** 53)),
                              (float(2 ** 53), 2 ** 53 + 1),
                              (-(2 ** 53) - 1, -float(2 ** 53))):
            compare(lambda: {"meta": {operator: bound}, "actual": actual},
                    ("python_numeric_comparisons",))
        for actual, bound in ((float("nan"), 1.0), (1.0, float("nan")),
                              (float("inf"), 1.0), (-float("inf"), 1.0),
                              (float("inf"), float("inf")), (-0.0, 0.0)):
            compare(lambda: {"meta": {operator: bound}, "actual": actual},
                    ("native_type_checks", "native_numeric_comparisons"))
    checks["all_four_safe_and_unsafe_numeric_bounds"] = True

    class Int(int):
        pass

    class Float(float):
        pass

    class Numeric(Number):
        def __repr__(self):
            return "Numeric(2)"

        def __lt__(self, other):
            return 2 < other

        def __gt__(self, other):
            return 2 > other

        def __le__(self, other):
            return 2 <= other

        def __ge__(self, other):
            return 2 >= other

    nonstandard = (lambda: Int(2), lambda: Float(2), lambda: Decimal("2"),
                   lambda: Fraction(2, 1), Numeric)
    for value in nonstandard:
        for kind in ("integer", "number"):
            compare(lambda: {"meta": {"type": kind}, "actual": value()},
                    ("python_type_checks",))
        for operator in operators:
            compare(lambda: {"meta": {operator: 1}, "actual": value()},
                    ("python_type_checks", "python_numeric_comparisons"))
    checks["nonstandard_numeric_types_use_actual_python_boundaries"] = True

    def custom_type():
        events, actual = [], 2

        def integer(checker, value):
            events.append({"integer": value is actual})
            return False

        return {"meta": {"type": "integer"}, "actual": actual, "events": events,
                "type_checker": original.TYPE_CHECKER.redefine("integer", integer)}

    compare(custom_type, ("python_type_checks",))
    checks["selected_custom_type_checker_is_called"] = True

    def live_type_policy():
        events, actual, state = [], {"trigger": 0, "value": 2}, {}

        def integer(checker, value):
            events.append({"replacement_integer": value is actual["value"]})
            return False

        replacement = original.TYPE_CHECKER.redefine("integer", integer)

        def replace(value):
            events.append({"replace_type_policy": value is actual["trigger"]})
            state["owner"].TYPE_CHECKER = replacement
            return True

        checker = FormatChecker(formats=[])
        checker.checks("replace")(replace)
        state.update(meta={"properties": {"trigger": {"format": "replace"},
                                          "value": {"type": "integer"}}},
                     actual=actual, events=events, checker=checker)
        return state

    compare(live_type_policy, ("native_type_checks", "python_type_checks"))
    checks["selected_type_policy_replacement_is_read_after_callback"] = True

    def observed_kind():
        events = []

        class Kind(str):
            def __hash__(self):
                events.append({"actual_kind_hash": self is kind})
                return str.__hash__(self)

        kind = Kind("integer")
        return {"meta": {"type": kind}, "actual": 2, "events": events}

    compare(observed_kind, ("python_type_checks",))
    checks["nonexact_type_kind_preserves_actual_hash_callback"] = True

    for kind in ("unknown-app-type", "\ud800"):
        compare(lambda: {"meta": {"type": kind}, "actual": 0},
                ("python_type_checks",))
    checks["unknown_and_surrogate_type_names_keep_actual_exception_graph"] = True

    def mutation(mode):
        events = []
        old, new = {"type": "string"}, {"type": "integer"}
        inserted = Decimal("2")
        if mode in ("after-binding", "same-child"):
            old = {"format": "change", "type": "string"}
            meta, actual = {"properties": {"value": old}}, {"value": 2}
        elif mode == "alias":
            meta = {"properties": {"first": old, "trigger": {"format": "change"}, "last": old}}
            actual = {"first": "ok", "trigger": 0, "last": 2}
        else:
            meta = {"properties": {"trigger": {"format": "change"}, "value": old}}
            actual = {"trigger": 0, "value": 2}
            if mode == "insert":
                old.update(type="number", maximum=3)

        def change(value):
            events.append({"change": value is actual.get("trigger", actual.get("value")),
                           "same_old": meta["properties"].get("value", old) is old})
            if mode == "insert":
                actual["value"] = inserted
            elif mode in ("same-child", "alias"):
                old["type"] = "integer"
            else:
                meta["properties"]["value"] = new
            return True

        checker = FormatChecker(formats=[])
        checker.checks("change")(change)
        state = {"meta": meta, "actual": actual, "checker": checker, "events": events,
                 "refs": {"old": old, "new": new, "inserted": inserted}}
        if mode == "after-binding":
            state["error_schema"] = old
        return state

    for mode in ("before-binding", "after-binding", "same-child", "alias"):
        compare(lambda: mutation(mode), ("native_type_checks",))
    checks["replacement_binding_same_child_and_multipath_alias_mutations"] = True
    compare(lambda: mutation("insert"), ("python_type_checks", "python_numeric_comparisons"))
    checks["callback_inserted_nonstandard_value_is_read_live"] = True

    def comparison_failure(operator):
        events, fault = [], RuntimeError("actual numeric comparison failure")

        class Compared(float):
            def fail(self, other):
                events.append({"comparison": operator, "bound": other})
                raise fault

            __lt__ = __gt__ = __le__ = __ge__ = fail

        return {"meta": {operator: 1}, "actual": Compared(2),
                "events": events, "fault": fault}

    for operator in operators:
        compare(lambda: comparison_failure(operator),
                ("python_type_checks", "python_numeric_comparisons"))
    checks["custom_comparison_exception_order_and_identity"] = True

    def descriptor_failure():
        events, fault = [], RuntimeError("actual integer descriptor failure")

        class Integral(float):
            @property
            def is_integer(self):
                events.append("integer-descriptor")
                raise fault

        return {"meta": {"type": "integer"}, "actual": Integral(2),
                "events": events, "fault": fault}

    compare(descriptor_failure, ("python_type_checks",))
    checks["integer_descriptor_exception_order_and_identity"] = True

    def iterator_failure():
        events, fault = [], RuntimeError("actual array iterator failure")

        class Values(list):
            def __iter__(self):
                events.append("iterator-start")
                yield self[0]
                events.append("iterator-after-child")
                raise fault

        actual = Values([1, 2])
        checker = FormatChecker(formats=[])
        checker.checks("trace")(lambda value: events.append({"child": value is actual[0]}) or True)
        return {"meta": {"prefixItems": [{"format": "trace"}, True]}, "actual": actual,
                "checker": checker, "events": events, "fault": fault}

    compare(iterator_failure, ("python_type_checks",))
    checks["array_iterator_callback_then_exception_order_and_identity"] = True

    reference_policy = kernel.app_reference_policy(provider._SPECIFICATIONS)
    value_policy = kernel.default_value_policy()
    try:
        type(value_policy)()
    except TypeError:
        pass
    else:
        raise AssertionError("opaque value policy has an arbitrary constructor")
    policy_events = []

    class RegistryProbe:
        def resolver_with_root(self, resource):
            policy_events.append("resolver")
            raise AssertionError("invalid policy reached Resource/Registry evaluation")

    class FakePolicy:
        def __getattribute__(self, name):
            policy_events.append(name)
            raise AssertionError("forged policy metadata was inspected")

    def rejected_policy(call):
        before = (kernel.app_schema_entry_count(), kernel.app_schema_frame_count(), counts())
        try:
            call()
        except TypeError:
            pass
        else:
            raise AssertionError("invalid value policy/configuration was accepted")
        assert not policy_events
        assert before == (kernel.app_schema_entry_count(), kernel.app_schema_frame_count(), counts())

    import app_schema_model
    from referencing.jsonschema import DRAFT202012
    specification_type = type(DRAFT202012)
    saved_create = specification_type.__dict__["create_resource"]

    def create_probe(specification, contents):
        policy_events.append("resource")
        return saved_create(specification, contents)

    try:
        specification_type.create_resource = create_probe
        for fake in (object(), FakePolicy(), reference_policy):
            rejected_policy(lambda: kernel.app_check_schema(
                {}, {}, RegistryProbe(), reference_policy, provider.TYPE_CHECKER, None,
                value_policy=fake))
        saved_boundary = app_schema_model.type_boundary
        try:
            app_schema_model.type_boundary = object()
            rejected_policy(kernel.default_value_policy)
            rejected_policy(lambda: kernel.app_check_schema(
                {}, {}, RegistryProbe(), reference_policy, provider.TYPE_CHECKER, None))
        finally:
            app_schema_model.type_boundary = saved_boundary
    finally:
        specification_type.create_resource = saved_create
    checks["opaque_and_invalid_value_policies_refuse_before_evaluation"] = True

    from jsonschema import _keywords
    source_type_name = sys.intern("TYPE_CHECKER")

    def getter_failure(native, stage):
        events, fault = [], RuntimeError("actual type owner getter failure")
        meta, actual = {"type": "integer"}, "bad"
        default = provider.TYPE_CHECKER

        class Owner:
            schema = meta
            _validators = [(_keywords.type, "type", "integer")]

            def __getattribute__(self, name):
                if name == "TYPE_CHECKER":
                    events.append({"source_name": name is source_type_name})
                    if stage == "check" or len(events) == 2:
                        raise fault
                    return default
                return object.__getattribute__(self, name)

            def is_type(self, value, kind):
                return original.is_type(self, value, kind)

        owner = Owner()
        try:
            if native:
                kernel.app_check_schema(actual, meta, provider._REGISTRY, reference_policy,
                                        default, None, value_policy=value_policy, type_owner=owner)
            else:
                next(original.iter_errors(owner, actual))
        except RuntimeError as error:
            assert error is fault
            outcome = failure_graph(error, fault)
        else:
            raise AssertionError("actual type owner getter was not reached")
        assert len(events) == (1 if stage == "check" else 2)
        assert all(event["source_name"] for event in events)
        return {"events": events, "error": outcome}

    for stage in ("check", "decorate"):
        assert first_difference(getter_failure(False, stage), getter_failure(True, stage)) is None
    checks["type_owner_getter_name_order_and_exception_identity"] = True

    def undefined_type():
        actual, events = {}, []
        undefined = exceptions.UndefinedTypeCheck("integer")

        class Checker:
            def is_type(self, value, kind):
                events.append({"same_input": value is actual, "kind": kind})
                raise undefined

        return {"meta": {"type": "integer"}, "actual": actual, "events": events,
                "type_checker": Checker(), "unknown_context": undefined}

    compare(undefined_type, ("python_type_checks",))
    checks["custom_undefined_type_preserves_actual_unknown_type_context"] = True

    class WeakInput(dict):
        pass

    retained = []

    def reentry_failure():
        state, events = {}, []
        actual = WeakInput({"held": []})
        retained.append(weakref.ref(actual))
        fault = RuntimeError("actual reentrant type failure")
        default = original.TYPE_CHECKER

        class Checker:
            def is_type(self, value, kind):
                events.append({"outer_input": value is actual, "kind": kind})
                owner = state["owner"]
                outer_meta, outer_type = owner.META_SCHEMA, owner.TYPE_CHECKER
                try:
                    owner.META_SCHEMA, owner.TYPE_CHECKER = {"type": "string"}, default
                    owner.check_schema("nested", format_checker=None)
                    events.append("nested-completed")
                finally:
                    owner.META_SCHEMA, owner.TYPE_CHECKER = outer_meta, outer_type
                raise fault

        state.update(meta={"type": "object"}, actual=actual, events=events,
                     type_checker=Checker(), fault=fault)
        return state

    reentered = []
    for selected in (original, provider):
        observed, delta = run(selected, reentry_failure)
        gc.collect()
        assert retained[-1]() is None, "failed reentry retained the actual input"
        if selected is provider:
            assert delta["python_type_checks"] > 0 and delta["native_type_checks"] > 0
        reentered.append(observed)
    assert first_difference(*reentered) is None
    checks["reentrant_type_callback_preserves_exception_and_releases_input"] = True
    assert len(checks) == 16 and all(value is True for value in checks.values())
    return checks
