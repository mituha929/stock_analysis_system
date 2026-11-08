"""Repeated synthetic callbacks/cache churn; no network, MySQL or DLL."""
from contextlib import nullcontext
from datetime import date,datetime,timedelta,timezone
from pathlib import Path
import gc
import json
import sys
import time
import tracemalloc
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.schemas.models import Candle
from app.services.capital_history_service import HistoryQuoteEvents,request_history_candles
from app.services.backfill_policy import history_request_limiter
from app.services.quote_cache import QuoteCache


def main():
    events=HistoryQuoteEvents(); events.connected.set()
    cache=QuoteCache()
    base=datetime(2026,1,1,tzinfo=timezone.utc)
    class Quote:
        def SKQuoteLib_RequestKLineAMByDate(self,*_args):
            events.records.extend(Candle(timestamp=base+timedelta(minutes=i),open=10,high=12,low=9,close=11,volume=100) for i in range(1200))
            events.completed.set()
            return 0
    quote=Quote()
    def cycle(index):
        rows=request_history_candles(quote,None,events,lambda:None,str(index),"day",date(2026,1,1),date(2026,1,1),1)
        assert len(rows)==1200 and not events.records
        cache.set_historical_candles(str(index),"day",rows)
        rows.clear()
    begin=time.monotonic()
    tracemalloc.start()
    try:
        with patch.object(history_request_limiter,"slot",side_effect=lambda:nullcontext()):
            for index in range(40): cycle(index)
            gc.collect()
            warmed=tracemalloc.get_traced_memory()[0]
            for index in range(40,240): cycle(index)
            gc.collect()
            final,peak=tracemalloc.get_traced_memory()
        assert len(cache._historical_candles)==20
        assert final-warmed < 1024*1024,"Repeated requests retained increasing Python allocations"
        print(json.dumps(dict(simulated_requests=240,bars_per_request=1200,total_bars=288000,
            cache_entries=len(cache._historical_candles),received_buffer_rows=len(events.records),
            warmed_python_mb=round(warmed/1024/1024,3),final_python_mb=round(final/1024/1024,3),
            growth_after_warmup_mb=round((final-warmed)/1024/1024,3),peak_python_mb=round(peak/1024/1024,3),
            elapsed_seconds=round(time.monotonic()-begin,3),passed=True,
            real_capital_dll_used=False),ensure_ascii=False),flush=True)
    finally:
        tracemalloc.stop()


if __name__=="__main__": main()
