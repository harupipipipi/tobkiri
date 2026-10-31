"""Differential checks of App-owned loop state over actual Python data."""


def control_checks(provider, kernel) -> dict[str, bool]:
    """Compare original execution order, live bindings, errors, and release."""
    import gc
    import weakref
    from jsonschema import Draft202012Validator as original, FormatChecker, exceptions
    from schema_kernel_observation import first_difference

    checks = {}

    def run(selected, make):
        state = make()
        meta, actual = state["meta"], state["actual"]
        events = state.setdefault("events", [])
        known = {}

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
                    "repr": repr(value), "identity": sorted(set(known.get(id(value), [])))}

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

        def observe(error, root=True):
            assert type(error) is (exceptions.SchemaError if root else exceptions.ValidationError)
            assert id(error.schema) in known and id(error.instance) in known
            return {"type": type(error).__name__, "message": error.message,
                    "schema": describe(error.schema), "instance": describe(error.instance),
                    "validator": describe(error.validator),
                    "validator_value": describe(error.validator_value),
                    "path": [describe(part) for part in error.path],
                    "schema_path": [describe(part) for part in error.schema_path],
                    "cause": graph(error.cause), "exception_cause": graph(error.__cause__),
                    "exception_context": graph(error.__context__),
                    "context": [observe(child, False) for child in error.context],
                    "children_reparented": all(child.parent is error for child in error.context)}

        index(meta, "meta")
        index(actual, "actual")
        for label, value in state.get("refs", {}).items():
            index(value, "retained/" + label)
        saved = selected.META_SCHEMA
        before = kernel.app_schema_entry_count()
        selected.META_SCHEMA = meta
        try:
            try:
                selected.check_schema(actual, format_checker=state.get("checker"))
            except exceptions.SchemaError as error:
                index(meta, "meta-after")
                index(actual, "actual-after")
                outcome = {"kind": "schema-error", "error": observe(error)}
                if "error_schema" in state:
                    assert error.schema is state["error_schema"]
                if "error_instance" in state:
                    assert error.instance is state["error_instance"]
                if state.get("shared_index"):
                    assert error.path[0] is error.schema_path[1]
                    assert error.path[0] == 300
            except Exception as error:
                outcome = {"kind": "exception", "error": graph(error)}
                if "fault" in state:
                    assert error is state["fault"]
            else:
                outcome = {"kind": "valid"}
        finally:
            selected.META_SCHEMA = saved
        assert kernel.app_schema_entry_count() - before == (selected is provider)
        if "fault" in state:
            assert outcome["kind"] == "exception"
        if state.get("forbid_hint"):
            assert not any(event == "hint" for event in events)
        if state.get("early_abort"):
            assert outcome["kind"] == "schema-error" and "later" not in events
        return {"outcome": outcome, "events": events,
                "meta": describe(meta), "actual": describe(actual)}

    def compare(make):
        difference = first_difference(run(original, make), run(provider, make))
        assert difference is None, difference

    def with_checker(meta, actual, change, events, **extra):
        checker = FormatChecker(formats=[])
        checker.checks("change")(change)
        return {"meta": meta, "actual": actual, "events": events, "checker": checker, **extra}

    def root_rows(delete):
        events, meta = [], {"format": "change", "type": "string", 7: None, "\ud800": None}

        def change(value):
            events.append({"bound": value == 0})
            if delete:
                del meta["type"]
            else:
                meta["type"] = "integer"
            return True

        return with_checker(meta, 0, change, events, error_schema=meta)

    for delete in (False, True):
        compare(lambda: root_rows(delete))
    checks["root_rows_freeze_values_and_ignore_unknown_keys"] = True

    def items_mutation(mode):
        events, actual = [], [0, 1, 2]

        def change(value):
            events.append(value)
            if len(events) == 1:
                if mode == "append":
                    actual.append(3)
                else:
                    del actual[1]
            return True

        return with_checker({"items": {"format": "change"}}, actual, change, events)

    for mode in ("append", "delete"):
        compare(lambda: items_mutation(mode))
    checks["items_range_keeps_original_length_during_append_and_delete"] = True

    def bound_instance():
        old, new, events = {}, {"new": True}, []
        actual = [old]

        def change(value):
            events.append({"old": value is old})
            actual[0] = new
            return True

        return with_checker({"items": {"format": "change", "type": "integer"}}, actual,
                            change, events, refs={"old": old, "new": new}, error_instance=old)

    compare(bound_instance)
    checks["already_bound_item_survives_parent_replacement"] = True

    def allof_mutation(mode):
        events = []
        first, later = {"format": "change", "type": "string"}, {"type": "string"}
        rows = [first, later]

        def change(value):
            events.append({"first": value == "ok"})
            if mode == "append":
                rows.append({"format": "visit"})
            elif mode == "delete":
                del rows[1]
            elif mode == "replace-next":
                rows[1] = {"type": "integer"}
            elif mode == "replace-bound":
                rows[0] = {"type": "integer"}
            else:
                first["type"] = "integer"
            return True

        state = with_checker({"allOf": rows}, "ok", change, events,
                             refs={"first": first, "later": later})
        state["checker"].checks("visit")(lambda value: events.append("appended") or True)
        return state

    for mode in ("append", "delete", "replace-next", "replace-bound", "same-child"):
        compare(lambda: allof_mutation(mode))
    checks["allof_live_iterator_append_delete_and_binding_mutations"] = True

    def dictionary_mutation(mode):
        events, actual = [], {"trigger": 0, "later": 2}
        properties = {"trigger": {"format": "change"}, "later": {"type": "integer"}}
        meta = {"properties": properties}
        if mode == "input-keys":
            meta = {"propertyNames": {"format": "change"}}

        def change(value):
            events.append({"visit": repr(value)})
            if mode == "add":
                properties["added"] = True
            elif mode == "delete":
                del properties["later"]
            else:
                actual.pop("later", None)
            return True

        return with_checker(meta, actual, change, events)

    for mode in ("add", "delete", "input-keys"):
        compare(lambda: dictionary_mutation(mode))
    checks["live_dictionary_key_changes_keep_actual_iterator_exceptions"] = True

    def prefix_order(left_size, right_size):
        events = []

        class Iterator:
            def __init__(self, name, values):
                self.name, self.values, self.index = name, values, 0

            def __iter__(self):
                return self

            def __next__(self):
                events.append({"next": self.name, "index": self.index})
                if self.index == len(self.values):
                    raise StopIteration
                value = self.values[self.index]
                self.index += 1
                return value

            def __length_hint__(self):
                events.append("hint")
                raise AssertionError("enumerate/zip must not request a length hint")

        class Values(list):
            def __iter__(self):
                events.append({"iter": "left"})
                return Iterator("left", list.copy(self))

        class Schemas(list):
            def __iter__(self):
                events.append({"iter": "right"})
                return Iterator("right", list.copy(self))

        actual = Values([0] * left_size)
        meta = {"prefixItems": Schemas([{"format": "change"} for _ in range(right_size)])}
        return with_checker(meta, actual, lambda value: events.append("child") or True,
                            events, forbid_hint=True)

    for left_size, right_size in ((3, 1), (1, 3), (0, 1), (1, 0)):
        compare(lambda: prefix_order(left_size, right_size))
    checks["prefix_zip_short_side_and_iterator_creation_order"] = True

    def foreign_root(failure):
        events, fault = [], RuntimeError("actual foreign pair failure")

        class Pair:
            def __init__(self, key, value):
                self.key, self.value = key, value

            def __iter__(self):
                events.append({"unpack": self.key})
                if failure == "unpack":
                    raise fault
                yield self.key
                yield self.value
                events.append("pair-exhausted")

        class Pairs:
            def __init__(self):
                self.index = 0

            def __iter__(self):
                events.append("pairs-iter")
                return self

            def __next__(self):
                events.append({"pairs-next": self.index})
                if self.index == 1:
                    if failure == "next":
                        raise fault
                    raise StopIteration
                self.index += 1
                return Pair("type", "integer")

            def __length_hint__(self):
                events.append("hint")
                raise AssertionError("root comprehension must not request length hints")

        class Meta(dict):
            def items(self):
                events.append("items")
                return Pairs()

        state = {"meta": Meta(type="integer"), "actual": "bad", "events": events,
                 "forbid_hint": True, "refs": {"type-value": "integer"}}
        if failure:
            state["fault"] = fault
        return state

    compare(lambda: foreign_root(None))
    checks["foreign_root_collects_pairs_without_length_hint"] = True
    for failure in ("next", "unpack"):
        compare(lambda: foreign_root(failure))
    checks["foreign_root_next_and_unpack_exception_order_and_identity"] = True

    def foreign_key():
        events = []

        class Key:
            def __hash__(self):
                events.append("hash")
                return hash("type")

            def __eq__(self, other):
                events.append({"equal": other == "type"})
                return other == "type"

            def __repr__(self):
                return "Key(type)"

        key = Key()
        return {"meta": {key: "integer"}, "actual": "bad", "events": events,
                "refs": {"key": key}}

    compare(foreign_key)
    checks["foreign_keyword_selection_preserves_hash_and_actual_key"] = True
    compare(lambda: {"meta": {"prefixItems": [True] * 300 + [{"type": "integer"}]},
                     "actual": [None] * 300 + ["bad"], "shared_index": True})
    checks["prefix_index_identity_above_small_integer_cache"] = True

    def foreign_items(fail):
        events, fault = [], RuntimeError("actual array length failure")

        class Values(list):
            def __len__(self):
                events.append("length")
                if fail:
                    raise fault
                return list.__len__(self)

            def __getitem__(self, index):
                events.append({"get": index})
                if index == 0:
                    self.append(9)
                return list.__getitem__(self, index)

        state = {"meta": {"items": True}, "actual": Values([0, 1, 2]), "events": events}
        if fail:
            state["fault"] = fault
        return state

    for fail in (False, True):
        compare(lambda: foreign_items(fail))
    checks["foreign_items_length_once_and_exception_identity"] = True

    retained = []

    def early_abort():
        class Held(dict):
            pass

        actual, later, events = Held(held=[]), Held(format="change"), []
        retained.extend((weakref.ref(actual), weakref.ref(later)))
        return with_checker({"allOf": [False, later]}, actual,
                            lambda value: events.append("later") or True, events, early_abort=True)

    outcomes = []
    for selected in (original, provider):
        outcomes.append(run(selected, early_abort))
        gc.collect()
        assert all(reference() is None for reference in retained[-2:])
    assert first_difference(*outcomes) is None
    checks["early_abort_releases_actual_input_and_pending_schema_handles"] = True
    assert len(checks) == 12 and all(value is True for value in checks.values())
    return checks
