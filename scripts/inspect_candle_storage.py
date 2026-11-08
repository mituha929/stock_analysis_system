r"""Read-only summary of the local MySQL K-line and backfill tables.

Examples (run from the ``capital_api`` directory)::

    .\.venv\Scripts\python.exe .\scripts\inspect_candle_storage.py
    .\.venv\Scripts\python.exe .\scripts\inspect_candle_storage.py --scope board-lot --intervals day,week

No INSERT, UPDATE, DELETE, CREATE, or Capital DLL call is performed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv


SCOPES = {
    "board-lot": {
        "label": "整張（上市／上櫃／興櫃）",
        "instrument_table": "instruments",
        "candle_table": "candles",
        "job_table": "candle_backfill_jobs",
        "markets": (0, 1, 4),
    },
    "odd-lot": {
        "label": "零股（上市零股／上櫃零股）",
        "instrument_table": "odd_lot_instruments",
        "candle_table": "odd_lot_candle",
        "job_table": "odd_lot_candle_backfill_jobs",
        "markets": (5, 6),
    },
}
MARKET_NAMES = {0: "上市", 1: "上櫃", 4: "興櫃", 5: "上市零股", 6: "上櫃零股"}
ALL_INTERVALS = ("1m", "5m", "15m", "60m", "day", "week", "month")


def parse_intervals(value: str) -> list[str]:
    if value.strip().lower() == "all":
        return list(ALL_INTERVALS)
    values = list(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))
    invalid = [item for item in values if item not in ALL_INTERVALS]
    if not values or invalid:
        raise argparse.ArgumentTypeError(f"intervals 僅支援 all 或 {','.join(ALL_INTERVALS)}")
    return values


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="唯讀查看 MySQL K 線與回補工作寫入狀況")
    result.add_argument(
        "--scope", choices=("all", *SCOPES), default="all",
        help="查看 all、board-lot 或 odd-lot；預設 all",
    )
    result.add_argument(
        "--intervals", type=parse_intervals, default=list(ALL_INTERVALS),
        help="all 或逗號分隔，例如 day,week；預設 all",
    )
    return result


def rows(cursor, query: str, parameters: tuple[object, ...]) -> list[tuple[object, ...]]:
    cursor.execute(query, parameters)
    return list(cursor.fetchall())


def print_table(headers: tuple[str, ...], values: list[tuple[object, ...]]) -> None:
    if not values:
        print("  （無資料）")
        return
    normalized = [["-" if value is None else str(value) for value in row] for row in values]
    widths = [len(header) for header in headers]
    for row in normalized:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    print("  " + " | ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("  " + "-+-".join("-" * width for width in widths))
    for row in normalized:
        print("  " + " | ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def table_exists(cursor, table_name: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name = %s",
        (table_name,),
    )
    return cursor.fetchone() is not None


def inspect_scope(cursor, scope: dict[str, object], intervals: list[str]) -> None:
    markets = tuple(scope["markets"])
    market_marks = ", ".join(["%s"] * len(markets))
    interval_marks = ", ".join(["%s"] * len(intervals))
    instrument_table = str(scope["instrument_table"])
    candle_table = str(scope["candle_table"])
    job_table = str(scope["job_table"])

    print(f"\n=== {scope['label']} ===")
    missing = [name for name in (instrument_table, candle_table, job_table) if not table_exists(cursor, name)]
    if missing:
        print(f"資料表不存在：{', '.join(missing)}")
        return

    active_instruments = rows(cursor, f"""
        SELECT market_no, COUNT(*)
        FROM `{instrument_table}`
        WHERE is_active = TRUE AND market_no IN ({market_marks})
        GROUP BY market_no ORDER BY market_no
    """, markets)
    print("有效商品數：")
    print_table(("市場", "檔數"), [(MARKET_NAMES.get(int(market), str(market)), count) for market, count in active_instruments])

    candle_summary = rows(cursor, f"""
        SELECT instruments.market_no, candles.interval_code, COUNT(*),
               COUNT(DISTINCT candles.symbol), MIN(candles.candle_time),
               MAX(candles.candle_time), MAX(candles.fetched_at)
        FROM `{candle_table}` AS candles
        INNER JOIN (
            SELECT DISTINCT market_no, symbol FROM `{instrument_table}`
            WHERE is_active = TRUE AND market_no IN ({market_marks})
        ) AS instruments ON instruments.symbol = candles.symbol
        WHERE candles.interval_code IN ({interval_marks})
        GROUP BY instruments.market_no, candles.interval_code
        ORDER BY instruments.market_no, FIELD(candles.interval_code, '1m', '5m', '15m', '60m', 'day', 'week', 'month')
    """, (*markets, *intervals))
    print("K 線資料：")
    print_table(
        ("市場", "週期", "根數", "商品數", "最早 K 線", "最新 K 線", "最近寫入"),
        [(MARKET_NAMES.get(int(market), str(market)), interval, count, symbols, first, last, fetched)
         for market, interval, count, symbols, first, last, fetched in candle_summary],
    )

    job_summary = rows(cursor, f"""
        SELECT market_no, interval_code, status, COUNT(*), MIN(updated_at), MAX(updated_at)
        FROM `{job_table}`
        WHERE market_no IN ({market_marks}) AND interval_code IN ({interval_marks})
        GROUP BY market_no, interval_code, status
        ORDER BY market_no, interval_code, status
    """, (*markets, *intervals))
    print("回補工作狀態：")
    print_table(
        ("市場", "週期", "狀態", "工作數", "最早更新", "最近更新"),
        [(MARKET_NAMES.get(int(market), str(market)), interval, job_status, count, first, last)
         for market, interval, job_status, count, first, last in job_summary],
    )

    slowest_successful = rows(cursor, f"""
        SELECT market_no, interval_code, MIN(symbol_updated_at)
        FROM (
            SELECT market_no, symbol, interval_code, MAX(updated_at) AS symbol_updated_at
            FROM `{job_table}`
            WHERE market_no IN ({market_marks}) AND interval_code IN ({interval_marks})
              AND status IN ('completed', 'insufficient_history')
            GROUP BY market_no, symbol, interval_code
        ) AS latest_symbol_updates
        GROUP BY market_no, interval_code
        ORDER BY market_no, interval_code
    """, (*markets, *intervals))
    print("最慢商品的最後成功更新時間（自動回補判斷依據）：")
    print_table(
        ("市場", "週期", "最慢成功更新"),
        [(MARKET_NAMES.get(int(market), str(market)), interval, updated)
         for market, interval, updated in slowest_successful],
    )


def main() -> int:
    arguments = parser().parse_args()
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    env_path = project_root / ".env"
    if not env_path.is_file():
        print(f"找不到 .env：{env_path}", file=sys.stderr)
        return 2
    load_dotenv(env_path)
    try:
        from app.services.mysql_instrument_repository import MySQLRepositoryError, MySQLSettings
        import mysql.connector

        settings = MySQLSettings.from_env()
    except Exception as error:
        print(f"設定或 MySQL driver 載入失敗：{error}", file=sys.stderr)
        return 2

    connection = None
    cursor = None
    try:
        connection = mysql.connector.connect(
            host=settings.host, port=settings.port, user=settings.user,
            password=settings.password, database=settings.database, connection_timeout=10,
        )
        cursor = connection.cursor()
        print(f"資料庫：{settings.database}（帳號：{settings.user}）")
        selected_scopes = SCOPES.values() if arguments.scope == "all" else (SCOPES[arguments.scope],)
        for scope in selected_scopes:
            inspect_scope(cursor, scope, arguments.intervals)
        return 0
    except MySQLRepositoryError as error:
        print(f"MySQL 設定錯誤：{error}", file=sys.stderr)
        return 2
    except mysql.connector.Error as error:
        print(f"MySQL 查詢失敗：{error}", file=sys.stderr)
        return 1
    finally:
        if cursor is not None:
            cursor.close()
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
