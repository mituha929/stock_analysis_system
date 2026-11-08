"""Official TWSE/TPEx profiles. Dry-run by default; --write uses existing MySQL."""
from datetime import datetime, timezone
from pathlib import Path
import argparse
import json
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from app.services.stock_master_sources import TAIPEI, StockSourceError, fetch_snapshots, validate_code
from app.services.stock_master_repository import StockMasterRepository, StockSyncError, json_value
from app.services.mysql_instrument_repository import MySQLRepositoryError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--write", action="store_true", help="公司及 ETF 四個來源全數有效後，交易式更新現有 MySQL")
    action.add_argument("--profile", metavar="CODE", help="只查 MySQL 已儲存資料，不連官方網站")
    action.add_argument("--runs", action="store_true", help="只查最近十次更新紀錄")
    parser.add_argument("--market", choices=("TWSE", "TPEx"), help="搭配 --profile 指定歷史市場")
    args = parser.parse_args()
    if args.market and not args.profile:
        parser.error("--market 必須搭配 --profile")
    try:
        if args.write or args.profile or args.runs:
            from dotenv import load_dotenv
            load_dotenv(PROJECT / ".env", override=False)
            repo = StockMasterRepository()
            if args.profile:
                validate_code(args.profile)
                result = repo.profile(args.profile, args.market)
            elif args.runs:
                result = repo.sync_runs()
            else:
                result = repo.synchronize()
        else:
            snapshots = fetch_snapshots()
            today = datetime.now(timezone.utc).astimezone(TAIPEI).date()
            result = {"status": "validated", "database_written": False, "markets": [{
                "market": s.market, "dataset": s.dataset, "source_date": s.source_date,
                "source_date_kind": s.source_date_kind, "count": len(s.records),
                "missing_dates": sum(r.listing_date is None for r in s.records),
                "scheduled": sum(r.listing_date is not None and r.listing_date > today for r in s.records),
                "warnings": s.warnings, "fingerprint": s.fingerprint(),
                "examples": [{"code": r.code, "name": r.name, "market": r.market, "security_type": r.security_type,
                              "listing_date": r.listing_date, "industry": r.industry}
                             for r in s.records if r.code in {"2330", "6488", "0050", "00631L", "00625K", "00679B", "00411A"}],
            } for s in snapshots]}
        print(json.dumps(json_value(result), ensure_ascii=False, indent=2))
        return 0 if result.get("status") not in {"busy", "retry_wait"} else 3
    except Exception as error:
        message = str(error) if isinstance(error, (StockSourceError, StockSyncError)) else "查無此代號" if isinstance(error, KeyError) else type(error).__name__
        result = {"status": "failed", "error": message}
        if isinstance(error, StockSyncError):
            result.update(run_id=error.run_id, commit_uncertain=error.commit_uncertain)
            if error.commit_uncertain:
                result["note"] = "提交結果不確定，請先查更新紀錄，不要盲目重跑"
        print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
