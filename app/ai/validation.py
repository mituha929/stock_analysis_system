"""Validate AI output before recursive model parsing; never execute a rule."""
from pydantic import ValidationError

from app.ai.ollama_client import AIError
from app.rules.schemas import Rule, RuleGroup
from app.rules.validation import MAX_ERRORS, issue, pointer, validate_tree


def reject(errors):
    code = "unsupported_field" if any(e["code"] == "unsupported_field" for e in errors) else "invalid_rule"
    raise AIError(code, "規則未通過驗證，請依 errors 的位置與原因修正。", 422, errors=errors)


def check_rule(raw):
    errors = validate_tree(raw)
    if errors:
        reject(errors)


def schema_errors(exc: ValidationError):
    # Deliberately exclude raw input, exception context and model-generated text.
    errors = []
    for error in exc.errors(include_input=False, include_context=False, include_url=False)[:MAX_ERRORS]:
        path = ""
        for part in error["loc"]:
            path = pointer(path, part)
        messages = {
            "missing": "缺少必要欄位。", "extra_forbidden": "不允許額外欄位。",
            "list_type": "必須是陣列。", "string_type": "必須是字串。",
            "string_too_short": "字串太短或為空。", "string_too_long": "字串超過長度限制。",
            "too_long": "項目數量超過限制。", "model_type": "必須是 JSON 物件。",
        }
        message = messages.get(error["type"], "欄位型態、長度或草稿結構不合法。")
        # Only translate known validator messages, never echo arbitrary exception text.
        if error["msg"] == "Value error, null rule requires unresolved_conditions":
            path, message = "/unresolved_conditions", "rule 為 null 時必須提供待釐清原因。"
        elif error["msg"] == "Value error, unresolved conditions must be nonempty and at most 500 characters":
            path, message = "/unresolved_conditions", "待釐清原因不得為空白或超過 500 字。"
        errors.append(issue(error["type"], path, message))
    return errors


def validated_rule(raw):
    check_rule(raw)
    try:
        return (RuleGroup if "logic" in raw else Rule).model_validate(raw)
    except ValidationError as exc:
        reject(schema_errors(exc))
