"""Bounded native differential checks of the App reference URI operation only."""


def uri_checks(kernel, policy) -> dict[str, bool]:
    """Use current genuine Python helpers as the oracle, never a URI fixture result."""
    import urllib.parse as parse
    import referencing._core as core
    from schema_kernel_observation import first_difference

    saved_core = {name: getattr(core, name) for name in ("urljoin", "urldefrag")}
    helper_names = ("urlparse", "urlsplit", "urlunparse", "urlunsplit", "_coerce_args", "_splitparams")
    saved_helpers = {name: getattr(parse, name) for name in helper_names}
    list_names = ("uses_relative", "uses_netloc", "uses_params", "_UNSAFE_URL_BYTES_TO_REMOVE")
    saved_lists = {name: (getattr(parse, name), list(getattr(parse, name))) for name in list_names}
    fields = ("native_uri_resolutions", "python_uri_boundaries")
    checks = {}

    def restore():
        for name, value in saved_core.items():
            setattr(core, name, value)
        for name, value in saved_helpers.items():
            setattr(parse, name, value)
        for name, (value, contents) in saved_lists.items():
            setattr(parse, name, value)
            value[:] = contents
        saved_helpers["urlsplit"].cache_clear()

    def counts():
        value = kernel.app_reference_counts()
        assert all(type(value.get(name)) is int and value[name] >= 0 for name in fields)
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

    def original(base, reference):
        if reference.startswith("#"):
            return base, reference[1:]
        # Python selects the OUTER callable before evaluating the inner call.
        uri, fragment = core.urldefrag(core.urljoin(base, reference))
        return uri, fragment

    def atom(value, state):
        name = type(value).__module__ + "." + type(value).__qualname__
        for index, marker in enumerate(state["markers"]):
            if value is marker:
                return {"type": name, "marker": index}
        if isinstance(value, str):
            return {"type": name, "value": str.__str__(value)}
        assert type(value) in (int, bool, type(None)), ("unsupported URI observation", name)
        return {"type": name, "value": value}

    def event(state, name, args, kwargs):
        state["events"].append({"call": name, "args": [atom(v, state) for v in args],
                                "keywords": [[k, atom(v, state)] for k, v in kwargs.items()],
                                "is_base": [v is state["base"] for v in args],
                                "is_reference": [v is state["reference"] for v in args]})

    def run(native, base, reference, setup):
        state = {"base": base, "reference": reference, "events": [], "markers": []}
        try:
            if setup is not None:
                setup(state)
            # Reset only the genuine parser's incidental cache, so callback
            # observations never depend on which differential leg ran first.
            saved_helpers["urlsplit"].cache_clear()
            before = counts()
            aliases = []
            try:
                result = (kernel.app_reference_uri(state["base"], state["reference"], policy)
                          if native else original(state["base"], state["reference"]))
                assert type(result) is tuple and len(result) == 2
                aliases = [[value is candidate for candidate in
                            (state["base"], state["reference"], *state["markers"])] for value in result]
                outcome = {"kind": "result", "values": [atom(value, state) for value in result]}
            except Exception as error:
                outcome = {"kind": "exception", "error": graph(error),
                           "is_supplied_exception": error is state.get("fault")}
            after = counts()
            delta = {name: after[name] - before[name] for name in fields}
            assert all(value >= 0 for value in delta.values())
            if not native:
                assert not any(delta.values()), "Python URI oracle entered the native operation"
            if "expected_calls" in state:
                assert [row["call"] for row in state["events"]] == state["expected_calls"]
            return {"outcome": outcome, "events": state["events"]}, aliases, delta
        finally:
            # Restoration errors propagate outside the observation catch.
            restore()

    def compare(base, reference, mode="native", setup=None):
        expected, expected_aliases, _ = run(False, base, reference, setup)
        actual, aliases, delta = run(True, base, reference, setup)
        difference = first_difference(expected, actual)
        assert difference is None, (base, reference, difference)
        for wanted, observed in zip(expected_aliases, aliases):
            assert all(not flag or observed[index] for index, flag in enumerate(wanted))
        if mode == "native":
            assert delta[fields[0]] > 0 and delta[fields[1]] == 0, delta
        elif mode == "boundary":
            assert delta[fields[0]] == 0 and delta[fields[1]] > 0, delta
        else:
            assert mode == "prefix" and not any(delta.values()), delta

    base = "http://a/b/c/d;p?q"
    try:
        for parent, reference in (("not a URI", "#"), ("https://[é]/", "#/x"),
                                  (base, "##"), (base, "#?"), ("", "#fragment")):
            compare(parent, reference, "prefix")
        checks["fragment_prefix_preserves_inputs_without_uri_parsing"] = True

        for parent, reference in (("", ""), ("", "child#frag"), (base, ""), (base, "?"),
                                  (base, "?#"), (base, "?new"), (base, "child?"), (base, "child#")):
            compare(parent, reference)
        checks["empty_queries_and_fragments_keep_original_values_types_and_aliases"] = True

        for reference in ("http://g", "https://g/a#f", "//g/a", "g:h", "HTTP:g",
                          "http:/g", "mailto:user@example", "file:///tmp/a"):
            compare(base, reference)
        checks["absolute_network_and_scheme_resolution_matches_original"] = True

        for reference in ("g", "./g", "../g", "../../g", "../../../g", "/./g", "/../g",
                          "g//h/", "a/./b/../", ".."):
            compare(base, reference)
        checks["slash_and_dot_segment_resolution_matches_original"] = True

        for reference in (";x", "g;x?y#s", "%2e%2e/x", "g%2Fh#%2f", "..;x",
                          "\x00 \t../g\r\n", "g\t/\nh\r", "g\x1fx "):
            compare(base, reference)
        checks["params_percent_and_ascii_controls_match_original"] = True

        for parent, reference in (("parent", "child-long"), ("parent-long", "?"),
                                  ("/relative/base", "../child"), ("//host/path", "child")):
            compare(parent, reference, "boundary")
        checks["scheme_less_bases_preserve_python_input_alias_paths"] = True

        for parent, reference in (("https://é.example/a", "../g"), (base, "☃"), ("", "é"),
                                  (base, "//h℀x/x"), ("https://[::1]/a", "g"),
                                  (base, "//[::1]/x"), (base, "http://[bad]/"),
                                  (base, "http://[::1")):
            compare(parent, reference, "boundary")
        checks["unicode_and_brackets_delegate_before_native_parsing"] = True

        def subclass_setup(which):
            def setup(state):
                class Observed(str):
                    def startswith(self, *args, **kwargs):
                        event(state, "startswith", args, kwargs)
                        return str.startswith(self, *args, **kwargs)

                    def __getitem__(self, key):
                        state["events"].append({"getitem": [key.start, key.stop, key.step]
                            if type(key) is slice else key, "exact_slice": type(key) is slice})
                        return str.__getitem__(self, key)
                state[which] = Observed(state[which])
            return setup
        compare(base, "#frag", "prefix", subclass_setup("reference"))
        compare(base, "child", "boundary", subclass_setup("reference"))
        compare(base, "", "boundary", subclass_setup("base"))
        checks["string_subclass_protocols_and_exact_fragment_slice_are_preserved"] = True

        def core_patch(kind):
            def setup(state):
                def join(*args, **kwargs):
                    event(state, "join", args, kwargs)
                    if kind == "swap":
                        core.urldefrag = late
                    if kind == "raise":
                        try:
                            raise LookupError("actual URI helper cause")
                        except LookupError as cause:
                            raise state["fault"] from cause
                    return saved_core["urljoin"](*args, **kwargs)

                def defrag(*args, **kwargs):
                    event(state, "defrag", args, kwargs)
                    return saved_core["urldefrag"](*args, **kwargs)

                def late(*args, **kwargs):
                    event(state, "late-defrag", args, kwargs)
                    raise AssertionError("inner join changed an already selected outer callable")

                if kind != "defrag":
                    core.urljoin = join
                if kind in ("defrag", "swap"):
                    core.urldefrag = defrag
                if kind == "raise":
                    state["fault"] = RuntimeError("actual URI helper failure")
                state["expected_calls"] = (["join", "defrag"] if kind == "swap" else
                                           ["defrag"] if kind == "defrag" else ["join"])
            return setup
        for kind in ("join", "defrag", "swap", "raise"):
            compare(base, "child#frag", "boundary", core_patch(kind))
        checks["live_reference_helpers_call_order_and_real_exception_graphs_are_preserved"] = True

        def stdlib_patch(name):
            def setup(state):
                def callback(*args, **kwargs):
                    event(state, name, args, kwargs)
                    return saved_helpers[name](*args, **kwargs)
                setattr(parse, name, callback)
            return setup
        for name in ("urlparse", "_coerce_args", "_splitparams"):
            compare(base, "child;new#frag", "boundary", stdlib_patch(name))

        def list_patch(name):
            def setup(state):
                value = getattr(parse, name)
                if name == "_UNSAFE_URL_BYTES_TO_REMOVE":
                    value[:] = ["x"]
                else:
                    value.remove("http")
                assert getattr(parse, name) is saved_lists[name][0]
                state["events"].append({"same_metadata_list": True, "changed": name})
            return setup
        for name in list_names:
            compare("http://host/a;b/c;d", "xchild;new", "boundary", list_patch(name))
        checks["stdlib_helpers_and_in_place_configuration_changes_use_python_boundary"] = True

        def iterable_defrag(length):
            def setup(state):
                state["markers"] = [object(), object()]
                values = [state["markers"][0], state["reference"], state["markers"][1]][:length]

                class Items:
                    def __iter__(self):
                        state["events"].append({"iter": True})
                        return Cursor()

                class Cursor:
                    def __init__(self):
                        self.index = 0

                    def __iter__(self):
                        return self

                    def __next__(self):
                        index = self.index
                        self.index += 1
                        state["events"].append({"next": index})
                        if index == len(values):
                            raise StopIteration
                        return values[index]

                def defrag(*args, **kwargs):
                    event(state, "defrag-iterable", args, kwargs)
                    return Items()
                core.urldefrag = defrag
            return setup
        for length in (2, 3, 1):
            compare(base, "child#frag", "boundary", iterable_defrag(length))
        checks["defrag_iterables_use_genuine_unpacking_and_preserve_returned_objects"] = True
    finally:
        restore()

        assert len(checks) == 11 and all(value is True for value in checks.values())
    return checks
