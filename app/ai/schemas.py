from typing import Any, Literal

from pydantic import Field, model_validator

from app.rules.fields import BOUNDS, FIELDS, TRENDS, field_operators
from app.rules.schemas import Rule, RuleGroup, RuleTestRequest, StrictModel


class ParseRequest(StrictModel):
    text: str = Field(min_length=1, max_length=2000, strict=True)
    history_interval: Literal["1m", "5m", "15m", "60m", "day", "week", "month"] = "day"

    @model_validator(mode="after")
    def clean_text(self):
        self.text = self.text.strip()
        if not self.text:
            raise ValueError("text must not be blank")
        return self


class Assumption(StrictModel):
    source_text: str = Field(min_length=1, max_length=200, strict=True)
    interpreted_as: str = Field(min_length=1, max_length=500, strict=True)


class RuleDraft(StrictModel):
    rule: Rule | RuleGroup | None
    assumptions: list[Assumption] = Field(max_length=30)
    unresolved_conditions: list[str] = Field(max_length=30)

    @model_validator(mode="before")
    @classmethod
    def check_tree(cls, data):
        if isinstance(data, dict) and data.get("rule") is not None:
            # Apply the SAME node/depth guard before recursive Pydantic parsing.
            RuleTestRequest.limit_tree({"rule": data["rule"]})
        return data

    @model_validator(mode="after")
    def check_unresolved(self):
        if any(not value.strip() or len(value) > 500 for value in self.unresolved_conditions):
            raise ValueError("unresolved conditions must be nonempty and at most 500 characters")
        if self.rule is None and not self.unresolved_conditions:
            raise ValueError("null rule requires unresolved_conditions")
        return self


class ParseResponse(RuleDraft):
    original_text: str
    history_interval: str
    model: str
    prompt_version: str
    status: Literal["draft", "needs_clarification"]
    requires_confirmation: Literal[True] = True
    elapsed_ms: int


class ValidateRequest(StrictModel):
    # Keep untrusted values raw so validation can report precise paths, without coercion.
    rule: Any = Field(..., description="待驗證的 Rule 或巢狀 AND/OR RuleGroup；不接受 null。",
                      examples=[{"field": "rsi14", "operator": "between", "value": [40, 60]}])


class ValidateResponse(StrictModel):
    valid: Literal[True] = True
    rule: Rule | RuleGroup
    requires_confirmation: Literal[True] = True


def generation_schema() -> dict:
    """Keep the model's grammar aligned with the existing registry and rule schema."""
    schema = RuleDraft.model_json_schema()
    for name in ("Rule", "FieldReference"):
        schema["$defs"][name]["properties"]["field"]["enum"] = list(FIELDS)
    # Group identical constraints instead of duplicating a schema for every field.
    groups = {}
    for name, kind in FIELDS.items():
        groups.setdefault((kind, *BOUNDS.get(name, (None, None))), []).append(name)
    alternatives = []
    for (kind, lower, upper), names in groups.items():
        literal = {"type": kind}
        if kind == "number":
            if lower is not None:
                literal["minimum"] = lower
            if upper is not None:
                literal["maximum"] = upper
        elif kind == "string":
            literal["enum"] = list(TRENDS)
        ref = {"type": "object", "required": ["field"], "additionalProperties": False,
               "properties": {"field": {"type": "string", "enum": [n for n, t in FIELDS.items() if t == kind]}}}
        alternatives.append({"properties": {
            "field": {"enum": names},
            "operator": {"enum": [op for op in field_operators(names[0]) if op != "between"]},
            "value": {"anyOf": [literal, ref]},
        }})
        if kind == "number":
            alternatives.append({"properties": {
                "field": {"enum": names}, "operator": {"const": "between"},
                "value": {"type": "array", "items": literal, "minItems": 2, "maxItems": 2},
            }})
    schema["$defs"]["Rule"]["anyOf"] = alternatives
    return schema
