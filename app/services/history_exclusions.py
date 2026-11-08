"""Explicit, audited exclusions of vendor aggregate periods, never bad-row auto-skip."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
import json
from pathlib import Path

from app.services.capital_history_connection import CapitalHistoryError

POLICY_PATH = Path(__file__).resolve().parents[2] / "config" / "history_exclusions.json"


@dataclass(frozen=True)
class HistoryExclusion:
    id: str
    symbol: str
    interval: str
    start: date
    end: date
    reason: str

    def info(self):
        return dict(id=self.id, symbol=self.symbol, interval=self.interval,
                    start_date=self.start, end_date=self.end, reason=self.reason,
                    source="capital_native_history")


@lru_cache(maxsize=1)
def load_history_exclusions() -> tuple[HistoryExclusion, ...]:
    """Fail closed on malformed config; reload only on API/CLI restart."""
    try:
        data = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("top level must be an array")
        result, ids, keys = [], set(), set()
        for item in data:
            if set(item) != {"id", "symbol", "interval", "period_start", "reason"}:
                raise ValueError("unexpected or missing rule fields")
            if not all(isinstance(value, str) and value.strip() for value in item.values()):
                raise ValueError("rule fields must be nonempty strings")
            symbol, interval = item["symbol"], item["interval"]
            first = date.fromisoformat(item["period_start"])
            if symbol != symbol.strip().upper() or "*" in symbol or len(symbol) > 32:
                raise ValueError("an exact uppercase symbol is required")
            if interval == "week" and first.weekday() == 0:
                last = first + timedelta(days=6)
            elif interval == "month" and first.day == 1:
                last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            else:
                raise ValueError("only Monday-labelled week or first-day-labelled month is allowed")
            key = (symbol, interval, first)
            if item["id"] in ids or key in keys or len(item["id"]) > 100 or len(item["reason"]) > 300:
                raise ValueError("duplicate or oversized rule")
            ids.add(item["id"]); keys.add(key)
            result.append(HistoryExclusion(item["id"], symbol, interval, first, last, item["reason"]))
        return tuple(sorted(result, key=lambda rule: (rule.symbol, rule.interval, rule.start)))
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        raise CapitalHistoryError(f"歷史 K 線排除設定無效：{error}") from error


def excluded_periods(symbol: str, interval: str, first: date, last: date):
    return [rule for rule in load_history_exclusions()
            if rule.symbol == symbol.upper() and rule.interval == interval
            and rule.start <= last and rule.end >= first]


def exclusion_info(symbol: str, interval: str, first: date, last: date):
    return [rule.info() for rule in excluded_periods(symbol, interval, first, last)]


def allowed_history_ranges(symbol: str, interval: str, first: date, last: date):
    """Subtract whole aggregate periods without splitting a remaining candle."""
    ranges, current = [], first
    for rule in excluded_periods(symbol, interval, first, last):
        if current < rule.start:
            ranges.append((current, rule.start - timedelta(days=1)))
        current = max(current, rule.end + timedelta(days=1))
        if current > last:
            break
    if current <= last:
        ranges.append((current, last))
    return ranges


def excluded_label(symbol: str, interval: str, label: date) -> bool:
    # A vendor may label a weekly/monthly candle by its first trading day.
    return bool(excluded_periods(symbol, interval, label, label))
