"""Differential checks of explicitly retained builtin META fragment resources."""


def fragment_checks(provider, kernel) -> dict[str, bool]:
    """Use real builtin Resources; neither names nor contents confer ownership."""
    from jsonschema import Draft202012Validator as original, FormatChecker, exceptions
    from jsonschema import validators
    from referencing import Anchor, Resource
    from referencing.jsonschema import DRAFT202012, DynamicAnchor
    from schema_kernel_observation import first_difference, meta_nodes, observe

    core_uri = "https://json-schema.org/draft/2020-12/meta/core"
    root_uri = "https://fragments.invalid/root.json"
    external_uri = "https://fragments.invalid/external.json"
    child_uri = "https://fragments.invalid/child.json"
    fixture_key = "_tobkiri_fragment_checks"
    resource = validators.SPECIFICATIONS[core_uri]
    assert type(resource) is Resource and resource._specification is DRAFT202012
    definitions = resource.contents["$defs"]
    assert type(definitions) is dict and fixture_key not in definitions
    dynamic = validators.SPECIFICATIONS._anchors[(core_uri, "meta")]
    assert type(dynamic) is DynamicAnchor
    assert type(dynamic.resource) is Resource
    assert dynamic.resource._specification is DRAFT202012
    registry = validators._REMOTE_WARNING_REGISTRY
    saved_meta, saved_retrieve = original.META_SCHEMA, registry._retrieve
    saved_pointer = Resource.__dict__["pointer"]
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
            expected = state.get("external_resource", resource)
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
        fixture = {"integer": {"type": "integer"}, "string": {"type": "string"},
                   "a/b~c": {"type": "integer"}, "array": [{"type": "integer"}],
                   "changing": {"type": "string"},
                   "opaque": {"$ref": core_uri + "#/$defs/anchorString"}}
        assert fixture_key not in definitions
        definitions[fixture_key] = fixture
        try:
            state = make(fixture)
            state.setdefault("events", [])
            meta, actual, events = state["meta"], state["actual"], state["events"]
            original.META_SCHEMA = meta
            known = meta_nodes()
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
            else:
                outcome = {"kind": "valid"}
            after = counts()
            delta = {name: after[name] - before[name] for name in fields}
            assert all(n >= 0 for n in delta.values())
            if selected is original:
                assert not any(delta.values()), "original oracle entered native fragments"
            if "error_schema" in state:
                assert outcome["kind"] == "schema-error"
            if "expected_requests" in state:
                assert requests == state["expected_requests"], requests
            if state.get("pointer_calls") is not None:
                assert sum("pointer" in event for event in events) == state["pointer_calls"]
            if state.get("descriptor"):
                assert events == [{"descriptor_get": True, "same_resource": True, "same_owner": True}]
            if state.get("opaque"):
                assert any("opaque_lookup" in event for event in events)
                assert any("helper" in event for event in events)
            return {"outcome": outcome, "events": events, "actual": actual}, delta
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
                            original.META_SCHEMA = saved_meta
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
        def make(fixture):
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

        def dynamic_anchor(fixture):
            return {"meta": {"$id": root_uri, "$dynamicRef": core_uri + "#meta"},
                    "actual": {"$anchor": 0},
                    "error_schema": resource.contents["$defs"]["anchorString"]}
        compare(dynamic_anchor, "native_dynamic_anchors")
        checks["existing_builtin_dynamic_anchor_enters_native_path"] = True

        # Inspect only the existing crawled inventory. Calling anchors() here
        # would create new Resources and would not establish builtin provenance.
        static = [(uri, name, anchor) for (uri, name), anchor in
                  validators.SPECIFICATIONS._anchors.items()
                  if type(anchor) is Anchor and type(anchor.resource) is Resource
                  and anchor.resource._specification is DRAFT202012]
        if static:
            uri, name, _anchor = sorted(static, key=lambda item: item[:2])[0]
            compare(lambda fixture: {"meta": {"$id": root_uri, "$ref": uri + "#" + name},
                                     "actual": {}}, "native_static_anchors")
            checks["existing_builtin_static_anchor_enters_native_path"] = True
        else:
            checks["no_static_anchor_resource_fabricated_for_empty_inventory"] = True

        def patched_pointer(fixture):
            state = basic("/integer", "bad", lambda f: f["integer"])(fixture)
            state["pointer_calls"] = 1
            state["install"] = lambda state: setattr(
                Resource, "pointer", pointer_wrapper(saved_pointer, state))
            return state
        compare(patched_pointer, zero=True)
        checks["patched_resource_pointer_dispatches_actual_callback_once"] = True

        def mid_operation_patch(fixture):
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

        def descriptor_pointer(fixture):
            state = basic("/integer", "bad", lambda f: f["integer"])(fixture)

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

        def helper_with_opaque_result(fixture):
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
            def make(fixture):
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

        def owned_alias(fixture):
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
            def make(fixture):
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
            def make(fixture):
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

        def mutate_contents(fixture):
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
            Resource.pointer = saved_pointer
        finally:
            try:
                object.__setattr__(DRAFT202012, "maybe_in_subresource", saved_helper)
            finally:
                try:
                    object.__setattr__(registry, "_retrieve", saved_retrieve)
                finally:
                    original.META_SCHEMA = saved_meta

    assert len(checks) == 13 and all(value is True for value in checks.values())
    return checks
