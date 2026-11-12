"""Effect-free, bounded play-script parser and condition interpreter.

Commands are newline separated, arguments use shell-like quoting (not
shell execution). Dynamic markers are consumed by Runtime, never native input.
"""

from __future__ import annotations

import ast
import math
import operator
import shlex

from .input_plan import bounded_seconds, normalize_native_action

_MAX_BYTES = 65536
_BIN = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_COMPARE = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}


def _tree(expr: str) -> ast.Expression:
    if not isinstance(expr, str) or not expr.strip() or len(expr.encode("utf-8")) > 4096:
        raise ValueError("condition must contain 1..4096 UTF-8 bytes")
    try:
        tree = ast.parse(expr, mode="eval")
    except (SyntaxError, RecursionError) as exc:
        raise ValueError("invalid condition syntax") from exc
    nodes = list(ast.walk(tree))
    if len(nodes) > 256:
        raise ValueError("condition has too many nodes")
    allowed = (
        ast.Expression,
        ast.Constant,
        ast.Name,
        ast.Load,
        ast.Subscript,
        ast.BoolOp,
        ast.And,
        ast.Or,
        ast.UnaryOp,
        ast.Not,
        ast.UAdd,
        ast.USub,
        ast.BinOp,
        ast.Compare,
        *_BIN,
        *_COMPARE,
    )

    def validate(node, depth=0):
        if depth > 24 or not isinstance(node, allowed):
            raise ValueError("unsupported or deeply nested condition")
        if isinstance(node, ast.Constant):
            _scalar(node.value)
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise ValueError("private names are not allowed")
        for child in ast.iter_child_nodes(node):
            validate(child, depth + 1)

    validate(tree)
    return tree


def _scalar(value):
    if value is None or type(value) is bool:
        return value
    if type(value) is str and len(value) <= 4096:
        return value
    if (
        type(value) in {int, float}
        and abs(value) <= 10**15
        and (type(value) is int or math.isfinite(value))
    ):
        return value
    raise ValueError("condition value exceeds scalar bounds")


def evaluate_condition(expr: str, sample: dict) -> bool:
    """Interpret only a small AST over bounded JSON data, without eval or calls."""
    tree = _tree(expr)
    count = 0

    def json_value(value, depth=0):
        nonlocal count
        count += 1
        if count > 4096 or depth > 24:
            raise ValueError("sample exceeds bounds")
        if type(value) is dict:
            if any(type(key) is not str or len(key) > 4096 for key in value):
                raise ValueError("sample object keys must be bounded strings")
            return {key: json_value(item, depth + 1) for key, item in value.items()}
        if type(value) is list:
            return [json_value(item, depth + 1) for item in value]
        return _scalar(value)

    if type(sample) is not dict:
        raise ValueError("sample must be an object")
    values = json_value(sample)

    def run(node):
        if isinstance(node, ast.Expression):
            return run(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id == "sample":
                return values
            if node.id not in values:
                raise ValueError(f"unknown sample name: {node.id}")
            return values[node.id]
        if isinstance(node, ast.Subscript):
            container, key = run(node.value), run(node.slice)
            if type(container) is not dict or type(key) is not str:
                raise ValueError("only dictionary string subscripts are supported")
            if key not in container:
                raise ValueError("sample dictionary key is missing")
            return container[key]
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                return all(bool(run(value)) for value in node.values)
            return any(bool(run(value)) for value in node.values)
        if isinstance(node, ast.UnaryOp):
            value = run(node.operand)
            if isinstance(node.op, ast.Not):
                return not value
            if type(value) not in {int, float}:
                raise ValueError("unary arithmetic requires numbers")
            return _scalar(value if isinstance(node.op, ast.UAdd) else -value)
        if isinstance(node, ast.BinOp):
            left, right = run(node.left), run(node.right)
            if type(left) not in {int, float} or type(right) not in {int, float}:
                raise ValueError("arithmetic requires numbers")
            try:
                return _scalar(_BIN[type(node.op)](left, right))
            except (ArithmeticError, TypeError) as exc:
                raise ValueError("invalid bounded arithmetic") from exc
        if isinstance(node, ast.Compare):
            left = run(node.left)
            for op, operand in zip(node.ops, node.comparators, strict=True):
                right = run(operand)
                try:
                    if not _COMPARE[type(op)](left, right):
                        return False
                except (TypeError, ValueError) as exc:
                    raise ValueError("invalid condition comparison") from exc
                left = right
            return True
        raise ValueError("unsupported condition")

    return bool(run(tree))


def _commands(script: str) -> list[str]:
    return [
        line.strip()
        for line in script.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _seconds(text: str) -> float:
    multiplier = 0.001 if text.endswith("ms") else 1
    suffix = 2 if text.endswith("ms") else 1 if text.endswith("s") else 0
    try:
        value = float(text[:-suffix] if suffix else text) * multiplier
    except ValueError as exc:
        raise ValueError("invalid duration") from exc
    return bounded_seconds(value)


def parse_script(script: str) -> list[dict]:
    """Parse and validate the complete plan before callers may perform effects."""
    if not isinstance(script, str):
        raise ValueError("script must be a string")
    try:
        size = len(script.encode("utf-8"))
    except UnicodeError as exc:
        raise ValueError("script contains invalid Unicode") from exc
    if size > _MAX_BYTES:
        raise ValueError("script exceeds 64 KiB")
    commands = _commands(script)
    if len(commands) > 100:
        raise ValueError("script exceeds 100 commands")
    plan = []
    for command in commands:
        # Text is literal remainder, with optional outer quoting. Never split it
        # into script commands, interpolate it, or treat trailing # as comments.
        head_parts = command.split(maxsplit=1)
        head = head_parts[0]
        remainder = head_parts[1] if len(head_parts) > 1 else ""
        name = head.lower()
        if name in {"type", "paste"}:
            args = []
        else:
            args = shlex.split(remainder, comments=False, posix=True)
        action = None
        if name == "until":
            condition = remainder.strip()
            _tree(condition)
            action = {"action": "until", "condition": condition}
        elif name in {"probe", "shot"}:
            if args:
                raise ValueError(f"{name} takes no arguments")
            action = {"action": name}
        elif name == "wait":
            if len(args) != 1:
                raise ValueError("wait requires one duration")
            action = {"action": "wait", "seconds": _seconds(args[0])}
        elif name in {"hold", "tap", "down", "up"}:
            if not args or len(args) > (2 if name in {"hold", "tap"} else 1):
                raise ValueError(f"invalid {name} arguments")
            if name == "hold" and len(args) != 2:
                raise ValueError("hold requires key and duration")
            action = {
                "action": {"tap": "key", "down": "key_down", "up": "key_up"}.get(name, name),
                "key": args[0],
            }
            if len(args) == 2 and name != "tap":
                action["seconds"] = _seconds(args[1])
            if name == "tap":
                try:
                    repeats = int(args[1]) if len(args) == 2 else 1
                except ValueError as exc:
                    raise ValueError("tap repeat count must be integer") from exc
                if not 1 <= repeats <= 100 or len(plan) + repeats > 100:
                    raise ValueError("tap repeats exceed command budget")
                plan.extend(normalize_native_action(action) for _ in range(repeats))
                continue
        elif name in {"look", "move", "lmb", "rmb", "mmb"}:
            if not args and name in {"lmb", "rmb", "mmb"}:
                plan.append(
                    normalize_native_action(
                        {
                            "action": "click",
                            "button": {"lmb": "left", "rmb": "right", "mmb": "middle"}[name],
                        }
                    )
                )
                continue
            if len(args) != 2:
                raise ValueError(f"{name} requires two integer coordinates")
            try:
                x, y = map(int, args)
            except ValueError as exc:
                raise ValueError("coordinates must be integers") from exc
            if name == "look":
                action = {"action": "look", "dx": x, "dy": y}
            else:
                action = {"action": "move" if name == "move" else "click", "x": x, "y": y}
                if name != "move":
                    action["button"] = {"lmb": "left", "rmb": "right", "mmb": "middle"}[name]
        elif name == "scroll":
            if not 1 <= len(args) <= 2:
                raise ValueError("scroll requires notches and optional direction")
            if args[0] in {"up", "down", "left", "right", "vertical", "horizontal"}:
                if len(args) != 2:
                    raise ValueError("direction-first scroll requires an amount")
                args = [args[1], args[0]]
            try:
                amount = int(args[0])
            except ValueError as exc:
                raise ValueError("scroll amount must be integer notches") from exc
            action = {
                "action": "scroll",
                "scroll_amount": amount,
                "scroll_direction": args[1] if len(args) == 2 else "vertical",
            }
        elif name in {"type", "paste"}:
            literal = name == "type" and remainder.startswith("--literal ")
            if literal:
                remainder = remainder[len("--literal ") :]
            if not remainder:
                raise ValueError(f"{name} requires text")
            if remainder.startswith(("'", '"')):
                parts = shlex.split(remainder, comments=False, posix=True)
                if len(parts) != 1:
                    raise ValueError("quoted text must be a single argument")
                remainder = parts[0]
            action = {"action": "text" if name == "type" else "paste", "text": remainder}
            if name == "type":
                action["literal"] = literal
        else:
            raise ValueError(f"unknown script command: {name}")
        if action["action"] not in {"wait", "probe", "until", "shot"}:
            action = normalize_native_action(action)
        plan.append(action)
        if len(plan) > 100:
            raise ValueError("script exceeds 100 expanded commands")
    return plan
