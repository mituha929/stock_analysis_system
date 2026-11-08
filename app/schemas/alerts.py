from typing import Literal

from pydantic import Field, model_validator

from app.rules.schemas import FieldReference, RuleGroup, RuleTestRequest, StrictModel


class AlertCreate(RuleTestRequest):
    name: str = Field(min_length=1, max_length=100)
    symbol: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
    source: Literal["mysql", "quote"] = "mysql"
    enabled: bool = Field(default=True, strict=True)
    check_interval_seconds: int = Field(default=60, ge=1, le=86400, strict=True)
    cooldown_seconds: int = Field(default=300, ge=0, le=604800, strict=True)
    quote_max_age_seconds: int = Field(default=60, ge=1, le=3600, strict=True)

    @model_validator(mode="after")
    def validate_source(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("name cannot be blank")
        self.symbol = self.symbol.upper()
        if self.source == "quote":
            stack = [self.rule]
            while stack:
                node = stack.pop()
                if isinstance(node, RuleGroup):
                    stack.extend(node.rules)
                    continue
                fields = [node.field]
                if isinstance(node.value, FieldReference):
                    fields.append(node.value.field)
                if any(field not in ("price", "volume", "change_percent") for field in fields):
                    raise ValueError("quote source supports price, volume and change_percent only")
        return self


class AlertEnabled(StrictModel):
    enabled: bool = Field(strict=True)
