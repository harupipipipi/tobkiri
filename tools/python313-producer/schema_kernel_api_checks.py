"""Actual guest observations for the port's lazy public API, not product setup."""


def exercise_api():
    """Exercise real validators/callbacks and retain their original objects."""
    from jsonschema import Draft202012Validator, exceptions, validators
    from schema_adapter import install

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
    install(extended)
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

    def stopped(validator, value, instance, schema):
        raise StopIteration("callback exception")

    exceptional = validators.extend(Draft202012Validator, {"native-stop": stopped})
    install(exceptional)
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

    return {
        "lazy_start_and_resume": True,
        "actual_error_objects_and_references": True,
        "outer_close_keeps_retained_callback_open": True,
        "interleaved_iterators": True,
        "pep479_and_failed_iterator_closed": True,
        "false_descend_preserves_original_early_paths": True,
        "evolve_preserves_actual_class_and_schema": True,
    }
