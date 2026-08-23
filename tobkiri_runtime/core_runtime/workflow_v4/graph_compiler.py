"""Deterministic ``rumi_graph`` adapter for Workflow v4 definitions.

The adapter is deliberately authority-free.  It accepts only exact operation
identities from an already captured Contract catalog and emits a normal
Workflow v4 definition.  It never discovers Packs, resolves legacy handlers,
or executes a graph node.
"""

from __future__ import annotations

from collections import defaultdict
import heapq
import re
from typing import Any, Mapping

from .models import WorkflowValidationError, require_mapping

_GRAPH_NODE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_STEP_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_WHEN = re.compile(
    r"^(?:true|false|inputs\.[a-z][a-z0-9_.-]*\s*(?:==|!=)\s*"
    r"(?:true|false|null|-?[0-9]+|'[^']{0,256}'))$"
)
_MAX_NODES = 1_026
_MAX_EDGES = 4_096


class GraphCompilerV4:
    """Compile one bounded editor graph against a captured Contract catalog."""

    def __init__(self, catalog_snapshot: Mapping[str, Any]) -> None:
        """Capture exact operation identities and manifest schema contracts."""

        self._catalog_digest = self._required_string(
            catalog_snapshot, "catalog_digest", "Contract catalog"
        )
        security_epoch = catalog_snapshot.get("security_epoch")
        if not isinstance(security_epoch, int) or security_epoch < 0:
            raise WorkflowValidationError(
                "Contract catalog security_epoch is invalid"
            )
        self._security_epoch = security_epoch
        raw_operations = catalog_snapshot.get("operations")
        if not isinstance(raw_operations, list):
            raise WorkflowValidationError("Contract catalog operations are invalid")
        self._operations: dict[tuple[str, str, str, str], Mapping[str, Any]] = {}
        for index, raw_operation in enumerate(raw_operations):
            operation = require_mapping(
                raw_operation, f"Contract catalog operations[{index}]"
            )
            key = self._operation_key(operation, f"Contract catalog operations[{index}]")
            self._required_string(
                operation, "input_schema_digest", f"Contract catalog operations[{index}]"
            )
            self._required_string(
                operation, "output_schema_digest", f"Contract catalog operations[{index}]"
            )
            if key in self._operations:
                raise WorkflowValidationError(
                    "Contract catalog has duplicate operation identity"
                )
            self._operations[key] = operation

    @property
    def catalog_digest(self) -> str:
        """Return the captured catalog digest used by this compiler."""

        return self._catalog_digest

    @property
    def security_epoch(self) -> int:
        """Return the captured security epoch used by this compiler."""

        return self._security_epoch

    def compile(self, graph: Mapping[str, Any]) -> dict[str, Any]:
        """Return a Workflow v4 document and normalized editor graph.

        Graph edges express ordering only.  Every incoming step edge becomes a
        ``depends_on`` entry.  An edge condition becomes the target step's
        restricted ``when`` expression when the mapping is lossless.
        """

        self._reject_unknown(
            graph,
            {
                "version",
                "flow_id",
                "name",
                "max_concurrency",
                "entrypoint_node_id",
                "nodes",
                "edges",
            },
            "rumi_graph",
        )
        if graph.get("version") != 1:
            raise WorkflowValidationError("rumi_graph.version must be 1")
        raw_nodes = graph.get("nodes")
        raw_edges = graph.get("edges")
        if not isinstance(raw_nodes, list) or not 1 <= len(raw_nodes) <= _MAX_NODES:
            raise WorkflowValidationError(
                f"rumi_graph.nodes must contain 1..{_MAX_NODES} nodes"
            )
        if not isinstance(raw_edges, list) or len(raw_edges) > _MAX_EDGES:
            raise WorkflowValidationError(
                f"rumi_graph.edges must contain at most {_MAX_EDGES} edges"
            )

        nodes: dict[str, dict[str, Any]] = {}
        runtime_ids: set[str] = set()
        for index, raw_node in enumerate(raw_nodes):
            node = self._normalize_node(raw_node, index)
            node_id = node["id"]
            if node_id in nodes:
                raise WorkflowValidationError(
                    f"rumi_graph.nodes[{index}].id is duplicated"
                )
            if node["type"] == "step":
                runtime_id = node["data"]["id"]
                if runtime_id in runtime_ids:
                    raise WorkflowValidationError(
                        f"Workflow step id {runtime_id} is duplicated"
                    )
                runtime_ids.add(runtime_id)
            nodes[node_id] = node

        entrypoint = self._required_string(graph, "entrypoint_node_id", "rumi_graph")
        entry = nodes.get(entrypoint)
        if entry is None or entry["type"] != "trigger":
            raise WorkflowValidationError(
                "rumi_graph.entrypoint_node_id must identify the trigger node"
            )
        if entry["data"].get("type") != "rumi_start":
            raise WorkflowValidationError("rumi_graph trigger must be rumi_start")
        trigger_count = sum(node["type"] == "trigger" for node in nodes.values())
        if trigger_count != 1:
            raise WorkflowValidationError("rumi_graph must contain exactly one trigger")

        normalized_edges: list[dict[str, Any]] = []
        outgoing: dict[str, list[str]] = defaultdict(list)
        incoming: dict[str, list[dict[str, Any]]] = defaultdict(list)
        indegree = {node_id: 0 for node_id in nodes}
        edge_ids: set[str] = set()
        for index, raw_edge in enumerate(raw_edges):
            edge = self._normalize_edge(raw_edge, index, nodes)
            edge_id = edge["id"]
            if edge_id in edge_ids:
                raise WorkflowValidationError(
                    f"rumi_graph.edges[{index}].id is duplicated"
                )
            edge_ids.add(edge_id)
            if nodes[edge["source"]]["type"] == "end":
                raise WorkflowValidationError("rumi_graph end nodes cannot have outputs")
            if nodes[edge["target"]]["type"] == "trigger":
                raise WorkflowValidationError("rumi_graph trigger cannot have inputs")
            normalized_edges.append(edge)
            outgoing[edge["source"]].append(edge["target"])
            incoming[edge["target"]].append(edge)
            indegree[edge["target"]] += 1

        ordered_node_ids = self._topological_order(nodes, outgoing, indegree)
        reachable = self._reachable(entrypoint, outgoing)
        unreachable_steps = sorted(
            node_id
            for node_id, node in nodes.items()
            if node["type"] == "step" and node_id not in reachable
        )
        if unreachable_steps:
            raise WorkflowValidationError(
                "rumi_graph contains unreachable steps: " + ", ".join(unreachable_steps)
            )

        steps = []
        for node_id in ordered_node_ids:
            node = nodes[node_id]
            if node["type"] != "step":
                continue
            predecessors = sorted(
                {
                    nodes[edge["source"]]["data"]["id"]
                    for edge in incoming[node_id]
                    if nodes[edge["source"]]["type"] == "step"
                }
            )
            conditions = {
                str(edge["data"]["when"])
                for edge in incoming[node_id]
                if "when" in edge["data"]
            }
            has_unconditional = any(
                "when" not in edge["data"] for edge in incoming[node_id]
            )
            if len(conditions) > 1 or (conditions and has_unconditional):
                raise WorkflowValidationError(
                    f"step {node['data']['id']} has branch conditions that cannot "
                    "map to one Workflow v4 when expression"
                )
            step = {
                "id": node["data"]["id"],
                "request": dict(node["data"]["request"]),
            }
            if predecessors:
                step["depends_on"] = predecessors
            if conditions:
                step["when"] = next(iter(conditions))
            for field in ("retry", "timeout_ms"):
                if field in node["data"]:
                    value = node["data"][field]
                    step[field] = dict(value) if isinstance(value, Mapping) else value
            steps.append(step)
        if not steps:
            raise WorkflowValidationError("rumi_graph must contain at least one step")

        document: dict[str, Any] = {
            "workflow_api_version": "io.tobkiri.workflow.v4",
            "name": self._graph_name(graph),
            "max_concurrency": self._max_concurrency(graph),
            "steps": steps,
        }
        normalized_graph = {
            "version": 1,
            "name": document["name"],
            "max_concurrency": document["max_concurrency"],
            "entrypoint_node_id": entrypoint,
            "nodes": [nodes[node_id] for node_id in sorted(nodes)],
            "edges": sorted(
                normalized_edges,
                key=lambda edge: (edge["source"], edge["target"], edge["id"]),
            ),
        }
        return {"document": document, "normalized_graph": normalized_graph}

    def _normalize_node(self, raw_node: Any, index: int) -> dict[str, Any]:
        context = f"rumi_graph.nodes[{index}]"
        node = require_mapping(raw_node, context)
        self._reject_unknown(node, {"id", "type", "position", "data"}, context)
        node_id = self._required_string(node, "id", context)
        if _GRAPH_NODE_ID.fullmatch(node_id) is None:
            raise WorkflowValidationError(
                f"rumi_graph.nodes[{index}].id is invalid"
            )
        node_type = self._required_string(node, "type", context)
        if node_type not in {"trigger", "step", "end"}:
            raise WorkflowValidationError(
                f"rumi_graph.nodes[{index}].type is invalid"
            )
        if "position" in node:
            self._validate_position(node["position"], context)
        data_context = f"{context}.data"
        data = require_mapping(node.get("data"), data_context)
        allowed_data = {
            "trigger": {"type", "ports", "title", "description"},
            "step": {
                "id",
                "request",
                "ports",
                "retry",
                "timeout_ms",
                "title",
                "description",
            },
            "end": {"ports", "title", "description"},
        }[node_type]
        self._reject_unknown(data, allowed_data, data_context)
        self._optional_text(data, "title", 256, data_context)
        self._optional_text(data, "description", 4_096, data_context)
        normalized_data: dict[str, Any] = {}
        operation: Mapping[str, Any] | None = None
        if node_type == "trigger":
            normalized_data["type"] = self._required_string(
                data, "type", f"rumi_graph.nodes[{index}].data"
            )
        elif node_type == "step":
            step_id = self._required_string(
                data, "id", f"rumi_graph.nodes[{index}].data"
            )
            if _STEP_ID.fullmatch(step_id) is None:
                raise WorkflowValidationError(f"Workflow step id {step_id} is invalid")
            request = require_mapping(
                data.get("request"), f"rumi_graph.nodes[{index}].data.request"
            )
            self._reject_unknown(
                request,
                {
                    "contract_id",
                    "contract_revision_digest",
                    "operation_id",
                    "function_principal_id",
                    "input",
                },
                f"rumi_graph.nodes[{index}].data.request",
            )
            operation = self._operations.get(
                self._operation_key(
                    request, f"rumi_graph.nodes[{index}].data.request"
                )
            )
            if operation is None:
                raise WorkflowValidationError(
                    f"rumi_graph.nodes[{index}] is not an exact active catalog operation"
                )
            request_input = require_mapping(
                request.get("input"),
                f"rumi_graph.nodes[{index}].data.request.input",
            )
            normalized_data.update(
                {
                    "id": step_id,
                    "request": {
                        "contract_id": request["contract_id"],
                        "contract_revision_digest": request[
                            "contract_revision_digest"
                        ],
                        "operation_id": request["operation_id"],
                        "function_principal_id": request["function_principal_id"],
                        "input": dict(request_input),
                    },
                }
            )
            if "retry" in data:
                retry_context = f"rumi_graph.nodes[{index}].data.retry"
                retry = require_mapping(data["retry"], retry_context)
                self._reject_unknown(
                    retry, {"max_attempts", "backoff_ms"}, retry_context
                )
                normalized_data["retry"] = dict(retry)
            if "timeout_ms" in data:
                normalized_data["timeout_ms"] = data["timeout_ms"]

        raw_ports = data.get("ports")
        if operation is None:
            normalized_data["ports"] = self._normalize_ports(raw_ports, index)
        else:
            manifest_ports = self._normalize_ports(
                self._manifest_ports(operation), index
            )
            if raw_ports is not None:
                supplied_ports = self._normalize_ports(raw_ports, index)
                if supplied_ports != manifest_ports:
                    raise WorkflowValidationError(
                        f"rumi_graph.nodes[{index}] step ports must match the "
                        "captured operation schema contracts"
                    )
            normalized_data["ports"] = manifest_ports
        return {"id": node_id, "type": node_type, "data": normalized_data}

    def _normalize_ports(self, raw_ports: Any, node_index: int) -> list[dict[str, Any]]:
        if (
            not isinstance(raw_ports, list)
            or not raw_ports
            or len(raw_ports) > 64
        ):
            raise WorkflowValidationError(
                f"rumi_graph.nodes[{node_index}] must have 1..64 port contracts"
            )
        ports = []
        port_ids: set[str] = set()
        for port_index, raw_port in enumerate(raw_ports):
            context = f"rumi_graph.nodes[{node_index}].data.ports[{port_index}]"
            port = require_mapping(raw_port, context)
            self._reject_unknown(port, {"id", "direction", "contracts"}, context)
            port_id = self._required_string(port, "id", context)
            if _GRAPH_NODE_ID.fullmatch(port_id) is None or port_id in port_ids:
                raise WorkflowValidationError(f"{context}.id is invalid or duplicated")
            port_ids.add(port_id)
            direction = self._required_string(port, "direction", context)
            if direction not in {"input", "output"}:
                raise WorkflowValidationError(f"{context}.direction is invalid")
            contracts = port.get("contracts")
            if (
                not isinstance(contracts, list)
                or not contracts
                or len(contracts) > 64
                or not all(
                    isinstance(item, str) and 1 <= len(item) <= 256
                    for item in contracts
                )
                or len(set(contracts)) != len(contracts)
            ):
                raise WorkflowValidationError(
                    f"{context}.contracts must be a non-empty string array"
                )
            ports.append(
                {
                    "id": port_id,
                    "direction": direction,
                    "contracts": sorted(set(contracts)),
                }
            )
        return sorted(ports, key=lambda port: (port["direction"], port["id"]))

    def _normalize_edge(
        self,
        raw_edge: Any,
        index: int,
        nodes: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        edge = require_mapping(raw_edge, f"rumi_graph.edges[{index}]")
        context = f"rumi_graph.edges[{index}]"
        self._reject_unknown(
            edge,
            {"id", "source", "target", "sourceHandle", "targetHandle", "data"},
            context,
        )
        edge_id = self._required_string(edge, "id", context)
        source = self._required_string(edge, "source", context)
        target = self._required_string(edge, "target", context)
        if _GRAPH_NODE_ID.fullmatch(edge_id) is None:
            raise WorkflowValidationError(f"{context}.id is invalid")
        if source not in nodes or target not in nodes or source == target:
            raise WorkflowValidationError(f"{context} has invalid endpoints")
        source_handle = edge.get("sourceHandle")
        target_handle = edge.get("targetHandle")
        if not isinstance(source_handle, str) or not source_handle:
            raise WorkflowValidationError(f"{context}.sourceHandle is required")
        if not isinstance(target_handle, str) or not target_handle:
            raise WorkflowValidationError(f"{context}.targetHandle is required")
        source_port = self._find_port(nodes[source], source_handle)
        target_port = self._find_port(nodes[target], target_handle)
        if source_port is None or source_port["direction"] != "output":
            raise WorkflowValidationError(f"{context} source port is unavailable")
        if target_port is None or target_port["direction"] != "input":
            raise WorkflowValidationError(f"{context} target port is unavailable")
        if not set(source_port["contracts"]).intersection(target_port["contracts"]):
            raise WorkflowValidationError(f"{context} port contracts do not match")
        data = edge.get("data", {})
        if not isinstance(data, Mapping):
            raise WorkflowValidationError(f"{context}.data must be an object")
        self._reject_unknown(data, {"when"}, f"{context}.data")
        normalized_data: dict[str, Any] = {}
        if "when" in data:
            when = data["when"]
            if not isinstance(when, str) or _WHEN.fullmatch(when) is None:
                raise WorkflowValidationError(
                    f"{context}.data.when is outside the restricted CEL subset"
                )
            normalized_data["when"] = when
        return {
            "id": edge_id,
            "source": source,
            "target": target,
            "sourceHandle": source_handle,
            "targetHandle": target_handle,
            "data": normalized_data,
        }

    def _manifest_ports(self, operation: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "id": "contract-input",
                "direction": "input",
                "contracts": [str(operation["input_schema_digest"])],
            },
            {
                "id": "contract-output",
                "direction": "output",
                "contracts": [str(operation["output_schema_digest"])],
            },
        ]

    def _operation_key(
        self, operation: Mapping[str, Any], context: str
    ) -> tuple[str, str, str, str]:
        return (
            self._required_string(operation, "contract_id", context),
            self._required_string(operation, "contract_revision_digest", context),
            self._required_string(operation, "operation_id", context),
            self._required_string(operation, "function_principal_id", context),
        )

    def _topological_order(
        self,
        nodes: Mapping[str, Mapping[str, Any]],
        outgoing: Mapping[str, list[str]],
        indegree: Mapping[str, int],
    ) -> list[str]:
        remaining = dict(indegree)
        ready = [node_id for node_id, degree in remaining.items() if degree == 0]
        heapq.heapify(ready)
        ordered = []
        while ready:
            node_id = heapq.heappop(ready)
            ordered.append(node_id)
            for target in sorted(outgoing.get(node_id, [])):
                remaining[target] -= 1
                if remaining[target] == 0:
                    heapq.heappush(ready, target)
        if len(ordered) != len(nodes):
            raise WorkflowValidationError("rumi_graph contains a cycle")
        return ordered

    def _reachable(
        self, entrypoint: str, outgoing: Mapping[str, list[str]]
    ) -> set[str]:
        reachable = {entrypoint}
        pending = [entrypoint]
        while pending:
            current = pending.pop()
            for target in outgoing.get(current, []):
                if target not in reachable:
                    reachable.add(target)
                    pending.append(target)
        return reachable

    def _find_port(
        self, node: Mapping[str, Any], port_id: str
    ) -> Mapping[str, Any] | None:
        data = require_mapping(node.get("data"), "normalized graph node data")
        ports = data.get("ports", [])
        return next((port for port in ports if port["id"] == port_id), None)

    def _graph_name(self, graph: Mapping[str, Any]) -> str:
        self._optional_text(graph, "flow_id", 256, "rumi_graph")
        self._optional_text(graph, "name", 256, "rumi_graph")
        value = graph.get("name", graph.get("flow_id", "Graph workflow"))
        if not isinstance(value, str) or not 1 <= len(value) <= 256:
            raise WorkflowValidationError("rumi_graph name is invalid")
        return value

    def _max_concurrency(self, graph: Mapping[str, Any]) -> int:
        value = graph.get("max_concurrency", 1)
        if not isinstance(value, int) or not 1 <= value <= 32:
            raise WorkflowValidationError(
                "rumi_graph.max_concurrency must be between 1 and 32"
            )
        return value

    @staticmethod
    def _required_string(value: Mapping[str, Any], key: str, context: str) -> str:
        result = value.get(key)
        if not isinstance(result, str) or not result:
            raise WorkflowValidationError(f"{context}.{key} is required")
        return result

    @staticmethod
    def _optional_text(
        value: Mapping[str, Any], key: str, maximum: int, context: str
    ) -> None:
        result = value.get(key)
        if result is not None and (
            not isinstance(result, str) or not 1 <= len(result) <= maximum
        ):
            raise WorkflowValidationError(f"{context}.{key} is invalid")

    @staticmethod
    def _reject_unknown(
        value: Mapping[str, Any], allowed: set[str], context: str
    ) -> None:
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise WorkflowValidationError(
                f"{context} has unknown properties: {', '.join(unknown)}"
            )

    def _validate_position(self, value: Any, context: str) -> None:
        position = require_mapping(value, f"{context}.position")
        self._reject_unknown(position, {"x", "y"}, f"{context}.position")
        if set(position) != {"x", "y"} or any(
            not isinstance(position[axis], (int, float))
            or isinstance(position[axis], bool)
            for axis in ("x", "y")
        ):
            raise WorkflowValidationError(f"{context}.position is invalid")


__all__ = ["GraphCompilerV4"]
