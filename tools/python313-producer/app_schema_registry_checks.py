"""Source-built Registry DATA comparisons, independent of fragment ownership."""


def registry_checks(provider, kernel) -> dict[str, bool]:
    import gc
    import sys
    import weakref

    from jsonschema import Draft202012Validator as original, FormatChecker, exceptions
    from referencing import Registry, Resource, _core
    from referencing.jsonschema import DRAFT202012
    from rpds import HashTrieMap
    from app_schema_model import (
        DefinitionModel, ROOT_URI, _registry_with_resources, _registry_crawl,
        _registry_combine, registry_root,
    )
    from schema_kernel_observation import first_difference, meta_nodes, observe

    checks = {}
    policy = kernel.app_registry_policy()
    root_uri = "https://registry.invalid/root"
    child_uri = "https://registry.invalid/child"
    fields = ("primary_resource_hits", "primary_anchor_hits", "opaque_lookups")

    def counts():
        value = kernel.app_registry_counts()
        assert set(value) == set(fields)
        assert all(type(value[name]) is int and value[name] >= 0 for name in fields)
        return value

    def delta(before):
        after = counts()
        assert all(after[name] >= before[name] for name in fields)
        return {name: after[name] - before[name] for name in fields}

    def compare(label, make, *, native_hits=False, opaque=False):
        observations = []
        for native in (False, True):
            before = counts()
            observations.append(make(native))
            change = delta(before)
            if native and native_hits:
                assert change["primary_resource_hits"] > 0, label
            if native and opaque:
                assert change["primary_resource_hits"] == 0, label
                assert change["opaque_lookups"] > 0, label
        difference = first_difference(*observations)
        assert difference is None, {"case": label, "difference": difference,
                                    "observations": observations}
        checks[label] = True

    def build(native, pairs, *, crawl=True, retrieve=None):
        if native:
            state = policy.empty() if retrieve is None else policy.with_retrieve(retrieve)
            state = _registry_with_resources(state, pairs)
            return _registry_crawl(state) if crawl else state
        registry = Registry() if retrieve is None else Registry(retrieve=retrieve)
        registry = registry.with_resources(pairs)
        return registry.crawl() if crawl else registry

    def actual_registry(native, value):
        return value.registry if native else value

    def resource(native, value, uri):
        return value._resource(uri) if native else value.get_or_retrieve(uri)

    def anchor(native, value, uri, name):
        return value._anchor(uri, name) if native else value.anchor(uri, name)

    def documents():
        child = {"$id": child_uri, "$anchor": "named", "$dynamicAnchor": "dyn",
                 "type": "integer"}
        root = {"$id": root_uri, "$anchor": "root", "$defs": {"child": child}}
        return root, child

    def inspect(native, value, root, child):
        registry = actual_registry(native, value)
        rows = []
        for uri, expected in ((root_uri, root), (child_uri, child)):
            retrieved = resource(native, value, uri)
            assert retrieved.registry is registry
            assert retrieved.value.contents is expected
            rows.append([uri, retrieved.value.contents.get("type"),
                         type(retrieved.value).__name__])
        for uri, name, expected in ((root_uri, "root", root),
                                    (child_uri, "named", child),
                                    (child_uri, "dyn", child)):
            retrieved = anchor(native, value, uri, name)
            assert retrieved.registry is registry
            assert retrieved.value.resource.contents is expected
            rows.append([uri, name, type(retrieved.value).__name__])
        return rows

    # A fresh source-built definition graph must reach the actual invocation
    # root path; original-lane state is never rewritten for the provider leg.
    outcomes = []
    before = counts()
    fresh = DefinitionModel(policy)
    saved = {name: getattr(provider, name) for name in (
        "META_SCHEMA", "_SPECIFICATIONS", "_REGISTRY", "_PRIMARY_REGISTRY")}
    try:
        provider.META_SCHEMA = fresh.meta
        provider._SPECIFICATIONS = fresh.specifications
        provider._REGISTRY = fresh.registry
        provider._PRIMARY_REGISTRY = fresh.registry_state
        for selected in (original, provider):
            actual = {"properties": {"bad": {"type": 42}}}
            try:
                selected.check_schema(actual, format_checker=None)
            except exceptions.SchemaError as error:
                outcomes.append(observe(error, actual, meta_nodes(selected)))
            else:
                raise AssertionError("fresh default graph accepted invalid type")
        assert first_difference(*outcomes) is None
        change = delta(before)
        assert change["primary_resource_hits"] > 0
        assert change["primary_anchor_hits"] > 0
        checks["fresh_default_root_reaches_primary_resource_and_anchor_bindings"] = True
    finally:
        for name, value in saved.items():
            setattr(provider, name, value)

    def crawl_callbacks(native):
        events = []
        root, child = documents()
        labels = {id(root): "root", id(child): "child"}
        methods = {name: Resource.__dict__[name] for name in ("id", "anchors", "subresources")}
        def wrapper(name):
            def call(self):
                events.append([name, labels.get(id(self.contents), "nested")])
                if name == "id" and self.contents is root:
                    root["$defs"]["late"] = {"$id": "late", "type": "boolean"}
                return methods[name](self)
            return call
        try:
            for name in methods:
                setattr(Resource, name, wrapper(name))
            value = build(native, [(root_uri, DRAFT202012.create_resource(root))])
            child["type"] = "string"
            root["$defs"]["child"] = {"type": "null"}
            observed = inspect(native, value, root, child)
            late = resource(native, value, "https://registry.invalid/late")
            assert late.value.contents is root["$defs"]["late"]
            return {"events": events, "observed": observed}
        finally:
            for name, method in methods.items():
                setattr(Resource, name, method)
    compare("crawl_records_all_actual_writes_and_live_bound_child_objects",
            crawl_callbacks, native_hits=True)

    def priority(native):
        one, two = {"type": "integer"}, {"type": "string"}
        three = {"type": "boolean"}
        key_one = root_uri.encode().decode()
        key_two = (" " + root_uri)[1:]
        assert type(key_one) is type(key_two) is str and key_one == key_two
        assert key_one is not key_two
        value = build(native, [(key_one, DRAFT202012.create_resource(one)),
                               (key_two, DRAFT202012.create_resource(two))], crawl=False)
        assert resource(native, value, root_uri).value.contents is two
        registry = actual_registry(native, value)
        # Observe actual map iteration/key identity, never the native table.
        stored = next(iter(registry._resources))
        key_identity = [stored is key_one, stored is key_two]
        other = build(native, [(root_uri, DRAFT202012.create_resource(three))], crawl=False)
        value = _registry_combine(value, (other,)) if native else value.combine(other)
        assert resource(native, value, root_uri).value.contents is three
        return {"key_identity": key_identity, "winner": "boolean"}
    compare("initial_duplicate_and_combine_priority_preserve_actual_key_objects",
            priority, native_hits=True)

    def retrieval_selection(native):
        events, returned = [], {"type": "integer"}
        def selected(uri):
            events.append(["retrieve", uri])
            return DRAFT202012.create_resource(returned)
        base = build(native, [])
        remote = build(native, [], retrieve=selected)
        combined = _registry_combine(base, (remote,)) if native else base.combine(remote)
        found = resource(native, combined, "https://registry.invalid/remote")
        assert found.value.contents is returned
        def conflicting(uri):
            raise AssertionError("conflicting retriever ran")
        other = build(native, [], retrieve=conflicting)
        try:
            _registry_combine(combined, (other,)) if native else combined.combine(other)
        except ValueError as error:
            failure = [type(error).__name__, str(error)]
        else:
            raise AssertionError("retrieval conflict was silently accepted")
        return {"events": events, "failure": failure}
    compare("actual_retrieval_selection_conflict_and_pure_miss_once", retrieval_selection)

    def changed_items(native, *, replace):
        root, child = documents()
        value = build(native, [(root_uri, DRAFT202012.create_resource(root))])
        remote = build(native, [])
        view_type = type(HashTrieMap().items())
        saved_iter = view_type.__dict__["__iter__"]
        events = []
        injected = DRAFT202012.create_resource({"type": "null"})
        def changed(self):
            events.append("items-iter")
            if replace:
                return iter([("https://registry.invalid/injected", injected)])
            return saved_iter(self)
        try:
            view_type.__iter__ = changed
            combined = _registry_combine(value, (remote,)) if native else value.combine(remote)
        finally:
            view_type.__iter__ = saved_iter
        if replace:
            found = resource(native, combined, "https://registry.invalid/injected")
            assert found.value is injected
            # Only the resources map is queried: the same changed iterator was
            # also used for anchors, and its intentionally foreign rows stay DATA.
            observed = "injected"
        else:
            observed = inspect(native, combined, root, child)
        return {"events": events, "observed": observed}
    compare("empty_bulk_items_iterator_override_is_called_once_and_stays_opaque",
            lambda native: changed_items(native, replace=False), opaque=True)
    compare("canonical_bulk_outer_cannot_admit_changed_internal_iterator_output",
            lambda native: changed_items(native, replace=True), opaque=True)

    def foreign_stored(native):
        events = []
        class Foreign(str):
            def rstrip(self, chars=None):
                events.append("rstrip")
                return self
            def __hash__(self):
                events.append("hash")
                return str.__hash__(self)
            def __eq__(self, other):
                events.append("equal")
                return str.__eq__(self, other)
        document = {"type": "integer"}
        value = build(native, [(Foreign(root_uri), DRAFT202012.create_resource(document))],
                      crawl=False)
        found = resource(native, value, root_uri)
        assert found.value.contents is document
        return events
    compare("foreign_stored_key_never_gets_scanned_or_native_equality_probed",
            foreign_stored, opaque=True)

    def foreign_query(native):
        document = {"type": "integer"}
        value = build(native, [(root_uri, DRAFT202012.create_resource(document))], crawl=False)
        events = []
        class Foreign(str):
            def __hash__(self):
                events.append("hash")
                return str.__hash__(self)
        found = resource(native, value, Foreign(root_uri))
        assert found.value.contents is document
        return events
    compare("foreign_query_goes_directly_to_the_single_actual_method",
            foreign_query, opaque=True)

    def member_descriptor(native):
        document = {"type": "integer"}
        value = build(native, [(root_uri, DRAFT202012.create_resource(document))], crawl=False)
        saved_member = Registry.__dict__["_resources"]
        events = []
        class Descriptor:
            def __get__(self, receiver, owner):
                if receiver is None:
                    return self
                events.append("resources-get")
                return saved_member.__get__(receiver, owner)
            def __set__(self, receiver, value):
                saved_member.__set__(receiver, value)
        try:
            Registry._resources = Descriptor()
            found = resource(native, value, root_uri)
            assert found.value.contents is document
        finally:
            Registry._resources = saved_member
        assert events == ["resources-get"]
        return events
    compare("custom_member_is_not_read_twice_by_probe_then_fallback",
            member_descriptor, opaque=True)

    def live_namespace(native):
        document = {"type": "integer"}
        value = build(native, [(root_uri, DRAFT202012.create_resource(document))], crawl=False)
        saved_get = HashTrieMap.__dict__["get"]
        events = []
        class Descriptor:
            def __get__(self, receiver, owner):
                if receiver is None:
                    return self
                events.append("map-get-bind")
                actual = saved_get.__get__(receiver, owner)
                def call(uri):
                    events.append("map-get-call")
                    return actual(uri)
                return call
        try:
            HashTrieMap.get = Descriptor()
            assert resource(native, value, root_uri).value.contents is document
        finally:
            HashTrieMap.get = saved_get
        assert events == ["map-get-bind", "map-get-call"]
        return events
    compare("retained_namespace_is_live_and_custom_map_get_binds_only_at_source_point",
            live_namespace, opaque=True)

    def metaclass_namespace(native):
        document = {"type": "integer"}
        value = build(native, [(root_uri, DRAFT202012.create_resource(document))], crawl=False)
        metaclass = type(Registry)
        saved_own = metaclass.__dict__.get("__getattribute__")
        saved_get = metaclass.__getattribute__
        events = []
        def get(self, name):
            if self is Registry:
                events.append(name)
                raise AssertionError("lookup eligibility invoked a metaclass getter")
            return saved_get(self, name)
        try:
            metaclass.__getattribute__ = get
            assert resource(native, value, root_uri).value.contents is document
        finally:
            if saved_own is None:
                del metaclass.__getattribute__
            else:
                metaclass.__getattribute__ = saved_own
        assert not events
        return events
    compare("namespace_eligibility_bypasses_metaclass_user_getters",
            metaclass_namespace, native_hits=True)

    def update_exception(native):
        document = {"type": "integer"}
        base = build(native, [(root_uri, DRAFT202012.create_resource(document))], crawl=False)
        other = build(native, [])
        events = []
        class Fault(Exception):
            pass
        fault = Fault("actual update refusal")
        saved_update = HashTrieMap.__dict__["update"]
        class Descriptor:
            def __get__(self, receiver, owner):
                if receiver is None:
                    return self
                events.append("update-bind")
                def call(other):
                    events.append("update-call")
                    raise fault
                return call
        try:
            HashTrieMap.update = Descriptor()
            try:
                _registry_combine(base, (other,)) if native else base.combine(other)
            except Fault as error:
                assert error is fault
                outcome = [type(error).__name__, str(error)]
                error.__traceback__ = None
            else:
                raise AssertionError("actual update exception was suppressed")
        finally:
            HashTrieMap.update = saved_update
        assert resource(native, base, root_uri).value.contents is document
        return {"events": events, "outcome": outcome}
    compare("actual_bulk_exception_has_no_retry_or_published_native_commit",
            update_exception, native_hits=True)

    def root_inner_override(native, *, fail):
        root, child = documents()
        value = build(native, [(root_uri, DRAFT202012.create_resource(root))])
        registry = actual_registry(native, value)
        resource_value = DRAFT202012.create_resource({"$id": "https://registry.invalid/invocation"})
        saved_inner = Registry.__dict__["with_resource"]
        saved_id = Resource.__dict__["id"]
        events = []
        class Fault(Exception):
            pass
        fault = Fault("inner root refusal")
        def identifier(self):
            if self is resource_value:
                events.append("root-id")
            return saved_id(self)
        class Descriptor:
            def __get__(self, receiver, owner):
                if receiver is None:
                    return self
                events.append("inner-bind")
                def call(uri, actual):
                    assert actual is resource_value
                    events.append(["inner-call", uri])
                    if fail:
                        raise fault
                    return saved_inner(receiver, uri, actual)
                return call
        try:
            Resource.id = identifier
            Registry.with_resource = Descriptor()
            try:
                if native:
                    resolver, owned = registry_root(registry, resource_value, value)
                    assert owned is None, "custom inner return was promoted from its shape"
                else:
                    resolver = registry.resolver_with_root(resource_value)
                found = resolver.lookup(child_uri)
                assert found.contents is child
                outcome = "resolved"
            except Fault as error:
                assert fail and error is fault
                outcome = str(error)
                error.__traceback__ = None
        finally:
            Resource.id = saved_id
            Registry.with_resource = saved_inner
        return {"events": events, "outcome": outcome}
    compare("outer_canonical_root_with_custom_inner_return_stays_opaque",
            lambda native: root_inner_override(native, fail=False))
    compare("root_id_binding_and_actual_inner_exception_keep_source_order",
            lambda native: root_inner_override(native, fail=True))

    def retrieved_boundary(native, *, fail):
        root, child = documents()
        value = build(native, [(root_uri, DRAFT202012.create_resource(root))])
        registry = actual_registry(native, value)
        events = []
        saved_retrieved = _core.Retrieved
        class Fault(Exception):
            pass
        fault = Fault("actual Retrieved constructor refusal")
        class Returned:
            def __init__(self, actual):
                self.actual = actual
            @property
            def value(self):
                events.append("value-read")
                return self.actual.value
            @property
            def registry(self):
                events.append("registry-read")
                return self.actual.registry
        def constructor(**kwargs):
            events.append(["construct", list(kwargs)])
            assert kwargs["registry"] is registry
            if fail:
                raise fault
            return Returned(saved_retrieved(**kwargs))
        try:
            _core.Retrieved = constructor
            try:
                found = resource(native, value, child_uri)
                assert found.registry is registry
                assert found.value.contents is child
                found = anchor(native, value, child_uri, "named")
                assert found.registry is registry
                assert found.value.resource.contents is child
                outcome = "actual fields"
            except Fault as error:
                assert fail and error is fault
                outcome = str(error)
                error.__traceback__ = None
        finally:
            _core.Retrieved = saved_retrieved
        return {"events": events, "outcome": outcome}
    compare("actual_Retrieved_constructor_kwargs_and_public_field_reads",
            lambda native: retrieved_boundary(native, fail=False), native_hits=True)
    compare("actual_Retrieved_constructor_supplied_exception_identity",
            lambda native: retrieved_boundary(native, fail=True), native_hits=True)

    # Exercise the actual production ReferenceState field reads as well as the
    # bounded construction DATA observer. In particular a NoSuchAnchor raised
    # by .value belongs inside DynamicAnchor.resolve's original try block.
    saved_retrieved = _core.Retrieved
    field_observations = []
    for selected in (original, provider):
        events = []
        class Returned:
            def __init__(self, actual):
                self.actual = actual
            @property
            def registry(self):
                events.append("registry-read")
                return self.actual.registry
            @property
            def value(self):
                value = self.actual.value
                events.append(["value-read", type(value).__name__])
                if (type(value).__name__ == "DynamicAnchor"
                        and value.resource.contents.get("$id") == ROOT_URI):
                    from referencing.exceptions import NoSuchAnchor
                    events.append("root-value-refused")
                    raise NoSuchAnchor(ref=ROOT_URI, resource=value.resource, anchor="meta")
                return value
        def constructor(**kwargs):
            events.append(["construct", list(kwargs)])
            return Returned(saved_retrieved(**kwargs))
        actual = {"type": 42, "properties": {"bad": {"type": "integer"}}}
        try:
            _core.Retrieved = constructor
            try:
                selected.check_schema(actual, format_checker=None)
            except exceptions.SchemaError as error:
                outcome = observe(error, actual, meta_nodes(selected))
            else:
                raise AssertionError("invalid nested schema was accepted")
        finally:
            _core.Retrieved = saved_retrieved
        assert "root-value-refused" in events
        field_observations.append({"events": events, "outcome": outcome})
    assert first_difference(*field_observations) is None, field_observations
    checks["production_Retrieved_fields_and_dynamic_value_exception_catch_are_preserved"] = True

    # GC owns each actual Python edge exactly once, even with many shared
    # native snapshots. No assertions depend on RPDS's untracked map contents.
    document = {"type": "integer"}
    actual_resource = DRAFT202012.create_resource(document)
    local_policy = kernel.app_registry_policy()
    state = _registry_with_resources(local_policy.empty(), [(root_uri, actual_resource)])
    def referent(value, name, payload=None):
        for item in gc.get_referents(value):
            if type(item).__name__ == name:
                if payload is None or any(child is payload for child in gc.get_referents(item)):
                    return item
        raise AssertionError("GC-visible owner is missing: " + name)
    rows = referent(state, "Rows", actual_resource)
    metadata = referent(state, "RegistryMetadata")
    rows_before, meta_before = sys.getrefcount(rows), sys.getrefcount(metadata)
    resource_before = sys.getrefcount(actual_resource)
    snapshots = [state.fork() for _ in range(16)]
    assert sys.getrefcount(rows) == rows_before + 16
    assert sys.getrefcount(metadata) == meta_before + 16
    assert sys.getrefcount(actual_resource) == resource_before
    cycle = [snapshots]
    cycle.append(cycle)
    del snapshots
    gc.collect()
    assert resource(True, state, root_uri).value is actual_resource
    cycle.clear()
    del cycle
    gc.collect()
    assert sys.getrefcount(rows) == rows_before
    assert sys.getrefcount(metadata) == meta_before
    assert sys.getrefcount(actual_resource) == resource_before
    checks["shared_gc_holders_have_actual_refcounted_edges_without_flattening"] = True

    before = counts()
    saved_retrieved = _core.Retrieved
    def while_view(**kwargs):
        assert sys.getrefcount(rows) == rows_before + 1
        assert sys.getrefcount(metadata) == meta_before + 1
        gc.collect()
        return saved_retrieved(**kwargs)
    try:
        _core.Retrieved = while_view
        found = state._resource(root_uri)
        assert found.value is actual_resource
        del found
    finally:
        _core.Retrieved = saved_retrieved
    assert delta(before)["primary_resource_hits"] > 0
    assert sys.getrefcount(rows) == rows_before
    assert sys.getrefcount(metadata) == meta_before
    rows_weak, metadata_weak = weakref.ref(rows), weakref.ref(metadata)
    del state, local_policy, rows, metadata
    gc.collect()
    assert rows_weak() is None and metadata_weak() is None
    # The canonical holder is a separate real static Python root: creating a
    # new independent policy after that collection still yields valid metadata.
    another = kernel.app_registry_policy().empty()
    assert type(another.registry) is Registry
    del another
    checks["pure_rust_view_has_real_holder_roots_and_drop_reclaims_dynamic_holders"] = True

    cleanup_observations = []
    for selected in (original, provider):
        saved_meta = selected.META_SCHEMA
        events = []
        class Meta(dict):
            pass
        class Node:
            def __del__(self):
                try:
                    selected.check_schema({"type": "integer"}, format_checker=None)
                    events.append("cleanup-reentered")
                except BaseException as error:
                    events.append(["cleanup-error", type(error).__name__, str(error)])
        node = Node()
        cycle = [node]
        cycle.append(cycle)
        meta = Meta({"$id": "https://registry.invalid/weak-root",
                     "allOf": [{"format": "held-root"},
                               {"$ref": "https://json-schema.org/draft/2020-12/meta/validation"}],
                     "ignored": cycle})
        meta_weak, node_weak = weakref.ref(meta), weakref.ref(node)
        checker = FormatChecker(formats=[])
        def check(actual):
            selected.META_SCHEMA = {"type": "null"}
            gc.collect()
            assert meta_weak() is not None and node_weak() is not None
            events.append("bound-root-alive")
            return True
        checker.checks("held-root")(check)
        selected.META_SCHEMA = meta
        del node, meta, cycle
        try:
            try:
                selected.check_schema({"minimum": "bad"}, format_checker=checker)
            except exceptions.SchemaError as error:
                # The actual error can retain its Python caller frame until
                # this original traceback is released; do not claim otherwise.
                error.__traceback__ = None
            else:
                raise AssertionError("invalid minimum was accepted")
        finally:
            selected.META_SCHEMA = saved_meta
        gc.collect()
        assert meta_weak() is None and node_weak() is None
        assert events == ["bound-root-alive", "cleanup-reentered"], events
        cleanup_observations.append(events)
    assert first_difference(*cleanup_observations) is None
    checks["invocation_view_keeps_live_root_then_releases_cycles_with_cleanup_reentry"] = True

    class FakeState:
        def __getattribute__(self, name):
            raise AssertionError("fake construction metadata was inspected")
    for fake in (object(), FakeState()):
        before = kernel.app_schema_entry_count()
        try:
            kernel.app_check_schema({}, {}, provider._REGISTRY,
                                    kernel.default_reference_policy(), provider.TYPE_CHECKER,
                                    None, registry_state=fake)
        except TypeError:
            pass
        else:
            raise AssertionError("arbitrary registry state entered validation")
        assert kernel.app_schema_entry_count() == before
    checks["arbitrary_registry_state_rejected_before_input_evaluation"] = True
    return checks
