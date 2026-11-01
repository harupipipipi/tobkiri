"""Trace source-level core imports without inventing call-time provider proof.

Entities retain component scope, core instance, kind and local index. Equal
module hashes, types or numeric global values never merge instances. Nested
component instantiation and canonical-function semantics remain unresolved.
"""

from __future__ import annotations


class SourceBindings:
    """Resolve only explicit core module/instance/alias/export/import edges."""

    def __init__(self, rows: list[list[str]], modules: list[dict]):
        self.modules = {m["id"]: m for m in modules}
        self.module_indices = {}
        self.parents = {}
        self.instances = {}
        self.arguments = {}
        self.exports = {}
        self.core_aliases = {}
        self.canonicals = {}
        self.definitions = {}
        for module in modules:
            for body in module["bodies"]:
                self._put(
                    self.definitions,
                    (module["id"], "Func", body["index"]),
                    [str(body["offset"]), str(body["bytes"])],
                )
        body_rows = set()
        for row in rows:
            tag = row[0]
            if tag == "module":
                self._put(
                    self.module_indices,
                    (int(row[1]), int(row[2])),
                    ("definition", int(row[3])),
                )
            elif tag == "component":
                self._put(self.parents, int(row[3]), int(row[1]))
            elif tag == "component-export" and row[3] == "Module":
                self._put(
                    self.module_indices,
                    (int(row[1]), int(row[4])),
                    ("alias", int(row[1]), int(row[5])),
                )
            elif tag == "outer-alias" and row[2] == "CoreModule":
                self._put(
                    self.module_indices,
                    (int(row[1]), int(row[3])),
                    ("outer", int(row[4]), int(row[5])),
                )
            elif tag == "instantiate":
                self._put(
                    self.instances, (int(row[1]), int(row[2])), ("module", int(row[3]))
                )
            elif tag == "instance-exports":
                self._put(self.instances, (int(row[1]), int(row[2])), ("exports",))
            elif tag == "argument":
                if row[4] != "Instance":
                    raise ValueError("unsupported core argument kind")
                self._put(
                    self.arguments, (int(row[1]), int(row[2]), row[3]), int(row[5])
                )
            elif tag == "instance-export":
                self._put(
                    self.exports,
                    (int(row[1]), int(row[2]), self.kind(row[4]), row[3]),
                    int(row[5]),
                )
            elif tag == "core-alias":
                self._put(
                    self.core_aliases,
                    (int(row[1]), self.kind(row[2]), int(row[3])),
                    (int(row[4]), row[5]),
                )
            elif tag == "canonical-core-function":
                self._put(self.canonicals, (int(row[1]), "Func", int(row[2])), row[3])
            elif tag in {"global", "memory", "table"}:
                self._put(
                    self.definitions, (int(row[1]), tag.title(), int(row[2])), row[3:]
                )
            elif tag == "body":
                key = (int(row[1]), "Func", int(row[2]))
                if key in body_rows or self.definitions.get(key) != row[3:]:
                    raise ValueError(
                        "function row differs from independently bound body"
                    )
                body_rows.add(key)

    @staticmethod
    def _put(mapping: dict, key: object, value: object) -> None:
        if key in mapping:
            raise ValueError("duplicate source namespace identity")
        mapping[key] = value

    @staticmethod
    def kind(kind: str) -> str:
        """Exact function references occupy the same function index space."""
        return "Func" if kind == "FuncExact" else kind

    def _walk(self, query: tuple, chain: list, seen: set) -> dict:
        if query in seen or len(chain) >= 128:
            raise ValueError("source binding cycle or depth bound")
        seen = {*seen, query}
        chain.append(list(query))
        operation, *args = query
        if operation == "module":
            scope, index = args
            binding = self.module_indices.get((scope, index))
            if binding is None:
                raise ValueError(
                    "external or component-derived module remains unresolved"
                )
            if binding[0] == "definition":
                return {"module": binding[1]}
            if binding[0] == "alias":
                return self._walk(("module", *binding[1:]), chain, seen)
            _, count, target = binding
            for _ in range(count):
                scope = self.parents[scope]
            return self._walk(("module", scope, target), chain, seen)
        if operation == "core":
            scope, kind, index = args
            key = (scope, kind, index)
            if key in self.canonicals:
                return {
                    "endpoint": "canonical-core-function",
                    "scope": scope,
                    "index": index,
                    "declaration": self.canonicals[key],
                    "semantics_resolved": False,
                }
            instance, name = self.core_aliases[key]
            return self._walk(("export", scope, instance, kind, name), chain, seen)
        if operation == "export":
            scope, instance, kind, name = args
            binding = self.instances[(scope, instance)]
            if binding[0] == "exports":
                index = self.exports[(scope, instance, kind, name)]
                return self._walk(("core", scope, kind, index), chain, seen)
            module = self._walk(("module", scope, binding[1]), chain, seen)["module"]
            matches = [
                e
                for e in self.modules[module]["exports"]
                if e[0] == name and self.kind(e[1]) == kind
            ]
            if len(matches) != 1:
                raise ValueError("core export name and kind did not resolve uniquely")
            return self._walk(
                ("entity", scope, instance, module, kind, int(matches[0][2])),
                chain,
                seen,
            )
        if operation == "entity":
            scope, instance, module, kind, index = args
            imports = [
                i
                for i in self.modules[module]["imports"]
                if self.kind(i[2]) == kind and int(i[3]) == index
            ]
            if imports:
                if len(imports) != 1:
                    raise ValueError("duplicate module-local imported entity")
                imported = imports[0]
                argument = self.arguments[(scope, instance, imported[0])]
                return self._walk(
                    ("export", scope, argument, kind, imported[1]), chain, seen
                )
            definition = self.definitions[(module, kind, index)]
            return {
                "endpoint": "defined-source-entity",
                "scope": scope,
                "instance": instance,
                "module": module,
                "kind": kind,
                "index": index,
                "definition": definition,
                "identity_scope": "source core instance, not a runtime observation",
            }
        raise ValueError("unknown source query")

    def resolve(self, scope: int, instance: int, kind: str, name: str) -> dict:
        """Retain unsupported edges as unresolved with their traversed path."""
        chain = []
        try:
            endpoint = self._walk(
                ("export", scope, instance, self.kind(kind), name), chain, set()
            )
            return {
                "status": "source-edge-resolved",
                "endpoint": endpoint,
                "chain": chain,
                "runtime_provider_verified": False,
            }
        except (ValueError, KeyError) as error:
            return {
                "status": "unresolved",
                "reason": str(error),
                "chain": chain,
                "runtime_provider_verified": False,
            }

    def import_queries(self) -> list[dict]:
        """Trace poll/errno/shared-memory edges for every source core instance."""
        result = []
        names = {"poll", "errno", "h_errno", "memory", "__indirect_function_table"}
        for (scope, instance), binding in self.instances.items():
            if binding[0] != "module":
                continue
            try:
                module = self._walk(("module", scope, binding[1]), [], set())["module"]
            except (ValueError, KeyError):
                continue
            for imported in self.modules[module]["imports"]:
                namespace, name, kind, index, *_ = imported
                if name not in names:
                    continue
                chain = []
                try:
                    endpoint = self._walk(
                        (
                            "entity",
                            scope,
                            instance,
                            module,
                            self.kind(kind),
                            int(index),
                        ),
                        chain,
                        set(),
                    )
                    resolution = {
                        "status": "source-edge-resolved",
                        "endpoint": endpoint,
                    }
                except (ValueError, KeyError) as error:
                    resolution = {"status": "unresolved", "reason": str(error)}
                result.append(
                    {
                        "scope": scope,
                        "instance": instance,
                        "module": module,
                        "import_namespace": namespace,
                        "import_name": name,
                        "kind": kind,
                        "local_index": int(index),
                        **resolution,
                        "chain": chain,
                        "runtime_provider_verified": False,
                    }
                )
        return result
