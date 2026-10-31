"""Observe live callbacks with the actual default META and checker singleton."""


def callout_checks(provider, kernel) -> dict[str, bool]:
    """Compare genuine original operations; no input/META snapshot is a result."""
    from jsonschema import Draft202012Validator as original, exceptions
    from jsonschema import _keywords as keywords, _utils as utilities
    from schema_kernel_observation import first_difference, meta_nodes, observe

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
        meta = selected.META_SCHEMA
        checker = selected.FORMAT_CHECKER
        mapping = checker.checkers
        saved_regex = mapping["regex"]
        actual = {"pattern": "marker", "required": ["alpha", "beta"]}
        if mode == "live-input":
            actual = {"pattern": "marker", "maxItems": 0}
        events = []
        fault = RuntimeError("default checker supplied failure")
        known = meta_nodes(selected)
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
            assert selected.META_SCHEMA is meta
            assert selected.FORMAT_CHECKER is checker and checker.checkers is mapping
            try:
                selected.check_schema(actual)
            except exceptions.SchemaError as error:
                outcome = {"kind": "schema-error", "error": observe(error, actual, known)}
            except Exception as error:
                outcome = {"kind": "exception", "graph": graph(error), "is_fault": error is fault}
            else:
                outcome = {"kind": "valid"}
            assert selected.META_SCHEMA is meta
            assert selected.FORMAT_CHECKER is checker and checker.checkers is mapping
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


def owned_model_checks(provider, kernel) -> dict[str, bool]:
    """Check source reconstruction, owner isolation and selected live state."""
    from app_schema_model import DefinitionModel, ROOT_URI, SOURCE_DOCUMENTS
    from jsonschema import Draft202012Validator as original, validators, exceptions
    from schema_kernel_observation import node_digest

    def nodes(value):
        found = set()
        def visit(item):
            if type(item) not in (dict, list) or id(item) in found:
                return
            found.add(id(item))
            for child in (item.values() if type(item) is dict else item):
                visit(child)
        visit(value)
        return found

    own = provider._SPECIFICATIONS
    old = validators.SPECIFICATIONS
    checks = {}
    assert len(SOURCE_DOCUMENTS) == len(own) == 8
    assert node_digest(provider.META_SCHEMA) == node_digest(original.META_SCHEMA)
    assert provider.META_SCHEMA is not original.META_SCHEMA
    assert provider.META_SCHEMA is not own.contents(ROOT_URI)
    assert provider.META_SCHEMA["allOf"] is own.contents(ROOT_URI)["allOf"]
    actual_nodes = nodes(provider.META_SCHEMA)
    old_nodes = nodes(original.META_SCHEMA)
    for uri in own:
        assert node_digest(own.contents(uri)) == node_digest(old.contents(uri))
        assert provider._REGISTRY[uri] is own[uri]
        actual_nodes.update(nodes(own.contents(uri)))
        old_nodes.update(nodes(old.contents(uri)))
    assert actual_nodes.isdisjoint(old_nodes)
    checks["source_documents_and_owned_graph_have_no_original_container_aliases"] = True
    assert provider.FORMAT_CHECKER is not original.FORMAT_CHECKER
    assert provider.FORMAT_CHECKER.checkers is not original.FORMAT_CHECKER.checkers
    assert provider.FORMAT_CHECKER.checkers == original.FORMAT_CHECKER.checkers
    checks["independent_registration_table_retains_actual_callbacks_and_exception_classes"] = True

    saved_meta, saved_checker = original.META_SCHEMA, original.FORMAT_CHECKER
    saved_type = original.TYPE_CHECKER
    core = old.contents("https://json-schema.org/draft/2020-12/meta/core")
    anchor = core["$defs"]["anchorString"]
    saved_anchor_type = anchor["type"]
    saved_regex = saved_checker.checkers["regex"]
    before = kernel.app_schema_entry_count()
    try:
        anchor["type"] = "integer"
        def forbidden(value):
            raise AssertionError("unselected Original checker was invoked")
        saved_checker.checkers["regex"] = (forbidden, ())
        original.META_SCHEMA = {"type": "integer"}
        original.FORMAT_CHECKER = None
        original.TYPE_CHECKER = None
        # Construct AFTER those mutations, from declared source DATA, not an
        # earlier deepcopy or the unselected class/registry/checker state.
        rebuilt = DefinitionModel()
        assert node_digest(rebuilt.meta) == node_digest(provider.META_SCHEMA)
        assert rebuilt.specifications.contents(
            "https://json-schema.org/draft/2020-12/meta/core"
        )["$defs"]["anchorString"]["type"] == "string"
        assert rebuilt.checker.checkers == provider.FORMAT_CHECKER.checkers
        provider.check_schema({"$anchor": "good"})
        provider.check_schema({"pattern": "^[a-z]+$"})
        assert kernel.app_schema_entry_count() == before + 2
        checks["unselected_original_class_graph_and_checker_changes_do_not_flow_in"] = True
    finally:
        try:
            anchor["type"] = saved_anchor_type
        finally:
            saved_checker.checkers["regex"] = saved_regex
            original.META_SCHEMA = saved_meta
            original.FORMAT_CHECKER = saved_checker
            original.TYPE_CHECKER = saved_type

    saved_owned = provider.META_SCHEMA
    replacement = {"type": "integer"}
    try:
        provider.META_SCHEMA = replacement
        provider.check_schema(1, format_checker=None)
        try:
            provider.check_schema("bad", format_checker=None)
        except exceptions.SchemaError as error:
            assert error.schema is replacement and error.instance == "bad"
        else:
            raise AssertionError("selected replacement META was ignored")
        assert original.META_SCHEMA is saved_meta
        checks["selected_provider_replacement_meta_and_error_reference_are_live"] = True
    finally:
        provider.META_SCHEMA = saved_owned
    assert len(checks) == 4 and all(checks.values())
    return checks
