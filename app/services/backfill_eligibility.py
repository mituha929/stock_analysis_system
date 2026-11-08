"""One explicit symbol exclusion policy for every automatic candle queue.

Do not change instrument.is_active: vendor sync owns that listing metadata.
Historical storage and explicit read-only/manual queries are unaffected.
"""
from functools import lru_cache
import json
from pathlib import Path
import re

from app.services.capital_history_connection import CapitalHistoryError

EXCLUDED_SYMBOLS_PATH = Path(__file__).resolve().parents[2] / "config" / "backfill_excluded_symbols.json"


@lru_cache(maxsize=1)
def excluded_backfill_symbols() -> frozenset[str]:
    try:
        symbols = json.loads(EXCLUDED_SYMBOLS_PATH.read_text(encoding="utf-8"))
        if (not isinstance(symbols, list)
                or any(not isinstance(s, str) or not re.fullmatch(r"[A-Z0-9_.-]{1,32}", s) for s in symbols)
                or len(set(symbols)) != len(symbols)):
            raise ValueError("expected unique exact uppercase symbols, no wildcards")
        return frozenset(symbols)
    except (OSError, ValueError, TypeError) as error:
        raise CapitalHistoryError(f"回補商品排除設定無效：{error}") from error


def backfill_symbol_predicate(alias: str | None = None) -> str:
    """Append with AND; literals come only from strict ASCII-validated config.

    Keeping this parameter-free also protects legacy query parameter ordering.
    No user-supplied symbol or table string is interpolated without validation.
    """
    if alias is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
        raise ValueError("Invalid SQL alias")
    symbols = sorted(excluded_backfill_symbols())
    if not symbols:
        return "1=1"
    column = f"{alias}.symbol" if alias else "symbol"
    literals = ",".join("'" + symbol + "'" for symbol in symbols)
    return f"{column} NOT IN ({literals})"
