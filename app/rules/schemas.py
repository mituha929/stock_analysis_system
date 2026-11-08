from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.rules.fields import FIELDS
from app.rules.validation import leaf_errors, number


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldReference(StrictModel):
    field: str

    @model_validator(mode="after")
    def supported(self):
        if self.field not in FIELDS:
            raise ValueError(f"unsupported_field: {self.field}")
        return self


class Rule(StrictModel):
    field: str
    operator: Literal[">", ">=", "<", "<=", "==", "!=", "between", "cross_above", "cross_below"]
    value: bool | int | float | str | list[float] | FieldReference

    @model_validator(mode="before")
    @classmethod
    def validate_raw(cls, data):
        if not isinstance(data, dict):
            return data
        errors = leaf_errors(data)
        if errors:
            raise ValueError(f"{errors[0]['code']}: {errors[0]['message']}")
        return data


class RuleGroup(StrictModel):
    logic: Literal["AND", "OR"]
    rules: list[Rule | RuleGroup] = Field(min_length=1, max_length=100)


class RuleTestRequest(StrictModel):
    rule: Rule | RuleGroup
    history_interval: Literal["1m", "5m", "15m", "60m", "day", "week", "month"] = "day"
    candle_table: str = Field(default="candles", pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")

    @model_validator(mode="before")
    @classmethod
    def limit_tree(cls, data):
        if not isinstance(data, dict):
            return data
        stack, count = [(data.get("rule"), 1)], 0
        while stack:
            node, depth = stack.pop()
            count += 1
            if depth > 10 or count > 100:
                raise ValueError("rule tree exceeds 10 levels or 100 nodes")
            if isinstance(node, dict) and isinstance(node.get("rules"), list):
                stack.extend((child, depth + 1) for child in node["rules"])
        return data
