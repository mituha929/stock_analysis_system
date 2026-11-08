"""Maintain latest candles and per-interval history goals (not storage caps).

The script keeps one COM quote login for its entire run, throttles requests,
and stores an individual job record per symbol.  Re-run it with the same
arguments after interruption to continue from pending/unfinished jobs.

The script acquires the same machine-level DLL lock as FastAPI. If the API is
currently connected, it waits rather than opening a second quote connection.
"""

from __future__ import annotations

import argparse
from contextlib import nullcontext
import importlib
import os
import struct
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


KLINE_TYPES: dict[str, tuple[int, int]] = {
    "1m": (0, 1), "5m": (0, 5), "15m": (0, 15), "60m": (0, 60),
    "day": (4, 0), "week": (5, 0), "month": (6, 0),
}
ALL_INTERVALS = ("day", *(interval for interval in KLINE_TYPES if interval != "day"))


def parse_date(value: str) -> date:
    for pattern in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            pass
    raise argparse.ArgumentTypeError("日期必須是 YYYY-MM-DD 或 YYYYMMDD")


def parse_markets(value: str) -> list[int]:
    allowed = {0, 1, 4, 5, 6}
    try:
        markets = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as error:
        raise argparse.ArgumentTypeError("markets 必須是逗號分隔的整數") from error
    if not markets or any(item not in allowed for item in markets):
        raise argparse.ArgumentTypeError("markets 僅支援 0、1、4、5、6")
    return markets


def parse_intervals(value: str) -> list[str]:
    if value.strip().lower() == "all":
        return list(ALL_INTERVALS)
    intervals = [item.strip() for item in value.split(",") if item.strip()]
    if not intervals or any(item not in KLINE_TYPES for item in intervals):
        raise argparse.ArgumentTypeError(f"intervals 僅支援 {','.join(ALL_INTERVALS)} 或 all")
    return sorted(dict.fromkeys(intervals), key=lambda interval: interval != "day")


def lookback_start(interval: str, end_date: date, target_bars: int) -> date:
    """Legacy calendar estimate; the shared planner now owns query ranges."""
    calendar_days_per_bar = {
        "1m": 10 / 360, "5m": 16 / 360, "15m": 45 / 360,
        "60m": 160 / 360, "day": 1.55,
        "week": 10.5, "month": 31.5,
    }[interval]
    return end_date - timedelta(days=max(7, int(target_bars * calendar_days_per_bar)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="批次回補群益商品的 K 線至 MySQL")
    parser.add_argument("--markets", type=parse_markets, default=[0, 1, 4], help="市場別，例如 0,1,4；預設 0,1,4")
    parser.add_argument("--odd-lot", action="store_true", help="零股模式：使用市場 5、6，並分別寫入 odd_lot_candle 與 odd_lot_candle_backfill_jobs")
    parser.add_argument("--instrument-table", default="instruments", help="商品主檔資料表；預設 instruments")
    parser.add_argument("--candle-table", default="candles", help="K 線資料表；預設 candles")
    parser.add_argument("--job-table", default="candle_backfill_jobs", help="回補工作表；預設 candle_backfill_jobs")
    parser.add_argument("--intervals", type=parse_intervals, default=list(ALL_INTERVALS), help="週期清單，例如 1m,5m,day；預設所有週期")
    parser.add_argument("--target-bars", type=int, default=None, help="留空依 .env 各週期目標（一般1200、週560、月100）；指定則覆寫所選週期，既有歷史不刪除")
    parser.add_argument("--start-date", type=parse_date, help="自訂起日；只可搭配單一 interval")
    parser.add_argument("--end-date", type=parse_date, default=None, help="資料截止日；預設最近已收盤交易日，週／月僅使用已完成期間")
    parser.add_argument("--max-jobs", type=int, help="本次最多處理幾檔，用於分批測試")
    parser.add_argument("--request-timeout", type=int, default=30, help="每檔等待 K 線完成秒數")
    parser.add_argument("--connect-timeout", type=int, default=60, help="等待群益商品資料就緒秒數")
    parser.add_argument("--min-request-interval", type=float, default=None, help="每次 K 線請求間隔；預設讀取共用設定 0.8 秒")
    parser.add_argument("--retry-failed", action="store_true", help="只重試目前最新失敗商品")
    parser.add_argument("--pause", action="store_true", help="請求目前回補在區間寫入後安全暫停")
    parser.add_argument("--resume", action="store_true", help="接續最後保存的工作範圍")
    parser.add_argument("--dry-run", action="store_true", help="只建立／檢查工作，不連線群益也不抓 K 線")
    return parser


def return_message(center: Any, code: int) -> str:
    try:
        return str(center.SKCenterLib_GetReturnCodeMessage(code))
    except Exception:
        return "未知錯誤"


def ensure_success(center: Any, code: int, method: str) -> None:
    from app.services.capital_history_connection import assert_history_code
    assert_history_code(code, center, allow_login_warning=method == "SKCenterLib_Login")


class RecyclingKLineSession:
    """CLI facade using the same recyclable native host as the HTTP API."""
    def __init__(self, project_root, user_id, password, authority, connect_timeout, request_timeout=30):
        from dataclasses import replace
        from app.services.capital_worker_service import CapitalWorkerService, NativeWorkerConfig
        from app.services.backfill_control import BackfillControl
        from app.services.quote_cache import QuoteCache
        config=replace(NativeWorkerConfig.from_env(),connect_timeout=connect_timeout,
                       history_timeout=request_timeout,rpc_timeout=max(180,request_timeout*8+60))
        self.service=CapitalWorkerService(QuoteCache(),control=BackfillControl(),config=config)
        self.credentials=(user_id,password,authority)

    def __enter__(self):
        try:
            self.service.connect(*self.credentials)
            return self
        except BaseException:
            self.service.stop()
            raise

    def request(self,symbol,interval,start,end,_timeout):
        return self.service.fetch_historical_candles(symbol,interval,start,end,cache_result=False)

    def history_checkpoint(self,committed):
        self.service.history_checkpoint(committed)

    def __exit__(self,*_errors):
        self.service.stop()
        self.credentials=None


class CapitalKLineSession:
    """A single-login COM session that serializes RequestKLineAMByDate calls."""

    def __init__(self, project_root: Path, user_id: str, password: str, authority: int, connect_timeout: int) -> None:
        self.project_root = project_root
        self.user_id = user_id
        self.password = password
        self.authority = authority
        self.connect_timeout = connect_timeout
        self.center: Any | None = None
        self.quote: Any | None = None
        self._directory_handle: Any | None = None
        self._reply_handler: Any | None = None
        self._quote_handler: Any | None = None
        self._pythoncom: Any | None = None
        self._event: Any | None = None

    def __enter__(self) -> "CapitalKLineSession":
        try:
            return self._open()
        except Exception:
            # A failing __enter__ does not automatically call __exit__.
            self.__exit__()
            raise

    def _open(self) -> "CapitalKLineSession":
        import comtypes.client
        import pythoncom
        from app.services.capital_history_connection import (
            CapitalHistoryConnectionError, enter_history_monitor,
            reconnect_history_monitor, require_history_connection, with_history_connection_recovery,
        )
        from app.services.capital_history_service import HistoryQuoteEvents

        self._pythoncom = pythoncom
        pythoncom.CoInitialize()
        dll_path = Path(os.getenv("CAPITAL_SKCOM_DLL", self._default_dll()))
        if not dll_path.is_file():
            raise RuntimeError(f"找不到 SKCOM.dll：{dll_path}")
        if hasattr(os, "add_dll_directory"):
            self._directory_handle = os.add_dll_directory(str(dll_path.parent))
        from app.services.capital_dll_environment import ensure_dll_directory_on_path
        ensure_dll_directory_on_path(dll_path.parent)
        comtypes.client.GetModule(str(dll_path))
        sk = importlib.import_module("comtypes.gen.SKCOMLib")
        self.center = comtypes.client.CreateObject(sk.SKCenterLib, interface=sk.ISKCenterLib)
        self.quote = comtypes.client.CreateObject(sk.SKQuoteLib, interface=sk.ISKQuoteLib)
        reply = comtypes.client.CreateObject(sk.SKReplyLib, interface=sk.ISKReplyLib)

        class ReplyEvent:
            def OnReplyMessage(self, _user: str, _message: str) -> int:
                return -1

        self._event = HistoryQuoteEvents()
        reply_event = ReplyEvent()
        # Retain handlers and sink instances for the full session.  The Reply
        # event must be registered before Login, per Capital's requirement.
        self._reply = reply
        self._reply_handler = comtypes.client.GetEvents(reply, reply_event)
        self._quote_handler = comtypes.client.GetEvents(self.quote, self._event)
        self._reply_event = reply_event
        ensure_success(self.center, self.center.SKCenterLib_SetAuthority(self.authority), "SKCenterLib_SetAuthority")
        ensure_success(self.center, self.center.SKCenterLib_Login(self.user_id, self.password), "SKCenterLib_Login")
        try:
            enter_history_monitor(
                self.quote, self.center, self._event.connected, pythoncom.PumpWaitingMessages, self.connect_timeout,
            )
        except CapitalHistoryConnectionError:
            with_history_connection_recovery(
                lambda: require_history_connection(self._event.connected),
                lambda: reconnect_history_monitor(
                    self.quote, self.center, self._event.connected, pythoncom.PumpWaitingMessages, self.connect_timeout,
                ),
            )
        return self

    def request(self, symbol: str, interval: str, start_date: date, end_date: date, timeout: int):
        from app.services.capital_history_service import request_history_candles
        if self.center is None or self.quote is None or self._event is None or self._pythoncom is None:
            raise RuntimeError("群益 K 線連線尚未建立")
        return request_history_candles(
            self.quote, self.center, self._event, self._pythoncom.PumpWaitingMessages,
            symbol, interval, start_date, end_date, timeout, self.connect_timeout,
        )

    def __exit__(self, *_exception: object) -> None:
        try:
            if self.quote is not None:
                self.quote.SKQuoteLib_LeaveMonitor()
        except Exception:
            pass
        self._reply_handler = None
        self._quote_handler = None
        self._event = self._reply_event = None
        self.center = self.quote = self._reply = None
        if self._directory_handle is not None:
            self._directory_handle.close()
            self._directory_handle = None
        if self._pythoncom is not None:
            self._pythoncom.CoUninitialize()
            self._pythoncom = None

    def _default_dll(self) -> Path:
        architecture = "x64" if struct.calcsize("P") * 8 == 64 else "x86"
        return self.project_root.parent / "CapitalAPI_2.13.58_PythonExample" / "元件" / architecture / "SKCOM.dll"


def _main_locked() -> int:
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    load_dotenv(project_root / ".env")
    args = build_parser().parse_args()
    from app.services.backfill_control import BackfillControl
    from app.services.backfill_engine import BackfillRunner
    from app.services.empty_history_review import pending_review_text
    from app.services.backfill_policy import completed_end, history_request_limiter, request_interval
    from app.services.backfill_targets import target_for,refresh_plan_targets
    from app.services.capital_history_service import CapitalHistoryError, configured_history_limits
    from app.services.incremental_candle_repository import MySQLCandleRepository
    from app.services.capital_process_lock import get_capital_process_lock
    from app.services.taiwan_trading_calendar import trading_day_status

    control = BackfillControl()
    if args.pause:
        control.pause(True)
        print("已請求安全暫停；目前區間寫入後停止，進度保留。")
        return 0
    if args.max_jobs is not None and args.max_jobs < 1:
        print("--max-jobs 必須大於 0", file=sys.stderr)
        return 2
    if args.target_bars is not None and not 1 <= args.target_bars <= 10000:
        print("--target-bars 必須介於 1 到 10000", file=sys.stderr)
        return 2
    try:
        history_limits = configured_history_limits()
        history_request_limiter.override = request_interval(args.min_request_interval)
    except (ValueError, CapitalHistoryError) as error:
        print(error, file=sys.stderr)
        return 2
    if args.start_date is not None and len(args.intervals) != 1:
        print("--start-date 只可搭配單一週期", file=sys.stderr)
        return 2
    if args.odd_lot:
        if args.markets != [0, 1, 4] and not set(args.markets).issubset({5, 6}):
            print("--odd-lot 僅可搭配市場 5、6", file=sys.stderr)
            return 2
        if (args.instrument_table,args.candle_table,args.job_table) != ("instruments","candles","candle_backfill_jobs"):
            print("--odd-lot 不可與自訂資料表參數合併使用", file=sys.stderr)
            return 2
        args.markets = [5, 6] if args.markets == [0, 1, 4] else args.markets
        args.instrument_table,args.candle_table,args.job_table = "odd_lot_instruments","odd_lot_candle","odd_lot_candle_backfill_jobs"
        print("資料來源說明：目前歷史 K 線函式以整股成交計算，零股表名稱不代表零股成交 K 線。")
    repository = MySQLCandleRepository()
    try:
        plans = control.saved_plans() if args.resume else []
        if plans and args.target_bars is not None:
            for plan in plans:
                if plan.get("target_bars") != args.target_bars: plan["planned_on"] = None
                plan.update(target_bars=args.target_bars,target_source="override")
        if not plans:
            plans = []
            for interval in args.intervals:
                end = completed_end(interval,args.end_date)
                if args.start_date and args.start_date > end:
                    print("start-date 不可晚於最後已完成交易日", file=sys.stderr)
                    return 2
                for market in args.markets:
                    failed = None
                    if args.retry_failed:
                        repository.ensure_incremental_storage(args.candle_table,args.job_table)
                        failed = repository.failed_symbols(market,interval,args.job_table,args.instrument_table)
                    plans.append(dict(market_no=market,interval=interval,
                            start_date=None,end_date=end,candle_table=args.candle_table,
                            instrument_table=args.instrument_table,
                            job_table=args.job_table,target_bars=target_for(interval,args.target_bars),
                            target_source="config" if args.target_bars is None else "override",batch_v2=True,planned_on=None,
                            as_of_date=args.end_date,explicit_start=args.start_date,only_symbols=failed,force=args.retry_failed))
                    print(f"市場 {market} {interval}：已排入族群，開工時批次規劃逐檔最新缺口；較舊歷史另排慢速補齊。")
    except Exception as error:
        print(f"準備回補失敗：{error}", file=sys.stderr)
        return 1
    if not plans:
        print("所有選定商品已完成更新。")
        return 0
    refresh_plan_targets(plans)
    target_text=", ".join(f"{i}:{n}" for i,n in {p["interval"]:p["target_bars"] for p in plans}.items())
    print(f"共用查詢間隔 {history_request_limiter.override} 秒；補齊目標 {target_text}（不是儲存上限）；可從每個已提交區間續傳。")
    print("單次查詢上限（日曆天）：" + ", ".join(f"{i}:{history_limits[i]}" for i in dict.fromkeys(p["interval"] for p in plans)))
    if args.dry_run:
        for plan in plans:
            summary = repository.plan_batch(plan["market_no"],plan["interval"],plan["end_date"],plan["target_bars"],
                plan["candle_table"],plan["job_table"],plan.get("instrument_table",args.instrument_table),
                force=plan.get("force",False),explicit_start=plan.get("explicit_start"),only_symbols=plan.get("only_symbols"))
            print(f"市場 {plan['market_no']} {plan['interval']}：準備 {summary['queued_jobs']} 筆（未呼叫群益）。")
        return 0
    for plan in plans:
        if not plan.get("batch_v2"):
            plan.update(batch_v2=True,planned_on=None,instrument_table=plan.get("instrument_table",args.instrument_table),
                        target_bars=plan.get("target_bars",target_for(plan["interval"],args.target_bars)))
    user_id,password = os.getenv("CAPITAL_USER_ID"),os.getenv("CAPITAL_PASSWORD")
    if not user_id or not password:
        print("請在 .env 設定 CAPITAL_USER_ID 與 CAPITAL_PASSWORD", file=sys.stderr)
        return 2
    try:
        authority = int(os.getenv("CAPITAL_AUTHORITY","0"))
        if authority not in range(4):
            raise ValueError
    except ValueError:
        print("CAPITAL_AUTHORITY 必須是 0、1、2 或 3", file=sys.stderr)
        return 2
    # A new explicit invocation is permission to resume the persisted work.
    control.pause(False)
    if args.resume:
        control.update(state="queued",resume_pending=True)
    if not args.resume:
        control.update(state="prepared",processed=0,completed=0,insufficient_history=0,failed=0)
    # Keep the scope even if login/session startup fails before the first job.
    control.update(source="cli",manual_run=True,plans=plans,target_bars=args.target_bars or 1200)
    processed = 0
    try:
        # DLL ownership belongs exclusively to the spawned native worker;
        # holding its machine lock in this CLI would deadlock the child login.
        with nullcontext():
            with RecyclingKLineSession(project_root,user_id,password,authority,args.connect_timeout,args.request_timeout) as session:
                class Adapter:
                    cache = None
                    def fetch_historical_candles(self,symbol,interval,start,end,**_options):
                        return session.request(symbol,interval,start,end,args.request_timeout)
                    def history_checkpoint(self,committed):
                        session.history_checkpoint(committed)
                def report(job,status,count,elapsed):
                    print(f"{job['symbol']} {job['interval_code']}：{count} 根、{status}，{elapsed:.2f} 秒",flush=True)
                while True:
                    result = BackfillRunner(Adapter(),repository,control,source="cli",
                        max_jobs=args.max_jobs,on_job=report).run(plans,args.target_bars or 1200,owns_worker_lock=True)
                    processed = max(processed,result["processed"])
                    if result.get('pending_review'):
                        print(pending_review_text(result),flush=True)
                    if result.get("deferred") or result.get('audit_pending_symbols'):
                        print(result.get('pause_reason',f"最新資料優先輪完成；{result['deferred']} 筆慢速補齊保留等待")+"；使用 --resume 續跑，或保持此程序等候。",flush=True)
                        while not control.snapshot().get("pause_requested"):
                            retry = datetime.fromisoformat(control.snapshot()["next_retry_at"])
                            if datetime.now(timezone.utc) >= retry: break
                            time.sleep(1)
                        if control.snapshot().get("pause_requested"): return 0
                        continue
                    if not result.get("paused"):
                        print(f"本次處理 {processed} 筆；完成 {result['completed']}、不足 {result['insufficient_history']}、失敗 {result['failed']}。")
                        return 0 if not result["failed"] else 1
                    print(f"已暫停：{result['pause_reason']}；區間進度已保留。",flush=True)
                    if result["pause_kind"] == "manual":
                        return 0
                    print("15 秒後檢查並嘗試接續；可用 --pause 或 Ctrl+C 停止。",flush=True)
                    for _ in range(15):
                        if control.snapshot().get("pause_requested"):
                            return 0
                        time.sleep(1)
    except KeyboardInterrupt:
        control.pause(True)
        print("已中斷；已寫入區間保留，可用 --resume 接續。")
        return 130
    except Exception as error:
        print(f"回補未完成：{error}；已提交進度保留。",file=sys.stderr)
        return 1


def main() -> int:
    # Hold the shared worker lease BEFORE planning, login or changing control
    # state, and throughout all reconnect/backoff rounds. A preflight alone
    # leaves a race with the API's background preparation/start transition.
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0,str(project_root))
    load_dotenv(project_root / ".env")
    from app.services.backfill_control import BackfillControl
    from app.services.capital_process_lock import CapitalProcessLockError
    control = BackfillControl()
    if "--pause" in sys.argv:
        return _main_locked()
    from app.services.capital_history_service import configured_history_limits, CapitalHistoryError
    try:
        configured_history_limits()
    except (ValueError,CapitalHistoryError) as error:
        print(error,file=sys.stderr)
        return 2
    try:
        with control.worker():
            state = control.snapshot()
            if (state.get("manual_daily_run") or {}).get("phase") in {"waiting_stop", "parking", "ready"}:
                print("手動每日更新正在安全切換；請等待目前切換完成。",file=sys.stderr)
                return 1
            if (state.get("scheduled_daily_priority") or {}).get("phase") in {"waiting_stop", "ready", "running"}:
                print("排程每日更新正在優先切換或執行；請等待完成。",file=sys.stderr)
                return 1
            if state.get("preparation_pending") or state.get("preparation_complete") is False:
                print("完整重抓尚未準備完成；請先完成重抓全部資料。",file=sys.stderr)
                return 1
            if state.get("resume_pending") and not state.get("pause_requested"):
                print("API 已排入續跑；請先安全暫停，勿啟動第二個工作。",file=sys.stderr)
                return 1
            return _main_locked()
    except CapitalProcessLockError:
        print("API 或獨立腳本已有回補執行／準備中；本次不登入、不重排工作。",file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
