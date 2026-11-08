"""Explicit retry of ONE reviewed empty range; preserves candles/checkpoints."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--id',type=int,required=True)
    parser.add_argument('--reason',required=True)
    parser.add_argument('--allow-retry',action='store_true',help='Authorize this targeted queue change')
    args = parser.parse_args()
    if not args.allow_retry or args.id < 1:
        parser.error('positive id and explicit --allow-retry required')
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
    from app.services.backfill_control import BackfillControl
    from app.services.incremental_candle_repository import MySQLCandleRepository
    # Same machine-wide lease as API/CLI workers; a running worker => fail,
    # never interrupt it or steal its rows. This tool never starts a worker.
    with BackfillControl().worker():
        result = MySQLCandleRepository().request_empty_review_retry(args.id,args.reason)
    print(f"Review #{result['id']} marked retry_requested; checkpoint/data unchanged. "
          'No login or fetch started. Resume normal work explicitly when ready.')


if __name__ == '__main__':
    main()
