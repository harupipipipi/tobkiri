"""Actual pattern/format calls and caught FormatError conversion order."""


def pattern_format_checks(provider, kernel) -> dict[str, bool]:
    import re
    import sys
    from jsonschema import Draft202012Validator as original, FormatChecker, exceptions
    from jsonschema import _keywords
    from schema_kernel_observation import first_difference

    checks = {}
    pairs = 0

    def run(selected, make):
        state = make(selected)
        meta, actual = state["meta"], state["actual"]
        refs = {"meta": meta, "actual": actual, **state.get("refs", {})}
        events = state.setdefault("events", [])

        def reference(value):
            return sorted(name for name, item in refs.items() if value is item)

        def graph(error, seen=None):
            if error is None:
                return None
            seen = {} if seen is None else seen
            if id(error) in seen:
                return {"ref": seen[id(error)]}
            seen[id(error)] = len(seen)
            return {"type": type(error).__module__ + "." + type(error).__qualname__,
                    "message": str(error), "identity": reference(error),
                    "suppressed": error.__suppress_context__,
                    "cause": graph(error.__cause__, seen),
                    "context": graph(error.__context__, seen)}

        def invoke():
            try:
                selected.check_schema(actual, format_checker=state.get("checker"))
            except BaseException as error:
                # Error observation is outside the actual keyword handler.
                state["armed"] = False
                if type(error) is exceptions.SchemaError:
                    if "produced" in state:
                        assert error is state["produced"]
                    assert error.schema is state.get("error_schema", meta)
                    assert error.instance is state.get("error_instance", actual)
                    assert error.validator_value is error.schema[error.validator]
                    if "error_cause" in state:
                        assert error.cause is state["error_cause"]
                    result = {"kind": "schema-error", "message": error.message,
                              "schema": reference(error.schema),
                              "instance": reference(error.instance),
                              "validator": error.validator,
                              "validator_value": reference(error.validator_value),
                              "path": list(error.path), "schema_path": list(error.schema_path),
                              "cause": graph(error.cause), "exception": graph(error)}
                else:
                    assert state.get("exception", False), graph(error)
                    if state.get("wrapped_stop"):
                        assert type(error) is RuntimeError
                        assert str(error) == "generator raised StopIteration"
                        assert error.__cause__ is state["fault"]
                        assert error.__context__ is state["fault"]
                    elif "fault" in state:
                        assert error is state["fault"]
                    if "handling" in state:
                        assert state["fault"].__context__ is state["handling"]
                    if "error_context" in state:
                        assert error.__context__ is state["error_context"]
                    result = {"kind": "exception", "exception": graph(error)}
                return result
            state["armed"] = False
            assert not state.get("must_fail"), "source operation unexpectedly succeeded"
            return {"kind": "valid"}

        saved = selected.META_SCHEMA, selected.TYPE_CHECKER, selected.FORMAT_CHECKER
        saved_constructor = exceptions.ValidationError, _keywords.ValidationError
        saved_create_from = exceptions.SchemaError.__dict__.get("create_from")
        saved_search = re.search
        before = kernel.app_schema_entry_count()
        selected.META_SCHEMA = meta
        if "type_checker" in state:
            selected.TYPE_CHECKER = state["type_checker"]
        try:
            if "install" in state:
                state["install"]()
            if "outer" in state:
                try:
                    raise state["outer"]
                except RuntimeError:
                    outcome = invoke()
                    assert sys.exception() is state["outer"]
                    events.append("outer-restored")
            else:
                outcome = invoke()
            if "verify" in state:
                state["verify"]()
        finally:
            re.search = saved_search
            exceptions.ValidationError, _keywords.ValidationError = saved_constructor
            if saved_create_from is None:
                if "create_from" in exceptions.SchemaError.__dict__:
                    del exceptions.SchemaError.create_from
            else:
                exceptions.SchemaError.create_from = saved_create_from
            selected.META_SCHEMA, selected.TYPE_CHECKER, selected.FORMAT_CHECKER = saved
        assert kernel.app_schema_entry_count() - before == (
            state.get("calls", 1) if selected is provider else 0)
        return {"outcome": outcome, "events": events}

    def compare(make):
        nonlocal pairs
        expected, actual = run(original, make), run(provider, make)
        difference = first_difference(expected, actual)
        assert difference is None, {"difference": difference, "original": expected, "app": actual}
        pairs += 1

    def nonstring(selected, actual):
        events = []
        def string(checker, value):
            assert value is actual
            events.append("type-string")
            return False
        def forbidden(*args):
            raise AssertionError("matcher reached for a non-string")
        def verify():
            assert events == ["type-string"], events
        return {"meta": {"pattern": "["}, "actual": actual, "events": events,
                "type_checker": selected.TYPE_CHECKER.redefine("string", string),
                "install": lambda: setattr(re, "search", forbidden),
                "verify": verify}
    for factory in (lambda: None, lambda: False, lambda: 7, list, dict):
        compare(lambda selected: nonstring(selected, factory()))
    checks["nonstring_selected_type_check_precedes_and_bypasses_matcher"] = True

    for pattern, actual, failed in (("^雪+$", "雪雪", False), ("^雪+$", "雨", True),
                                     ("\ud800", "\ud800", False), ("[", "text", True)):
        compare(lambda selected, p=pattern, a=actual, f=failed: {
            "meta": {"pattern": p}, "actual": a, "must_fail": f,
            "exception": p == "[", "refs": {"pattern": p}})
    checks["actual_regex_handles_unicode_surrogates_and_invalid_pattern"] = True

    def selected_matcher(selected, matched):
        events, actual, pattern = [], "live", "literal"
        class Result:
            def __bool__(self):
                events.append("matcher-truth")
                return matched
        def search(p, value):
            assert p is pattern and value is actual
            events.append("actual-search")
            return Result()
        def string(checker, value):
            assert value is actual
            events.append("type-string")
            re.search = search
            return True
        def verify():
            assert events == ["type-string", "actual-search", "matcher-truth"], events
        return {"meta": {"pattern": pattern}, "actual": actual, "events": events,
                "refs": {"pattern": pattern}, "must_fail": not matched,
                "type_checker": selected.TYPE_CHECKER.redefine("string", string),
                "verify": verify}
    for matched in (False, True):
        compare(lambda selected: selected_matcher(selected, matched))
    checks["matcher_is_selected_after_live_type_callback_and_truth_tested_once"] = True

    compare(lambda selected: {"meta": {"format": "unknown"}, "actual": object()})
    compare(lambda selected: {"meta": {"format": "unknown"}, "actual": 7,
                              "checker": FormatChecker(formats=[])})
    checks["disabled_or_unregistered_format_has_no_callback"] = True

    def live_registration(selected):
        events, actual = [], {"live": True}
        checker = FormatChecker(formats=[])
        replacement = FormatChecker(formats=[])
        def forbidden(value):
            raise AssertionError("current operation changed its bound checker")
        replacement.checks("second")(forbidden)
        def second(value):
            assert value is actual
            events.append("second-new")
            return True
        def first(value):
            assert value is actual
            events.append("first")
            checker.checkers["second"] = (second, ())
            selected.FORMAT_CHECKER = replacement
            return True
        checker.checks("first")(first)
        checker.checks("second")(forbidden)
        return {"meta": {"allOf": [{"format": "first"}, {"format": "second"}]},
                "actual": actual, "checker": checker, "events": events}
    compare(live_registration)
    checks["bound_checker_retains_live_registration_after_callback_owner_replacement"] = True

    def callback_fault(selected, caught, stop, truth=False):
        events, actual = [], ["actual"]
        fault = StopIteration("supplied stop") if stop else ValueError("supplied failure")
        checker = FormatChecker(formats=[])
        class Result:
            def __bool__(self):
                events.append("result-truth")
                raise fault
        def callback(value):
            assert value is actual
            events.append("callback")
            if truth:
                return Result()
            raise fault
        checker.checks("fault", raises=(type(fault),) if caught else ())(callback)
        state = {"meta": {"format": "fault"}, "actual": actual, "checker": checker,
                 "events": events, "refs": {"fault": fault}, "must_fail": True}
        if caught and not truth:
            state["error_cause"] = fault
        else:
            state.update(exception=True, fault=fault, wrapped_stop=stop)
        return state
    for stop in (False, True):
        compare(lambda selected: callback_fault(selected, True, stop))
    checks["checker_catches_configured_faults_before_keyword_PEP479"] = True
    for stop in (False, True):
        compare(lambda selected: callback_fault(selected, False, stop))
    checks["uncaught_callback_exception_identity_or_PEP479_cause_is_preserved"] = True
    for stop in (False, True):
        compare(lambda selected: callback_fault(selected, True, stop, truth=True))
    checks["result_truth_runs_outside_checker_configured_except"] = True

    def descriptor_fault(selected, stop):
        events = []
        fault = StopIteration("check bind stop") if stop else LookupError("check bind failure")
        class Checker:
            @property
            def check(self):
                events.append("check-bind")
                raise fault
        return {"meta": {"format": "bind"}, "actual": "input", "checker": Checker(),
                "events": events, "refs": {"fault": fault}, "fault": fault,
                "exception": True, "must_fail": True, "wrapped_stop": stop}
    for stop in (False, True):
        compare(lambda selected: descriptor_fault(selected, stop))
    checks["actual_check_descriptor_fault_precedes_call_and_preserves_identity"] = True

    def conversion(selected, failure=None, stop=False, reenter=False, noncallable=False):
        events, actual = [], "input"
        fault = StopIteration("conversion stop") if stop else LookupError("conversion failure")
        old_cause, new_cause = ValueError("old cause"), ValueError("new cause")
        saved_constructor = exceptions.ValidationError
        state = {"meta": {"format": "convert"}, "actual": actual, "events": events,
                 "armed": False, "must_fail": True}
        class Detailed(exceptions.FormatError):
            def __getattribute__(self, name):
                if name in ("message", "cause") and state["armed"]:
                    assert sys.exception() is self
                    events.append(name)
                    if failure == name:
                        raise fault
                    if name == "message":
                        # The constructor was already selected, and this live
                        # cause replacement must be read by the later getter.
                        exceptions.ValidationError = replacement
                        _keywords.ValidationError = replacement
                        self.cause = new_cause
                        if reenter:
                            saved = selected.META_SCHEMA
                            try:
                                selected.META_SCHEMA = {"type": "integer"}
                                selected.check_schema(1, format_checker=None)
                            finally:
                                selected.META_SCHEMA = saved
                            assert sys.exception() is self
                            events.append("reentered")
                return super().__getattribute__(name)
        handled = Detailed("actual format message", cause=old_cause)
        state["refs"] = {"handling": handled, "fault": fault,
                         "old-cause": old_cause, "new-cause": new_cause}
        def replacement(*args, **kwargs):
            raise AssertionError("message getter replaced an already-selected constructor")
        def constructor(message, *, cause):
            assert sys.exception() is handled
            assert message == "actual format message" and cause is new_cause
            events.append("constructor")
            if failure == "constructor":
                raise fault
            return saved_constructor(message, cause=cause)
        class Check:
            @property
            def check(self):
                events.append("check-bind")
                def call(value, kind):
                    assert value is actual and kind is state["meta"]["format"]
                    events.append("check-call")
                    state["armed"] = True
                    raise handled
                return call
        def install():
            # Configure the same actual constructor at each lane's existing
            # boundary. This compares order, not an independent private-alias
            # mutation contract between the App and the generic source module.
            chosen = object() if noncallable else constructor
            exceptions.ValidationError = chosen
            _keywords.ValidationError = chosen
        state.update(checker=Check(), install=install)
        if failure:
            state.update(exception=True, fault=fault, handling=handled, wrapped_stop=stop)
        elif noncallable:
            state.update(exception=True, error_context=handled)
        else:
            state["error_cause"] = new_cause
        if reenter:
            outer = RuntimeError("outer handled exception")
            state["outer"] = outer
            state["refs"]["outer"] = outer
            state["calls"] = 2
        def verify():
            wanted = ["check-bind", "check-call", "message"]
            if failure != "message":
                if reenter:
                    wanted.append("reentered")
                wanted.append("cause")
                if failure != "cause" and not noncallable:
                    wanted.append("constructor")
            if reenter:
                wanted.append("outer-restored")
            assert events == wanted, events
        state["verify"] = verify
        return state
    compare(conversion)
    checks["actual_constructor_is_bound_before_message_then_live_cause_getters"] = True
    for failure in ("message", "cause", "constructor"):
        for stop in (False, True):
            compare(lambda selected: conversion(selected, failure=failure, stop=stop))
        checks[f"{failure}_supplied_exception_identity_context_and_PEP479_are_preserved"] = True
    compare(lambda selected: conversion(selected, reenter=True))
    checks["handled_exception_is_restored_after_conversion_and_real_reentry"] = True
    compare(lambda selected: conversion(selected, noncallable=True))
    checks["noncallable_constructor_is_called_only_after_both_actual_getters"] = True

    def final_producer(selected, *, outer, prior, suppressed):
        events, actual = [], "actual producer input"
        cause, previous = ValueError("retained cause"), LookupError("prior context")
        checker = FormatChecker(formats=[])
        saved = exceptions.SchemaError.create_from
        state = {"meta": {"format": "producer"}, "actual": actual,
                 "checker": checker, "events": events, "must_fail": True,
                 "error_cause": cause, "refs": {"cause": cause, "prior": previous}}
        if outer:
            state["outer"] = RuntimeError("actual outer handler")
            state["refs"]["outer"] = state["outer"]
        def callback(value):
            assert value is actual
            events.append("callback")
            return False
        checker.checks("producer")(callback)
        def create(cls, error):
            events.append("create-from")
            # Call the real factory once on this actual yielded validation
            # error. The returned instance is the one each lane must raise.
            produced = saved(error)
            assert type(produced) is exceptions.SchemaError
            assert produced.schema is state["meta"] and produced.instance is actual
            produced.cause = produced.__cause__ = cause
            produced.__suppress_context__ = suppressed
            produced.__context__ = previous if prior else None
            if outer == "backlink":
                state["outer"].__context__ = produced
            elif outer == "self-cycle":
                state["outer"].__context__ = state["outer"]
            state["produced"] = produced
            return produced
        def verify():
            produced = state["produced"]
            assert produced.__cause__ is cause and produced.cause is cause
            assert produced.__suppress_context__ is suppressed
            assert produced.__context__ is (state["outer"] if outer else
                                           previous if prior else None)
            if outer == "backlink":
                assert state["outer"].__context__ is None
            elif outer == "self-cycle":
                assert state["outer"].__context__ is state["outer"]
            assert events == (["callback", "create-from", "outer-restored"] if outer else
                              ["callback", "create-from"]), events
        state.update(install=lambda: setattr(exceptions.SchemaError, "create_from", classmethod(create)),
                     verify=verify)
        return state
    for outer in (None, "new"):
        for prior in (False, True):
            for suppressed in (False, True):
                compare(lambda selected: final_producer(
                    selected, outer=outer, prior=prior, suppressed=suppressed))
    checks["final_producer_preserves_actual_instance_cause_suppression_and_context"] = True
    for outer in ("backlink", "self-cycle"):
        for suppressed in (False, True):
            compare(lambda selected: final_producer(
                selected, outer=outer, prior=True, suppressed=suppressed))
    checks["final_producer_uses_CPython_context_cycle_handling_without_reconstruction"] = True
    assert len(checks) == 17 and pairs == 43 and all(checks.values()), (len(checks), pairs)
    return checks
