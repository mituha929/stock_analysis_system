"""Bounded, side-effect-free validation with JSON Pointer error locations."""
import math

from app.rules.fields import BOUNDS, FIELDS, TRENDS, field_operators

MAX_DEPTH = 10
MAX_NODES = 100
MAX_ERRORS = 50


def number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        # Downstream indicators and range values use floats; reject unrepresentable integers.
        return False


def pointer(path, key):
    return path + "/" + str(key).replace("~", "~0").replace("/", "~1")


def issue(code, path, message):
    return {"code": code, "path": path, "message": message}


def shape_errors(data, expected, path):
    errors = [issue("missing_property", pointer(path, key), "缺少必要欄位。")
              for key in sorted(expected - data.keys())]
    for key in data:
        if key not in expected:
            errors.append(issue("extra_property", pointer(path, key), "不允許額外欄位。"))
        if len(errors) >= MAX_ERRORS:
            break
    return errors


def leaf_errors(data, path="/rule"):
    errors = shape_errors(data, {"field", "operator", "value"}, path)
    field = data.get("field")
    if not isinstance(field, str) or field not in FIELDS:
        errors.append(issue("unsupported_field", path + "/field", "欄位不在白名單，請查詢 /api/v1/rules/fields。"))
        return errors
    kind, op = FIELDS[field], data.get("operator")
    if not isinstance(op, str) or op not in field_operators(field):
        errors.append(issue("unsupported_operator", path + "/operator", "此欄位不支援該運算子。"))
        return errors
    if "value" not in data:
        return errors
    value, value_path = data["value"], path + "/value"
    if isinstance(value, dict):
        errors.extend(shape_errors(value, {"field"}, value_path))
        other = value.get("field")
        if not isinstance(other, str) or other not in FIELDS:
            errors.append(issue("unsupported_field", value_path + "/field", "比較欄位不在白名單。"))
        elif FIELDS[other] != kind:
            errors.append(issue("incompatible_field", value_path + "/field", "比較的兩個欄位必須具有相同資料型態。"))
        if op == "between":
            errors.append(issue("invalid_range", value_path, "between 必須使用兩個數字，不能使用欄位參照。"))
        return errors
    if op == "between":
        if not isinstance(value, list) or len(value) != 2 or not all(number(v) for v in value):
            return errors + [issue("invalid_range", value_path, "between 必須為 [下限, 上限]，兩者均為有限數字。")]
        if value[0] > value[1]:
            errors.append(issue("invalid_range", value_path, "between 下限不得大於上限。"))
        values = [(v, pointer(value_path, i)) for i, v in enumerate(value)]
    elif kind == "number":
        if not number(value):
            return errors + [issue("invalid_type", value_path, "必須是有限數字，不能使用字串、布林或 null。")]
        values = [(value, value_path)]
    elif kind == "boolean":
        if type(value) is not bool:
            errors.append(issue("invalid_type", value_path, "必須是 JSON true 或 false。"))
        return errors
    else:
        if not isinstance(value, str) or value not in TRENDS:
            errors.append(issue("invalid_enum", value_path, "趨勢必須使用白名單中的 trend_values。"))
        return errors
    lower, upper = BOUNDS.get(field, (None, None))
    for val, loc in values:
        if (lower is not None and val < lower) or (upper is not None and val > upper):
            message = f"數值必須介於 {lower} 與 {upper}（含兩端）。" if upper is not None else f"數值不得小於 {lower}。"
            errors.append(issue("out_of_range", loc, message))
    return errors


def validate_tree(rule, path="/rule"):
    """Root counts as one node/level. Do not recurse into oversized trees."""
    stack, errors, count = [(rule, path, 1)], [], 0
    while stack and len(errors) < MAX_ERRORS:
        node, loc, depth = stack.pop()
        count += 1
        if count > MAX_NODES:
            errors.append(issue("too_many_nodes", loc, "規則樹最多 100 個節點（包含群組）。"))
            break
        if depth > MAX_DEPTH:
            errors.append(issue("too_deep", loc, "規則樹最多 10 層（根節點為第 1 層）。"))
            continue
        if not isinstance(node, dict):
            errors.append(issue("invalid_node", loc, "規則節點必須是 JSON 物件。"))
        elif "logic" in node or "rules" in node:
            errors.extend(shape_errors(node, {"logic", "rules"}, loc))
            if node.get("logic") not in ("AND", "OR"):
                errors.append(issue("invalid_logic", loc + "/logic", "群組邏輯只能是 AND 或 OR。"))
            children = node.get("rules")
            if not isinstance(children, list) or not children:
                errors.append(issue("invalid_children", loc + "/rules", "rules 必須是非空陣列。"))
            elif count + len(stack) + len(children) > MAX_NODES:
                errors.append(issue("too_many_nodes", loc + "/rules", "規則樹最多 100 個節點（包含群組）。"))
            else:
                stack.extend((child, pointer(loc + "/rules", i), depth + 1)
                             for i, child in reversed(list(enumerate(children))))
        else:
            errors.extend(leaf_errors(node, loc))
    return errors[:MAX_ERRORS]
