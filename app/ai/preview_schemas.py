"""V3.10/11 contracts; editing any setting requires a new preview."""
from typing import Literal
from pydantic import Field, model_validator, field_validator
from app.ai.schemas import ParseRequest
from app.rules.schemas import StrictModel
from app.schemas.screener import Universe, SortSpec, ScreenerRequest


class PreviewSettings(StrictModel):
    universe: Universe = Field(default_factory=Universe)
    history_interval: Literal["day", "week", "month"] = "day"
    candle_table: Literal["candles", "odd_lot_candle"] = "candles"
    history_bars: int = Field(default=360, ge=20, le=2000, strict=True)
    limit: int = Field(default=20, ge=1, le=1000, strict=True)
    timeout_seconds: int = Field(default=120, ge=1, le=300, strict=True)
    sort: list[SortSpec] = Field(default_factory=lambda: [
        SortSpec(field="matched_count",direction="desc"),
        SortSpec(field="direction_score",direction="desc"),
        SortSpec(field="risk_score",direction="asc")],min_length=1,max_length=6)

    @model_validator(mode="after")
    def validate_configuration(self):
        # Reuse the complete existing screener contract, without permitting a
        # model or a request to add hidden prefilters or execution options.
        self.screener({"field":"price","operator":">=","value":0})
        return self

    def screener(self, rule):
        return ScreenerRequest.model_validate({**self.model_dump(),"rule":rule})


class PreviewRequest(ParseRequest):
    # Override V3.8's minute periods: first guided UI supports complete D/W/M.
    history_interval: Literal["day", "week", "month"] = "day"
    settings: PreviewSettings = Field(default_factory=PreviewSettings)
    replaces: str | None = Field(default=None,max_length=128)

    @model_validator(mode="after")
    def consistent_interval(self):
        if self.history_interval != self.settings.history_interval:
            raise ValueError("history_interval must match settings.history_interval")
        return self


class ReviseRequest(StrictModel):
    preview_id: str = Field(min_length=16,max_length=128)
    revision: str = Field(min_length=32,max_length=64)
    rule: dict


class ExecuteRequest(StrictModel):
    preview_id: str = Field(min_length=16,max_length=128)
    revision: str = Field(min_length=32,max_length=64)
    text: str = Field(min_length=1,max_length=2000)
    settings: PreviewSettings
    confirmed: Literal[True]
    assumptions_confirmed: Literal[True]

    @field_validator("confirmed","assumptions_confirmed",mode="before")
    @classmethod
    def explicit_confirmation(cls,value):
        if type(value) is not bool or value is not True:
            raise ValueError("confirmation must be the JSON boolean true")
        return value


class InvalidateRequest(StrictModel):
    preview_id: str = Field(min_length=16,max_length=128)
    editable: bool = Field(default=False,strict=True)
