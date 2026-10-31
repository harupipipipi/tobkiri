"""Differential checks of the App-owned object keyword cursors."""


def object_checks(provider, kernel) -> dict[str, bool]:
    """Compare fresh actual objects, callbacks and errors with the real oracle."""
    from itertools import permutations
    import re
    from jsonschema import Draft202012Validator as original, FormatChecker, exceptions
    from schema_kernel_observation import first_difference

    checks = {}
    base_search, base_checker = re.search, original.TYPE_CHECKER

    def run(selected, make):
        state = make()
        meta, actual = state["meta"], state["actual"]
        events, known = state.setdefault("events", []), {}

        def index(value, path, active=()):
            known.setdefault(id(value), []).append(path)
            if id(value) in active:
                return
            assert len(active) < 20
            if type(value) is dict:
                for position, (key, child) in enumerate(value.items()):
                    index(key, path + f"/{position}.key", (*active, id(value)))
                    index(child, path + f"/{position}.value", (*active, id(value)))
            elif type(value) is list:
                for position, child in enumerate(value):
                    index(child, path + f"/{position}", (*active, id(value)))

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
                    "message": str(error), "supplied": error is state.get("fault"),
                    "suppressed": error.__suppress_context__,
                    "cause": graph(error.__cause__, seen),
                    "context": graph(error.__context__, seen)}

        def observe(error):
            assert type(error) in (exceptions.SchemaError, exceptions.ValidationError)
            assert id(error.schema) in known and id(error.instance) in known
            return {"type": type(error).__name__, "message": error.message,
                    "message_type": type(error.message).__name__,
                    "schema": describe(error.schema), "instance": describe(error.instance),
                    "validator": describe(error.validator),
                    "validator_value": describe(error.validator_value),
                    "path": [describe(item) for item in error.path],
                    "schema_path": [describe(item) for item in error.schema_path],
                    "cause": graph(error.cause), "exception": graph(error),
                    "context": [observe(child) for child in error.context],
                    "parents_are_actual": all(child.parent is error for child in error.context)}

        index(meta, "meta")
        index(actual, "actual")
        for name, value in state.get("refs", {}).items():
            index(value, "retained/" + name)
        saved = selected.META_SCHEMA, selected.TYPE_CHECKER, re.search
        selected.META_SCHEMA = meta
        if "type_checker" in state:
            selected.TYPE_CHECKER = state["type_checker"]
        if "search" in state:
            re.search = state["search"]
        before = kernel.app_schema_entry_count()
        try:
            if "install" in state:
                state["install"]()
            try:
                selected.check_schema(actual, format_checker=state.get("checker"))
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
            selected.META_SCHEMA, selected.TYPE_CHECKER, re.search = saved
        assert kernel.app_schema_entry_count() - before == (selected is provider)
        if "fault" in state:
            assert outcome["kind"] == "exception"
        assert "unexpected-length-hint" not in events
        return {"outcome": outcome, "events": events,
                "meta": describe(meta), "actual": describe(actual)}

    def compare(make):
        difference = first_difference(run(original, make), run(provider, make))
        assert difference is None, difference

    def ordinary(order, value):
        rules = {"properties": {"x": {"type": "integer"}},
                 "patternProperties": {"^x": {"minimum": 1}, "x$": {"maximum": 2}},
                 "additionalProperties": False}
        return {"meta": {name: rules[name] for name in order}, "actual": {"x": value, "z": 3}}

    for order in permutations(("properties", "patternProperties", "additionalProperties")):
        for value in (0, 3, "bad"):
            compare(lambda: ordinary(order, value))
    checks["three_object_keywords_preserve_independent_keyword_and_error_order"] = True

    def overlap():
        events, checker = [], FormatChecker(formats=[])
        for label in ("property", "left", "right", "extra"):
            def mark(value, label=label):
                events.append({"branch": label, "value": value})
                return True
            checker.checks(label)(mark)
        return {"meta": {"properties": {"x": {"format": "property"}},
                         "patternProperties": {"^x": {"format": "left"}, "x$": {"format": "right"}},
                         "additionalProperties": {"format": "extra"}},
                "actual": {"x": 1, "z": 2}, "events": events, "checker": checker}

    compare(overlap)
    checks["overlapping_properties_patterns_and_extras_all_run"] = True

    for patterns, actual in (({"(?i)a": {}, "b": {}}, {"B": 0}),
                             ({"a": {}, "(?i)b": {}}, {"B": 0}),
                             ({"[": {}}, {"x": 0}),
                             ({"^x": {}}, {"y": 0, "z": 1})):
        compare(lambda: {"meta": {"additionalProperties": False, "patternProperties": dict(patterns)},
                         "actual": dict(actual)})
    checks["additional_properties_uses_one_combined_regex_and_current_error_patterns"] = True

    def key_mutation(keyword, target, remove):
        events, checker = [], FormatChecker(formats=[])
        actual = {"x": 0, "y": 1}
        mapping = {"x" if keyword == "properties" else "^x": {"format": "mutate"},
                   "y" if keyword == "properties" else "^y": {}}

        def mutate(value):
            events.append({"mutation": target, "remove": remove, "value": value})
            changed = mapping if target == "schema" else actual
            if remove:
                del changed[next(reversed(changed))]
            else:
                changed["new"] = {} if target == "schema" else 2
            return True

        checker.checks("mutate")(mutate)
        return {"meta": {keyword: mapping}, "actual": actual, "checker": checker, "events": events}

    for keyword in ("properties", "patternProperties"):
        for target in ("schema", "instance"):
            for remove in (False, True):
                compare(lambda: key_mutation(keyword, target, remove))
    checks["live_property_pattern_and_instance_dict_iterators_observe_key_changes"] = True

    def binding(keyword, mode):
        events, checker = [], FormatChecker(formats=[])
        actual = {"x": 0}
        child = {"format": "mutate", "type": "integer"}
        key = "x" if keyword == "properties" else "^x"
        mapping = {key: child}

        def mutate(value):
            events.append({"mutate": mode, "old_value": value == 0})
            if mode == "child":
                mapping[key] = {"type": "string"}
            elif mode == "instance":
                actual["x"] = "new"
            else:
                child["type"] = "string"
            return True

        checker.checks("mutate")(mutate)
        return {"meta": {keyword: mapping}, "actual": actual, "checker": checker,
                "events": events, "refs": {"child": child, "old_instance": actual["x"]}}

    for keyword in ("properties", "patternProperties"):
        for mode in ("child", "instance", "same-child"):
            compare(lambda: binding(keyword, mode))
    checks["bound_property_and_pattern_children_survive_parent_replacement_and_read_live_children"] = True

    def regex_binding(mode):
        events = []
        child = {"type": "integer"}
        patterns, actual = {"^x": child}, {"x": 0}

        def search(pattern, key):
            events.append({"search": pattern, "key": key, "mode": mode})
            if mode == "child":
                patterns[pattern] = {"type": "string"}
            elif mode == "instance":
                actual[key] = "new"
            else:
                child["type"] = "string"
            return base_search(pattern, key)

        return {"meta": {"patternProperties": patterns}, "actual": actual, "search": search,
                "events": events, "refs": {"bound_child": child, "old_instance": actual["x"]}}

    for mode in ("child", "instance", "same-child"):
        compare(lambda: regex_binding(mode))
    checks["pattern_regex_callback_retains_prebound_value_and_subschema"] = True

    def regex_boundary(mode):
        events, fault = [], RuntimeError("actual regex operation failure")

        class Result:
            def __bool__(self):
                events.append("regex-truth")
                if mode == "truth-fault":
                    raise fault
                return True

        def second(pattern, key):
            events.append({"search": "second", "key": key})
            return Result()

        def first(pattern, key):
            events.append({"search": "first", "key": key})
            if mode == "search-fault":
                raise fault
            re.search = second
            return Result()

        state = {"meta": {"patternProperties": {".": {}}}, "actual": {"x": 0, "y": 1},
                 "search": first, "events": events}
        if mode.endswith("fault"):
            state["fault"] = fault
        return state

    for mode in ("replace", "search-fault", "truth-fault"):
        compare(lambda: regex_boundary(mode))
    checks["regex_callback_lookup_truth_and_supplied_exception_identity"] = True

    def foreign_key(mode):
        events, fault, enabled = [], RuntimeError("actual foreign key hash failure"), [False]

        class Key(str):
            def __hash__(self):
                events.append("key-hash")
                if enabled[0] and mode == "hash-fault":
                    raise fault
                return str.__hash__(self)

            def __eq__(self, other):
                events.append({"key-equal": repr(other)})
                return str.__eq__(self, other)

        key = Key("x")
        meta = ({"properties": {"x": {"type": "integer"}}} if mode == "property" else
                {"additionalProperties": False})
        state = {"meta": meta, "actual": {key: 0}, "events": events,
                 "install": lambda: enabled.__setitem__(0, True)}
        if mode == "hash-fault":
            state["fault"] = fault
        return state

    for mode in ("property", "additional", "hash-fault"):
        compare(lambda: foreign_key(mode))
    compare(lambda: {"meta": {"patternProperties": {re.compile("^x"): {"type": "integer"}}},
                     "actual": {"x": "bad"}})
    compare(lambda: {"meta": {"patternProperties": {".": {}}}, "actual": {3: 0}})
    checks["foreign_keys_hashing_compiled_patterns_and_genuine_regex_type_errors"] = True

    def foreign_protocol(mode):
        events, fault = [], RuntimeError("actual object protocol failure")

        class Values(dict):
            def items(self):
                events.append("instance-items")
                if mode == "items-fault":
                    raise fault
                return dict.items(self)

            def __contains__(self, key):
                events.append({"contains": key})
                if mode == "contains-fault":
                    raise fault
                return dict.__contains__(self, key)

            def __getitem__(self, key):
                events.append({"getitem": key})
                if mode == "getitem-fault":
                    raise fault
                return dict.__getitem__(self, key)

        actual = Values(x=0)
        keyword = "patternProperties" if mode in ("items", "items-fault") else "properties"
        mapping = {"." if keyword == "patternProperties" else "x": {}}
        if mode == "items":
            mapping["^x"] = {}
        state = {"meta": {keyword: mapping}, "actual": actual, "events": events, "refs": {"child": 0}}
        if mode.endswith("fault"):
            state["fault"] = fault
        return state

    for mode in ("items", "items-fault", "contains-fault", "getitem-fault"):
        compare(lambda: foreign_protocol(mode))
    checks["foreign_instance_items_membership_and_getitem_are_actual_operations"] = True

    def foreign_items(keyword, fail):
        events, fault = [], RuntimeError("actual foreign item iterator failure")
        pairs = [("x" if keyword == "properties" else "^x", {}),
                 ("y" if keyword == "properties" else "^y", {})]

        class Pair:
            def __init__(self, pair):
                self.pair = pair

            def __iter__(self):
                events.append({"unpack": self.pair[0]})
                return iter(self.pair)

        class Iterator:
            def __init__(self):
                self.position = 0

            def __iter__(self):
                events.append("items-iter")
                return self

            def __next__(self):
                events.append({"items-next": self.position})
                if fail:
                    raise fault
                if self.position == len(pairs):
                    raise StopIteration
                self.position += 1
                return Pair(pairs[self.position - 1])

            def __length_hint__(self):
                events.append("unexpected-length-hint")
                raise AssertionError("ordinary object loops do not collect foreign items")

        class Mapping(dict):
            def items(self):
                events.append("items")
                return Iterator()

        state = {"meta": {keyword: Mapping(pairs)}, "actual": {"x": 0, "y": 1}, "events": events}
        if fail:
            state["fault"] = fault
        return state

    for keyword in ("properties", "patternProperties"):
        for fail in (False, True):
            compare(lambda: foreign_items(keyword, fail))
    checks["foreign_property_items_preserve_iteration_unpack_and_exceptions_without_collection"] = True

    def additional_timing(mode):
        events = []
        ap = False if mode == "message" else {"type": "integer"}
        actual = {"x": "bad"}
        meta = {"additionalProperties": ap, "properties": {}, "patternProperties": {}}

        class Checker:
            def is_type(self, value, kind):
                events.append({"type": kind, "is_ap": value is ap})
                if value is ap and kind == "object":
                    if mode == "message":
                        meta["patternProperties"] = {"late-pattern": {}}
                    elif mode == "properties":
                        meta["properties"]["x"] = {}
                    elif mode == "replace":
                        actual["x"] = 0
                    else:
                        actual["y"] = "new"
                return base_checker.is_type(value, kind)

        return {"meta": meta, "actual": actual, "events": events, "type_checker": Checker()}

    for mode in ("message", "properties", "replace", "append"):
        compare(lambda: additional_timing(mode))
    checks["extras_finish_before_ap_type_callback_and_error_patterns_are_read_after_it"] = True

    def bound_classification():
        events, actual = [], {"x": 0, "y": 1}
        meta = {"additionalProperties": False, "properties": {}, "patternProperties": {"^x": {}}}

        def search(pattern, key):
            events.append({"combined": pattern, "key": key})
            meta["properties"] = {"y": {}}
            meta["patternProperties"] = {".*": {}}
            return base_search(pattern, key)

        return {"meta": meta, "actual": actual, "search": search, "events": events}

    compare(bound_classification)

    def classification_mutation(remove):
        events, actual = [], {"x": 0, "y": 1}

        def search(pattern, key):
            events.append({"key": key, "remove": remove})
            if remove:
                del actual["y"]
            else:
                actual["z"] = 2
            return base_search(pattern, key)

        return {"meta": {"additionalProperties": False, "patternProperties": {"^x": {}}},
                "actual": actual, "search": search, "events": events}

    for remove in (False, True):
        compare(lambda: classification_mutation(remove))
    checks["additional_classification_keeps_bound_properties_and_joined_pattern"] = True

    def contexts():
        return {"meta": {"anyOf": [
                    {"properties": {"x": {"type": "integer"}, "y": {"type": "string"}}},
                    {"patternProperties": {".": {"type": "null"}, "^x": {"type": "string"}}},
                    {"additionalProperties": {"type": "null"}}]},
                "actual": {"x": True, "y": 3}}

    compare(contexts)
    checks["object_child_errors_keep_full_context_order_paths_and_actual_references"] = True

    def set_order():
        events, checker = [], FormatChecker(formats=[])

        class Values(dict):
            def __iter__(self):
                events.append("instance-iter")
                return iter(("b", "a", "b"))

        def mark(value):
            events.append({"extra-value": value})
            return True

        checker.checks("extra")(mark)
        return {"meta": {"additionalProperties": {"format": "extra"}},
                "actual": Values(a=0, b=1), "events": events, "checker": checker}

    compare(set_order)
    checks["extras_use_real_python_set_deduplication_and_iteration_order"] = True

    def duplicate_key_lifetime():
        import weakref
        events, references = [], []

        class Key:
            def __hash__(self):
                return 7

            def __eq__(self, other):
                return type(other) is Key

        class Iterator:
            def __init__(self):
                self.position = 0

            def __iter__(self):
                return self

            def __next__(self):
                events.append({"next": self.position,
                               "alive": [reference() is not None for reference in references]})
                if self.position == 3:
                    raise StopIteration
                self.position += 1
                key = Key()
                references.append(weakref.ref(key))
                return key

        class Values(dict):
            def __iter__(self):
                return Iterator()

        class Checker:
            def is_type(self, value, kind):
                if value is True and kind == "object":
                    events.append({"ap-alive": [reference() is not None for reference in references]})
                return base_checker.is_type(value, kind)

        return {"meta": {"additionalProperties": True}, "actual": Values(),
                "events": events, "type_checker": Checker()}

    compare(duplicate_key_lifetime)
    checks["deduplicated_foreign_property_lifetime_spans_next_but_ends_before_ap_policy"] = True

    from jsonschema import _keywords, _utils
    make = lambda: {"meta": {"additionalProperties": False}, "actual": {"extra": 0}}
    expected = run(original, make)
    saved = (_keywords.find_additional_properties, _keywords.extras_msg,
             _utils.find_additional_properties, _utils.extras_msg)

    def unselected(*args, **kwargs):
        raise AssertionError("generic object helper became an App algorithm hook")

    try:
        _keywords.find_additional_properties = _keywords.extras_msg = unselected
        _utils.find_additional_properties = _utils.extras_msg = unselected
        actual = run(provider, make)
    finally:
        (_keywords.find_additional_properties, _keywords.extras_msg,
         _utils.find_additional_properties, _utils.extras_msg) = saved
    assert first_difference(expected, actual) is None
    checks["generic_additional_property_helpers_are_not_app_algorithm_hooks"] = True
    assert len(checks) == 16 and all(value is True for value in checks.values())
    return checks
