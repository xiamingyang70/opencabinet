# contract.py
from __future__ import annotations

import ast
import json
import math
from pathlib import Path
from typing import Any, Mapping

try:
    from jsonschema import Draft202012Validator
except ImportError as exc:
    raise ImportError("contract.py requires the 'jsonschema' package") from exc


DEFAULT_CONTRACT = (
    Path(__file__).resolve().parent
    / "data"
    / "operator_contract_1.0.0.json"
)

_ALLOWED_ROOTS = frozenset(("spec", "space", "params"))
_ALLOWED_FUNCS = {
    "min": min,
    "max": max,
    "round": round,
}


class ContractError(ValueError):
    pass


class ExpressionError(ContractError):
    pass


def load_contract(path: str | Path | None = None) -> dict[str, Any]:
    contract_path = Path(path) if path else DEFAULT_CONTRACT
    with contract_path.open("r", encoding="utf-8") as stream:
        schema = json.load(stream)
    Draft202012Validator.check_schema(schema)
    return schema


def validator_for(
    definition: str,
    schema: Mapping[str, Any] | None = None,
) -> Draft202012Validator:
    root = dict(schema or load_contract())
    definitions = root.get("$defs", {})
    if definition not in definitions:
        raise ContractError(f"Unknown contract definition: {definition}")
    wrapper = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": definitions,
        "$ref": f"#/$defs/{definition}",
    }
    return Draft202012Validator(wrapper)


def validate_contract(
    value: Any,
    definition: str,
    schema: Mapping[str, Any] | None = None,
) -> Any:
    validator = validator_for(definition, schema)
    errors = sorted(
        validator.iter_errors(value),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        messages = []
        for error in errors:
            path = ".".join(str(part) for part in error.absolute_path) or "$"
            messages.append(f"{path}: {error.message}")
        raise ContractError("\n".join(messages))
    return value


def validate_spec(value: Any, schema=None) -> Any:
    return validate_contract(value, "CabinetSpec", schema)


def validate_space(value: Any, schema=None) -> Any:
    return validate_contract(value, "Space", schema)


def validate_operator(value: Any, schema=None) -> Any:
    return validate_contract(value, "Operator", schema)


def validate_semantic(value: Any, schema=None) -> Any:
    return validate_contract(value, "SemanticAssembly", schema)


def validate_board_graph(value: Any, schema=None) -> Any:
    return validate_contract(value, "BoardGraph", schema)


def _lookup_attr(value: Any, name: str) -> Any:
    if not isinstance(value, Mapping):
        raise ExpressionError(f"Attribute access requires a mapping: {name}")
    if name.startswith("_") or name not in value:
        raise ExpressionError(f"Unknown expression attribute: {name}")
    return value[name]


class _SafeEvaluator(ast.NodeVisitor):
    def __init__(self, context: Mapping[str, Any]):
        self.context = context

    def visit_Expression(self, node: ast.Expression) -> Any:
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant) -> Any:
        value = node.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ExpressionError("Only numeric constants are allowed")
        return value

    def visit_Name(self, node: ast.Name) -> Any:
        if node.id not in _ALLOWED_ROOTS:
            raise ExpressionError(f"Forbidden expression name: {node.id}")
        if node.id not in self.context:
            raise ExpressionError(f"Missing expression context: {node.id}")
        return self.context[node.id]

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        return _lookup_attr(self.visit(node.value), node.attr)

    def visit_BinOp(self, node: ast.BinOp) -> Any:
        left = self.visit(node.left)
        right = self.visit(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        raise ExpressionError("Only +, -, * and / are allowed")

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        operand = self.visit(node.operand)
        if isinstance(node.op, ast.UAdd):
            return +operand
        if isinstance(node.op, ast.USub):
            return -operand
        raise ExpressionError("Only unary + and - are allowed")

    def visit_Call(self, node: ast.Call) -> Any:
        if not isinstance(node.func, ast.Name):
            raise ExpressionError("Only direct function calls are allowed")
        function = _ALLOWED_FUNCS.get(node.func.id)
        if function is None:
            raise ExpressionError(f"Forbidden function: {node.func.id}")
        if node.keywords:
            raise ExpressionError("Keyword arguments are not allowed")
        args = [self.visit(arg) for arg in node.args]
        if node.func.id == "round" and not 1 <= len(args) <= 2:
            raise ExpressionError("round() accepts one or two arguments")
        if node.func.id in ("min", "max") and not args:
            raise ExpressionError(f"{node.func.id}() requires arguments")
        return function(*args)

    def generic_visit(self, node: ast.AST) -> Any:
        raise ExpressionError(
            f"Forbidden expression syntax: {type(node).__name__}"
        )


def evaluate_expression(
    expression: int | float | str,
    *,
    spec: Mapping[str, Any],
    space: Mapping[str, Any],
    params: Mapping[str, Any],
) -> float:
    if isinstance(expression, bool):
        raise ExpressionError("Boolean is not a numeric expression")
    if isinstance(expression, (int, float)):
        result = expression
    elif isinstance(expression, str):
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError as exc:
            raise ExpressionError(f"Invalid expression: {expression}") from exc
        result = _SafeEvaluator(
            {"spec": spec, "space": space, "params": params}
        ).visit(tree)
    else:
        raise ExpressionError(
            f"Unsupported expression type: {type(expression).__name__}"
        )

    if isinstance(result, bool) or not isinstance(result, (int, float)):
        raise ExpressionError("Expression result must be numeric")
    result = float(result)
    if not math.isfinite(result):
        raise ExpressionError("Expression result must be finite")
    return result


def evaluate_expr_box(
    box: Mapping[str, Any],
    *,
    spec: Mapping[str, Any],
    space: Mapping[str, Any],
    params: Mapping[str, Any],
) -> dict[str, float]:
    required = ("x", "y", "z", "w", "d", "h")
    missing = [field for field in required if field not in box]
    if missing:
        raise ExpressionError(f"ExprBox missing fields: {missing}")
    result = {
        field: evaluate_expression(
            box[field], spec=spec, space=space, params=params
        )
        for field in required
    }
    if result["w"] <= 0 or result["d"] <= 0 or result["h"] <= 0:
        raise ExpressionError("ExprBox dimensions must be positive")
    return result


def dump_json(
    value: Any,
    path: str | Path,
    *,
    indent: int = 2,
) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=indent)
        stream.write("\n")
    return output
