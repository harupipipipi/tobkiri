"""
domain.tool.builder — AI handler generation helpers for runtime tools.
Falls back to a fail-closed executable template when AI code is unavailable.
"""
import ast
import json


from domain.ai_client.client import AIClient


def generate_skeleton(name, description, parameters):
    """
    JSON Schema (parameters) から fail-closed handler template を生成する。
    戻り値: Python コード文字列
    """
    schema_literal = _python_literal(parameters if isinstance(parameters, dict) else {})
    name_literal = repr(str(name or "unnamed_tool"))
    description_literal = repr(str(description or ""))

    return '''def handler(arguments, context):
    """Fail-closed handler template generated when executable tool logic is unavailable."""
    tool_name = {name_literal}
    tool_description = {description_literal}
    schema = {schema_literal}

    def _error(code, message, details):
        return {{
            "result": message,
            "is_error": True,
            "widget": {{
                "type": "tool_error",
                "code": code,
                "tool_name": tool_name,
                "description": tool_description,
                "details": details,
            }},
        }}

    def _matches_type(value, expected_type):
        if expected_type in (None, "any"):
            return True
        if expected_type == "string":
            return isinstance(value, str)
        if expected_type == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if expected_type == "number":
            return (isinstance(value, int) or isinstance(value, float)) and not isinstance(value, bool)
        if expected_type == "boolean":
            return isinstance(value, bool)
        if expected_type == "array":
            return isinstance(value, list)
        if expected_type == "object":
            return isinstance(value, dict)
        if expected_type == "null":
            return value is None
        return True

    def _matches_any_type(value, expected_type):
        if isinstance(expected_type, list):
            for item in expected_type:
                if _matches_type(value, item):
                    return True
            return False
        return _matches_type(value, expected_type)

    if not isinstance(arguments, dict):
        return _error(
            "INVALID_ARGUMENTS",
            "Tool '{{}}' expected arguments to be an object.".format(tool_name),
            {{"errors": ["arguments must be an object"]}},
        )

    errors = []
    required = schema.get("required", [])
    properties = schema.get("properties", {{}})
    if not isinstance(required, list):
        errors.append("schema.required must be a list")
        required = []
    if not isinstance(properties, dict):
        errors.append("schema.properties must be an object")
        properties = {{}}

    for key in required:
        if key not in arguments:
            errors.append("missing required argument '{{}}'".format(key))

    for key, rule in properties.items():
        if key not in arguments or not isinstance(rule, dict):
            continue
        expected_type = rule.get("type")
        value = arguments.get(key)
        if not _matches_any_type(value, expected_type):
            errors.append("argument '{{}}' does not match schema type '{{}}'".format(key, expected_type))
        enum_values = rule.get("enum")
        if isinstance(enum_values, list) and value not in enum_values:
            errors.append("argument '{{}}' must be one of {{}}".format(key, enum_values))

    if errors:
        return _error(
            "INVALID_ARGUMENTS",
            "Invalid arguments for tool '{{}}': {{}}".format(tool_name, "; ".join(errors)),
            {{"errors": errors}},
        )

    return _error(
        "NOT_IMPLEMENTED",
        "Tool '{{}}' has no executable handler logic. Provide explicit handler_code or configure an AI provider and regenerate it.".format(tool_name),
        {{
            "schema_validated": True,
            "reason": "handler_template_unimplemented",
        }},
    )
'''.format(name_literal=name_literal, description_literal=description_literal, schema_literal=schema_literal)


def _python_literal(value):
    """Return an ASCII Python literal for JSON-like values."""
    try:
        return repr(json.loads(json.dumps(value, ensure_ascii=True)))
    except Exception:
        return repr({})


def _extract_content(result):
    if not isinstance(result, dict):
        return ""
    content = result.get("content", "")
    if not content and "choices" in result:
        choices = result.get("choices", [])
        if choices:
            msg = choices[0].get("message", {})
            content = msg.get("content", "")
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        content = "\n".join(parts)
    return content if isinstance(content, str) else ""


def _strip_markdown_fences(content):
    lines = content.strip().splitlines()
    plain = []
    fenced = []
    in_fence = False
    saw_fence = False
    for line in lines:
        if line.strip().startswith("```"):
            in_fence = not in_fence
            saw_fence = True
            continue
        if in_fence:
            fenced.append(line)
        elif not saw_fence:
            plain.append(line)
    return "\n".join(fenced if saw_fence else plain).strip()


def _is_valid_handler_code(content):
    if not content:
        return False
    try:
        tree = ast.parse(
            content,
            filename="<generated_tool_handler>",
            mode="exec",
            type_comments=True,
        )
    except SyntaxError:
        return False

    handler_node = None
    for index, node in enumerate(tree.body):
        if (
            index == 0
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue
        if isinstance(node, ast.FunctionDef) and node.name == "handler":
            if handler_node is not None:
                return False
            handler_node = node
            continue
        return False

    if handler_node is None or handler_node.decorator_list:
        return False
    if handler_node.returns is not None or getattr(handler_node, "type_comment", None):
        return False
    if getattr(handler_node, "type_params", None):
        return False

    args = handler_node.args
    if (
        args.posonlyargs
        or args.vararg
        or args.kwonlyargs
        or args.kw_defaults
        or args.kwarg
        or args.defaults
    ):
        return False
    for arg in args.args:
        if arg.annotation is not None or getattr(arg, "type_comment", None):
            return False
    if [arg.arg for arg in args.args] != ["arguments", "context"]:
        return False

    try:
        compile(tree, "<generated_tool_handler>", "exec")
    except (SyntaxError, ValueError, TypeError):
        return False
    return True


def generate_handler_code_with_ai(name, description, parameters, model=None):
    """
    AI を使って完全な handler コードを生成する。
    model: 使用する AI モデル文字列（None の場合は利用可能な最初の非 stub プロバイダーを使用）
    戻り値: Python コード文字列
    """
    try:
        client = AIClient()

        if model is None:
            providers = client.list_providers()
            for p in providers:
                if p.get("id") != "stub":
                    models = client.list_models(provider=p.get("id"))
                    if models:
                        model = models[0].get("id")
                        break
            if model is None:
                return generate_skeleton(name, description, parameters)
    except Exception:
        return generate_skeleton(name, description, parameters)

    params_json = json.dumps(parameters, ensure_ascii=False, indent=2)

    messages = [
        {
            "role": "system",
            "content": (
                "You are a tool handler code generator for the rumiai ecosystem. "
                "Generate a Python function named 'handler' that takes two arguments: "
                "'arguments' (dict) and 'context' (dict). "
                "The function must return a dict with keys: "
                "'result' (str), 'is_error' (bool), 'widget' (dict or None). "
                "Output ONLY the Python code, no markdown fences, no explanation."
            ),
        },
        {
            "role": "user",
            "content": (
                "Generate a handler function for a tool with the following spec:\n\n"
                "Name: {name}\n"
                "Description: {description}\n"
                "Parameters (JSON Schema):\n{params_json}\n\n"
                "The function signature must be: def handler(arguments, context):\n"
                "Return a dict with 'result', 'is_error', 'widget' keys."
            ).format(name=name, description=description, params_json=params_json),
        },
    ]

    try:
        result = client.complete(model, messages)
        content = _strip_markdown_fences(_extract_content(result))
        if _is_valid_handler_code(content):
            return content
        return generate_skeleton(name, description, parameters)
    except Exception:
        return generate_skeleton(name, description, parameters)
