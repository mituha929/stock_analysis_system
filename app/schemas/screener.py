from typing import Literal
import re

from pydantic import Field, model_validator

from app.rules.schemas import RuleTestRequest, StrictModel


class Universe(StrictModel):
    kind: Literal["markets", "symbols", "watchlist"] = "markets"
    markets: list[Literal[0, 1, 4, 5, 6]] = Field(default_factory=lambda: [0, 1], min_length=1, max_length=5)
    symbols: list[str] = Field(default_factory=list, max_length=5000)
    category_nos: list[int] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_scope(self):
        self.markets = list(dict.fromkeys(self.markets))
        self.symbols = list(dict.fromkeys(s.strip().upper() for s in self.symbols))
        if any(not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,31}", s) for s in self.symbols):
            raise ValueError("invalid symbol")
        if self.kind == "symbols" and not self.symbols:
            raise ValueError("symbols universe requires symbols")
        if self.kind != "symbols" and self.symbols:
            raise ValueError("symbols is only valid for symbols universe")
        if any(type(n) is not int or n < 0 for n in self.category_nos):
            raise ValueError("category_nos must be nonnegative integers")
        return self


class Prefilter(StrictModel):
    min_price: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    max_price: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    min_volume: int | None = Field(default=None, ge=0, strict=True)
    max_volume: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def bounds(self):
        for low, high in ((self.min_price, self.max_price), (self.min_volume, self.max_volume)):
            if low is not None and high is not None and low > high:
                raise ValueError("prefilter minimum must not exceed maximum")
        return self


class SortSpec(StrictModel):
    field: Literal["matched_count", "direction_score", "risk_score", "price", "volume", "symbol"]
    direction: Literal["asc", "desc"]


class ScreenerRequest(RuleTestRequest):
    universe: Universe = Field(default_factory=Universe)
    prefilter: Prefilter = Field(default_factory=Prefilter)
    candle_table: Literal["candles", "odd_lot_candle"] = "candles"
    history_bars: int = Field(default=360, ge=2, le=2000, strict=True)
    limit: int = Field(default=100, ge=1, le=1000, strict=True)
    timeout_seconds: int = Field(default=120, ge=1, le=300, strict=True)
    sort: list[SortSpec] = Field(default_factory=lambda: [SortSpec(field="matched_count", direction="desc"), SortSpec(field="direction_score", direction="desc"), SortSpec(field="risk_score", direction="asc")], min_length=1, max_length=6)

    @model_validator(mode="after")
    def validate_tables(self):
        permitted = {0, 1, 4} if self.candle_table == "candles" else {5, 6}
        if not set(self.universe.markets) <= permitted:
            raise ValueError("markets do not match candle_table (candles: 0,1,4; odd_lot_candle: 5,6)")
        if len({s.field for s in self.sort}) != len(self.sort):
            raise ValueError("duplicate sort fields")
        return self
