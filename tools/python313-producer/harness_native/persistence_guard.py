"""Fail-closed Source proof for the catalog-only Host dependency replacement.

These are reviewed AST fingerprints, not runtime authorization or observed
execution counts. Both successful and exceptional branches of the complete
catalog/validator bodies are pinned. Any change requires another reachability
review; a new import/use/branch cannot silently inherit this qualification.
"""

from __future__ import annotations

import ast
from collections import Counter
import hashlib
import json
from typing import Mapping

RUNTIME = "tobkiri_runtime/"
SERVICE = RUNTIME + "ecosystem/defaultspack/domain/runtime_v4/service.py"
COMPOSITION = RUNTIME + "ecosystem/defaultspack/defaultspack/profile_runtime_composition.py"
SECURE = RUNTIME + "tobkiri_protocol/secure_persistence.py"
VIEW = "tools/python313-producer/harness_native/persistence_view.py"
_ENTRY = "783e97b5efe8918df15b2d23e7cecea249cec4d4bcfade1dc56fa7cf43711a80"
_COMPOSITION = "348f06dbe67f9482dffa1dc115cb4739607392162d392306ee70f1f4394b497a"
_INITIALIZATION = "f5d908bd386abb162423be3714bf5eea2fc7b95644fd3ef687f77d37558f4c30"
_PURE = {
    "tobkiri_protocol/__init__.py": "b2111f21fbd25dd9d132eadaada28a577dbaa3eea6d01bc728b937c8211200d1",
    "tobkiri_protocol/bundle_catalog.py": "884a084764c1ebf2d057b0a0a611c03eccc4a49b547c35916358cf65ca083200",
    "tobkiri_protocol/canonical.py": "5146f3e61c3fc41de76de1f332a03d92f6c8902dfbc3cfa861c8e87a8810412b",
    "tobkiri_protocol/captured_inputs.py": "2192d13b8a65d597eef7abad46614e344f3605f412db40ef4bb5b7b2161fd4e7",
    "tobkiri_protocol/errors.py": "ccdf194b1a040064456e970ffb014ff56ee458e0177b0701fb991304f6d99209",
    "tobkiri_protocol/executable_catalog.py": "093086b317915ed4d673887fde70b428ec6bd3e09245b7405732bd9810dd3971",
    "tobkiri_protocol/ids.py": "1c0fac10d59cf154c5c0b9a23e6b108ea83b7b93673192be7e9cde6e71935028",
    "tobkiri_protocol/platform_artifact.py": "37bcd885d89d0862851f6cd909d95c9e4b9006baff3a227138d22b927b6eabff",
    "tobkiri_protocol/profile_scope.py": "bf3b738f560ec2e9534dd9a24bcfbc33c9bf077280b50dbd86756430f3078630",
    "tobkiri_protocol/validation.py": "050dbb0ae5119b6c5f94afc40bcf16d261737d3c0abd0ea337bd8ce2b59a84e2",
    "ecosystem/defaultspack/domain/profile_resolution.py": "b4ce357cfeea5537d28b2bf05c02fde6b15214d2a08bcfb26b55a610c8fde61a",
    "ecosystem/defaultspack/domain/_profile_runtime_records.py": "aa88b5462fd970127f0713922a48b2eaf70b9a0cd350aa60714ada6afee3ad26",
    "ecosystem/defaultspack/domain/runtime_v4/_record_validation_cache.py": "371bc47fea5756af4ae6abe9f5aa3d5ddac62ed783292ad5f0e8979e9e2be98a",
}
_NAMES = {"SecureDirectory", "SecurePersistenceError"}
_METHODS = (
    "bundled_profile_root", "host_resource_root", "bootstrap_profile_id",
    "setup_profile_preview", "_confirmation", "bootstrap_confirmation",
    "profile_confirmation", "setup_listing", "setup_preview",
    "setup_activation_decision", "setup_activation_success",
    "setup_activation_failure", "retired_setup_response", "load_catalog",
    "catalog_with_packs", "catalog_with_profiles", "resolve_profile",
    "dynamic_profile_edges", "denied",
)
_USES = Counter({
    ("SecureDirectory", "__init__"): 1,
    ("SecurePersistenceError", "__init__"): 1,
    ("SecurePersistenceError", "_write_state"): 1,
    ("SecurePersistenceError", "_read_state"): 1,
    ("SecurePersistenceError", "_unlink_state"): 1,
    ("SecurePersistenceError", "_activation_lock"): 1,
})


def _hash(node: ast.AST) -> str:
    # Stable across the source-tool Python versions: ast.dump changed its
    # rendering of empty optional fields. These sources have no type params.
    def shape(value):
        if isinstance(value, ast.AST):
            return [type(value).__name__, [
                [name, shape(child)] for name, child in ast.iter_fields(value)
                if name != "type_params" or child
            ]]
        if isinstance(value, list):
            return [shape(child) for child in value]
        if type(value) is bytes:
            return ["bytes", value.hex()]
        if value is Ellipsis:
            return ["ellipsis"]
        if type(value) is complex:
            return ["complex", value.real, value.imag]
        return value
    encoded = json.dumps(shape(node), ensure_ascii=True, allow_nan=False,
                         separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


class _Initialization(ast.NodeTransformer):
    def visit_FunctionDef(self, node):
        node.body = [ast.Pass()]
        return node

    visit_AsyncFunctionDef = visit_FunctionDef


def validate(files: Mapping[str, bytes]) -> dict:
    """Inspect source bytes only; execute no original/facade module or AST."""
    def tree(path):
        if type(files.get(path)) is not bytes:
            raise ValueError("catalog persistence guard source is missing")
        return ast.parse(files[path], filename=path)

    reviewed = {}
    for path, expected in _PURE.items():
        parsed = tree(RUNTIME + path)
        if _hash(parsed) != expected or any(
            isinstance(node, ast.Name) and node.id in _NAMES
            for node in ast.walk(parsed)
        ):
            raise ValueError("catalog success/failure Source closure changed")
        reviewed[path] = expected

    composition = tree(COMPOSITION)
    if _hash(composition) != _COMPOSITION:
        raise ValueError("catalog runtime Source closure changed")
    dispatcher = tree("tools/python313-producer/harness_native/dispatcher.py")
    (methods,) = [node for node in dispatcher.body if isinstance(node, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "METHODS"
                          for t in node.targets)]
    if ast.literal_eval(methods.value) != _METHODS:
        raise ValueError("catalog runtime method inventory changed")
    (runtime,) = [node for node in composition.body
                  if isinstance(node, ast.ClassDef)
                  and node.name == "DefaultspackProfileRuntime"]
    (entry,) = [node for node in runtime.body
                if isinstance(node, ast.FunctionDef) and node.name == "load_catalog"]
    if _hash(entry) != _ENTRY:
        raise ValueError("catalog dependency-replacement entry changed")

    service = tree(SERVICE)
    (dependency,) = [node for node in ast.walk(service)
                     if isinstance(node, ast.ImportFrom)
                     and node.module == "tobkiri_protocol.secure_persistence"]
    if (dependency not in service.body or dependency.level
            or [(alias.name, alias.asname) for alias in dependency.names]
            != [("SecureDirectory", None), ("SecurePersistenceError", None)]):
        raise ValueError("catalog Host dependency import changed")
    parents = {child: node for node in ast.walk(service)
               for child in ast.iter_child_nodes(node)}
    uses = Counter()
    for node in ast.walk(service):
        if not isinstance(node, ast.Name) or node.id not in _NAMES:
            continue
        chain, current = [], node
        while current in parents:
            current = parents[current]
            chain.append(current)
        methods = [item.name for item in chain
                   if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]
        classes = [item.name for item in chain if isinstance(item, ast.ClassDef)]
        if len(methods) != 1 or classes != ["ActivationStore"]:
            raise ValueError("Host persistence symbol escaped its unused methods")
        if node.id == "SecureDirectory":
            parent = parents[node]
            if (not isinstance(parent, ast.Call) or parent.func is not node
                    or ast.dump(parent) != ast.dump(ast.parse(
                        "SecureDirectory(self.state_root, create=True)", mode="eval"
                    ).body)):
                raise ValueError("Host persistence constructor use changed")
        elif not any(
            isinstance(item, ast.ExceptHandler)
            and item.type is not None and node in ast.walk(item.type)
            for item in chain
        ):
            raise ValueError("Host exception symbol acquired a semantic use")
        uses[(node.id, methods[0])] += 1
    if uses != _USES or _hash(_Initialization().visit(tree(SERVICE))) != _INITIALIZATION:
        raise ValueError("Host persistence use or service initialization changed")

    # This is a translated class with this private facade's own identity. The
    # original persistence module is never executed or entered into sys.modules.
    secure, view = tree(SECURE), tree(VIEW)
    def error_body(parsed, path):
        (node,) = [item for item in parsed.body if isinstance(item, ast.ClassDef)
                   and item.name == "SecurePersistenceError"]
        if (node.keywords or node.decorator_list or len(node.bases) != 1
                or not isinstance(node.bases[0], ast.Name)
                or node.bases[0].id != "OSError" or len(node.body) != 1
                or not isinstance(node.body[0], ast.Expr)
                or not isinstance(node.body[0].value, ast.Constant)
                or type(node.body[0].value.value) is not str):
            raise ValueError("translated private Host exception body changed")
        return ast.get_source_segment(files[path].decode("utf-8"), node)
    if error_body(secure, SECURE) != error_body(view, VIEW):
        raise ValueError("translated private Host exception body changed")

    return {
        "format": "io.tobkiri.catalog-host-dependency-replacement.v1",
        "scope": "catalog-only",
        "source_closure_ast_sha256": reviewed,
        "entry_ast_sha256": _ENTRY,
        "runtime_ast_sha256": _COMPOSITION,
        "service_initialization_ast_sha256": _INITIALIZATION,
        "catalog_success_and_failure_symbol_uses": 0,
        "translated_exception_is_original_module_class": False,
        "original_import_environment_preserved": False,
        "host_persistence_connected": False,
        "runtime_execution_proven": False,
    }
