"""Actual guest observations for the port's lazy public API, not product setup."""

from schema_urljoin_checks import exercise_urljoin
from schema_descend_checks import exercise_descend, _original_keywords
from schema_callable_checks import (exercise_callable_contracts,
                                    exercise_callable_diagnostics)


def exercise_api():
    """Exercise real validators/callbacks and retain their original objects."""
    from jsonschema import Draft202012Validator, exceptions, validators

    events = []
    children = []

    def callback(validator, value, instance, schema):
        def errors():
            try:
                events.append("first")
                yield exceptions.ValidationError("first")
                events.append("second")
                yield exceptions.ValidationError("second")
            finally:
                events.append("closed")

        child = errors()
        children.append(child)
        return child

    extended = validators.extend(Draft202012Validator, {"native-test": callback})
    schema, instance = {"native-test": True}, {"actual": []}
    validator = extended(schema)
    cursor = validator.iter_errors(instance)
    assert events == [] and children == []
    first = next(cursor)
    assert events == ["first"]
    assert type(first) is exceptions.ValidationError
    assert first.instance is instance and first.schema is schema
    assert first.validator_value is schema["native-test"]
    second = cursor.send("ignored by the original traversal's for loop")
    assert events == ["first", "second"] and second.message == "second"
    assert second.schema is schema and second.instance is instance
    try:
        next(cursor)
    except StopIteration:
        pass
    else:
        raise AssertionError("native cursor did not finish")
    assert events == ["first", "second", "closed"]

    # Original iter_errors uses for, not yield-from: closing its outer generator
    # must not close a callback iterator retained elsewhere.
    events.clear()
    cursor = validator.iter_errors(instance)
    next(cursor)
    child = children[-1]
    cursor.close()
    assert events == ["first"]
    assert next(child).message == "second"
    child.close()
    assert events == ["first", "second", "closed"]

    # Independent iterators retain the same real Validator but separate progress.
    events.clear()
    left, right = validator.iter_errors(instance), validator.iter_errors(instance)
    assert next(left).message == "first" and next(right).message == "first"
    assert next(left).message == "second" and next(right).message == "second"
    left.close()
    right.close()
    for child in children:
        child.close()

    import gc
    import weakref

    delegated = []
    custom = validators.extend(Draft202012Validator, {})
    original_descend = custom.descend

    def retain_descend(self, *args, **kwargs):
        child = original_descend(self, *args, **kwargs)
        delegated.append(child)
        return child

    custom.descend = retain_descend
    cursor = custom({"properties": {"x": {"type": "integer"}}}).iter_errors({"x": "bad"})
    del cursor
    assert delegated == []
    cursor = custom({"properties": {"x": {"type": "integer"}}}).iter_errors({"x": "bad"})
    next(cursor)
    assert len(delegated) == 1 and delegated[0].gi_frame is not None
    cursor.close()
    assert delegated[0].gi_frame is None

    cursor = custom({"properties": {"x": {"type": "integer"}}}).iter_errors({"x": "bad"})
    next(cursor)
    retained_child = delegated[-1]
    del cursor
    assert retained_child.gi_frame is None

    cyclic_instance = {"x": "bad"}
    cursor = custom({"properties": {"x": {"type": "integer"}}}).iter_errors(cyclic_instance)
    next(cursor)
    retained_child = delegated[-1]
    cyclic_instance["cycle"] = cursor
    del cyclic_instance, cursor
    gc.collect()
    assert retained_child.gi_frame is None

    list(custom({"properties": {"x": {"type": "integer"}}}).iter_errors({"x": "bad"}))
    assert delegated[-1].gi_frame is None

    def stopped(validator, value, instance, schema):
        raise StopIteration("callback exception")

    exceptional = validators.extend(Draft202012Validator, {"native-stop": stopped})
    cursor = exceptional({"native-stop": True}).iter_errors(instance)
    try:
        next(cursor)
    except RuntimeError as error:
        assert str(error) == "generator raised StopIteration"
        assert type(error.__cause__) is StopIteration
        assert error.__context__ is error.__cause__
    else:
        raise AssertionError("callback StopIteration was swallowed")
    try:
        next(cursor)
    except StopIteration:
        pass
    else:
        raise AssertionError("failed generator resumed")

    error = next(validator.descend(instance, False, path="unused", schema_path="unused"))
    assert error.schema is False and error.instance is instance
    assert list(error.path) == [] and list(error.schema_path) == []
    evolved_schema = {"type": "integer"}
    evolved = validator.evolve(schema=evolved_schema)
    assert type(evolved) is extended and evolved.schema is evolved_schema

    ephemeral = validators.extend(Draft202012Validator, {})
    original_class = weakref.ref(ephemeral)
    del ephemeral
    gc.collect()
    assert original_class() is None

    exercise_types()
    exercise_evolve()
    exercise_post_init()
    urljoin_checks = exercise_urljoin()
    descend_checks = exercise_descend()
    callable_checks = exercise_callable_contracts()
    return {
        "lazy_start_and_resume": True,
        "actual_error_objects_and_references": True,
        "outer_close_keeps_retained_callback_open": True,
        "interleaved_iterators": True,
        "pep479_and_failed_iterator_closed": True,
        "false_descend_preserves_original_early_paths": True,
        "evolve_preserves_actual_class_and_schema": True,
        "unregistered_extension_class_not_cached": True,
        "keyword_delegation_closes_retained_child": True,
        "keyword_deallocation_closes_retained_child": True,
        "keyword_cycle_collection_closes_retained_child": True,
        "unstarted_keyword_does_not_acquire_child": True,
        "default_type_dispatch_matches_original": True,
        "evolve_order_live_aliases_and_name_resolution": True,
        "post_init_order_live_callbacks_and_state": True,
        **urljoin_checks,
        **descend_checks,
        **callable_checks,
    }


def exercise_implementation_diagnostics():
    """Record additional Python-function hypotheses outside product requirements."""
    return {
        "scope": "additional private Python-function compatibility hypotheses",
        "required_contract_checks": False,
        "observations": exercise_callable_diagnostics(),
    }


def exercise_types():
    """Compare real return objects/exceptions with the original type method."""
    import numbers
    from fractions import Fraction
    from jsonschema import Draft202012Validator, exceptions

    schema = {}
    validator = Draft202012Validator(schema)
    method = Draft202012Validator.is_type
    original = getattr(method, "__wrapped__", method)

    class ListSubclass(list):
        pass

    class DictSubclass(dict):
        pass

    class StrSubclass(str):
        pass

    values = [None, True, False, 1, 10 ** 300, 1.0, 1.2, float("nan"),
              float("inf"), -float("inf"), complex(1, 2), Fraction(1, 2),
              [], {}, "x", (), ListSubclass(), DictSubclass(), StrSubclass("x")]
    for value in values:
        for kind in ("array", "object", "string", "boolean", "null", "integer", "number"):
            assert validator.is_type(value, kind) is original(validator, value, kind)

    # The original integral-float predicate may return a subclass's arbitrary
    # result object. Integer fast dispatch must not normalize that to bool.
    marker, calls = object(), []

    class FloatSubclass(float):
        def is_integer(self):
            calls.append("integer")
            return marker

    value = FloatSubclass(1)
    assert validator.is_type(value, "integer") is marker
    assert original(validator, value, "integer") is marker
    assert calls == ["integer", "integer"]

    raised = ValueError("integer override")

    class RaisingFloat(float):
        def is_integer(self):
            raise raised

    for check in (validator.is_type, lambda value, kind: original(validator, value, kind)):
        try:
            check(RaisingFloat(1), "integer")
        except ValueError as error:
            assert error is raised
        else:
            raise AssertionError("integer subclass exception was lost")

    class RegisteredNumber:
        pass

    value = RegisteredNumber()
    assert validator.is_type(value, "number") is original(validator, value, "number") is False
    numbers.Number.register(RegisteredNumber)
    assert validator.is_type(value, "number") is original(validator, value, "number") is True
    numbers.Number._abc_caches_clear()
    assert validator.is_type(value, "number") is original(validator, value, "number") is True

    def outcome(check, kind):
        try:
            result = check(1, kind)
        except Exception as error:
            if isinstance(error, exceptions.UnknownType):
                assert error.instance is not None and error.schema is schema
            return type(error), str(error), error.__suppress_context__
        return result

    for kind in ("unknown", "\ud800", StrSubclass("integer")):
        assert outcome(validator.is_type, kind) == outcome(
            lambda value, name: original(validator, value, name), kind
        )

    saved = Draft202012Validator.TYPE_CHECKER
    custom_calls = []

    def custom(checker, value):
        assert checker is Draft202012Validator.TYPE_CHECKER
        custom_calls.append(value)
        return value is marker

    Draft202012Validator.TYPE_CHECKER = saved.redefine("integer", custom)
    try:
        assert validator.is_type(marker, "integer") is True
        assert original(validator, marker, "integer") is True
        assert custom_calls == [marker, marker]
    finally:
        Draft202012Validator.TYPE_CHECKER = saved

    reads = []
    lookup_missing = "__getattribute__" not in Draft202012Validator.__dict__
    original_lookup = Draft202012Validator.__getattribute__

    def lookup(self, name):
        if name == "TYPE_CHECKER":
            reads.append(name)
        return original_lookup(self, name)

    Draft202012Validator.__getattribute__ = lookup
    Draft202012Validator.TYPE_CHECKER = saved.redefine("integer", custom)
    try:
        assert validator.is_type(marker, "integer") is True
        assert reads == ["TYPE_CHECKER"]
        reads.clear()
        assert original(validator, marker, "integer") is True
        assert reads == ["TYPE_CHECKER"]
    finally:
        Draft202012Validator.TYPE_CHECKER = saved
        if lookup_missing:
            del Draft202012Validator.__getattribute__
        else:
            Draft202012Validator.__getattribute__ = original_lookup

    fail_lookup = [False]

    class CheckerDescriptor:
        def __get__(self, instance, owner):
            reads.append("descriptor")
            if fail_lookup[0]:
                raise raised
            return saved

    Draft202012Validator.TYPE_CHECKER = CheckerDescriptor()
    try:
        reads.clear()
        assert validator.is_type(1, "integer") is True
        assert reads == ["descriptor"]
        reads.clear()
        assert original(validator, 1, "integer") is True
        assert reads == ["descriptor"]
        fail_lookup[0] = True
        reads.clear()
        try:
            validator.is_type(1, "integer")
        except ValueError as error:
            assert error is raised
        else:
            raise AssertionError("checker descriptor exception was lost")
        assert reads == ["descriptor"]
    finally:
        Draft202012Validator.TYPE_CHECKER = saved

    checker_class = type(saved)
    checker_method = checker_class.is_type
    checker_class.is_type = lambda checker, value, kind: marker
    try:
        assert validator.is_type(1, "integer") is marker
        assert original(validator, 1, "integer") is marker
    finally:
        checker_class.is_type = checker_method

    override_calls = []

    def override(self, instance, kind):
        override_calls.append((instance, kind))
        return True

    Draft202012Validator.is_type = override
    try:
        assert list(Draft202012Validator({"type": "string"}).iter_errors(1)) == []
        assert override_calls == [(1, "string")]
    finally:
        Draft202012Validator.is_type = method

    import jsonschema

    # Fixed builtin kind literals must retain their actual original identity;
    # actual schema-supplied type strings must remain the caller's objects.
    from jsonschema import _keywords, validators
    literal_class = validators.extend(Draft202012Validator, {})
    literal_calls = []

    def inspect_kind(self, value, kind):
        literal_calls.append(kind)
        return False

    literal_class.is_type = inspect_kind
    for name, keyword_value, value, kind_name in (
        ("properties", {}, {}, "object"), ("items", {}, [], "array"),
        ("minimum", 0, 1, "number"), ("minLength", 0, "x", "string"),
    ):
        original_keyword = getattr(_original_keywords, name)
        kind = next(item for item in original_keyword.__code__.co_consts
                    if type(item) is str and item == kind_name)
        schema = {name: keyword_value}
        actual_validator = literal_class(schema)
        literal_calls.clear()
        assert list(original_keyword(actual_validator, keyword_value, value, schema)) == []
        assert len(literal_calls) == 1 and literal_calls[0] is kind
        literal_calls.clear()
        assert list(actual_validator.iter_errors(value)) == []
        assert len(literal_calls) == 1 and literal_calls[0] is kind
    supplied_kind = "".join(["str", "ing"])
    supplied_schema = {"type": supplied_kind}
    literal_calls.clear()
    errors = list(literal_class(supplied_schema).iter_errors(1))
    assert len(errors) == 1 and type(errors[0]) is exceptions.ValidationError
    assert errors[0].schema is supplied_schema and errors[0].validator_value is supplied_kind
    assert len(literal_calls) == 1 and literal_calls[0] is supplied_kind

    for cls in (jsonschema.Draft3Validator, jsonschema.Draft4Validator,
                jsonschema.Draft6Validator, jsonschema.Draft7Validator,
                jsonschema.Draft201909Validator):
        other = cls({})
        reference = getattr(cls.is_type, "__wrapped__", cls.is_type)
        assert other.is_type(1.0, "integer") is reference(other, 1.0, "integer")


def exercise_evolve():
    """Compare the original method's actual alias/namespace/evaluation contract."""
    import builtins
    import jsonschema
    from jsonschema import Draft202012Validator, FormatChecker, validators

    method = Draft202012Validator.evolve
    original = getattr(method, "__wrapped__", method)
    closure = dict(zip(original.__code__.co_freevars, original.__closure__ or ()))
    fields = closure["evolve_fields"].cell_contents
    saved_fields = fields[:]
    namespace, builtin_names = original.__globals__, original.__builtins__
    missing = object()
    saved_selector = namespace.get("validator_for", missing)
    saved_getattr = namespace.get("getattr", missing)
    saved_builtins = namespace.get("__builtins__", missing)
    saved_builtin_selector = builtin_names.get("validator_for", missing)
    lookup_missing = "__getattribute__" not in Draft202012Validator.__dict__
    saved_lookup = Draft202012Validator.__getattribute__
    checker = FormatChecker()
    validator = Draft202012Validator({"type": "object"}, format_checker=checker)
    actual_schema = {"type": "integer"}
    events, mutate = [], [False]
    watched = {"schema", "__class__", "_ref_resolver", "format_checker", "_registry", "_resolver"}

    def lookup(self, name):
        if name in watched:
            events.append(name)
        if name == "_ref_resolver" and mutate[0]:
            mutate[0] = False
            fields.append(("schema", "extra_schema"))
        return saved_lookup(self, name)

    def constructor(**changes):
        events.append("constructor")
        return changes

    def selector(schema, *, default):
        assert schema is actual_schema and default is Draft202012Validator
        events.append("selector")
        return constructor

    def compare(changes, mutation=False):
        observations = []
        for function in (original, method):
            fields[:] = saved_fields
            events.clear()
            mutate[0] = mutation
            result = function(validator, **changes)
            observations.append((result, events[:]))
        left, right = observations
        assert list(left[0]) == list(right[0])
        assert all(left_key is right_key for left_key, right_key in zip(left[0], right[0]))
        assert all(value is right[0][key] for key, value in left[0].items())
        assert left[1] == right[1]
        assert left[1][:3] == ["schema", "__class__", "selector"]
        assert left[1][-1] == "constructor"
        return right

    def restore(mapping, key, value):
        if value is missing:
            mapping.pop(key, None)
        else:
            mapping[key] = value

    Draft202012Validator.__getattribute__ = lookup
    namespace["validator_for"] = selector
    try:
        result, reads = compare({"schema": actual_schema, "format_checker": None})
        assert result["format_checker"] is None and "format_checker" not in reads
        result, reads = compare({"schema": actual_schema}, mutation=True)
        assert result["extra_schema"] is saved_lookup(validator, "schema")
        assert reads.count("schema") == 2

        def tracked_getattr(obj, attribute):
            events.append("global_getattr:" + attribute)
            return builtins.getattr(obj, attribute)

        namespace["getattr"] = tracked_getattr
        compare({"schema": actual_schema})
        namespace.pop("getattr", None)
        # Function.__builtins__ remains the actual captured dictionary even if
        # the module later rebinds its __builtins__ entry.
        namespace["__builtins__"] = {"getattr": lambda *_: missing}
        compare({"schema": actual_schema})
        namespace.pop("validator_for")
        builtin_names["validator_for"] = selector
        compare({"schema": actual_schema})
        builtin_names.pop("validator_for", None)
        for function in (original, method):
            events.clear()
            try:
                function(validator, schema=actual_schema)
            except NameError as error:
                assert error.name == "validator_for"
                assert str(error) == "name 'validator_for' is not defined"
            else:
                raise AssertionError("missing original name was invented")
            assert events == ["schema"]

        raised = ValueError("selected constructor")

        def failing_constructor(**changes):
            raise raised

        namespace["validator_for"] = lambda schema, *, default: failing_constructor
        for function in (original, method):
            fields[:] = saved_fields
            try:
                function(validator, schema=actual_schema)
            except ValueError as error:
                assert error is raised
            else:
                raise AssertionError("constructor exception disappeared")
    finally:
        fields[:] = saved_fields
        restore(namespace, "validator_for", saved_selector)
        restore(namespace, "getattr", saved_getattr)
        restore(namespace, "__builtins__", saved_builtins)
        restore(builtin_names, "validator_for", saved_builtin_selector)
        if lookup_missing:
            del Draft202012Validator.__getattribute__
        else:
            Draft202012Validator.__getattribute__ = saved_lookup

    result = validator.evolve(schema=actual_schema)
    assert type(result) is Draft202012Validator and result.schema is actual_schema
    assert result.format_checker is checker
    assert result._registry is validator._registry and result._resolver is validator._resolver
    assert result._ref_resolver is validator._ref_resolver
    other_schema = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "integer"}
    assert type(validator.evolve(schema=other_schema)) is jsonschema.Draft7Validator
    custom = validators.extend(Draft202012Validator, {})
    marker = object()
    custom.evolve = lambda self, **changes: marker
    assert custom({}).evolve(schema={}) is marker
    original_factory = getattr(validators.create, "__wrapped__", validators.create)
    uninstalled = original_factory(Draft202012Validator.META_SCHEMA,
                                  validators=Draft202012Validator.VALIDATORS)

    class CallableEvolve:
        def __call__(self, **changes):
            return marker

    callable_evolve = CallableEvolve()
    uninstalled.evolve = callable_evolve
    import sys

    if "schema_adapter" in sys.modules:
        sys.modules["schema_adapter"].install(uninstalled)
    assert uninstalled.evolve is callable_evolve
    assert uninstalled({}).evolve(schema={}) is marker


def exercise_post_init():
    """Compare genuine post-init objects, getter order, and partial writes."""
    import sys
    from jsonschema import Draft202012Validator, validators
    from referencing import Registry

    events, state = [], {"active": False}
    raised = ValueError("post-init callback")
    first, second, ignored = object(), object(), object()
    original_id_of = Draft202012Validator.ID_OF

    def handler_a(*args):
        return ()

    def handler_b(*args):
        return ()

    def applicable(schema):
        events.append(("applicable", schema))
        for key, value in schema.items():
            events.append(("yield", key))
            yield key, value
            if state.get("fail") == "applicable":
                raise raised
            if state.get("live") and key == "first":
                cls.VALIDATORS["second"] = handler_b
                events.append("replace_second")

    def id_of(schema):
        if not state["active"]:
            return original_id_of(schema)
        events.append(("id_of", schema))
        if state.get("fail") == "id_of":
            raise raised
        return state.get("id")

    # Supply callbacks to the actual factory before the adapter captures their
    # closure values. No replacement class, resource, or registry implements the
    # behavior under comparison.
    cls = validators.create(
        Draft202012Validator.META_SCHEMA,
        validators={"first": handler_a, "second": handler_a},
        id_of=id_of, applicable_validators=applicable,
    )
    if "schema_adapter" in sys.modules:
        sys.modules["schema_adapter"].install(cls)
    method = cls.__attrs_post_init__
    original = getattr(method, "__wrapped__", method)
    closure = dict(zip(original.__code__.co_freevars, original.__closure__ or ()))
    specification = closure["specification"].cell_contents
    assert closure["applicable_validators"].cell_contents is applicable
    assert closure["id_of"].cell_contents is id_of
    namespace, builtin_names = original.__globals__, original.__builtins__
    missing = object()
    saved_getattr = namespace.get("getattr", missing)
    saved_builtins = namespace.get("__builtins__", missing)
    saved_builtin_getattr = builtin_names["getattr"]
    saved_specifications = namespace["SPECIFICATIONS"]
    saved_remote = namespace["_REMOTE_WARNING_REGISTRY"]
    lookup_missing = "__getattribute__" not in cls.__dict__
    saved_lookup, saved_validators = cls.__getattribute__, cls.VALIDATORS
    registry_type, specification_type = type(saved_specifications), type(specification)
    saved_combine = registry_type.combine
    saved_resource = specification_type.create_resource
    saved_resolver = registry_type.resolver_with_root
    custom_registry = Registry()
    preset = custom_registry.resolver()
    legacy = validators._RefResolver.from_schema({})
    saved_scopes = legacy._scopes_stack[:]
    legacy_type, saved_push = type(legacy), type(legacy).push_scope
    watched = {"_resolver", "_registry", "schema", "VALIDATORS", "_ref_resolver"}
    # Compare actual original LOAD_ATTR names, not merely equal replacements.
    original_names = {name: name for name in original.__code__.co_names}
    push_scope_name = next(value for value in original.__code__.co_consts
                           if type(value) is str and value == "push_scope")

    def lookup(self, name):
        if self is state.get("target") and name in watched:
            assert type(name) is str and name is original_names[name]
            events.append(name)
            if name == "VALIDATORS":
                state["validator_reads"] += 1
                if state.get("fresh") and state["validator_reads"] == 2:
                    cls.VALIDATORS["first"] = handler_b
                    events.append("replace_first")
        return saved_lookup(self, name)

    def combine(self, *registries):
        events.append(("combine", self, registries))
        if state.get("fail") == "combine":
            raise raised
        return saved_combine(self, *registries)

    def create_resource(self, contents):
        assert self is specification
        events.append(("resource", contents))
        if state.get("fail") == "resource":
            raise raised
        resource = saved_resource(self, contents)
        assert resource.contents is contents
        return resource

    def resolver_with_root(self, resource):
        events.append(("resolver", resource.contents))
        if state.get("fail") == "resolver":
            raise raised
        result = saved_resolver(self, resource)
        state["created_resolver"] = result
        return result

    def push_scope(self, scope):
        assert self is legacy
        events.append(("push_scope", scope))
        if state.get("fail") == "push_scope":
            raise raised
        return saved_push(self, scope)

    def tracked_getattr(obj, name, *defaults):
        if name == "push_scope":
            assert type(name) is str and name is push_scope_name
            assert defaults == (None,)
            events.append(("getattr", obj, name, defaults[0]))
            if state.get("fail") == "getattr":
                raise raised
        return saved_builtin_getattr(obj, name, *defaults)

    def compare(schema, resolver=preset, registry=custom_registry, ref=None, **controls):
        observations = []
        for function in (original, method):
            validator = object.__new__(cls)
            old_validators = [(handler_a, "old", ignored)]
            for name, value in (("schema", schema), ("_resolver", resolver),
                                ("_registry", registry), ("_ref_resolver", ref),
                                ("_validators", old_validators)):
                object.__setattr__(validator, name, value)
            cls.VALIDATORS = saved_validators.copy()
            legacy._scopes_stack[:] = saved_scopes
            state.clear()
            state.update(active=True, target=validator, validator_reads=0, **controls)
            events.clear()
            failure = None
            try:
                for _ in range(controls.get("repeat", 1)):
                    assert function(validator) is None
            except ValueError as error:
                assert error is raised
                failure = error
            actual_resolver = saved_lookup(validator, "_resolver")
            if "created_resolver" in state:
                assert actual_resolver is state["created_resolver"]
            else:
                assert actual_resolver is resolver
            actual_validators = saved_lookup(validator, "_validators")
            observations.append((events[:], actual_validators,
                                 actual_validators is old_validators,
                                 actual_resolver is resolver,
                                 legacy._scopes_stack[:], failure))
            state["active"] = False
        assert observations[0] == observations[1]
        # Tuple members must retain the actual callbacks and original schema
        # values, including after a live VALIDATORS replacement.
        assert all(all(a is b for a, b in zip(left, right))
                   for left, right in zip(observations[0][1], observations[1][1]))
        return observations[1]

    def restore(mapping, name, value):
        if value is missing:
            mapping.pop(name, None)
        else:
            mapping[name] = value

    cls.__getattribute__ = lookup
    registry_type.combine = combine
    specification_type.create_resource = create_resource
    registry_type.resolver_with_root = resolver_with_root
    legacy_type.push_scope = push_scope
    namespace["getattr"] = tracked_getattr
    schema = {"first": first, "missing": ignored, "second": second}
    try:
        reads, values, old, retained, _, failure = compare(schema, fresh=True, live=True)
        assert not old and retained and failure is None
        assert values == [(handler_b, "first", first), (handler_b, "second", second)]
        assert reads == ["_resolver", "schema", "schema", "schema", ("applicable", schema),
                         ("yield", "first"), "VALIDATORS", "VALIDATORS", "replace_first",
                         "replace_second", ("yield", "missing"), "VALIDATORS",
                         ("yield", "second"), "VALIDATORS", "VALIDATORS", "_ref_resolver",
                         ("getattr", None, "push_scope", None)]
        # A preset resolver prevents every registry/resource operation.
        assert compare(schema, fail="resource")[-1] is None
        for boolean, schema_reads in ((True, 1), (False, 2)):
            reads, values, old, _, _, failure = compare(boolean)
            assert values == [] and not old and failure is None
            assert reads == ["_resolver"] + ["schema"] * schema_reads + [
                "_ref_resolver", ("getattr", None, "push_scope", None)]
            compare(boolean, resolver=None)

        reads = compare(schema, resolver=None)[0]
        assert reads[:6] == ["_resolver", "_registry",
                             ("combine", saved_specifications, (custom_registry,)),
                             "schema", ("resource", schema), ("resolver", schema)]
        reads = compare(schema, resolver=None, registry=saved_remote)[0]
        assert reads[:5] == ["_resolver", "_registry", "schema",
                             ("resource", schema), ("resolver", schema)]
        reads = compare(schema, resolver=None, repeat=2)[0]
        assert sum(event == ("resource", schema) for event in reads) == 1
        assert sum(event == ("applicable", schema) for event in reads) == 2
        for stage in ("combine", "resource", "resolver"):
            _, _, old, retained, _, failure = compare(schema, resolver=None, fail=stage)
            assert old and retained and failure is raised
        # Resolver assignment precedes keyword evaluation; a failed list
        # comprehension leaves the old _validators but keeps the new resolver.
        _, _, old, retained, _, failure = compare(schema, resolver=None, fail="applicable")
        assert old and not retained and failure is raised

        for scope in (None, "", "nested"):
            reads, _, old, _, scopes, failure = compare(schema, ref=legacy, id=scope)
            assert not old and failure is None
            assert reads[-(1 if scope is None else 2):] == (
                [("id_of", schema)] if scope is None else
                [("id_of", schema), ("push_scope", scope)])
            assert len(scopes) == len(saved_scopes) + (scope is not None)
        for stage in ("getattr", "id_of", "push_scope"):
            _, _, old, _, scopes, failure = compare(schema, ref=legacy, id="nested", fail=stage)
            assert not old and scopes == saved_scopes and failure is raised
        legacy_type.push_scope = None
        reads = compare(schema, ref=legacy, fail="id_of")[0]
        assert reads[-1] == ("getattr", legacy, "push_scope", None)
        legacy_type.push_scope = push_scope

        # Resolve globals and the function's captured builtins dictionary at
        # call time, while retaining its actual specification closure object.
        namespace["SPECIFICATIONS"] = custom_registry
        reads = compare(schema, resolver=None)[0]
        assert reads[2] == ("combine", custom_registry, (custom_registry,))
        namespace["_REMOTE_WARNING_REGISTRY"] = custom_registry
        assert compare(schema, resolver=None)[0][2] == "schema"
        namespace.pop("getattr", None)
        namespace["__builtins__"] = {}
        builtin_names["getattr"] = tracked_getattr
        compare(schema, ref=legacy, id="nested")
    finally:
        state["active"] = False
        cls.VALIDATORS = saved_validators
        if lookup_missing:
            del cls.__getattribute__
        else:
            cls.__getattribute__ = saved_lookup
        registry_type.combine = saved_combine
        specification_type.create_resource = saved_resource
        registry_type.resolver_with_root = saved_resolver
        legacy_type.push_scope = saved_push
        legacy._scopes_stack[:] = saved_scopes
        namespace["SPECIFICATIONS"] = saved_specifications
        namespace["_REMOTE_WARNING_REGISTRY"] = saved_remote
        restore(namespace, "getattr", saved_getattr)
        restore(namespace, "__builtins__", saved_builtins)
        builtin_names["getattr"] = saved_builtin_getattr

    original_factory = getattr(validators.create, "__wrapped__", validators.create)
    uninstalled = original_factory(Draft202012Validator.META_SCHEMA)
    marker, calls = object(), []

    class CallablePostInit:
        def __call__(self):
            calls.append(marker)
            return marker

    custom = CallablePostInit()
    uninstalled.__attrs_post_init__ = custom
    if "schema_adapter" in sys.modules:
        sys.modules["schema_adapter"].install(uninstalled)
    assert uninstalled.__attrs_post_init__ is custom
    assert type(uninstalled({})) is uninstalled and calls == [marker]


def exercise_unsupported_native():
    """Observe out-of-contract non-str keys separately from guest APIs.

    The declared type-name parameter is str. Original rpds panics for this
    key collisions; an uncatchable Wasm trap cannot be placed in the same
    successful return as the normal API suite. No schema case is removed.
    """
    import sys
    from jsonschema import Draft202012Validator

    if sys.platform != "linux":
        raise RuntimeError("native unsupported-key observation requires Linux CI")
    validator = Draft202012Validator({})
    original = getattr(Draft202012Validator.is_type, "__wrapped__", Draft202012Validator.is_type)
    cases = []
    for kind in (None, [], b"integer"):
        outcomes = []
        for check in (validator.is_type, lambda value, kind: original(validator, value, kind)):
            try:
                check(1, kind)
            except BaseException as error:
                name = type(error).__module__ + "." + type(error).__qualname__
                if not isinstance(error, Exception) and name != "pyo3_runtime.PanicException":
                    raise
                outcomes.append({"type": name, "message": str(error),
                                 "suppress_context": error.__suppress_context__})
            else:
                raise AssertionError("original unsupported-key failure disappeared")
        assert outcomes[0] == outcomes[1]
        cases.append({"kind": repr(kind), "outcome": outcomes[0]})
    return {
        "same_original_outcome": True,
        "cases": cases,
        "scope": "out-of-contract native non-str keys only; no Wasm negative-probe proof",
    }


if __name__ == "__main__":
    import json
    import os
    import sys

    if (sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true"
            or sys.version_info[:3] != (3, 13, 16)):
        raise RuntimeError("actual original API execution is restricted to pinned Linux CI")
    print(json.dumps({
        "format": "tobkiri.schema-port-original-api/1",
        "checks": exercise_api(),
        "implementation_diagnostics": exercise_implementation_diagnostics(),
        "wasm_executed": False, "production_enabled": False,
    }, sort_keys=True))
    print(json.dumps({
        "format": "tobkiri.schema-port-original-unsupported/1",
        "observation": exercise_unsupported_native(),
        "wasm_executed": False, "production_enabled": False,
    }, sort_keys=True))
