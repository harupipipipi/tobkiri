"""Actual Original/provider comparisons of the App-owned URI DATA interval."""


def uri_checks(provider, kernel) -> dict[str, bool]:
    """Retain real callbacks, raw handles, errors, and opaque resolver dispatch."""
    import urllib.parse as parse

    from jsonschema import Draft202012Validator as original, FormatChecker, exceptions
    from jsonschema import validators
    from referencing import Registry, Resource, _core
    from referencing.jsonschema import DRAFT202012
    from schema_kernel_observation import first_difference, meta_nodes, observe

    fields = ("native_join_defrags", "native_join_only",
              "python_resolve_boundaries", "python_join_boundaries")
    saved_root = Registry.__dict__["resolver_with_root"]
    saved_get = Registry.__dict__["get_or_retrieve"]
    saved_anchor = Registry.__dict__["anchor"]
    saved_pointer = Resource.__dict__["pointer"]
    saved_join, saved_defrag = _core.urljoin, _core.urldefrag
    saved_parse = parse.urlparse
    checks = {}
    comparisons = 0

    def counts():
        value = kernel.app_uri_counts()
        assert type(value) is dict and set(value) == set(fields)
        assert all(type(value[name]) is int and value[name] >= 0 for name in fields)
        return value

    def graph(error, seen=None):
        if error is None:
            return None
        seen = {} if seen is None else seen
        if id(error) in seen:
            return {"ref": seen[id(error)]}
        seen[id(error)] = len(seen)
        return {"id": seen[id(error)],
                "type": type(error).__module__ + "." + type(error).__qualname__,
                "message": str(error), "suppressed": error.__suppress_context__,
                "cause": graph(error.__cause__, seen),
                "context": graph(error.__context__, seen)}

    def text(value):
        # Observations must not invoke a foreign __str__/__repr__ protocol.
        if isinstance(value, str):
            body = str.__str__(value)
        elif type(value) is bytes:
            body = value.hex()
        else:
            body = None
        return {"type": type(value).__module__ + "." + type(value).__qualname__,
                "value": body}

    def run(selected, make):
        state = make(selected)
        events = state.setdefault("events", [])
        base, reference = state["base"], state["reference"]
        target = state.setdefault("target", {"type": "integer"})
        actual = state.get("actual", "bad")
        meta = state.setdefault("meta", {"$ref": reference, "$defs": {"target": target}})
        resource = Resource.from_contents(target, default_specification=DRAFT202012)
        saved_meta = selected.META_SCHEMA
        selected.META_SCHEMA = meta
        known = meta_nodes(selected)
        current_uri = []

        def uri_event(value):
            return {**text(value), "same_base": value is base,
                    "same_reference": value is reference}

        class OpaqueResolver:
            def __getattribute__(self, name):
                if name in ("_base_uri", "_registry", "_previous"):
                    raise AssertionError("opaque URI resolver state was inspected")
                return object.__getattribute__(self, name)

            def lookup(self, ref):
                events.append({"opaque_lookup": ref is reference})
                assert ref is reference
                return _core.Resolved(contents=target, resolver=self)

        def root(receiver, supplied):
            if supplied.contents is not meta:
                return saved_root(receiver, supplied)
            events.append({"root": True, "same_meta": supplied.contents is meta})
            if state.get("opaque"):
                return OpaqueResolver()
            return _core.Resolver(base_uri=base, registry=receiver)

        def get(receiver, uri):
            events.append({"retrieve": uri_event(uri)})
            current_uri[:] = [uri]
            if "on_get" in state:
                state["on_get"](uri)
            selected_resource = state.get("resources", {}).get(uri, resource)
            return _core.Retrieved(value=selected_resource, registry=receiver)

        def anchor(receiver, uri, name):
            events.append({"anchor": text(name), "uri": uri_event(uri),
                           "same_retrieved_uri": uri is current_uri[0]})
            if "on_anchor" in state:
                state["on_anchor"](name)
            return _core.Retrieved(
                value=_core.Anchor(name=name, resource=resource), registry=receiver)

        def pointer(receiver, pointer, resolver):
            if receiver is not resource:
                return saved_pointer(receiver, pointer=pointer, resolver=resolver)
            events.append({"pointer": text(pointer),
                           "base": uri_event(resolver._base_uri),
                           "same_retrieved_uri": resolver._base_uri is current_uri[0]})
            assert resolver._base_uri is current_uri[0]
            return _core.Resolved(contents=target, resolver=resolver)

        Registry.resolver_with_root = root
        Registry.get_or_retrieve = get
        Registry.anchor = anchor
        if state.get("pointer_override", True):
            Resource.pointer = pointer
        before = counts()
        try:
            if "install" in state:
                state["install"]()
            try:
                selected.check_schema(actual, format_checker=state.get("checker"))
            except exceptions.SchemaError as error:
                outcome = {"kind": "schema-error", "error": observe(error, actual, known),
                           "same_target": error.schema is target}
            except Exception as error:
                outcome = {"kind": "exception", "error": graph(error),
                           "same_fault": error is state.get("fault")}
            else:
                outcome = {"kind": "valid"}
            if "verify" in state:
                state["verify"](outcome)
            after = counts()
            delta = {name: after[name] - before[name] for name in fields}
            assert all(value >= 0 for value in delta.values())
            if selected is original:
                assert not any(delta.values()), delta
            return {"outcome": outcome, "events": events}, delta
        finally:
            Registry.resolver_with_root = saved_root
            Registry.get_or_retrieve = saved_get
            Registry.anchor = saved_anchor
            Resource.pointer = saved_pointer
            _core.urljoin, _core.urldefrag = saved_join, saved_defrag
            parse.urlparse = saved_parse
            selected.META_SCHEMA = saved_meta
            if "cleanup" in state:
                state["cleanup"]()

    def compare(make, mode="native"):
        nonlocal comparisons
        expected, _ = run(original, make)
        actual, delta = run(provider, make)
        difference = first_difference(expected, actual)
        assert difference is None, (comparisons, difference)
        if mode == "native":
            assert delta == dict(zip(fields, (1, 0, 0, 0))), delta
        elif mode == "boundary":
            assert delta == dict(zip(fields, (0, 0, 1, 0))), delta
        elif mode == "skip":
            assert not any(delta.values()), delta
        elif mode == "two-boundaries":
            assert delta == dict(zip(fields, (0, 0, 2, 0))), delta
        elif mode == "owned-join":
            assert delta["native_join_defrags"] > 0 and delta["native_join_only"] > 0
            assert delta["python_resolve_boundaries"] == 1
            assert delta["python_join_boundaries"] == 0
        elif mode == "owned-join-boundary":
            assert delta["native_join_defrags"] == delta["native_join_only"] == 0
            assert delta["python_join_boundaries"] > 0
            assert delta["python_resolve_boundaries"] >= 2
        else:
            raise AssertionError(mode)
        comparisons += 1
        return actual

    def simple(base, reference):
        return lambda selected: {"base": base, "reference": reference}

    def hash_mode(base, reference):
        if reference.startswith("#"):
            return "skip"
        return "boundary" if "#" in base or "#" in reference else "native"

    relative = (
        "g:h", "g", "./g", "g/", "/g", "//g", "?y", "g?y", "#s",
        "g#s", "g?y#s", ";x", "g;x", "g;x?y#s", "", ".", "./", "..",
        "../", "../g", "../..", "../../", "../../g", "../../../g",
        "../../../../g", "/./g", "/../g", "g.", ".g", "g..", "..g",
        "./../g", "./g/.", "g/./h", "g/../h", "g;x=1/./y", "g;x=1/../y",
        "g?y/./x", "g?y/../x", "g#s/./x", "g#s/../x", "http:g",
        "g//h///i", "?", "#", "?#", "%2e%2e/g", "%2f/%7e/%23",
        "/a//b///c", "/..", "/.", ";", ";?", "g;?", "//", "///g", "////g",
    )
    for ref in relative:
        compare(simple("http://a/b/c/d;p?q", ref), hash_mode("http://a/b/c/d;p?q", ref))
    checks["rfc_paths_params_queries_fragments_and_literal_percent_escapes"] = True

    for base, ref in (
        ("https://host/a/b?x#f", "../c"), ("ftp://host/a/b;p", "../c;q"),
        ("file:///a/b", "../c"), ("http:a/b", "../c"), ("http:", "child"),
        ("HTTP://A/a", "HTTP:b"), ("https://host/a", "HTTP://Else/b?"),
        ("gopher://host/a;b", "?next"), ("git://host/a;b", "next;c"),
        ("https://host/a/b", "1bad:c"), ("https://host/a/b", "a_b:c"),
        ("https://host/a/b", ":c"), ("https://host/a/b", "a+b-c.d:e"),
        ("https://user:pass@host:99999/a", "//other:bad/b"),
        ("https://host/a", "//user:pass@other/path;p?q#frag"),
        ("https://host/a", "//other"), ("https://host/a", "//?q"),
        ("https://host/a", "//?#f"),
        ("https://host/a", "git:/path;#f"),
        ("https://host/a", "hdl:opaque;#f"),
        ("https://host/a", "sip:opaque;parameter#f"),
        ("https://host/a", "itms-services:/path#f"),
        ("https://host/a", "unknown:/path#f"),
        ("https://host/a", "gopher:/path;#f"),
        ("https://host/a", "prospero:/path;#f"),
    ):
        compare(simple(base, ref), "boundary" if base.startswith("git:") else hash_mode(base, ref))
    checks["scheme_tables_case_inheritance_authority_and_empty_netloc_rules"] = True

    for base, ref in (
        ("http://a/\t\nb", " c\r/d"), ("http:\n//a/a", "../c"),
        ("http://a/a ", "?x"), ("http://a/a", "\x00\x1f ../c\t?x\r#f\n"),
        ("http://a/a", "https:\n//b/c?"), ("http://a/a", "\tother:c?"),
        ("", "\x00HTTP://A/path?"), ("", "\x00HTTP://A/path?#f"),
        ("", "raw\tpath?"), ("", "raw\tpath?#f"),
    ):
        compare(simple(base, ref), hash_mode(base, ref))
    checks["source_control_stripping_preserves_raw_early_returns"] = True

    # Build noninterned strings for these probes. Equality is insufficient:
    # empty arguments and nonrelative schemes must retain the actual raw input.
    for base, ref, same_base, same_ref in (
        ("", "https://identity.invalid/unchanged?", False, True),
        ("https://identity.invalid/unchanged?", "", True, False),
        ("http://base.invalid/a", "\tother:raw?", False, True),
        ("urn:base", "relative-value?", False, True),
    ):
        def identity(selected, base=base, ref=ref, same_base=same_base, same_ref=same_ref):
            base, ref = (value.encode().decode() for value in (base, ref))
            state = {"base": base, "reference": ref}
            def verify(outcome):
                event = next(event["retrieve"] for event in state["events"] if "retrieve" in event)
                assert event["same_base"] is same_base and event["same_reference"] is same_ref
                assert outcome["kind"] == "schema-error" and outcome["same_target"]
            state["verify"] = verify
            return state
        compare(identity, "boundary" if base.startswith("urn:") else "native")
    compare(simple("", ""))
    checks["empty_arguments_and_unchanged_urls_preserve_actual_input_handles"] = True

    for base, ref in (
        ("urn:base:a", "next"), ("unknown:a/b", "../c#f"),
        ("mailto:a", "foo#frag"), ("custom:a", "foo#frag"),
        ("", "foo#frag"),
        ("relative/base", "next"), ("a", "same-singleton-path"),
        ("\thttp://host/a", "../c"), ("http://host/a", "snow/雪?語#片"),
        ("http://例.test/a", "next"), ("http://host/a", "//example：80/b"),
        ("http://host/a", "//[::1]/b"), ("http://[invalid]/a", "next"),
        ("http://host/a", "//v[bad]/b"), ("http://host/a", "//[::1]bad/b"),
        ("http://host/a", "path[bracket]"), ("", "http://[invalid]/a"),
        ("http://host/a", "path\ud800"), ("http://host/\udfff", "next"),
        (b"http://host/a", "next"), ("http://host/a", b"next"),
    ):
        compare(simple(base, ref), "boundary")

    def cached_path(selected, base):
        events, uris = [], []
        first, second = (value.encode().decode() for value in
                         ("cached-uri-path#anchor", "cached-uri-path#anchor"))
        assert first == second and first is not second
        def retrieved(uri):
            if uris:
                events.append({"same_previous_uri": uri is uris[0]})
                assert uri is uris[0], "original shared-parser path identity was lost"
            uris.append(uri)
        def verify(outcome):
            assert outcome["kind"] == "valid" and len(uris) == 2
        return {"base": base, "reference": first,
                "meta": {"allOf": [{"$ref": first}, {"$ref": second}]},
                "target": {}, "actual": {}, "events": events,
                "on_get": retrieved, "verify": verify}
    for base in ("", "mailto:a", "custom:a"):
        compare(lambda selected, base=base: cached_path(selected, base), "two-boundaries")
    checks[
        "unsupported_unicode_surrogate_bytes_brackets_and_schemeless_bases_use_original_once"
    ] = True

    def subclass_case(selected):
        events = []
        class Text(str):
            def __bool__(self):
                events.append("text-bool")
                return len(self) != 0
        return {"base": Text("https://foreign.invalid/a/b"),
                "reference": Text("../c?x#y"), "events": events}
    compare(subclass_case, "boundary")

    def falsey_case(selected):
        events = []
        class Falsey:
            def __bool__(self):
                events.append("base-bool")
                return False
        return {"base": Falsey(), "reference": "raw-relative", "events": events}
    compare(falsey_case, "boundary")

    def truth_fault_case(selected):
        events = []
        fault = LookupError("supplied URI base truth failure")
        class Raising:
            def __bool__(self):
                events.append("base-bool-fault")
                raise fault
        def verify(outcome):
            assert outcome["kind"] == "exception" and outcome["same_fault"]
            assert events.count("base-bool-fault") == 1
        return {"base": Raising(), "reference": "next", "events": events,
                "fault": fault, "verify": verify}
    compare(truth_fault_case, "boundary")
    checks["subclass_and_foreign_truth_operations_keep_original_order"] = True

    def fragment_case(selected, foreign=False):
        events = []
        class Text(str):
            def startswith(self, prefix):
                events.append({"startswith": prefix})
                return super().startswith(prefix)
            def __getitem__(self, key):
                events.append({"slice": [key.start, key.stop, key.step]})
                return super().__getitem__(key)
        fault = AssertionError("fragment-only lookup consulted a URI helper")
        def forbidden(*args, **kwargs):
            raise fault
        def install():
            _core.urljoin = _core.urldefrag = forbidden
        return {"base": "https://fragment.invalid/base#retained?", "reference":
                Text("#anchor") if foreign else "#anchor", "events": events,
                "install": install}
    compare(fragment_case, "skip")
    compare(lambda selected: fragment_case(selected, True), "boundary")
    checks["fragment_only_keeps_base_identity_and_skips_both_helpers"] = True

    def anchor_names(selected, kind):
        events, names = [], []
        base = "https://anchor-cache.invalid/root"
        ref = ("#cached-anchor-name" if kind == "direct-slice" else
               "child#cached-anchor-name")
        if kind == "base-fragment":
            base += "#cached-base-anchor-name"
            ref = ""
        first, second = (value.encode().decode() for value in (ref, ref))
        assert first == second
        if ref:
            assert first is not second
        def anchored(name):
            if names:
                same = name is names[0]
                events.append({"same_previous_anchor_name": same})
                assert same is (kind != "direct-slice"), (
                    "actual cached-fragment/direct-slice name identity changed", kind)
            names.append(name)
        def verify(outcome):
            assert outcome["kind"] == "valid" and len(names) == 2
        return {"base": base, "reference": first,
                "meta": {"allOf": [{"$ref": first}, {"$ref": second}]},
                "target": {}, "actual": {}, "events": events,
                "on_anchor": anchored, "verify": verify}
    for kind in ("joined-fragment", "base-fragment", "direct-slice"):
        compare(lambda selected, kind=kind: anchor_names(selected, kind),
                "skip" if kind == "direct-slice" else "two-boundaries")
    checks["actual_anchor_names_keep_shared_cached_fragment_or_direct_slice_identity"] = True

    def getter_fault(selected, mode):
        events = []
        fault = LookupError("supplied URI " + mode + " failure")
        class Truth:
            def __bool__(self):
                events.append("startswith-truth")
                if mode == "truth":
                    raise fault
                return True
        class Text(str):
            def __getattribute__(self, name):
                if name == "startswith":
                    events.append("startswith-get")
                    if mode == "getter":
                        raise fault
                return super().__getattribute__(name)
            def startswith(self, prefix):
                events.append("startswith-call")
                return Truth()
            def __getitem__(self, key):
                events.append({"slice": [key.start, key.stop, key.step]})
                raise fault
        def forbidden(*args, **kwargs):
            raise AssertionError("URI callbacks preceded the reference operation")
        def install():
            _core.urljoin = _core.urldefrag = forbidden
        def verify(outcome):
            assert outcome["kind"] == "exception" and outcome["same_fault"]
        return {"base": "http://getter.invalid/a", "reference": Text("#a"),
                "events": events, "fault": fault, "install": install, "verify": verify}
    for mode in ("getter", "truth", "slice"):
        compare(lambda selected, mode=mode: getter_fault(selected, mode), "boundary")
    checks["reference_getter_truth_and_slice_failures_preserve_supplied_exception_identity"] = True

    def unpack_case(selected, mode):
        events = []
        fault = LookupError("supplied URI unpack failure")
        class Text(str):
            pass
        joined = "https://unpack.invalid/joined"
        uri = "https://unpack.invalid/result"
        class Pair:
            def __iter__(self):
                events.append("pair-iter")
                return Iterator()
        class Iterator:
            def __iter__(self):
                events.append("iterator-iter")
                return self
            def __next__(self):
                index = getattr(self, "index", 0)
                self.index = index + 1
                events.append({"pair-next": index})
                if mode == "fault" and index == 1:
                    raise fault
                if index == 0:
                    return uri
                if index == 1:
                    return ""
                if mode == "extra" and index == 2:
                    return "unexpected-third-value"
                raise StopIteration
            def __length_hint__(self):
                raise AssertionError("two-item unpack consulted a length hint")
        def join(base, ref):
            events.append({"join": ref is reference})
            return joined
        def defrag(value):
            events.append({"defrag": value is joined})
            assert value is joined
            return Pair()
        reference = Text("next")
        def install():
            _core.urljoin, _core.urldefrag = join, defrag
        def verify(outcome):
            if mode == "fault":
                assert outcome["kind"] == "exception" and outcome["same_fault"]
            elif mode == "extra":
                assert outcome["kind"] == "exception"
                assert outcome["error"]["type"] == "builtins.ValueError"
            else:
                assert outcome["kind"] == "schema-error" and outcome["same_target"]
        return {"base": "http://unpack.invalid/base", "reference": reference,
                "events": events, "fault": fault, "install": install, "verify": verify}
    for mode in ("normal", "fault", "extra"):
        compare(lambda selected, mode=mode: unpack_case(selected, mode), "boundary")

    def callback_fault(selected, mode):
        events = []
        fault = LookupError("supplied URI " + mode + " callback failure")
        class Text(str):
            pass
        reference = Text("next")
        joined = "https://callback.invalid/joined"
        def join(base, ref):
            events.append({"join": ref is reference})
            if mode == "join":
                raise fault
            return joined
        def defrag(value):
            events.append({"defrag": value is joined})
            raise fault
        def install():
            _core.urljoin, _core.urldefrag = join, defrag
            if mode == "noncallable-defrag":
                _core.urldefrag = 42
        def verify(outcome):
            assert outcome["kind"] == "exception"
            if mode == "noncallable-defrag":
                assert outcome["error"]["type"] == "builtins.TypeError"
            else:
                assert outcome["same_fault"]
            calls = [event for event in events if "join" in event or "defrag" in event]
            assert calls == ([{"join": True}, {"defrag": True}] if mode == "defrag"
                             else [{"join": True}])
        return {"base": "http://callback.invalid/base", "reference": reference,
                "events": events, "fault": fault, "install": install, "verify": verify}
    for mode in ("join", "defrag", "noncallable-defrag"):
        compare(lambda selected, mode=mode: callback_fault(selected, mode), "boundary")
    checks["actual_defrag_pair_unpack_iteration_and_supplied_failure_are_preserved"] = True

    def live_aliases(selected):
        events = []
        class Text(str):
            pass
        first, second = Text("first"), Text("second")
        meta = {"allOf": [{"$ref": first}, {"$ref": second}]}
        def later_defrag(value):
            events.append("later-defrag")
            return value, ""
        def later_join(base, ref):
            events.append({"later-join": ref is second})
            assert ref is second
            return "https://live.invalid/second"
        def first_defrag(value):
            events.append("first-defrag")
            return value, ""
        def first_join(base, ref):
            events.append({"first-join": ref is first})
            assert ref is first
            _core.urldefrag, _core.urljoin = later_defrag, later_join
            return "https://live.invalid/first"
        def install():
            _core.urldefrag, _core.urljoin = first_defrag, first_join
        def verify(outcome):
            calls = [event for event in events if isinstance(event, str) or
                     "first-join" in event or "later-join" in event]
            assert calls == [{"first-join": True}, "first-defrag",
                             {"later-join": True}, "later-defrag"]
            assert outcome["kind"] == "valid"
        return {"base": "http://live.invalid/root", "reference": first,
                "meta": meta, "target": {}, "actual": {}, "events": events,
                "install": install, "verify": verify}
    compare(live_aliases, "two-boundaries")
    checks["defrag_binding_survives_join_rebind_and_next_lookup_uses_current_aliases"] = True

    def bound_reference(selected):
        events = []
        class Text(str):
            pass
        first, old_next, next_ref = Text("first"), Text("old-next"), Text("next")
        first_child, second_child = {"$ref": first}, {"$ref": old_next}
        meta = {"allOf": [first_child, second_child]}
        def join(base, ref):
            events.append({"bound-first": ref is first, "live-next": ref is next_ref})
            if ref is first:
                first_child["$ref"] = Text("replaced-current")
                second_child["$ref"] = next_ref
            else:
                assert ref is next_ref
            return saved_join(base, ref)
        def install():
            _core.urljoin = join
        def verify(outcome):
            assert outcome["kind"] == "valid"
            assert [event for event in events if "bound-first" in event] == [
                {"bound-first": True, "live-next": False},
                {"bound-first": False, "live-next": True}]
        return {"base": "http://binding.invalid/root", "reference": first,
                "meta": meta, "target": {}, "actual": {}, "events": events,
                "install": install, "verify": verify}
    compare(bound_reference, "two-boundaries")
    checks["current_bound_reference_survives_replacement_and_next_reference_is_live"] = True

    def live_native(selected, fragment=True):
        events = []
        child = {"$ref": "old"}
        target = {"type": "integer"}
        checker = FormatChecker(formats=[])
        def change(value):
            events.append({"change": value == "bad"})
            child["$ref"] = "current?query#anchor" if fragment else "current?query"
            return True
        checker.checks("change-uri")(change)
        return {"base": "https://live-native.invalid/a/root", "reference": "old",
                "meta": {"allOf": [{"format": "change-uri"}, child],
                         "$defs": {"target": target}}, "target": target,
                "checker": checker, "events": events}
    for fragment in (True, False):
        actual = compare(lambda selected, fragment=fragment: live_native(selected, fragment),
                         "boundary" if fragment else "native")
        assert any(event.get("retrieve", {}).get("value") ==
                   "https://live-native.invalid/a/current?query" for event in actual["events"])
    checks["selected_format_callback_changes_the_next_native_reference"] = True

    def owned_join(selected, unicode_id=False):
        core_uri = "https://json-schema.org/draft/2020-12/meta/core"
        specifications = (validators.SPECIFICATIONS if selected is original
                          else selected._SPECIFICATIONS)
        resource = specifications[core_uri]
        definitions = resource.contents["$defs"]
        fixture_key = "_tobkiri_uri_checks"
        assert fixture_key not in definitions
        definitions[fixture_key] = {
            "$id": "https://owned-join.invalid/雪/context.json" if unicode_id else
                   "https://owned-join.invalid/nested/context.json",
            "properties": {"child": {"$ref": "value.json"}}}
        target = {"type": "integer"}
        ref = core_uri + "#/$defs/" + fixture_key + "/properties/child"
        def cleanup():
            del definitions[fixture_key]
        return {"base": "https://owned-join.invalid/root", "reference": ref,
                "meta": {"$ref": ref, "$defs": {"target": target}}, "target": target,
                "resources": {core_uri: resource}, "pointer_override": False,
                "cleanup": cleanup}
    compare(owned_join, "owned-join")
    compare(lambda selected: owned_join(selected, True), "owned-join-boundary")
    checks["actual_owned_pointer_subresource_executes_native_and_original_join_only_paths"] = True

    def opaque(selected):
        class Poison:
            def __getattribute__(self, name):
                if name == "startswith":
                    raise AssertionError("opaque reference was inspected by URI code")
                return object.__getattribute__(self, name)
        def forbidden(*args, **kwargs):
            raise AssertionError("opaque resolver dispatch consulted a URI helper")
        def install():
            _core.urljoin = _core.urldefrag = forbidden
        return {"base": object(), "reference": Poison(), "opaque": True, "install": install}
    actual = compare(opaque, "skip")
    assert any(event.get("opaque_lookup") for event in actual["events"])
    checks["opaque_selected_resolver_executes_actual_lookup_without_uri_work_or_state_reads"] = True

    baseline, _ = run(original, simple("https://isolation.invalid/a/b", "../c?x"))
    fault = LookupError("unselected shared urllib operation")
    callbacks = []
    def forbidden_join(*args, **kwargs):
        callbacks.append("join")
        raise fault
    def forbidden_defrag(*args, **kwargs):
        callbacks.append("defrag")
        raise fault
    def forbidden_parse(*args, **kwargs):
        callbacks.append("parse")
        raise fault
    def isolated(selected):
        state = simple("https://isolation.invalid/a/b", "../c?x")(selected)
        def install():
            _core.urljoin, _core.urldefrag = forbidden_join, forbidden_defrag
            parse.urlparse = forbidden_parse
        state["install"] = install
        return state
    actual, delta = run(provider, isolated)
    assert first_difference(baseline, actual) is None and not callbacks
    assert delta == dict(zip(fields, (1, 0, 0, 0)))
    try:
        _core.urljoin, _core.urldefrag = forbidden_join, forbidden_defrag
        parse.urlparse = forbidden_parse
        before = counts()
        resolver = _core.Resolver(base_uri="https://isolation.invalid/a/b", registry=Registry())
        try:
            resolver.lookup("../c?x")
        except LookupError as error:
            assert error is fault
        else:
            raise AssertionError("actual shared resolver ignored its selected URI callable")
        assert counts() == before and callbacks == ["join"]
    finally:
        _core.urljoin, _core.urldefrag = saved_join, saved_defrag
        parse.urlparse = saved_parse
    checks[
        "supported_owned_interval_is_independent_while_actual_shared_resolver_keeps_callbacks"
    ] = True

    before = counts()
    provider.check_schema({})
    after = counts()
    assert after["native_join_defrags"] > before["native_join_defrags"]
    assert all(after[name] >= before[name] for name in fields)
    checks["fresh_default_provider_reaches_meaningful_native_join_defrag_work"] = True

    # 145 symmetric pairs plus the independent-owner isolation comparison.
    assert comparisons == 145 and len(checks) == 17 and all(checks.values())
    return checks
