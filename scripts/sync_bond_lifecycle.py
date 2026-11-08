"""Preview official bond lifetimes; --apply stores metadata only under worker lock."""
from pathlib import Path
import argparse
import json
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from app.services.instrument_lifecycle import fetch_bond_lifecycles, today, blocked_reason


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true',help='僅寫入生命週期中繼資料；不登入、不抓行情、不修改斷點')
    parser.add_argument('--symbol',action='append',help='預覽指定代碼；不影響 --apply 的完整資料同步')
    args=parser.parse_args()
    rows=fetch_bond_lifecycles(today().isoformat())
    if not rows:
        raise RuntimeError('官方生命週期資料均未能核實，未寫入任何資料')
    output=dict(observed_on=str(today()),official_records=len(rows),applied=False,
                terminal_records=sum(bool(blocked_reason(r)) for r in rows),
                items=[{k:v for k,v in r.items() if k != 'raw_json'} for r in rows
                       if not args.symbol or r['symbol'] in args.symbol])
    if args.apply:
        from dotenv import load_dotenv
        from app.services.backfill_control import BackfillControl
        from app.services.incremental_candle_repository import MySQLCandleRepository
        load_dotenv(PROJECT/'.env',override=False)
        with BackfillControl().worker():
            MySQLCandleRepository().refresh_lifecycle()
        output['applied']=True
    print(json.dumps(output,ensure_ascii=False,default=str))


if __name__=='__main__': main()
