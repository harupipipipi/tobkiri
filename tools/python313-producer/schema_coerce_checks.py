"""Bounded call observations for the shared urllib argument-coercion helper."""


def _coerce_failure(error: BaseException | None, seen: tuple = ()) -> tuple | None:
    if error is None:
        return None
    if any(error is previous for previous in seen):
        return ("cycle", type(error))
    seen += (error,)
    return (type(error), str(error), error.__suppress_context__,
            _coerce_failure(error.__cause__, seen),
            _coerce_failure(error.__context__, seen), getattr(error, "name", None))


def _coerce_observe(function: object, args: tuple, kwargs: dict) -> tuple:
    try:
        value = function(*args, **kwargs)
    except Exception as error:
        return ("error", _coerce_failure(error)), error
    members = value if isinstance(value, tuple) else (value,)
    identity = tuple(tuple(item is argument for argument in args) for item in members)
    return ("value", type(value), value, tuple(type(item) for item in members),
            identity), value


def exercise_coerce() -> dict[str, bool]:
    """Compare original objects, live callbacks and the six actual URL callers."""
    import sys
    import urllib.parse as parse

    candidate = parse._coerce_args
    translated = getattr(parse, "_tobkiri_translated_coerce_args", False)
    if translated:
        reference = parse._tobkiri_original_coerce_args
        assert reference is not candidate
        kernel = sys.modules["_tobkiri_schema_kernel"]
    else:
        reference, kernel = candidate, None
    assert reference.__name__ == "_coerce_args"
    assert reference.__globals__ is vars(parse)
    split = parse.urlsplit
    assert split.cache_parameters()["typed"] is True
    names = ("_decode_args", "_noop", "_encode_result")
    saved = {name: getattr(parse, name) for name in names}
    events = []

    def compare(args: tuple, kwargs: dict | None = None, setup: object = None,
                raised: BaseException | None = None) -> object:
        observations, objects = [], []
        try:
            for function in (reference, candidate):
                vars(parse).update(saved)
                if setup is not None:
                    setup()
                events.clear()
                observation, value = _coerce_observe(function, args, kwargs or {})
                observations.append((observation, events[:]))
                objects.append(value)
            assert observations[0] == observations[1], (args, observations)
            if raised is not None:
                assert objects[0] is objects[1] is raised
            return objects[1]
        finally:
            vars(parse).update(saved)

    before = kernel.coerce_args_calls() if kernel is not None else None
    text = "".join(("original ", "text"))
    result = compare((text, ""))
    assert type(result) is tuple and result[0] is text
    assert result[1] == "" and result[-1] is saved["_noop"]
    result = compare((b"original bytes", ""))
    assert result[:-1] == ("original bytes", "")
    assert all(type(item) is str for item in result[:-1])
    assert result[-1] is saved["_encode_result"]
    if kernel is not None:
        assert type(before) is int and kernel.coerce_args_calls() > before

    for args in (
        (), ("",), (b"",), ("x",), (b"x",), (None,), (False,), (0,), ([],),
        (None, None), (None, ""), (None, "x"), ("x", None), ("x", 0),
        ("x", b""), (b"x", ""), ("", b"x"), (b"", "x"),
        ("x", b"y"), (b"x", "y"), (b"\xff",), ("雪", ""),
        (bytearray(b"abc"), ""), (True,),
        tuple("item" for _ in range(7)), tuple(b"item" for _ in range(33)),
    ):
        compare(args)
    for args in ((), ("x",), (b"x", "")):
        error = compare(args, {"unexpected": object()})
        assert type(error) is TypeError and "_coerce_args" in str(error)
    assert type(compare(())) is IndexError
    assert type(compare(("x", b"y"))) is TypeError
    assert type(compare((b"\xff",))) is UnicodeDecodeError

    failure = RuntimeError("coercion callback")
    failure.__cause__ = ValueError("coercion cause")
    decoded = "".join(("decoded ", "object"))

    class Text(str):
        def __bool__(self) -> bool:
            events.append(("text-bool", str(self)))
            return len(self) != 0

    class Falsey:
        def __bool__(self) -> bool:
            events.append("falsey")
            return False

        def decode(self, *args: object) -> object:
            raise AssertionError("a falsey input must not be decoded")

    class Decodable:
        def __bool__(self) -> bool:
            events.append("decode-bool")
            return True

        def decode(self, *args: object) -> str:
            events.append(("decode", args))
            return decoded

    class Raising:
        def __bool__(self) -> bool:
            events.append("raising-bool")
            raise failure

    falsey = Falsey()
    for args in ((Text("x"), Text("")), ("x", Text("y")), ("x", falsey),
                 (falsey, ""), (Decodable(), b""), (Text("x"), None)):
        result = compare(args)
        assert type(result) is tuple
    assert compare(("x", falsey))[1] is falsey
    assert compare((Decodable(),))[0] is decoded

    class RaisingDecode(Decodable):
        def decode(self, *args: object) -> str:
            events.append(("decode-raised", args))
            raise failure

    compare((RaisingDecode(),), raised=failure)
    compare((Raising(),), raised=failure)
    compare(("x", Raising()), raised=failure)

    def late_noop(value: object) -> object:
        events.append(("late-noop", value))
        return value

    def late_encode(value: object) -> object:
        events.append(("late-encode", value))
        return value

    def decode_live(args: tuple) -> tuple:
        assert type(args) is tuple and args == (b"x", "")
        events.append(("decode-live", args))
        parse._encode_result = late_encode
        return (decoded, "")

    def setup_decode() -> None:
        parse._decode_args = decode_live

    result = compare((b"x", ""), setup=setup_decode)
    assert result[0] is decoded and result[-1] is late_encode

    addition_result = object()

    class Decoded(tuple):
        def __add__(self, tail: tuple) -> object:
            assert type(tail) is tuple and tail == (late_encode,)
            events.append(("decoded-add", tail))
            parse._encode_result = saved["_encode_result"]
            return addition_result

    def setup_addition() -> None:
        def decode(args: tuple) -> tuple:
            events.append(("decode-before-add", args))
            parse._encode_result = late_encode
            return Decoded((decoded, ""))
        parse._decode_args = decode

    assert compare((b"x", ""), setup=setup_addition) is addition_result

    class MutatingText(str):
        def __bool__(self) -> bool:
            events.append("replace-next-noop")
            parse._noop = late_noop
            return True

    assert compare(("x", MutatingText("y")))[-1] is late_noop

    def stop(*args: object) -> object:
        events.append("helper-raised")
        raise failure

    def setup_stop() -> None:
        parse._decode_args = stop

    compare((b"x",), setup=setup_stop, raised=failure)

    def traced(name: str) -> object:
        def call(value: object) -> object:
            events.append((name, type(value), value))
            return saved[name](value)
        return call

    noop, encode = traced("_noop"), traced("_encode_result")
    public_cases = (
        (parse.urlsplit, ("https://user:pw@host:80/a?b#c", "", False)),
        (parse.urlsplit, (b"https://host/a?b#c", "")),
        (parse.urlparse, ("https://host/a;p?b#c", "")),
        (parse.urlparse, (b"https://host/a;p?b#c", "", False)),
        (parse.urljoin, ("https://host/a/b", "../c?x#y")),
        (parse.urljoin, (b"https://host/a/b", b"../c?x#y")),
        (parse.urljoin, ("", text)), (parse.urljoin, (text, "")),
        (parse.urldefrag, ("https://host/a#fragment",)),
        (parse.urldefrag, (b"https://host/a#fragment",)),
        (parse.urlunparse, (("https", "host", "/a", "p", "q", "f"),)),
        (parse.urlunparse, ((b"https", b"host", b"/a", b"p", b"q", b"f"),)),
        (parse.urlunsplit, (("https", "host", "/a", "q", "f"),)),
        (parse.urlunsplit, ((b"https", b"host", b"/a", b"q", b"f"),)),
        (parse.urlsplit, (b"http://host/\xff",)),
        (parse.urlparse, ("http://host/", b"https")),
        (parse.urljoin, ("http://host/", b"child")),
        (parse.urlsplit, ("http://[invalid]/",)),
    )
    try:
        for function, args in public_cases:
            observations = []
            for helper in (reference, candidate):
                parse._coerce_args = helper
                vars(parse).update(saved)
                parse._noop, parse._encode_result = noop, encode
                split.cache_clear()
                events.clear()
                observation, _ = _coerce_observe(function, args, {})
                observations.append((observation, events[:], split.cache_info()))
            assert observations[0] == observations[1], (function, args, observations)
        vars(parse).update(saved)
        parse._coerce_args = candidate
        split.cache_clear()
        first = split("https://shared-cache.test/a")
        cached = split.cache_info()
        count = kernel.coerce_args_calls() if kernel is not None else None
        assert split("https://shared-cache.test/a") is first
        assert split.cache_info().hits == cached.hits + 1
        if kernel is not None:
            assert kernel.coerce_args_calls() == count
        octets = split(b"https://shared-cache.test/a")
        assert type(octets) is parse.SplitResultBytes
        assert type(first) is parse.SplitResult and split.cache_info().currsize == 2
    finally:
        parse._coerce_args = candidate
        vars(parse).update(saved)
        split.cache_clear()
    assert parse.urlsplit is split and parse._coerce_args is candidate
    return {"coerce_args_original_objects_and_shared_urllib_apis": True}
