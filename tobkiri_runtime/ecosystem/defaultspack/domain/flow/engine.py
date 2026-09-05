"""FlowEngine — フロー実行エンジン（最小動作版）"""

import sys
import os
import copy
import importlib.util
import re

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from blocks._common import error, ok

from .context import FlowContext
from .result import FlowResult
from .modifier import ModifierLoader


_TEMPLATE_RE = re.compile(r"^\{\{\s*(.*?)\s*\}\}$")
_SIMPLE_REF_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_:-]+)*$")
_SUPPORTED_DECLARATIVE_STEP_TYPES = {"function", "subflow", "branch", "parallel"}
_FLOW_STACK_LIMIT = 10


class FlowEngine:
    """Flow 実行エンジン（最小動作版）

    シングルトンパターンで実装。flow_id を受けて対応する handler.py を
    動的にロード・実行する。flows/ ディレクトリ配下のフロー定義を
    自動的にスキャンして登録する。
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._flows = {}
        self._handlers = {}
        self._modifier_loader = ModifierLoader()
        self._base_dir = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "..")
        )
        self._load_flows()

    def _load_flows(self):
        """flows/ 配下のフロー定義をロードする

        flows/ ディレクトリ内の各サブディレクトリを走査し、
        flow.yaml が存在するものをフロー定義として登録する。
        併せて、トップレベルの *.flow.yaml も宣言的フローとして登録する。
        """
        flows_dir = os.path.join(self._base_dir, "flows")
        if not os.path.isdir(flows_dir):
            return
        for entry in sorted(os.listdir(flows_dir)):
            flow_path = os.path.join(flows_dir, entry)
            if not os.path.isdir(flow_path):
                continue
            yaml_path = os.path.join(flow_path, "flow.yaml")
            if not os.path.isfile(yaml_path):
                continue
            flow_def = self._parse_yaml(yaml_path)
            flow_id = flow_def.get("flow_id", entry)
            flow_def["_dir"] = flow_path
            flow_def["_yaml_path"] = yaml_path
            if "flow_id" not in flow_def:
                flow_def["flow_id"] = flow_id
            self._flows[flow_id] = flow_def
        for entry in sorted(os.listdir(flows_dir)):
            if not entry.endswith(".flow.yaml"):
                continue
            yaml_path = os.path.join(flows_dir, entry)
            if not os.path.isfile(yaml_path):
                continue
            flow_def = self._parse_yaml(yaml_path)
            default_id = entry[: -len(".flow.yaml")]
            flow_id = flow_def.get("flow_id", default_id)
            flow_def["_dir"] = flows_dir
            flow_def["_yaml_path"] = yaml_path
            flow_def["_declarative"] = True
            if "flow_id" not in flow_def:
                flow_def["flow_id"] = flow_id
            self._flows[flow_id] = flow_def

    def _parse_yaml(self, path):
        """YAML ファイルをパースする

        PyYAML が利用可能ならそれを使い、なければトップレベルの
        key: value ペアのみを抽出する簡易パーサーにフォールバックする。

        Args:
            path: YAML ファイルのパス

        Returns:
            パース結果の dict
        """
        try:
            import yaml
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                return data if isinstance(data, dict) else {}
        except ImportError:
            return self._parse_yaml_fallback(path)
        except Exception:
            return self._parse_yaml_fallback(path)

    def _parse_yaml_fallback(self, path):
        """YAML の簡易フォールバックパーサー

        トップレベルの key: value ペアのみを抽出する。
        ネストされた構造は無視する。

        Args:
            path: YAML ファイルのパス

        Returns:
            パース結果の dict
        """
        result = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.rstrip("\n\r")
                    if not stripped or stripped.lstrip().startswith("#"):
                        continue
                    if stripped[0] in (" ", "\t"):
                        continue
                    if ":" not in stripped:
                        continue
                    key, _, value = stripped.partition(":")
                    key = key.strip()
                    value = value.strip()
                    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
                        value = value[1:-1]
                    elif value.startswith("'") and value.endswith("'") and len(value) >= 2:
                        value = value[1:-1]
                    if value and key:
                        result[key] = value
        except Exception:
            pass
        return result

    def _get_handler(self, flow_id):
        """フロー ID に対応するハンドラモジュールを取得する

        キャッシュされたモジュールがあればそれを返し、なければ
        importlib で動的にロードしてキャッシュする。

        Args:
            flow_id: フロー ID

        Returns:
            handler モジュール。見つからなければ None。
        """
        if flow_id in self._handlers:
            return self._handlers[flow_id]
        flow_def = self._flows.get(flow_id)
        if not flow_def:
            return None
        if flow_def.get("_declarative") and "handler" not in flow_def:
            return None
        handler_file = flow_def.get("handler", "handler.py")
        flow_dir = flow_def.get("_dir", "")
        handler_path = os.path.join(flow_dir, handler_file)
        if not os.path.isfile(handler_path):
            return None
        try:
            module_name = "flows_{}_handler".format(
                flow_id.replace("/", "_").replace("-", "_")
            )
            spec = importlib.util.spec_from_file_location(module_name, handler_path)
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self._handlers[flow_id] = module
            return module
        except Exception:
            return None

    def _validate_declarative_flow(self, flow_def):
        """Validate a declarative YAML flow before execution.

        The runtime still supports legacy handler flows, but declarative flows
        are intentionally strict: orchestration steps may be function/subflow/
        branch/parallel only, and references must point to flow input or earlier
        step outputs.
        """
        errors = []
        flow_id = flow_def.get("flow_id")
        if not isinstance(flow_id, str) or not flow_id.strip():
            errors.append("flow_id must be a non-empty string")
        inputs = flow_def.get("inputs", {})
        outputs = flow_def.get("outputs", {})
        if inputs is not None and not isinstance(inputs, dict):
            errors.append("inputs must be a mapping")
        if outputs is not None and not isinstance(outputs, dict):
            errors.append("outputs must be a mapping")
        if isinstance(outputs, dict):
            for name, spec in outputs.items():
                if not isinstance(name, str) or not name.strip():
                    errors.append("outputs contains an empty output name")
                    continue
                if isinstance(spec, dict):
                    output_type = spec.get("type")
                    if output_type is not None and output_type not in {
                        "object",
                        "array",
                        "string",
                        "boolean",
                        "number",
                        "integer",
                        "any",
                    }:
                        errors.append("output '{}' has unsupported type '{}'".format(name, output_type))
                elif spec not in {
                    "object",
                    "array",
                    "string",
                    "boolean",
                    "number",
                    "integer",
                    "any",
                    None,
                }:
                    errors.append("output '{}' has unsupported type '{}'".format(name, spec))
        steps = flow_def.get("steps", [])
        if not isinstance(steps, list):
            return errors + ["steps must be a list"]
        seen_ids = set()
        known_roots = {"input", "context"}
        errors.extend(self._validate_steps(steps, known_roots, seen_ids, path="steps"))
        result_spec = flow_def.get("result", flow_def.get("return"))
        if result_spec is not None:
            errors.extend(
                self._validate_references_in_value(
                    result_spec,
                    known_roots,
                    "result",
                )
            )
        return errors

    def _validate_steps(self, steps, known_roots, seen_ids, path):
        errors = []
        if not isinstance(steps, list):
            return ["{} must be a list".format(path)]
        for index, step in enumerate(steps):
            step_path = "{}[{}]".format(path, index)
            if not isinstance(step, dict):
                errors.append("{} must be a mapping".format(step_path))
                continue
            step_id = step.get("id")
            label = step_id if isinstance(step_id, str) and step_id.strip() else step_path
            if not isinstance(step_id, str) or not step_id.strip():
                errors.append("{} is missing id".format(step_path))
            elif step_id in seen_ids:
                errors.append("step id '{}' is duplicated".format(step_id))
            else:
                seen_ids.add(step_id)
            step_type = step.get("type")
            if step_type not in _SUPPORTED_DECLARATIVE_STEP_TYPES:
                errors.append(
                    "step '{}' has unsupported type '{}'".format(label, step_type)
                )
                continue
            output_name = step.get("output")
            if output_name is not None and (
                not isinstance(output_name, str) or not output_name.strip()
            ):
                errors.append("step '{}' output must be a non-empty string".format(label))
            errors.extend(self._validate_references_in_value(step.get("when"), known_roots, label + ".when"))
            errors.extend(self._validate_references_in_value(step.get("input", {}), known_roots, label + ".input"))
            if step_type == "function":
                function_name = step.get("function")
                if not isinstance(function_name, str) or not function_name.strip():
                    errors.append("step '{}' is missing function".format(label))
            elif step_type == "subflow":
                flow_ref = self._subflow_ref(step)
                if not isinstance(flow_ref, str) or not flow_ref.strip():
                    errors.append("step '{}' is missing subflow flow id".format(label))
                else:
                    errors.extend(self._validate_references_in_value(flow_ref, known_roots, label + ".flow"))
                    if _TEMPLATE_RE.match(flow_ref) is None and flow_ref not in self._flows:
                        errors.append("step '{}' references unknown subflow '{}'".format(label, flow_ref))
            elif step_type == "branch":
                branches = step.get("branches")
                if branches is None:
                    branches = []
                    if "then" in step:
                        branches.append({"when": step.get("when"), "steps": step.get("then")})
                    if "else" in step:
                        else_spec = step.get("else")
                        branches.append(
                            {
                                "when": None,
                                "steps": else_spec.get("steps") if isinstance(else_spec, dict) else else_spec,
                            }
                        )
                if not isinstance(branches, list) or not branches:
                    errors.append("step '{}' branch must define branches".format(label))
                else:
                    for branch_index, branch in enumerate(branches):
                        branch_path = "{}.branches[{}]".format(label, branch_index)
                        if not isinstance(branch, dict):
                            errors.append("{} must be a mapping".format(branch_path))
                            continue
                        errors.extend(
                            self._validate_references_in_value(
                                branch.get("when"),
                                known_roots,
                                branch_path + ".when",
                            )
                        )
                        branch_known = set(known_roots)
                        errors.extend(
                            self._validate_steps(
                                branch.get("steps", []),
                                branch_known,
                                seen_ids,
                                branch_path + ".steps",
                            )
                        )
            elif step_type == "parallel":
                children = self._parallel_children(step)
                if not children:
                    errors.append("step '{}' parallel must define steps or branches".format(label))
                for child_index, child in enumerate(children):
                    child_steps = child.get("steps") if isinstance(child, dict) else None
                    child_path = "{}.parallel[{}]".format(label, child_index)
                    branch_known = set(known_roots)
                    errors.extend(
                        self._validate_steps(
                            child_steps if isinstance(child_steps, list) else [],
                            branch_known,
                            seen_ids,
                            child_path + ".steps",
                        )
                    )
            if output_name:
                known_roots.add(output_name)
            elif isinstance(step_id, str) and step_id.strip():
                known_roots.add(step_id)
        return errors

    def validate_flow(self, flow_id):
        """フロー定義を検証し、エラー文字列のリストを返す。"""
        flow_def = self._flows.get(flow_id)
        if flow_def is None:
            return ["Flow '{}' not found".format(flow_id)]
        if flow_def.get("_declarative") and "handler" not in flow_def:
            return self._validate_declarative_flow(flow_def)
        return []

    def _subflow_ref(self, step):
        return step.get("flow") or step.get("flow_id") or step.get("subflow")

    def _parallel_children(self, step):
        if isinstance(step.get("branches"), list):
            return [item for item in step.get("branches") if isinstance(item, dict)]
        if isinstance(step.get("steps"), list):
            return [
                {"id": child.get("id") if isinstance(child, dict) else str(index), "steps": [child]}
                for index, child in enumerate(step.get("steps"))
            ]
        return []

    def _validate_references_in_value(self, value, known_roots, label):
        errors = []
        for ref in self._template_refs(value):
            root = ref.split(".", 1)[0]
            if root not in known_roots:
                errors.append(
                    "{} references unknown value '{}'".format(label, ref)
                )
        return errors

    def _template_refs(self, value):
        refs = []
        if isinstance(value, str):
            match = _TEMPLATE_RE.match(value)
            if match is not None:
                refs.extend(self._expression_refs(match.group(1)))
        elif isinstance(value, dict):
            for item in value.values():
                refs.extend(self._template_refs(item))
        elif isinstance(value, list):
            for item in value:
                refs.extend(self._template_refs(item))
        return refs

    def _expression_refs(self, expression):
        refs = []
        for part in str(expression or "").split("||"):
            token = part.strip()
            if not token or token in {"true", "false", "null", "none"}:
                continue
            if token[0:1] in {"'", '"'}:
                continue
            try:
                float(token)
                continue
            except ValueError:
                pass
            if _SIMPLE_REF_RE.match(token):
                refs.append(token)
        return refs

    def _resolve_ref(self, expression, values):
        parts = [part for part in str(expression or "").split(".") if part]
        current = values
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
            else:
                return None
        return current

    def _resolve_template_expression(self, expression, values):
        for part in str(expression or "").split("||"):
            candidate = self._resolve_ref(part.strip(), values)
            if candidate not in (None, ""):
                return candidate
        return None

    def _resolve_value(self, value, values):
        if isinstance(value, str):
            match = _TEMPLATE_RE.match(value)
            if match is not None:
                return self._resolve_template_expression(match.group(1), values)
            return value
        if isinstance(value, dict):
            return {key: self._resolve_value(item, values) for key, item in value.items()}
        if isinstance(value, list):
            return [self._resolve_value(item, values) for item in value]
        return value

    def _condition_matches(self, condition, values):
        if condition in (None, ""):
            return True
        return self._condition_truthy(self._resolve_value(condition, values))

    @staticmethod
    def _condition_truthy(value):
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"", "false", "0", "no", "off", "null", "none"}:
                return False
            return True
        return bool(value)

    def _invoke_function_step(self, function_name, step_input, flow_context):
        del step_input, flow_context
        return error(
            f"Legacy function step {function_name!r} has no Pack v4 operation",
            "V4_OPERATION_UNAVAILABLE",
        )

    def _result_is_error(self, result):
        return isinstance(result, dict) and result.get("status") == "error"

    def _result_value(self, result):
        if not isinstance(result, dict):
            return result
        if "data" in result:
            return result.get("data")
        return result

    def _execute_function_step(self, flow_id, step, values, flow_context):
        step_input = self._resolve_value(step.get("input", {}), values)
        flow_context.emit_event(
            "flow.step.started",
            {"flow_id": flow_id, "step_id": step["id"], "function": step["function"]},
        )
        result = self._invoke_function_step(step["function"], step_input, flow_context)
        if not isinstance(result, dict):
            result = {"status": "ok", "data": result}
        return result

    def _execute_subflow_step(self, flow_id, step, values, flow_context):
        subflow_id = self._resolve_value(self._subflow_ref(step), values)
        subflow_id = str(subflow_id or "").strip()
        if not subflow_id:
            return error("subflow id is required", "FLOW_SUBFLOW_MISSING")
        parent_context = (
            dict(flow_context._parent_context)
            if isinstance(flow_context._parent_context, dict)
            else {}
        )
        stack = list(parent_context.get("_flow_stack") or [])
        if len(stack) >= _FLOW_STACK_LIMIT or subflow_id in stack:
            return error("subflow recursion limit exceeded", "FLOW_RECURSION_LIMIT")
        stack.append(flow_id)
        parent_context["_flow_stack"] = stack
        step_input = self._resolve_value(step.get("input", {}), values)
        flow_context.emit_event(
            "flow.step.started",
            {"flow_id": flow_id, "step_id": step["id"], "subflow": subflow_id},
        )
        result = self.execute(subflow_id, step_input, parent_context)
        if not isinstance(result, FlowResult):
            return {"status": "ok", "data": result}
        if not result.is_success():
            return result.output if isinstance(result.output, dict) else error("subflow failed", "FLOW_SUBFLOW_FAILED")
        payload = result.output
        if isinstance(payload, dict) and payload.get("status") == "ok":
            data = payload.get("data")
            if isinstance(data, dict) and "outputs" in data:
                return {"status": "ok", "data": data.get("outputs")}
            return {"status": "ok", "data": data}
        return {"status": "ok", "data": payload}

    def _branch_specs(self, step):
        branches = step.get("branches")
        if isinstance(branches, list):
            return branches
        specs = []
        if "then" in step:
            specs.append({"when": step.get("when"), "steps": step.get("then")})
        if "else" in step:
            else_spec = step.get("else")
            specs.append(
                {
                    "when": None,
                    "steps": else_spec.get("steps") if isinstance(else_spec, dict) else else_spec,
                }
            )
        return specs

    def _execute_branch_step(self, flow_id, step, values, outputs, flow_context):
        for branch_index, branch in enumerate(self._branch_specs(step)):
            if not isinstance(branch, dict):
                continue
            if not self._condition_matches(branch.get("when"), values):
                continue
            branch_outputs = {}
            result = self._execute_steps(
                flow_id,
                branch.get("steps") if isinstance(branch.get("steps"), list) else [],
                values,
                branch_outputs,
                flow_context,
            )
            if self._result_is_error(result):
                return result
            return {
                "status": "ok",
                "data": {
                    "branch_index": branch_index,
                    "outputs": branch_outputs,
                },
            }
        return {"status": "ok", "data": {"branch_index": None, "outputs": {}}}

    def _execute_parallel_step(self, flow_id, step, values, outputs, flow_context):
        del outputs
        combined = {}
        for branch_index, child in enumerate(self._parallel_children(step)):
            branch_id = str(child.get("id") or branch_index)
            branch_values = copy.deepcopy(values)
            branch_outputs = {}
            result = self._execute_steps(
                flow_id,
                child.get("steps") if isinstance(child.get("steps"), list) else [],
                branch_values,
                branch_outputs,
                flow_context,
            )
            if self._result_is_error(result):
                return result
            combined[branch_id] = branch_outputs
        return {"status": "ok", "data": combined}

    def _execute_step(self, flow_id, step, values, outputs, flow_context):
        step_type = step.get("type")
        if step_type == "function":
            return self._execute_function_step(flow_id, step, values, flow_context)
        if step_type == "subflow":
            return self._execute_subflow_step(flow_id, step, values, flow_context)
        if step_type == "branch":
            flow_context.emit_event(
                "flow.step.started",
                {"flow_id": flow_id, "step_id": step["id"], "type": "branch"},
            )
            return self._execute_branch_step(flow_id, step, values, outputs, flow_context)
        if step_type == "parallel":
            flow_context.emit_event(
                "flow.step.started",
                {"flow_id": flow_id, "step_id": step["id"], "type": "parallel"},
            )
            return self._execute_parallel_step(flow_id, step, values, outputs, flow_context)
        return error("unsupported step type '{}'".format(step_type), "FLOW_UNSUPPORTED_STEP")

    def _execute_steps(self, flow_id, steps, values, outputs, flow_context):
        for step in steps:
            step_id = step["id"]
            if not self._condition_matches(step.get("when"), values):
                flow_context.emit_event(
                    "flow.step.skipped",
                    {"flow_id": flow_id, "step_id": step_id},
                )
                continue
            result = self._execute_step(flow_id, step, values, outputs, flow_context)
            if not isinstance(result, dict):
                result = {"status": "ok", "data": result}
            if result.get("status") == "error":
                flow_context.emit_event(
                    "flow.step.error",
                    {"flow_id": flow_id, "step_id": step_id, "error": result.get("error")},
                )
                if str(step.get("on_error") or "").lower() == "continue":
                    value = {"error": result.get("error"), "continued": True}
                    output_name = step.get("output") or step_id
                    values[output_name] = value
                    outputs[output_name] = value
                    flow_context.set_variable(output_name, value)
                    continue
                return result
            output_name = step.get("output") or step_id
            value = self._result_value(result)
            values[output_name] = value
            outputs[output_name] = value
            flow_context.set_variable(output_name, value)
            flow_context.emit_event(
                "flow.step.completed",
                {"flow_id": flow_id, "step_id": step_id},
            )
        return {"status": "ok", "data": outputs}

    def _declared_outputs(self, flow_def, values, outputs):
        declared_outputs = flow_def.get("outputs")
        if not isinstance(declared_outputs, dict):
            return outputs, []
        output_data = {}
        type_errors = []
        for name, spec in declared_outputs.items():
            output_type = spec.get("type") if isinstance(spec, dict) else spec
            if isinstance(spec, dict) and "value" in spec:
                value = self._resolve_value(spec.get("value"), values)
            elif name in values:
                value = values.get(name)
            else:
                continue
            if output_type and output_type != "any" and not self._value_matches_type(value, output_type):
                type_errors.append(
                    "output '{}' expected {}, got {}".format(
                        name,
                        output_type,
                        type(value).__name__,
                    )
                )
            output_data[name] = value
        if not output_data:
            output_data = outputs
        return output_data, type_errors

    @staticmethod
    def _value_matches_type(value, output_type):
        if output_type == "object":
            return isinstance(value, dict)
        if output_type == "array":
            return isinstance(value, list)
        if output_type == "string":
            return isinstance(value, str)
        if output_type == "boolean":
            return isinstance(value, bool)
        if output_type == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if output_type == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        return True

    def _flow_result_output(self, flow_def, values, output_data):
        result_spec = flow_def.get("result", flow_def.get("return"))
        if result_spec is None:
            return ok({"flow_id": flow_def.get("flow_id"), "outputs": output_data})
        if isinstance(result_spec, dict) and "value" in result_spec:
            result_value = self._resolve_value(result_spec.get("value"), values)
        else:
            result_value = self._resolve_value(result_spec, values)
        if isinstance(result_value, dict) and result_value.get("status") in {"ok", "error"}:
            return result_value
        return ok(result_value)

    def _execute_declarative(self, flow_id, flow_def, trigger_input, flow_context):
        errors = self._validate_declarative_flow(flow_def)
        if errors:
            return FlowResult(
                status="error",
                output=error("; ".join(errors), "FLOW_VALIDATION_FAILED"),
                metadata={"flow_id": flow_id, "execution_id": flow_context.execution_id},
            )

        values = {
            "input": trigger_input if isinstance(trigger_input, dict) else {},
            "context": flow_context._parent_context if isinstance(flow_context._parent_context, dict) else {},
        }
        outputs = {}
        steps = flow_def.get("steps", [])
        step_result = self._execute_steps(flow_id, steps, values, outputs, flow_context)
        if self._result_is_error(step_result):
            return FlowResult(
                status="error",
                output=step_result,
                metadata={
                    "flow_id": flow_id,
                    "execution_id": flow_context.execution_id,
                },
            )

        output_data, type_errors = self._declared_outputs(flow_def, values, outputs)
        if type_errors:
            return FlowResult(
                status="error",
                output=error("; ".join(type_errors), "FLOW_OUTPUT_TYPE_FAILED"),
                metadata={"flow_id": flow_id, "execution_id": flow_context.execution_id},
            )
        result_output = self._flow_result_output(flow_def, values, output_data)
        return FlowResult(
            status="completed",
            output=result_output,
            metadata={
                "flow_id": flow_id,
                "execution_id": flow_context.execution_id,
                "created_at": flow_context.created_at,
                "runner": "declarative_flow_engine",
                "outputs": output_data,
                "step_outputs": outputs,
            },
        )

    def execute(self, flow_id, trigger_input, context=None):
        """フローを実行する

        指定された flow_id のハンドラをロードし、FlowContext を構築して
        handler.run() を呼び出す。結果を FlowResult として返す。

        Args:
            flow_id: 実行するフローの ID
            trigger_input: フローへの入力データ dict
            context: 親コンテキスト dict（オプション）

        Returns:
            FlowResult インスタンス
        """
        if flow_id not in self._flows:
            return FlowResult(
                status="error",
                output=error("Flow '{}' not found".format(flow_id)),
                metadata={"flow_id": flow_id},
            )

        flow_def = self._flows[flow_id]
        flow_config = flow_def.get("config_schema", {})
        parent_ctx = context if context is not None else {}
        session = {}
        if isinstance(parent_ctx, dict):
            session = parent_ctx.get("session", {})
            if not isinstance(session, dict):
                session = {}

        flow_context = FlowContext(
            flow_id=flow_id,
            trigger_input=trigger_input,
            flow_config=flow_config,
            session=session,
            parent_context=parent_ctx,
        )

        modifiers = self._modifier_loader.load_modifiers(flow_id)
        self._modifier_loader.apply_pre_hooks(modifiers, flow_context)

        flow_context.emit_event(
            "flow.started",
            {
                "flow_id": flow_id,
                "trigger_input_keys": (
                    list(trigger_input.keys())
                    if isinstance(trigger_input, dict)
                    else []
                ),
            },
        )

        if flow_def.get("_declarative") and "handler" not in flow_def:
            flow_result = self._execute_declarative(
                flow_id,
                flow_def,
                trigger_input,
                flow_context,
            )
            if flow_result.is_success():
                self._modifier_loader.apply_post_hooks(modifiers, flow_context, flow_result)
                flow_context.emit_event(
                    "flow.completed",
                    {"flow_id": flow_id, "status": flow_result.status},
                )
            return flow_result

        handler = self._get_handler(flow_id)
        if handler is None:
            return FlowResult(
                status="error",
                output=error(
                    "Handler for flow '{}' could not be loaded".format(flow_id)
                ),
                metadata={"flow_id": flow_id},
            )

        if not hasattr(handler, "run") or not callable(handler.run):
            return FlowResult(
                status="error",
                output=error(
                    "Handler for flow '{}' has no callable run()".format(flow_id)
                ),
                metadata={"flow_id": flow_id},
            )

        try:
            result_data = handler.run(trigger_input, flow_context)
        except Exception as exc:
            flow_context.emit_event("flow.error", {"error": str(exc)})
            return FlowResult(
                status="error",
                output=error(str(exc)),
                metadata={
                    "flow_id": flow_id,
                    "execution_id": flow_context.execution_id,
                    "exception_type": type(exc).__name__,
                },
            )

        session_messages = []
        if isinstance(flow_context.session, dict):
            session_messages = flow_context.session.get("messages", [])
            if not isinstance(session_messages, list):
                session_messages = []

        flow_result = FlowResult(
            status="completed",
            output=result_data if result_data is not None else {},
            messages=session_messages,
            metadata={
                "flow_id": flow_id,
                "execution_id": flow_context.execution_id,
                "created_at": flow_context.created_at,
            },
        )

        self._modifier_loader.apply_post_hooks(modifiers, flow_context, flow_result)

        flow_context.emit_event(
            "flow.completed",
            {"flow_id": flow_id, "status": flow_result.status},
        )

        return flow_result

    def list_flows(self):
        """利用可能なフロー一覧を返す

        Returns:
            フロー情報の dict のリスト。各要素は flow_id, name, description を含む。
        """
        result = []
        for flow_id in sorted(self._flows.keys()):
            flow_def = self._flows[flow_id]
            result.append(
                {
                    "flow_id": flow_id,
                    "name": flow_def.get("name", flow_id),
                    "description": flow_def.get("description", ""),
                    "version": flow_def.get("version", "0.0.0"),
                    "declarative": bool(flow_def.get("_declarative")),
                }
            )
        return result

    def get_flow(self, flow_id):
        """フロー定義を取得する

        内部管理用のキー（_dir, _yaml_path 等）は除外して返す。

        Args:
            flow_id: フロー ID

        Returns:
            フロー定義 dict。見つからなければ None。
        """
        flow_def = self._flows.get(flow_id)
        if flow_def is None:
            return None
        safe_copy = {}
        for key, value in flow_def.items():
            if not key.startswith("_"):
                safe_copy[key] = value
        return safe_copy

    def reload_flows(self):
        """フロー定義を再ロードする

        キャッシュをクリアして flows/ ディレクトリを再スキャンする。
        """
        self._flows.clear()
        self._handlers.clear()
        self._modifier_loader.clear_cache()
        self._load_flows()

    @classmethod
    def reset_instance(cls):
        """シングルトンインスタンスをリセットする（テスト用）"""
        cls._instance = None
