"""Bounded differential observations of the actual default reference policy."""


def reference_state_checks(provider, kernel) -> dict[str, bool]:
    """Compare live reference state with the unmodified original validator.

    All retrieval stays inside a controlled callback on the selected lane's
    actual registry. These observations establish no network or source rights.
    """
    import gc
    import weakref

    from jsonschema import Draft202012Validator as original, FormatChecker, exceptions
    from jsonschema import validators
    from referencing import Resource
    from referencing.jsonschema import DRAFT202012
    from schema_kernel_observation import first_difference, meta_nodes, observe

    root_uri = "https://refs.invalid/root.json"
    external_uri = "https://refs.invalid/external/value.json"
    original_registry = validators._REMOTE_WARNING_REGISTRY

    def registry_for(selected):
        return original_registry if selected is original else selected._REGISTRY

    saved_lanes = [
        (selected, selected.META_SCHEMA, registry_for(selected),
         registry_for(selected)._retrieve)
        for selected in (original, provider)
    ]
    checks = {}

    class RetrieveRefused(Exception):
        pass

    def graph(error, seen=None):
        if error is None:
            return None
        seen = {} if seen is None else seen
        if id(error) in seen:
            return {"ref": seen[id(error)]}
        index = len(seen)
        seen[id(error)] = index
        return {
            "id": index,
            "type": type(error).__module__ + "." + type(error).__qualname__,
            "message": str(error),
            "suppressed": error.__suppress_context__,
            "cause": graph(error.__cause__, seen),
            "context": graph(error.__context__, seen),
        }

    def index_resource(known, value, uri, path=()):
        if isinstance(value, (dict, list)):
            known.setdefault(id(value), []).append({"uri": uri, "path": list(path)})
            members = value.items() if isinstance(value, dict) else enumerate(value)
            for key, child in members:
                index_resource(known, child, uri, (*path, key))

    def run(selected, make):
        registry = registry_for(selected)
        saved_meta, saved_retrieve = selected.META_SCHEMA, registry._retrieve
        state = make()
        meta, actual = state["meta"], state["actual"]
        events = state.setdefault("events", [])
        returned, requests = [], []
        selected.META_SCHEMA = meta
        known = meta_nodes(selected)

        def retrieve(*args, **kwargs):
            event = {"args": list(args), "kwargs": list(kwargs.items())}
            requests.append(event)
            events.append({"retrieve": event})
            assert len(args) == 1 and not kwargs, "actual retrieve binding changed"
            uri = args[0]
            if state.get("retrieve_no_such_resource"):
                from referencing.exceptions import NoSuchResource
                raise NoSuchResource(ref=uri)
            contents = state.get("external", {})
            if uri not in contents:
                raise RetrieveRefused("controlled reference retrieval refused: " + str(uri))
            resource = Resource.from_contents(
                contents[uri], default_specification=DRAFT202012,
            )
            assert resource.contents is contents[uri]
            returned.append(resource)
            index_resource(known, resource.contents, "retrieved:" + uri)
            return resource

        object.__setattr__(registry, "_retrieve", retrieve)
        saved_pointer = Resource.pointer
        try:
            if "pointer_override" in state:
                Resource.pointer = state["pointer_override"](saved_pointer, events)
            try:
                selected.check_schema(actual, format_checker=state.get("checker"))
            except exceptions.SchemaError as error:
                outcome = {"kind": "schema-error", "error": observe(error, actual, known)}
                if "error_schema" in state:
                    assert error.schema is state["error_schema"]
                    outcome["schema_is_actual_selected_resource"] = True
            except Exception as error:
                outcome = {"kind": "exception", "error": graph(error)}
            else:
                outcome = {"kind": "valid"}
            if state.get("local_only"):
                assert not requests, "a locally crawled META unexpectedly needed retrieval"
            if "expected_requests" in state:
                assert requests == [
                    {"args": [uri], "kwargs": []}
                    for uri in state["expected_requests"]
                ]
                assert len(returned) == len(requests)
            if "error_schema" in state:
                assert outcome["kind"] == "schema-error"
            return {"outcome": outcome, "events": events, "actual": actual}
        finally:
            try:
                object.__setattr__(registry, "_retrieve", saved_retrieve)
            finally:
                try:
                    Resource.pointer = saved_pointer
                finally:
                    selected.META_SCHEMA = saved_meta

    def compare(name, make):
        expected, actual = run(original, make), run(provider, make)
        difference = first_difference(expected, actual)
        assert difference is None, (name, difference)
        checks[name] = True

    def child_id(value, present=True):
        def make():
            child = {"$ref": "#/$defs/expected"}
            if present:
                child["$id"] = value
            return {
                "meta": {
                    "$id": root_uri,
                    "$defs": {"expected": {"type": "integer"}},
                    "properties": {"value": child},
                },
                "actual": {"value": "bad"},
                "local_only": True,
            }
        return make

    try:
        for label, value, present in (
            ("missing", None, False), ("none", None, True), ("empty", "", True),
            ("relative", "child.json", True),
            ("absolute", "https://refs.invalid/child.json", True),
            ("trailing_fragment", "https://refs.invalid/child.json#", True),
            ("integer_zero", 0, True), ("integer", 7, True),
            ("boolean_false", False, True), ("boolean_true", True, True),
        ):
            compare("ordinary_child_id_" + label, child_id(value, present))

        def anchor(dynamic=False):
            def make():
                target = {"$dynamicAnchor" if dynamic else "$anchor": "expected",
                          "type": "integer"}
                return {
                    "meta": {"$id": root_uri, "$defs": {"target": target},
                             "properties": {"value": {
                                 "$dynamicRef" if dynamic else "$ref": "#expected",
                             }}},
                    "actual": {"value": "bad"}, "local_only": True,
                    "error_schema": target,
                }
            return make

        compare("ordinary_anchor", anchor())
        compare("dynamic_anchor", anchor(True))

        def nested_references():
            target = {"type": "integer"}
            return {
                "meta": {"$id": root_uri, "$defs": {
                    "child": {"$id": "nested/child.json", "$defs": {"target": target},
                              "$ref": "#/$defs/target"},
                }, "properties": {"value": {"$ref": "nested/child.json"}}},
                "actual": {"value": "bad"}, "local_only": True,
                "error_schema": target,
            }
        compare("nested_id_and_references", nested_references)

        def mutate_shared_id():
            shared = {"format": "visit", "$ref": "#/$defs/expected"}
            events = []
            checker = FormatChecker(formats=[])
            checker.checks("visit")(lambda value: events.append({"visit": value}) or True)

            def add_id(value):
                events.append({"add_id": value, "was_missing": "$id" not in shared})
                shared["$id"] = "later.json"
                return True

            checker.checks("add-id")(add_id)
            return {
                "meta": {"$id": root_uri, "$defs": {"expected": {"type": "integer"}},
                         "properties": {"first": shared, "between": {"format": "add-id"},
                                        "second": shared}},
                "actual": {"first": 1, "between": 0, "second": "bad"},
                "checker": checker, "events": events,
            }
        compare("shared_schema_reads_new_id_on_second_visit", mutate_shared_id)

        def external_resource():
            contents = {"type": "integer"}
            return {
                "meta": {"$id": root_uri,
                         "properties": {"value": {"$ref": external_uri}}},
                "actual": {"value": "bad"}, "external": {external_uri: contents},
                "expected_requests": [external_uri], "error_schema": contents,
            }
        compare("retrieve_uses_actual_returned_resource", external_resource)

        def restored_scope():
            expected = {"type": "integer"}
            return {
                "meta": {"$id": root_uri, "$defs": {"expected": expected},
                         "allOf": [{"$ref": external_uri}, {"$ref": "#/$defs/expected"}]},
                "actual": "bad",
                "external": {external_uri: {
                    "$id": external_uri, "type": "string",
                    "$defs": {"expected": {"type": "string"}},
                }},
                "expected_requests": [external_uri], "error_schema": expected,
            }
        compare("external_reference_restores_parent_scope", restored_scope)

        def pointer_reference(fragment):
            def make():
                target = {"type": "integer"}
                return {
                    "meta": {"$id": root_uri,
                             "$defs": {"a/b~c": target, "array": [target]},
                             "$ref": fragment},
                    "actual": "bad", "local_only": True,
                }
            return make

        for label, fragment in (
            ("escaped", "#/$defs/a~1b~0c"),
            ("array_index", "#/$defs/array/0"),
            ("missing", "#/$defs/missing"),
            ("missing_anchor", "#missing"),
            ("invalid_anchor", "#missing/anchor"),
        ):
            compare("reference_lookup_" + label, pointer_reference(fragment))

        def empty_fragment():
            target = {"type": "integer"}
            return {
                "meta": {"$id": root_uri, "$ref": external_uri + "#"},
                "actual": "bad", "external": {external_uri: target},
                "expected_requests": [external_uri], "error_schema": target,
            }
        compare("reference_lookup_empty_fragment", empty_fragment)

        def missing_resource():
            return {"meta": {"$id": root_uri, "$ref": external_uri},
                    "actual": {}, "external": {}}
        compare("reference_lookup_retrieve_failure_chain", missing_resource)

        def no_such_resource():
            state = missing_resource()
            state["retrieve_no_such_resource"] = True
            return state
        compare("reference_lookup_no_such_resource_suppressed_cause", no_such_resource)

        def custom_pointer_resolver():
            from referencing._core import Resolved

            target = {"$ref": "#/$defs/integer"}
            external = {"$defs": {"target": target,
                                  "integer": {"type": "integer"}}}

            class OpaqueResolver:
                def __init__(self, inner, events):
                    self.inner, self.events = inner, events

                def __getattribute__(self, name):
                    if name in ("_base_uri", "_registry", "_previous"):
                        raise AssertionError("custom resolver state was inspected")
                    return object.__getattribute__(self, name)

                def lookup(self, ref):
                    self.events.append({"custom_lookup": ref})
                    return self.inner.lookup(ref)

                def in_subresource(self, resource):
                    self.events.append({"custom_descend": resource.contents})
                    return OpaqueResolver(self.inner.in_subresource(resource), self.events)

            def override_pointer(actual_pointer, events):
                def pointer(self, pointer, resolver):
                    resolved = actual_pointer(self, pointer=pointer, resolver=resolver)
                    if self.contents is not external:
                        return resolved
                    return Resolved(contents=resolved.contents,
                                    resolver=OpaqueResolver(resolved.resolver, events))
                return pointer

            events = []
            return {
                "meta": {"$id": root_uri, "$ref": external_uri + "#/$defs/target"},
                "actual": "bad", "external": {external_uri: external}, "events": events,
                "expected_requests": [external_uri],
                "pointer_override": override_pointer,
                "error_schema": external["$defs"]["integer"],
            }
        compare("pointer_returned_custom_resolver_remains_opaque", custom_pointer_resolver)

        custom_uri = "urn:tobkiri:reference-state-custom-meta"
        mapping, missing = validators._META_SCHEMAS, object()
        saved_selection = mapping.get(custom_uri, missing)
        custom_observations = []
        try:
            for selected in (original, provider):
                saved_meta = selected.META_SCHEMA
                meta = {"$schema": custom_uri, "type": "integer"}
                actual, events = "bad", []
                checker = FormatChecker(formats=[])
                selected.META_SCHEMA = meta
                known = meta_nodes(selected)

                class Selected:
                    FORMAT_CHECKER = checker

                    def __init__(self, *args, **kwargs):
                        events.append({"construct": [len(args), list(kwargs)],
                                       "same_meta": kwargs["schema"] is meta,
                                       "same_checker": kwargs["format_checker"] is checker})
                        self.inner = original(*args, **kwargs)

                    def iter_errors(self, value):
                        events.append({"iter_errors": value is actual})
                        return self.inner.iter_errors(value)

                mapping[custom_uri] = Selected
                before = kernel.app_schema_entry_count()
                try:
                    try:
                        selected.check_schema(actual)
                    except exceptions.SchemaError as error:
                        outcome = observe(error, actual, known)
                    else:
                        raise AssertionError("selected original custom validator accepted the fixture")
                finally:
                    selected.META_SCHEMA = saved_meta
                assert kernel.app_schema_entry_count() == before
                assert len(events) == 2 and events[0]["same_meta"] and events[0]["same_checker"]
                assert events[1]["iter_errors"]
                custom_observations.append({"outcome": outcome, "events": events})
            assert first_difference(*custom_observations) is None
            checks["custom_meta_validator_selected_before_native_evaluation"] = True
        finally:
            if saved_selection is missing:
                del mapping[custom_uri]
            else:
                mapping[custom_uri] = saved_selection

        policy = kernel.default_reference_policy()
        try:
            type(policy)()
        except TypeError:
            pass
        else:
            raise AssertionError("opaque default reference policy has a Python constructor")
        checks["reference_policy_has_no_python_constructor"] = True

        class ArbitraryPolicy:
            def __getattribute__(self, name):
                raise AssertionError("arbitrary reference policy metadata was inspected")

        for fake in (None, object(), ArbitraryPolicy()):
            before = kernel.app_schema_entry_count()
            try:
                kernel.app_check_schema({}, {}, original_registry, fake,
                                        original.TYPE_CHECKER, None)
            except TypeError:
                pass
            else:
                raise AssertionError("arbitrary reference policy crossed the native boundary")
            assert kernel.app_schema_entry_count() == before
        checks["arbitrary_reference_policy_rejected_before_evaluation"] = True

        class InputWeakDict(dict):
            pass

        def released_context(selected):
            saved_meta = selected.META_SCHEMA
            meta = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
            actual = InputWeakDict({"retained": []})
            retained = weakref.ref(actual)
            selected.META_SCHEMA = meta
            known = meta_nodes(selected)
            try:
                try:
                    selected.check_schema(actual, format_checker=None)
                except exceptions.SchemaError as error:
                    assert len(error.context) == 2
                    outcome = observe(error, actual, known)
                else:
                    raise AssertionError("invalid context fixture was accepted")
            finally:
                selected.META_SCHEMA = saved_meta
            del actual
            gc.collect()
            assert retained() is None, "failed reference context retained the original input"
            return outcome

        assert first_difference(released_context(original), released_context(provider)) is None
        checks["failed_context_releases_input_after_exception_collection"] = True
    finally:
        for selected, saved_meta, registry, saved_retrieve in saved_lanes:
            try:
                object.__setattr__(registry, "_retrieve", saved_retrieve)
            finally:
                selected.META_SCHEMA = saved_meta

    assert len(checks) == 29 and all(value is True for value in checks.values())
    return checks
