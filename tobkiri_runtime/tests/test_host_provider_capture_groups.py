"""Resource capture grouping does not merge authority or executable identity."""

from types import SimpleNamespace

import pytest

from core_runtime.host_provider_hooks_v4 import group_host_provider_captures
from tobkiri_host.errors import AuthorizationError


class _Factory:
    capture_group = ("function.a", "function.b")


def _loaded(function_id, **changes):
    artifact = SimpleNamespace(pack_id="pack", publisher_lineage="publisher", digest="artifact")
    binding = SimpleNamespace(
        artifact=artifact,
        function=SimpleNamespace(implementation_digest="implementation"),
    )
    for key, value in changes.items():
        if key == "implementation_digest":
            setattr(binding.function, key, value)
        elif key != "backend_id":
            setattr(artifact, key, value)
    factory = _Factory()
    factory.function_id = function_id
    return (function_id, (binding,), factory, changes.get("backend_id", "backend"))


def test_explicit_capture_group_keeps_exact_operation_bindings():
    first, second = _loaded("function.a"), _loaded("function.b")
    grouped = group_host_provider_captures((first, second))
    assert len(grouped) == 1
    assert grouped[0] == ((first[1][0], second[1][0]), first[2], "backend")


@pytest.mark.parametrize(
    "field", ["pack_id", "publisher_lineage", "digest", "implementation_digest", "backend_id"]
)
def test_capture_never_shares_across_verified_identity(field):
    grouped = group_host_provider_captures(
        (
            _loaded("function.a"),
            _loaded("function.b", **{field: "other"}),
        )
    )
    assert len(grouped) == 2


@pytest.mark.parametrize(
    "declaration",
    [
        (),
        ["function.a"],
        ("function.b",),
        ("function.a", "function.a"),
        ("function.b", "function.a"),
        (None,),
    ],
)
def test_malformed_group_cannot_change_capture_lifetime(declaration):
    item = _loaded("function.a")
    item[2].capture_group = declaration
    with pytest.raises(AuthorizationError):
        group_host_provider_captures((item,))


def test_undeclared_factories_remain_separate():
    first, second = _loaded("function.a"), _loaded("function.b")
    first[2].capture_group = second[2].capture_group = None
    assert len(group_host_provider_captures((first, second))) == 2
