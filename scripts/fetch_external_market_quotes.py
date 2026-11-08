"""Fetch/validate only by default; --write atomically appends all five snapshots."""
from pathlib import Path
import argparse
import json
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from app.services.external_market_quotes import fetch_all
from app.services.external_market_quote_repository import COLUMNS, ExternalMarketQuoteRepository, QuoteWriteError, quote_row


def run(*, write=False):
    quotes = fetch_all()  # Never instantiate a DB repository before live validation passes.
    report = {"status": "validated", "database_written": False, "quotes": [q.to_dict() for q in quotes]}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if write:
        from dotenv import load_dotenv
        load_dotenv(PROJECT / ".env", override=False)
        repo = ExternalMarketQuoteRepository()
        batch_id = repo.save_batch(quotes)
        # A new connection confirms visibility after commit (not just uncommitted reads).
        try:
            rows = repo.read_batch(batch_id)
            expected = sorted([quote_row(q) for q in quotes], key=lambda row: (row[0], row[1], row[3]))
            actual = [tuple(row[col] for col in COLUMNS) for row in rows]
            if actual != expected:
                raise RuntimeError("提交後讀回資料不一致")
        except Exception:
            print(json.dumps({"status": "committed_but_readback_failed", "database_written": True,
                              "batch_id": batch_id, "note": "已提交，請查核此批次；不要盲目重跑"}, ensure_ascii=False))
            return 2
        print(json.dumps({"status": "written_and_verified", "database_written": True,
                          "table": "external_market_quote_snapshots", "batch_id": batch_id,
                          "verified_rows": len(rows)}, ensure_ascii=False))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="五筆來源驗證全數成功後才寫入 .env 指定的 MySQL")
    args = parser.parse_args()
    try:
        return run(write=args.write)
    except Exception as error:
        # No traceback here: .env/MySQL passwords and proxy settings must stay private.
        from app.services.external_market_quotes import QuoteFetchError
        from app.services.mysql_instrument_repository import MySQLRepositoryError
        reason = str(error) if isinstance(error, (QuoteFetchError, MySQLRepositoryError)) else type(error).__name__
        report = {"status": "failed", "reason": reason}
        if isinstance(error, QuoteWriteError):
            report.update(batch_id=error.batch_id, commit_uncertain=error.commit_uncertain)
            if error.commit_uncertain:
                report["note"] = "提交結果不確定，請查此 batch_id；不要盲目重跑"
        print(json.dumps(report, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
