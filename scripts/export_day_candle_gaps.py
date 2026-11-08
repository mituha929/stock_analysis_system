"""Export all saved daily-bar audit gaps, SELECT-only; never starts a fetch."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--markets',default='0,1,4,5,6')
    parser.add_argument('--include-resolved',action='store_true')
    parser.add_argument('--output',type=Path,required=True,help='New JSON file inside output/')
    args=parser.parse_args()
    selected=[int(s.strip()) for s in args.markets.split(',')]
    if not selected or any(m not in (0,1,4,5,6) for m in selected):
        parser.error('markets must be 0,1,4,5,6')
    output=args.output.resolve()
    if not output.is_relative_to((ROOT/'output').resolve()) or output.exists():
        parser.error('output must be a NEW file under the project output/ directory')
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
    from app.services.incremental_candle_repository import MySQLCandleRepository
    repo=MySQLCandleRepository()
    page=repo.day_integrity_report(selected,limit=1000,include_resolved=args.include_resolved)
    data=dict(generated_at=datetime.now(timezone.utc).isoformat(),markets=selected,
              storage_ready=page['storage_ready'],summary=page['summary'],total=page['total'],
              note='Saved audit state, paged during export (not a frozen snapshot if workers are active). Missing audits are not complete. No fetch or login.')
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x',encoding='utf-8') as stream:
        stream.write(json.dumps(data,ensure_ascii=False,default=str)[:-1])
        for field,total_field in (('items','total'),('assessments','assessment_total')):
            stream.write(',"'+field+'":[')
            offset,count=0,0
            while True:
                block=page if offset==0 else repo.day_integrity_report(selected,limit=1000,offset=offset,include_resolved=args.include_resolved)
                for row in block[field]:
                    if count: stream.write(',')
                    json.dump(row,stream,ensure_ascii=False,default=str)
                    count+=1
                if offset+1000>=block[total_field]: break
                offset+=1000
            stream.write(']')
        stream.write(',"export_complete":true}')
    print(f'Exported saved audit records to {output}; no source requests or database writes.')


if __name__=='__main__': main()
