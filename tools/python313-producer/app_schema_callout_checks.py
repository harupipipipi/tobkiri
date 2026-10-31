"""Observe live callbacks with the actual default META and checker singleton."""


def callout_checks(provider, kernel) -> dict[str, bool]:
    """Compare genuine original operations; no input/META snapshot is a result."""
    from jsonschema import Draft202012Validator as original, exceptions
    from jsonschema import _keywords as keywords, _utils as utilities
    from schema_kernel_observation import first_difference, meta_nodes, observe

    meta = original.META_SCHEMA
    checker = original.FORMAT_CHECKER
    mapping = checker.checkers
    saved_regex = mapping["regex"]
    saved_keyword_uniq = keywords.uniq
    saved_exported_uniq = utilities.uniq
    checks = {}

    def graph(error):
        if error is None:
            return None
        return {"type": type(error).__module__ + "." + type(error).__qualname__,
                "message": str(error), "suppressed": error.__suppress_context__,
                "cause": graph(error.__cause__), "context": graph(error.__context__)}

    def run(selected, mode):
        actual = {"pattern": "marker", "required": ["alpha", "beta"]}
        if mode == "live-input":
            actual = {"pattern": "marker", "maxItems": 0}
        events = []
        fault = RuntimeError("default checker supplied failure")
        known = meta_nodes()
        before = kernel.app_schema_entry_count()

        def helper(values):
            events.append({"call": "keyword-helper", "same_required": values is actual["required"]})
            assert values is actual["required"]
            return False

        def exporter(values):
            events.append({"call": "exporter-helper"})
            raise AssertionError("rebinding the exporter must not change its imported alias")

        def regex(value):
            events.append({"call": "default-regex", "same_pattern": value is actual["pattern"]})
            assert value is actual["pattern"]
            if mode == "live-input":
                actual["maxItems"] = "bad"
            elif mode == "rebind-import":
                keywords.uniq = helper
            elif mode == "rebind-exporter":
                utilities.uniq = exporter
            elif mode == "exception":
                raise fault
            return True

        try:
            mapping["regex"] = (regex, ())
            if mode == "existing-import":
                keywords.uniq = helper
            # All nominal identities survive. No format_checker argument and
            # no replacement META/provider/registry is passed to either leg.
            assert original.META_SCHEMA is meta
            assert original.FORMAT_CHECKER is checker and checker.checkers is mapping
            try:
                selected.check_schema(actual)
            except exceptions.SchemaError as error:
                outcome = {"kind": "schema-error", "error": observe(error, actual, known)}
            except Exception as error:
                outcome = {"kind": "exception", "graph": graph(error), "is_fault": error is fault}
            else:
                outcome = {"kind": "valid"}
            assert original.META_SCHEMA is meta
            assert original.FORMAT_CHECKER is checker and checker.checkers is mapping
            assert kernel.app_schema_entry_count() - before == (selected is provider)
            assert events and events[0] == {"call": "default-regex", "same_pattern": True}
            if mode in ("existing-import", "rebind-import"):
                assert events == [events[0], {"call": "keyword-helper", "same_required": True}]
                assert outcome["kind"] == "schema-error"
            elif mode == "rebind-exporter":
                assert len(events) == 1 and outcome["kind"] == "valid"
            elif mode == "live-input":
                assert len(events) == 1 and actual["maxItems"] == "bad"
                assert outcome["kind"] == "schema-error"
            else:
                assert mode == "exception" and len(events) == 1
                assert outcome["kind"] == "exception" and outcome["is_fault"]
            return {"outcome": outcome, "events": events, "actual": actual}
        finally:
            try:
                mapping["regex"] = saved_regex
            finally:
                try:
                    keywords.uniq = saved_keyword_uniq
                finally:
                    utilities.uniq = saved_exported_uniq

    for mode in ("live-input", "existing-import", "rebind-import", "rebind-exporter", "exception"):
        expected = run(original, mode)
        actual = run(provider, mode)
        difference = first_difference(expected, actual)
        assert difference is None, (mode, difference)
        checks[mode] = True
    assert len(checks) == 5 and all(checks.values())
    return checks
