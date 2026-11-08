"""Manage a standalone common holiday list for TWSE/TPEx/emerging stocks."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from app.services.stock_market_holidays import (
    MANUAL_PATH, MERGED_PATH, REPORT_PATH, SNAPSHOT_PATH, SUPPLEMENTS_PATH,
    StockHolidayError, build_calendar, calendar_status, iso_day, load_json,
    load_manual, refresh, save_json, today, validate_override, write_derived,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="上市／上櫃／興櫃共用休市名單；不修改正式回補")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("refresh", "build", "list", "add", "check"):
        command = commands.add_parser(name)
        command.add_argument("--as-of", type=iso_day, default=today())
        command.add_argument("--years", type=int, default=10)
        if name in {"add", "check"}:
            command.add_argument("day", type=iso_day)
        if name == "add":
            command.add_argument("--name", required=True)
            command.add_argument("--reason", required=True)
            command.add_argument("--source-url")
            command.add_argument("--open", action="store_true", help="人工更正為開市，包括週末例外")
        if name == "list":
            command.add_argument("--include-weekends", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "refresh":
            result = refresh(args.as_of, years=args.years, snapshot_path=SNAPSHOT_PATH,
                             manual_path=MANUAL_PATH, supplements_path=SUPPLEMENTS_PATH,
                             merged_path=MERGED_PATH, report_path=REPORT_PATH,
                             progress=lambda year, row: print(f"{year}：{row['status']}，原始 {len(row['records'])} 筆", flush=True))
        else:
            snapshot, supplements = load_json(SNAPSHOT_PATH), load_json(SUPPLEMENTS_PATH)
            manual = load_manual(MANUAL_PATH)
            if args.command == "add":
                if args.day > today():
                    raise StockHolidayError("未來預告請另存，尚未發生的日期不列為歷史休市")
                previous = next((row for row in manual["records"] if row["date"] == args.day.isoformat()), {})
                record = validate_override(dict(date=args.day.isoformat(), name=args.name, reason=args.reason,
                                                source_url=args.source_url if args.source_url is not None else previous.get("source_url", ""),
                                                is_closed=not args.open, enabled=True))
                manual = dict(manual, records=sorted([row for row in manual["records"] if row["date"] != record["date"]] + [record], key=lambda row: row["date"]))
                build_calendar(snapshot, supplements, manual, args.as_of, args.years)
                save_json(MANUAL_PATH, manual)
            if args.command in {"list", "check"}:
                result = build_calendar(snapshot, supplements, manual, args.as_of, args.years)
                if args.command == "check":
                    state, reason = calendar_status(result, args.day)
                    print(f"{args.day}：{state}（{reason}）")
                else:
                    for row in result["records"]:
                        if row["is_closed"] and (args.include_weekends or row["category"] != "weekend"):
                            names = "、".join(event["name"] for event in row["evidence"] if event["kind"] == "closed")
                            print(f"{row['date']}  {row['category']}  {names}")
            else:
                result = write_derived(snapshot, supplements, manual, args.as_of, years=args.years,
                                       merged_path=MERGED_PATH, report_path=REPORT_PATH)
        print(f"範圍 {result['range_start']}～{result['range_end']}；適用上市／上櫃／興櫃股票")
        print(f"已收錄休市 {result['closed_date_count']} 日（平日 {result['weekday_closed_count']}、週末 {result['weekend_closed_count']}）")
        print(f"缺少年度表：{result['missing_years']}；臨時休市仍需另附交易所公告")
        print(f"人工新增：{MANUAL_PATH}\n日期表：{REPORT_PATH}")
        print("未變更 MySQL、群益連線、.env 或回補工作；合併檔若已重建，後續交易日判斷可讀取新快照，不自動啟動抓取。")
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"休市名單作業失敗，請確認資料／來源：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
