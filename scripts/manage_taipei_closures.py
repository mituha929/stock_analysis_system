"""Fetch/list/edit Taipei closures without starting the API or using MySQL."""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from app.services.taipei_closures import (
    MANUAL_PATH, MERGED_PATH, PERIODS, REPORT_PATH, SCOPES, SNAPSHOT_PATH,
    TaipeiClosureError, build_files, iso_date, merge_closures, read_json,
    read_manual, record_key, refresh_files, save_json, taipei_today, validate_record,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="臺北市停班停課獨立名單；不更動正式交易日曆與回補")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("refresh", "build", "list", "add"):
        command = commands.add_parser(name)
        command.add_argument("--as-of", type=date.fromisoformat, default=taipei_today())
        command.add_argument("--years", type=int, default=10)
        if name == "add":
            command.add_argument("day", type=iso_date, help="YYYY-MM-DD")
            command.add_argument("--reason", required=True)
            command.add_argument("--scope", choices=("citywide", "partial_area", "specific_schools"), default="citywide")
            command.add_argument("--period", choices=tuple(key for key in PERIODS if key != "unknown"), default="full_day")
            command.add_argument("--classes-only", action="store_true", help="只停課、照常上班")
            command.add_argument("--description")
            command.add_argument("--source-url")
    args = parser.parse_args(argv)
    try:
        if args.command == "refresh":
            merged = refresh_files(args.as_of, years=args.years, snapshot_path=SNAPSHOT_PATH,
                                   manual_path=MANUAL_PATH, merged_path=MERGED_PATH, report_path=REPORT_PATH)
        else:
            manual = read_manual(MANUAL_PATH)
            snapshot = read_json(SNAPSHOT_PATH)
            if args.command == "add":
                if args.day > taipei_today():
                    raise TaipeiClosureError("未來預告不列入已發生紀錄，請到當日再加入")
                key = (args.day.isoformat(), args.scope, args.period)
                existing = next((item for item in manual["records"] if record_key(item) == key), {})
                description = args.description if args.description is not None else existing.get("description", "")
                source_url = args.source_url if args.source_url is not None else existing.get("source_url", "")
                if not description and args.scope != "citywide":
                    raise TaipeiClosureError("局部地區／指定學校需以 --description 說明適用對象")
                record = validate_record(dict(
                    date=args.day.isoformat(), reason=args.reason, scope=args.scope, period=args.period,
                    work_suspended=not args.classes_only, classes_suspended=True,
                    description=description, source_url=source_url, enabled=True), manual=True)
                updated = dict(manual, records=[item for item in manual["records"] if record_key(item) != record_key(record)] + [record])
                updated["records"].sort(key=record_key)
                merge_closures(snapshot, updated, args.as_of, args.years)  # Validate before writing user file.
                save_json(MANUAL_PATH, updated)
                manual = updated
                print(f"已補登 {args.day}；來源由你填寫，未自動視為交易所休市公告。")
            if args.command == "list":
                merged = merge_closures(snapshot, manual, args.as_of, args.years)
                for item in merged["records"]:
                    print(f"{item['date']}  {SCOPES[item['scope']]}／{PERIODS[item['period']]}  {item['reason']}")
            else:
                merged = build_files(snapshot, manual, args.as_of, years=args.years,
                                     merged_path=MERGED_PATH, report_path=REPORT_PATH)
        print(f"範圍：{merged['range_start']}～{merged['range_end']}")
        print(f"有停班／停課：{merged['date_count']} 日；全市全天同時停班停課：{len(merged['citywide_full_day_dates'])} 日；待確認：{len(merged['review_required_records'])} 筆")
        print(f"官方原檔最後紀錄日：{merged['source_last_record_date']}；不代表完整覆蓋至今天。")
        print(f"手動新增檔：{MANUAL_PATH}")
        if args.command != "list":
            print(f"整理結果：{MERGED_PATH}\n可閱讀日期表：{REPORT_PATH}")
        print("未修改 API、MySQL、回補進度、.env 或正式交易日曆。")
        return 0
    except (OSError, TaipeiClosureError, KeyError, TypeError) as error:
        print(f"停班停課名單作業失敗：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
