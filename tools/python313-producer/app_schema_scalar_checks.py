"""Differential checks of the independent App scalar constraint family."""


def scalar_checks(provider, kernel) -> dict[str, bool]:
    """Use fresh live values and the original oracle, without JSON cloning."""
    from jsonschema import Draft202012Validator as original, exceptions
    from schema_kernel_observation import first_difference
    checks = {}

    def run(selected, make):
        state = make()
        meta, actual = state["meta"], state["actual"]
        events, known = state.setdefault("events", []), {}

        def index(value, path, active=()):
            known.setdefault(id(value), []).append(path)
            if id(value) in active:
                return
            assert len(active) < 16
            children = (value.items() if type(value) is dict else
                        enumerate(value) if type(value) is list else ())
            for key, child in children:
                index(child, path + "/" + repr(key), (*active, id(value)))

        def describe(value):
            return {"type": type(value).__module__ + "." + type(value).__qualname__,
                    "repr": "".join((repr(value),)),
                    "identity": sorted(set(known.get(id(value), [])))}

        def graph(error, seen=None):
            if error is None:
                return None
            seen = {} if seen is None else seen
            if id(error) in seen:
                return {"ref": seen[id(error)]}
            seen[id(error)] = len(seen)
            return {"type": type(error).__module__ + "." + type(error).__qualname__,
                    "message": str(error), "is_supplied": error is state.get("fault"),
                    "suppressed": error.__suppress_context__,
                    "cause": graph(error.__cause__, seen),
                    "context": graph(error.__context__, seen)}

        def observe(error):
            assert type(error) is exceptions.SchemaError
            assert id(error.schema) in known and id(error.instance) in known
            return {"type": type(error).__name__, "message": error.message,
                    "message_type": type(error.message).__name__,
                    "schema": describe(error.schema), "instance": describe(error.instance),
                    "validator": describe(error.validator),
                    "validator_value": describe(error.validator_value),
                    "path": list(error.path), "schema_path": list(error.schema_path),
                    "cause": graph(error.cause), "exception_cause": graph(error.__cause__),
                    "exception_context": graph(error.__context__)}

        index(meta, "meta")
        index(actual, "actual")
        for label, value in state.get("refs", {}).items():
            index(value, "retained/" + label)
        saved = selected.META_SCHEMA, selected.TYPE_CHECKER
        before = kernel.app_schema_entry_count()
        selected.META_SCHEMA = meta
        if "checker" in state:
            selected.TYPE_CHECKER = state["checker"]
        try:
            try:
                selected.check_schema(actual, format_checker=None)
            except exceptions.SchemaError as error:
                index(meta, "meta-after")
                index(actual, "actual-after")
                outcome = {"kind": "schema-error", "error": observe(error)}
            except Exception as error:
                outcome = {"kind": "exception", "error": graph(error)}
                if "fault" in state:
                    assert error is state["fault"]
            else:
                outcome = {"kind": "valid"}
        finally:
            selected.META_SCHEMA, selected.TYPE_CHECKER = saved
        assert kernel.app_schema_entry_count() - before == (selected is provider)
        if "fault" in state:
            assert outcome["kind"] == "exception"
        assert "unexpected-format" not in events and "hint" not in events
        return {"outcome": outcome, "events": events,
                "actual": describe(actual), "meta": describe(meta)}

    def compare(make):
        difference = first_difference(run(original, make), run(provider, make))
        assert difference is None, difference

    def primitive(keyword, case):
        pairs = [(True, 1), (False, 0), (1, 1.0), (1.0, 1), (None, False),
                 (2 ** 240, 2 ** 240), (2 ** 240 + 1, float(2 ** 240)),
                 (float(2 ** 240), 2 ** 240), (-0.0, 0.0),
                 ("雪", "雪"), ("\ud800", "\ud800"), ("\ud800", "x")]
        if case == "same-nan":
            actual = value = float("nan")
        elif case == "different-nan":
            actual, value = float("nan"), float("nan")
        else:
            actual, value = pairs[case]
        return {"meta": {keyword: value if keyword == "const" else [value]}, "actual": actual}

    for keyword in ("const", "enum"):
        for case in (*range(12), "same-nan", "different-nan"):
            compare(lambda: primitive(keyword, case))
    checks["scalar_equality_bool_numeric_nan_identity_zero_and_unicode"] = True
    for keyword in ("const", "enum"):
        for same_nan in (False, True):
            for mismatch in (False, True):
                def containers():
                    first = float("nan")
                    second = first if same_nan else float("nan")
                    value = {"a": [True, first]}
                    actual = {"a": [1 if mismatch else True, second]}
                    return {"meta": {keyword: value if keyword == "const" else [value]}, "actual": actual}
                compare(containers)
    checks["container_equality_retains_recursive_boolean_semantics"] = True

    def equality(mode, keyword="const"):
        events, fault = [], RuntimeError("actual equality boundary failure")
        candidates = []

        class Result:
            def __bool__(self):
                events.append("truth")
                if mode == "truth-fault":
                    raise fault
                return mode == "short"

        class Candidate:
            def __eq__(self, other):
                events.append({"equal": other == 0})
                if mode == "compare-fault":
                    raise fault
                if mode == "append":
                    candidates.append(0)
                elif mode == "delete":
                    del candidates[1:]
                elif mode == "replace":
                    candidates[1] = 1
                return Result()

            def __repr__(self):
                return "Candidate()"

        class Later:
            def __eq__(self, other):
                events.append("unexpected-later-comparison")
                raise fault

            def __repr__(self):
                return "Later()"

        candidate = Candidate()
        candidates.append(candidate)
        if mode != "append":
            candidates.append(Later() if mode == "short" else 0)
        state = {"meta": {keyword: candidate if keyword == "const" else candidates},
                 "actual": 0, "events": events}
        if mode.endswith("fault"):
            state["fault"] = fault
        return state

    for mode in ("false", "compare-fault", "truth-fault"):
        compare(lambda: equality(mode))
    checks["foreign_equality_result_truth_order_and_exception_identity"] = True
    for mode in ("append", "delete", "replace", "short"):
        compare(lambda: equality(mode, "enum"))
    checks["enum_live_iterator_mutation_and_short_circuit"] = True

    def foreign_iter(keyword, fail=False):
        events, fault = [], RuntimeError("actual scalar iterator failure")

        class Iterator:
            def __init__(self):
                self.index = 0

            def __iter__(self):
                events.append("iterator-iter")
                return self

            def __next__(self):
                events.append({"next": self.index})
                if fail:
                    raise fault
                if self.index == 2:
                    raise StopIteration
                self.index += 1
                return ("string", "integer")[self.index - 1] if keyword == "type" else self.index

            def __length_hint__(self):
                events.append("hint")
                raise AssertionError("scalar genexpr does not request foreign length hints")

        class Values:
            def __iter__(self):
                events.append("values-iter")
                return Iterator()

            def __repr__(self):
                return "Values()"

        state = {"meta": {keyword: Values()}, "actual": None, "events": events}
        if fail:
            state["fault"] = fault
        return state

    for keyword in ("type", "enum"):
        for fail in (False, True):
            compare(lambda: foreign_iter(keyword, fail))
    checks["type_enum_foreign_genexpr_iterator_order_and_failure"] = True

    def type_policy(mode):
        events = []
        kinds = ["string"] if mode == "append" else ["string", "integer"]

        class Kind(str):
            def __iter__(self):
                raise AssertionError("single str subclass must not be iterated")

        specification = Kind("integer") if mode == "single" else kinds

        class Checker:
            def is_type(self, value, kind):
                events.append({"kind": str(kind), "same_single": kind is specification})
                if len(events) == 1:
                    if mode == "append":
                        kinds.append("integer")
                    elif mode == "delete":
                        del kinds[1:]
                    elif mode == "replace":
                        kinds[1] = "number"
                return kind == "integer"

        return {"meta": {"type": specification}, "actual": 0,
                "events": events, "checker": Checker()}

    for mode in ("single", "multiple", "append", "delete", "replace"):
        compare(lambda: type_policy(mode))
    checks["single_and_live_multiple_type_policy_call_order"] = True

    def spoofed_kind():
        events = []

        class Kind:
            @property
            def __class__(self):
                events.append("kind-class")
                return str

            def __iter__(self):
                raise AssertionError("source isinstance classified this kind as single")

            def __repr__(self):
                return "ForeignKind()"

        kind = Kind()

        class Checker:
            def is_type(self, value, selected):
                events.append({"same_kind": selected is kind})
                return False

        return {"meta": {"type": kind}, "actual": 0, "events": events, "checker": Checker()}

    compare(spoofed_kind)
    checks["foreign_type_specification_keeps_actual_class_protocol"] = True

    def repr_subclass():
        events = []

        class Text(str):
            def __format__(self, spec):
                events.append("unexpected-format")
                raise AssertionError("repr subclass must be normalized by join")

        class Kind(str):
            def __repr__(self):
                return Text("kind-repr")

        class Checker:
            def is_type(self, value, kind):
                events.append("type")
                return False

        return {"meta": {"type": Kind("integer")}, "actual": 0,
                "events": events, "checker": Checker()}

    compare(repr_subclass)
    checks["single_type_failure_normalizes_repr_string_subclass"] = True

    for keyword in ("minItems", "maxItems", "minLength", "maxLength", "minProperties", "maxProperties"):
        for bound in (-1, 0, 1, 2, 2 ** 240, 1.0, True):
            for length in (0, 1, 2):
                def sized():
                    actual = ([0] * length if keyword.endswith("Items") else
                              "\ud800雪"[:length] if keyword.endswith("Length") else
                              {str(index): None for index in range(length)})
                    return {"meta": {keyword: bound}, "actual": actual}
                compare(sized)
    checks["all_length_constraints_counts_unicode_and_boundaries"] = True

    def foreign_length(mode, minimum):
        events, fault = [], RuntimeError("actual length boundary failure")

        class Result:
            def __bool__(self):
                events.append("bound-truth")
                if mode == "truth-fault":
                    raise fault
                return True

        class Bound:
            def compare(self, length):
                events.append({"length-compare": length})
                if mode == "compare-fault":
                    raise fault
                return Result()

            __lt__ = __gt__ = compare

            def __eq__(self, other):
                events.append({"adjective": other})
                if mode == "adjective-fault":
                    raise fault
                return False

            def __repr__(self):
                return "Bound()"

        class Values(list):
            def __len__(self):
                events.append("length")
                if mode == "length-fault":
                    raise fault
                result = list.__len__(self)
                self.append(9)
                return result

        state = {"meta": {"minItems" if minimum else "maxItems": Bound()},
                 "actual": Values([0]), "events": events}
        if mode.endswith("fault"):
            state["fault"] = fault
        return state

    for minimum in (False, True):
        for mode in ("mutate", "length-fault", "compare-fault", "truth-fault", "adjective-fault"):
            compare(lambda: foreign_length(mode, minimum))
    checks["foreign_len_comparison_truth_mutation_and_exception_order"] = True

    def revisit(second_guard=False):
        events = []

        class Values(list):
            def __len__(self):
                events.append("length")
                result = list.__len__(self)
                self.append(1)
                return result

        if not second_guard:
            return {"meta": {"minItems": 0, "maxItems": 0}, "actual": Values(), "events": events}

        class Checker:
            def is_type(self, value, kind):
                events.append(kind)
                return len(events) == 1

        return {"meta": {"maxProperties": 0}, "actual": {}, "events": events, "checker": Checker()}

    compare(revisit)
    compare(lambda: revisit(True))
    checks["length_revisit_and_source_maxproperties_second_type_guard"] = True

    from jsonschema import _keywords, _utils
    make = lambda: {"meta": {"allOf": [{"type": "object"}, {"const": {"x": [True]}}]},
                    "actual": {"x": [True]}}
    expected = run(original, make)
    saved = _keywords.equal, _keywords.ensure_list, _utils.equal

    def unselected(*args, **kwargs):
        raise AssertionError("unselected generic helper became an App algorithm hook")

    try:
        _keywords.equal = _keywords.ensure_list = _utils.equal = unselected
        actual = run(provider, make)
    finally:
        _keywords.equal, _keywords.ensure_list, _utils.equal = saved
    assert first_difference(expected, actual) is None
    checks["removed_generic_helpers_are_not_app_algorithm_hooks"] = True
    assert len(checks) == 12 and all(value is True for value in checks.values())
    return checks
