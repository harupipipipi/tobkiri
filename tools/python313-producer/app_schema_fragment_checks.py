"""Differential checks of explicitly retained builtin META fragment resources."""


def fragment_checks(provider, kernel) -> dict[str, bool]:
    """Use real builtin Resources; neither names nor contents confer ownership."""
    from jsonschema import Draft202012Validator as original, FormatChecker, exceptions
    from jsonschema import validators
    from referencing import Anchor, Registry, Resource
    from referencing.jsonschema import DRAFT202012, DynamicAnchor
    from schema_kernel_observation import first_difference, meta_nodes, observe

    core_uri = "https://json-schema.org/draft/2020-12/meta/core"
    root_uri = "https://fragments.invalid/root.json"
    external_uri = "https://fragments.invalid/external.json"
    child_uri = "https://fragments.invalid/child.json"
    fixture_key = "_tobkiri_fragment_checks"

    def specifications_for(selected):
        if selected is original:
            return validators.SPECIFICATIONS
        return selected._SPECIFICATIONS

    def registry_for(selected):
        if selected is original:
            return validators._REMOTE_WARNING_REGISTRY
        return selected._REGISTRY

    saved_lanes = [
        (selected, selected.META_SCHEMA, registry_for(selected),
         registry_for(selected)._retrieve)
        for selected in (original, provider)
    ]
    saved_pointer = Resource.__dict__["pointer"]
    saved_anchor = Registry.__dict__["anchor"]
    saved_dynamic_name = DynamicAnchor.__dict__["name"]
    saved_helper = DRAFT202012.maybe_in_subresource
    checks = {}
    fields = ("native_pointers", "native_static_anchors", "native_dynamic_anchors",
              "adapter_constructions")

    def counts():
        value = kernel.app_reference_counts()
        assert type(value) is dict and all(
            name in value and type(value[name]) is int and value[name] >= 0 for name in fields
        )
        return {name: value[name] for name in fields}

    def graph(error, seen=None):
        if error is None:
            return None
        seen = {} if seen is None else seen
        if id(error) in seen:
            return {"ref": seen[id(error)]}
        index = len(seen)
        seen[id(error)] = index
        return {"id": index, "type": type(error).__module__ + "." + type(error).__qualname__,
                "message": str(error), "suppressed": error.__suppress_context__,
                "cause": graph(error.__cause__, seen), "context": graph(error.__context__, seen)}

    def index_nodes(known, value, uri, path=()):
        if isinstance(value, (dict, list)):
            known.setdefault(id(value), []).append({"uri": uri, "path": list(path)})
            children = value.items() if isinstance(value, dict) else enumerate(value)
            for key, child in children:
                index_nodes(known, child, uri, (*path, key))

    def reference(suffix):
        return core_uri + "#/$defs/" + fixture_key + suffix

    def pointer_wrapper(actual_pointer, state):
        def pointer(receiver, *args, **kwargs):
            expected = state.get("external_resource", state["owned_resource"])
            event = {
                "pointer": list(args[:1]) + ([kwargs["pointer"]] if "pointer" in kwargs else []),
                "arity": len(args), "keywords": list(kwargs),
                "same_resource": receiver is expected,
            }
            if "pointer_base_uri" in state:
                resolver = kwargs["resolver"]
                event["base_uri"] = resolver._base_uri
                event["registry_retains_returned_resource"] = (
                    resolver._registry[external_uri] is receiver)
                assert event["base_uri"] == state["pointer_base_uri"]
                assert event["registry_retains_returned_resource"]
            state["events"].append(event)
            assert receiver is expected
            return actual_pointer(receiver, *args, **kwargs)
        return pointer

    def run(selected, make):
        specifications = specifications_for(selected)
        resource = specifications[core_uri]
        assert type(resource) is Resource and resource._specification is DRAFT202012
        definitions = resource.contents["$defs"]
        assert type(definitions) is dict and fixture_key not in definitions
        dynamic = specifications._anchors[(core_uri, "meta")]
        assert type(dynamic) is DynamicAnchor
        assert type(dynamic.resource) is Resource
        assert dynamic.resource._specification is DRAFT202012
        registry = registry_for(selected)
        assert registry[core_uri] is resource
        saved_meta, saved_retrieve = selected.META_SCHEMA, registry._retrieve
        fixture = {"integer": {"type": "integer"}, "string": {"type": "string"},
                   "a/b~c": {"type": "integer"}, "array": [{"type": "integer"}],
                   "changing": {"type": "string"},
                   "opaque": {"$ref": core_uri + "#/$defs/anchorString"}}
        assert fixture_key not in definitions
        definitions[fixture_key] = fixture
        try:
            state = make(fixture, resource, specifications)
            state["owned_resource"] = resource
            state.setdefault("events", [])
            meta, actual, events = state["meta"], state["actual"], state["events"]
            selected.META_SCHEMA = meta
            known = meta_nodes(selected)
            requests = []

            def retrieve(*args, **kwargs):
                events.append({"retrieve": list(args), "keywords": list(kwargs)})
                assert len(args) == 1 and not kwargs
                uri = args[0]
                requests.append(uri)
                if "retrieve_resource" in state:
                    returned = state["retrieve_resource"](uri)
                else:
                    assert uri == external_uri and "external" in state, "unexpected META retrieval"
                    returned = Resource.from_contents(
                        state["external"], default_specification=DRAFT202012)
                    assert returned.contents is state["external"]
                assert type(returned) is Resource
                state["external_resource"] = returned
                index_nodes(known, returned.contents, "retrieved:" + uri)
                return returned

            object.__setattr__(registry, "_retrieve", retrieve)
            if "install" in state:
                state["install"](state)
            before = counts()
            try:
                selected.check_schema(actual, format_checker=state.get("checker"))
            except exceptions.SchemaError as error:
                outcome = {"kind": "schema-error", "error": observe(error, actual, known)}
                if "error_schema" in state:
                    assert error.schema is state["error_schema"]
                    outcome["schema_is_actual_resource_child"] = True
            except Exception as error:
                outcome = {"kind": "exception", "error": graph(error)}
                if "fault" in state:
                    assert error is state["fault"]
                    outcome["is_supplied_exception"] = True
            else:
                outcome = {"kind": "valid"}
            after = counts()
            delta = {name: after[name] - before[name] for name in fields}
            assert all(n >= 0 for n in delta.values())
            if selected is original:
                assert not any(delta.values()), "original oracle entered native fragments"
            if "error_schema" in state:
                assert outcome["kind"] == "schema-error"
            if "fault" in state:
                assert outcome["kind"] == "exception" and outcome["is_supplied_exception"]
            if "expected_requests" in state:
                assert requests == state["expected_requests"], requests
            if state.get("pointer_calls") is not None:
                assert sum("pointer" in event for event in events) == state["pointer_calls"]
            if state.get("descriptor"):
                assert events == [{"descriptor_get": True, "same_resource": True, "same_owner": True}]
            if state.get("opaque"):
                assert any("opaque_lookup" in event for event in events)
                assert any("helper" in event for event in events)
            if "dynamic_order" in state:
                positions = [index for index, event in enumerate(events) if "name_get" in event]
                assert len(positions) == 1, events
                position = positions[0]
                assert position > 0 and "anchor_bind" in events[position - 1], events
                assert events[position]["same_anchor"] and events[position]["same_owner"]
                assert not any("replacement_call" in event for event in events), events
                if state["dynamic_order"] == "raise":
                    assert position + 1 == len(events), events
                else:
                    assert events[position + 1]["anchor_call"] == events[position - 1]["anchor_bind"]
            return {"outcome": outcome, "events": events, "actual": actual}, delta
        finally:
            try:
                Registry.anchor = saved_anchor
            finally:
                try:
                    DynamicAnchor.name = saved_dynamic_name
                finally:
                    try:
                        Resource.pointer = saved_pointer
                    finally:
                        try:
                            object.__setattr__(DRAFT202012, "maybe_in_subresource", saved_helper)
                        finally:
                            try:
                                object.__setattr__(registry, "_retrieve", saved_retrieve)
                            finally:
                                try:
                                    selected.META_SCHEMA = saved_meta
                                finally:
                                    del definitions[fixture_key]

    def compare(make, positive=None, zero=False):
        expected, _ = run(original, make)
        actual, delta = run(provider, make)
        difference = first_difference(expected, actual)
        assert difference is None, difference
        if positive is not None:
            assert delta[positive] > 0, (positive, delta)
        if zero:
            assert not any(delta[name] for name in fields[:3]), delta
        return delta

    def basic(suffix, actual, target=None):
        def make(fixture, resource, specifications):
            state = {"meta": {"$id": root_uri, "$ref": reference(suffix)}, "actual": actual}
            if target is not None:
                state["error_schema"] = target(fixture)
            return state
        return make

    try:
        compare(basic("/integer", 1), "native_pointers")
        compare(basic("/integer", "bad", lambda f: f["integer"]), "native_pointers")
        checks["builtin_pointer_valid_invalid_and_schema_identity"] = True

        for suffix, target in (("/a~1b~0c", lambda f: f["a/b~c"]),
                               ("/array/0", lambda f: f["array"][0]),
                               ("/array/not-an-index", None), ("/missing", None)):
            compare(basic(suffix, "bad", target), "native_pointers")
        checks["builtin_pointer_escapes_indexes_and_genuine_failures"] = True

        def dynamic_anchor(fixture, resource, specifications):
            return {"meta": {"$id": root_uri, "$dynamicRef": core_uri + "#meta"},
                    "actual": {"$anchor": 0},
                    "error_schema": resource.contents["$defs"]["anchorString"]}
        compare(dynamic_anchor, "native_dynamic_anchors")
        checks["existing_builtin_dynamic_anchor_enters_native_path"] = True

        def dynamic_binding(mode):
            def make(fixture, resource, specifications):
                dynamic = specifications._anchors[(core_uri, "meta")]
                assert type(dynamic) is DynamicAnchor
                events, bindings = [], []
                fault = RuntimeError("actual dynamic anchor name getter failure")
                state = {"meta": {"$id": root_uri, "$dynamicRef": core_uri + "#meta"},
                         "actual": {"$anchor": 0}, "events": events, "dynamic_order": mode}
                if mode == "raise":
                    state["fault"] = fault
                else:
                    state["error_schema"] = resource.contents["$defs"]["anchorString"]

                def replacement(receiver, uri, name):
                    events.append({"replacement_call": True})
                    raise AssertionError("name getter changed an already-bound anchor operation")

                class AnchorDescriptor:
                    def __get__(self, receiver, owner):
                        if receiver is None:
                            return self
                        binding = len(bindings)
                        bindings.append(receiver)
                        events.append({"anchor_bind": binding,
                                       "exact_registry": type(receiver) is Registry,
                                       "same_owner": owner is Registry})
                        method = saved_anchor.__get__(receiver, owner)
                        assert method.__self__ is receiver

                        def call(uri, name):
                            events.append({"anchor_call": binding, "uri": uri, "name": name,
                                           "same_registry": method.__self__ is bindings[binding]})
                            return method(uri, name)

                        return call

                class NameDescriptor:
                    def __get__(self, receiver, owner):
                        if receiver is None:
                            return self
                        name = saved_dynamic_name.__get__(receiver, owner)
                        if receiver is dynamic:
                            events.append({"name_get": True, "same_anchor": receiver is dynamic,
                                           "same_owner": owner is DynamicAnchor})
                            if mode == "replace":
                                Registry.anchor = replacement
                            elif mode == "raise":
                                raise fault
                        return name

                def install(state):
                    Registry.anchor = AnchorDescriptor()
                    DynamicAnchor.name = NameDescriptor()

                state["install"] = install
                return state
            return make

        compare(dynamic_binding("ordered"), "native_dynamic_anchors")
        checks["dynamic_anchor_binds_registry_descriptor_before_live_name_getter"] = True
        compare(dynamic_binding("replace"), "native_dynamic_anchors")
        checks["dynamic_name_getter_replacement_preserves_already_bound_callable"] = True
        compare(dynamic_binding("raise"), "native_dynamic_anchors")
        checks["dynamic_name_getter_exception_preserves_supplied_identity_and_bind_order"] = True

        # Inspect only the existing crawled inventory. Calling anchors() here
        # would create new Resources and would not establish builtin provenance.
        def static_anchors(specifications):
            return sorted(
                [(uri, name, anchor) for (uri, name), anchor in
                 specifications._anchors.items()
                 if type(anchor) is Anchor and type(anchor.resource) is Resource
                 and anchor.resource._specification is DRAFT202012],
                key=lambda item: item[:2],
            )

        inventories = [static_anchors(specifications_for(selected))
                       for selected in (original, provider)]
        assert [item[:2] for item in inventories[0]] == [
            item[:2] for item in inventories[1]
        ], "selected builtin static-anchor inventories differ"
        if inventories[0]:
            def static_anchor(fixture, resource, specifications):
                uri, name, _anchor = static_anchors(specifications)[0]
                return {"meta": {"$id": root_uri, "$ref": uri + "#" + name},
                        "actual": {}}
            compare(static_anchor, "native_static_anchors")
            checks["existing_builtin_static_anchor_enters_native_path"] = True
        else:
            checks["no_static_anchor_resource_fabricated_for_empty_inventory"] = True

        def patched_pointer(fixture, resource, specifications):
            state = basic("/integer", "bad", lambda f: f["integer"])(
                fixture, resource, specifications)
            state["pointer_calls"] = 1
            state["install"] = lambda state: setattr(
                Resource, "pointer", pointer_wrapper(saved_pointer, state))
            return state
        compare(patched_pointer, zero=True)
        checks["patched_resource_pointer_dispatches_actual_callback_once"] = True

        def mid_operation_patch(fixture, resource, specifications):
            events = []
            checker = FormatChecker(formats=[])
            state = {"meta": {"$id": root_uri, "allOf": [
                        {"$ref": reference("/string")}, {"format": "patch-pointer"},
                        {"$ref": reference("/integer")}]},
                     "actual": "bad", "events": events, "checker": checker,
                     "error_schema": fixture["integer"], "pointer_calls": 1}

            def patch(value):
                events.append({"patch": value is state["actual"]})
                Resource.pointer = pointer_wrapper(saved_pointer, state)
                return True

            checker.checks("patch-pointer")(patch)
            return state
        compare(mid_operation_patch, "native_pointers")
        checks["format_callback_changes_next_pointer_dispatch"] = True

        def descriptor_pointer(fixture, resource, specifications):
            state = basic("/integer", "bad", lambda f: f["integer"])(
                fixture, resource, specifications)

            class Descriptor:
                def __get__(self, receiver, owner):
                    event = {"descriptor_get": True, "same_resource": receiver is resource,
                             "same_owner": owner is Resource}
                    state["events"].append(event)
                    assert event["same_resource"] and event["same_owner"]
                    return saved_pointer.__get__(receiver, owner)

            state["descriptor"] = True
            state["install"] = lambda state: setattr(Resource, "pointer", Descriptor())
            return state
        compare(descriptor_pointer, "native_pointers")
        checks["pointer_descriptor_gets_once_and_binds_actual_receiver"] = True

        def helper_with_opaque_result(fixture, resource, specifications):
            events = []
            locations = {}
            index_nodes(locations, resource.contents, core_uri)

            class OpaqueResolver:
                def __init__(self, inner):
                    self.inner = inner

                def __getattribute__(self, name):
                    if name in ("_base_uri", "_registry", "_previous"):
                        raise AssertionError("returned custom resolver was inspected")
                    return object.__getattribute__(self, name)

                def lookup(self, ref):
                    events.append({"opaque_lookup": ref})
                    return self.inner.lookup(ref)

                def in_subresource(self, subresource):
                    events.append({"opaque_descend": locations.get(id(subresource.contents), [])})
                    return OpaqueResolver(self.inner.in_subresource(subresource))

                def dynamic_scope(self):
                    return self.inner.dynamic_scope()

            def helper(*args, **kwargs):
                assert not args
                subresource = kwargs["subresource"]
                events.append({"helper": list(kwargs), "arity": len(args),
                               "segments": list(kwargs["segments"]),
                               "contents": locations.get(id(subresource.contents), []),
                               "actual_resource": type(subresource) is Resource,
                               "actual_specification": subresource._specification is DRAFT202012})
                result = saved_helper(*args, **kwargs)
                return result if type(result) is OpaqueResolver else OpaqueResolver(result)

            return {"meta": {"$id": root_uri, "$ref": reference("/opaque")}, "actual": "!",
                    "events": events, "opaque": True,
                    "error_schema": resource.contents["$defs"]["anchorString"],
                    "install": lambda state: object.__setattr__(
                        DRAFT202012, "maybe_in_subresource", helper)}
        compare(helper_with_opaque_result, "native_pointers")
        checks["live_subresource_helper_arguments_and_opaque_resolver_preserved"] = True

        def external_pointer(patched):
            def make(fixture, resource, specifications):
                target = {"type": "integer"}
                state = {"meta": {"$id": root_uri, "$ref": external_uri + "#/$defs/target"},
                         "actual": "bad", "external": {"$defs": {"target": target}},
                         "error_schema": target}
                if patched:
                    state["pointer_calls"] = 1
                    state["install"] = lambda state: setattr(
                        Resource, "pointer", pointer_wrapper(saved_pointer, state))
                return state
            return make
        compare(external_pointer(False), zero=True)
        compare(external_pointer(True), zero=True)
        checks["retrieved_default_resource_remains_opaque_and_dispatches_pointer"] = True

        def owned_alias(fixture, resource, specifications):
            fixture["alias"] = {"$ref": "child.json"}
            child = {"type": "integer"}
            state = {"meta": {"$id": root_uri, "$ref":
                              external_uri + "#/$defs/" + fixture_key + "/alias"},
                     "actual": "bad", "error_schema": child,
                     "expected_requests": [external_uri, child_uri], "events": []}

            def retrieve(uri):
                if uri == external_uri:
                    state["events"].append({"returned_owned_resource": True})
                    return resource
                assert uri == child_uri, "alias lookup used the builtin resource's canonical URI"
                returned = Resource.from_contents(child, default_specification=DRAFT202012)
                assert returned is not resource and returned.contents is child
                state["events"].append({"returned_fresh_child": True})
                return returned

            state["retrieve_resource"] = retrieve
            return state
        compare(owned_alias, "native_pointers")
        checks["owned_resource_alias_preserves_retrieved_base_uri_and_child_scope"] = True

        def changed_owned_alias(patch_method):
            def make(fixture, resource, specifications):
                key = "integer" if patch_method else "changing"
                state = {"meta": {"$id": root_uri, "$ref":
                                  external_uri + "#/$defs/" + fixture_key + "/" + key},
                         "actual": "bad", "error_schema": fixture[key], "events": [],
                         "expected_requests": [external_uri], "pointer_base_uri": external_uri}

                def retrieve(uri):
                    assert uri == external_uri
                    if patch_method:
                        Resource.pointer = pointer_wrapper(saved_pointer, state)
                        state["events"].append({"patched_before_return": True})
                    else:
                        state["events"].append({"type_before_return": fixture[key]["type"]})
                        fixture[key]["type"] = "integer"
                    return resource

                state["retrieve_resource"] = retrieve
                if patch_method:
                    state["pointer_calls"] = 1
                return state
            return make
        compare(changed_owned_alias(False), "native_pointers")
        compare(changed_owned_alias(True), zero=True)
        checks["owned_alias_uses_contents_and_pointer_method_after_retrieval"] = True

        def fresh_same_contents(patch_method):
            def make(fixture, resource, specifications):
                state = {"meta": {"$id": root_uri, "$ref":
                                  external_uri + "#/$defs/" + fixture_key + "/integer"},
                         "actual": "bad", "error_schema": fixture["integer"], "events": [],
                         "expected_requests": [external_uri], "pointer_base_uri": external_uri}

                def retrieve(uri):
                    assert uri == external_uri
                    returned = Resource(contents=resource.contents, specification=DRAFT202012)
                    assert returned is not resource and returned.contents is resource.contents
                    state["events"].append({"fresh_resource": returned is not resource,
                                            "same_contents": returned.contents is resource.contents})
                    if patch_method:
                        Resource.pointer = pointer_wrapper(saved_pointer, state)
                    return returned

                state["retrieve_resource"] = retrieve
                if patch_method:
                    state["pointer_calls"] = 1
                return state
            return make
        compare(fresh_same_contents(False), zero=True)
        compare(fresh_same_contents(True), zero=True)
        checks["fresh_resource_with_builtin_contents_never_acquires_ownership"] = True

        def intermediate_id(fixture, resource, specifications):
            nested_uri = "https://fragments.invalid/nested/value.json"
            fixture["$id"] = "https://fragments.invalid/nested/context.json"
            fixture["properties"] = {"child": {"$ref": "value.json"}}
            target = {"type": "integer"}
            expected = {nested_uri: target}
            state = {"meta": {"$id": root_uri, "$ref": reference("/properties/child")},
                     "actual": "bad", "error_schema": target,
                     "expected_requests": [nested_uri], "events": []}

            def retrieve(uri):
                contents = expected.get(uri)
                state["events"].append({"nested_source_get": uri,
                                        "found": contents is target})
                assert contents is target, "pointer did not enter the intermediate $id scope"
                returned = Resource.from_contents(contents, default_specification=DRAFT202012)
                assert returned is not resource and returned.contents is target
                return returned

            state["retrieve_resource"] = retrieve
            return state
        compare(intermediate_id, "native_pointers")
        checks["owned_pointer_intermediate_id_changes_subresource_scope"] = True

        def mutate_contents(fixture, resource, specifications):
            checker = FormatChecker(formats=[])
            events = []

            def change(value):
                events.append({"change": value == "bad", "previous": fixture["changing"]["type"]})
                fixture["changing"]["type"] = "integer"
                return True

            checker.checks("change-contents")(change)
            return {"meta": {"$id": root_uri, "allOf": [
                        {"$ref": reference("/changing")}, {"format": "change-contents"},
                        {"$ref": reference("/changing")}]},
                    "actual": "bad", "events": events, "checker": checker,
                    "error_schema": fixture["changing"]}
        delta = compare(mutate_contents, "native_pointers")
        assert delta["native_pointers"] >= 2
        checks["builtin_contents_mutation_is_read_on_next_reference"] = True
    finally:
        try:
            Registry.anchor = saved_anchor
        finally:
            try:
                DynamicAnchor.name = saved_dynamic_name
            finally:
                try:
                    Resource.pointer = saved_pointer
                finally:
                    try:
                        object.__setattr__(DRAFT202012, "maybe_in_subresource", saved_helper)
                    finally:
                        for selected, saved_meta, registry, saved_retrieve in saved_lanes:
                            try:
                                object.__setattr__(registry, "_retrieve", saved_retrieve)
                            finally:
                                selected.META_SCHEMA = saved_meta

    assert len(checks) == 17 and all(value is True for value in checks.values())
    return checks
