"""Pure evaluator. None means unknown, never a successful != comparison."""
import operator

from app.rules.fields import FIELDS
from app.rules.schemas import FieldReference, Rule, RuleGroup, number

COMPARISONS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le, "==": operator.eq, "!=": operator.ne}


def valid(field, value):
    kind = FIELDS[field]
    return number(value) if kind == "number" else type(value) is (bool if kind == "boolean" else str)


def evaluate(rule: Rule | RuleGroup, current: dict, previous: dict | None = None) -> dict:
    details = []
    previous = previous or {}

    def visit(node, path):
        if isinstance(node, RuleGroup):
            outcomes = [visit(child, f"{path}.{i}") for i, child in enumerate(node.rules)]
            if node.logic == "AND":
                return False if False in outcomes else None if None in outcomes else True
            return True if True in outcomes else None if None in outcomes else False
        left = current.get(node.field)
        ref = node.value.field if isinstance(node.value, FieldReference) else None
        right = current.get(ref) if ref else node.value
        ready = valid(node.field, left)
        if ref:
            ready = ready and valid(ref, right)
        prior_left = prior_right = None
        if node.operator.startswith("cross_"):
            prior_left = previous.get(node.field)
            prior_right = previous.get(ref) if ref else right
            ready = ready and valid(node.field, prior_left) and number(prior_right)
        matched = None
        if ready:
            if node.operator == "between":
                matched = right[0] <= left <= right[1]
            elif node.operator == "cross_above":
                matched = prior_left <= prior_right and left > right
            elif node.operator == "cross_below":
                matched = prior_left >= prior_right and left < right
            else:
                matched = COMPARISONS[node.operator](left, right)
        status = "matched" if matched is True else "not_matched" if matched is False else "insufficient_data"
        reason = f"{node.field} ({left}) {node.operator} {ref or node.value} ({right})：" + {"matched": "符合", "not_matched": "不符合", "insufficient_data": "資料不足，無法判定"}[status]
        details.append({"path": path, "field": node.field, "operator": node.operator, "value": node.value.model_dump() if ref else node.value, "actual": left, "expected": right, "previous_actual": prior_left, "previous_expected": prior_right, "matched": matched, "status": status, "reason": reason})
        return matched

    outcome = visit(rule, "rule")
    return {"matched": outcome is True, "status": "insufficient_data" if outcome is None else "matched" if outcome else "not_matched", "matched_count": sum(d["matched"] is True for d in details), "total_rules": len(details), "unknown_count": sum(d["matched"] is None for d in details), "reasons": [d["reason"] for d in details if d["matched"] is True], "details": details}
