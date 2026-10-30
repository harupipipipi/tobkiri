"""Bounded observations of the actual referencing urljoin boundary."""


def _failure(error: BaseException | None) -> tuple | None:
    if error is None:
        return None
    context = error.__context__
    return (type(error), str(error), _failure(error.__cause__),
            error.__suppress_context__,
            None if context is None else (type(context), str(context)),
            getattr(error, "name", None))


def _observe(join: object, args: tuple) -> tuple:
    try:
        value = join(*args)
    except Exception as error:
        return ("error", _failure(error)), error
    return ("value", type(value), value, tuple(value is arg for arg in args)), value


def exercise_urljoin() -> dict[str, bool]:
    """Compare real helpers, callbacks, exceptions, and Registry resolution."""
    import sys
    import urllib.parse as parse
    import referencing._core as core

    original, candidate = parse.urljoin, core.urljoin
    assert getattr(candidate, "__wrapped__", original) is original
    globals_ = original.__globals__
    assert globals_ is vars(parse)
    split = parse.urlsplit
    kernel = sys.modules.get("_tobkiri_schema_kernel")
    if candidate is original:
        kernel = None
    events = []

    def compare(*args: object, raised: BaseException | None = None) -> None:
        events.clear()
        split.cache_clear()
        expected, expected_object = _observe(original, args)
        expected_events = events[:]
        events.clear()
        split.cache_clear()
        before = kernel.urljoin_calls() if kernel is not None else None
        actual, actual_object = _observe(candidate, args)
        if kernel is not None:
            exact = all(type(value) in (str, bytes) for value in args[:2])
            assert kernel.urljoin_calls() - before == int(exact)
        assert actual == expected, (args, expected, actual)
        assert events == expected_events, (args, expected_events, events)
        if raised is not None:
            assert expected_object is actual_object is raised

    base = "http://a/b/c/d;p?q"
    relatives = (
        "g:h", "g", "./g", "g/", "/g", "//g", "?y", "g?y", "#s",
        "g#s", "g?y#s", ";x", "g;x", "g;x?y#s", "", ".", "./", "..",
        "../", "../g", "../..", "../../", "../../g", "../../../g",
        "../../../../g", "/./g", "/../g", "g.", ".g", "g..", "..g",
        "./../g", "./g/.", "g/./h", "g/../h", "g;x=1/./y", "g;x=1/../y",
        "g?y/./x", "g?y/../x", "g#s/./x", "g#s/../x", "http:g",
        "g//h///i", "//user:pass@[::1]:80/g", "?", "#", "?#", "%2e%2e/g",
    )
    for relative in relatives:
        compare(base, relative)
        compare(base.encode(), relative.encode(), False)
    for base, relative in (
        ("https://host/a/b?x#f", "../c"), ("ftp://host/a/b", "../c"),
        ("file:///a/b", "../c"), ("urn:base:a", "next"), ("", "../g"),
        ("http://a", ""), ("HTTP://A/a", "HTTP:b"), ("a/b", "../c"),
        ("http://例.test/雪/道", "../光?語#片"), ("http://a/\t\nb", " c\r/d"),
        ("http://[::1]/a", "//[invalid]/b"), ("http://[invalid]/a", "x"),
        ("http://a/a", "//example：80/b"), (b"http://a/\xff", b"x"),
        ("http://a/a", b"x"), (b"http://a/a", "x"),
    ):
        compare(base, relative)
        compare(base, relative, False)
    for _ in range(2):
        compare("https://host/a/b", "../c?x#f")
    before = split.cache_info()
    candidate("https://host/a/b", "../c?x#f")
    assert split.cache_info().hits > before.hits

    class Falsey:
        def __bool__(self) -> bool:
            events.append("falsey")
            return False

    class Text(str):
        def __bool__(self) -> bool:
            events.append("text-bool")
            return len(self) != 0

    class Octets(bytes):
        def decode(self, *args: object, **kwargs: object) -> str:
            events.append("bytes-decode")
            return super().decode(*args, **kwargs)

    failed = RuntimeError("urljoin callback")
    failed.__cause__ = ValueError("original callback cause")

    class Raising:
        def __bool__(self) -> bool:
            events.append("raising")
            raise failed

    marker = object()
    for base, relative in (
        (None, marker), (Falsey(), marker), ("http://a/", Falsey()),
        (None, None), ("", None), (0, marker), (marker, ""),
        (Text(""), marker), (Text("http://a/b"), Text("../c")),
        (Octets(b"http://a/b"), Octets(b"../c")),
        (1, "x"), ("http://a/", []), ("http://a/", [1]),
    ):
        compare(base, relative)
    compare(Raising(), "x", raised=failed)
    compare("http://a/", Raising(), raised=failed)
    compare("http://a/b#x", "c#y", Falsey())
    compare("http://a/b#x", "c#y", Raising(), raised=failed)

    helpers = {name: globals_[name]
               for name in ("_coerce_args", "urlparse", "urlunparse")}

    def traced(name: str, function: object) -> object:
        def call(*args: object, **kwargs: object) -> object:
            events.append((name, args, kwargs))
            return function(*args, **kwargs)
        return call

    try:
        for name, function in helpers.items():
            globals_[name] = traced(name, function)
        compare("https://host/a/b;p?x#f", "../c;q?y#s", False)

        def live(join: object) -> tuple:
            events.clear()
            split.cache_clear()
            globals_["urlunparse"] = traced("urlunparse", helpers["urlunparse"])

            def first(*args: object, **kwargs: object) -> object:
                events.append(("first-parse", args, kwargs))
                result = helpers["urlparse"](*args, **kwargs)
                assert isinstance(result, (parse.ParseResult, parse.ParseResultBytes))
                globals_["urlparse"] = traced("later-parse", helpers["urlparse"])
                globals_["urlunparse"] = traced("later-unparse", helpers["urlunparse"])
                return result

            globals_["urlparse"] = first
            result = _observe(join, ("http://host/a/b;p", "../c;q"))
            return result[0], events[:]

        assert live(candidate) == live(original)
        globals_.update(helpers)

        class Scheme(str):
            def __ne__(self, other: object) -> bool:
                events.append(("scheme-ne", other is self))
                return True

        scheme = Scheme("http")

        def same_scheme(*args: object, **kwargs: object) -> object:
            parsed = helpers["urlparse"](*args, **kwargs)
            return parsed._replace(scheme=scheme)

        globals_["urlparse"] = same_scheme
        compare("http://host/a/b", "../c")
        assert ("scheme-ne", True) in events
        globals_.update(helpers)
        for name in helpers:
            def stop(*args: object, **kwargs: object) -> object:
                events.append("helper-raised")
                raise failed
            globals_[name] = stop
            compare("http://host/a/b", "../c", raised=failed)
            globals_[name] = helpers[name]
    finally:
        globals_.update(helpers)

    missing = object()
    saved_filter = globals_.get("filter", missing)
    saved_index_error = globals_.get("IndexError", missing)
    try:
        globals_["filter"] = traced("filter", original.__builtins__["filter"])
        compare("http://host/a/b", "../c//d")
        for catch in (ValueError, 42, (IndexError, 42), ((IndexError,),)):
            globals_["IndexError"] = catch
            compare("http://host/a", "../../../c")
        del globals_["urlparse"]
        compare("http://host/a", "c")
        compare("", "c")  # Original early return must not resolve a helper.
    finally:
        globals_["urlparse"] = helpers["urlparse"]
        for name, value in (
            ("filter", saved_filter), ("IndexError", saved_index_error),
        ):
            if value is missing:
                globals_.pop(name, None)
            else:
                globals_[name] = value

    config = {name: (globals_[name], globals_[name][:])
              for name in ("uses_relative", "uses_netloc", "uses_params")}
    try:
        for name in config:
            globals_[name].append("x-join-probe")
        compare("x-join-probe://host/a/b;p?x", "../c;q?y")
        parse.uses_relative.remove("http")
        compare("http://host/a/b", "../c")
        parse.uses_relative.append("http")
        parse.uses_netloc.remove("http")
        compare("http://host/a/b", "//other/c")
        parse.uses_params.remove("http")
        compare("http://host/a/b;p", "?x")
    finally:
        for name, (value, contents) in config.items():
            globals_[name] = value
            value[:] = contents
    assert parse.urljoin is original and core.urljoin is candidate
    assert parse.urlsplit is split
    _exercise_registry(original, candidate)
    return {"urljoin_actual_helpers_callbacks_and_registry": True}


def _exercise_registry(original: object, candidate: object) -> None:
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012
    import referencing._core as core

    child = {"$id": "nested/child.json", "$anchor": "node", "type": "integer"}
    contents = {"$id": "https://join.test/a/root.json", "$defs": {"child": child}}
    other = {"type": "string"}
    resource = Resource.from_contents(contents, default_specification=DRAFT202012)
    no_id = Resource.from_contents(other, default_specification=DRAFT202012)
    registry = Registry().with_resources((
        (contents["$id"], resource), ("https://join.test/other.json", no_id),
    )).crawl()
    resolver = registry.resolver(contents["$id"])
    assert resolver.in_subresource(no_id) is resolver
    try:
        for ref in ("nested/child.json", "./nested/../nested/child.json#node",
                    "../other.json", "root.json#/$defs/child", "missing.json",
                    "nested/child.json#missing", "root.json#/$defs/absent"):
            core.urljoin = original
            expected, left = _observe(resolver.lookup, (ref,))
            core.urljoin = candidate
            actual, right = _observe(resolver.lookup, (ref,))
            if expected[0] == "error":
                assert actual == expected
            else:
                assert actual[0] == "value" and type(right) is type(left)
                assert right.contents is left.contents
                assert (right.resolver is resolver) == (left.resolver is resolver)
                assert right.resolver._base_uri == left.resolver._base_uri
                assert right.resolver._registry is left.resolver._registry is registry
        core.urljoin = original
        left = resolver.in_subresource(resource)
        core.urljoin = candidate
        right = resolver.in_subresource(resource)
        assert right._base_uri == left._base_uri
        assert right._registry is left._registry is registry
    finally:
        core.urljoin = candidate
