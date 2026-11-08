"""Direct test for SKQuoteLib_RequestKLineAMByDate.

Before running, set CAPITAL_USER_ID and CAPITAL_PASSWORD in the current
PowerShell session. This script uses the same machine-level DLL lock as the
API and backfill script, so it waits instead of connecting concurrently.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import struct
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


KLINE_TYPES = {"1m": (0, 1), "5m": (0, 5), "15m": (0, 15), "60m": (0, 60), "day": (4, 0), "week": (5, 0), "month": (6, 0)}


def date_argument(value: str) -> str:
    for pattern in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, pattern).strftime("%Y%m%d")
        except ValueError:
            pass
    raise argparse.ArgumentTypeError("日期必須是 YYYYMMDD 或 YYYY-MM-DD")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="直接測試群益歷史 K 線 callback")
    result.add_argument("--symbol", required=True, help="商品代碼，例如 2330")
    result.add_argument("--interval", choices=KLINE_TYPES, default="day", help="K 線週期")
    result.add_argument("--start-date", required=True, type=date_argument)
    result.add_argument("--end-date", required=True, type=date_argument)
    result.add_argument("--authority", type=int, default=0, choices=range(4), help="0=正式、1=正式SGX、2=測試、3=測試SGX")
    result.add_argument("--am-session", action="store_true", help="只查 AM 盤；預設全盤")
    result.add_argument("--timeout", type=int, default=30, help="等待連線及資料完成的秒數")
    result.add_argument("--output", type=Path, help="輸出 JSON 路徑；預設為目前資料夾")
    return result


def return_message(center: Any, code: int) -> str:
    try:
        return str(center.SKCenterLib_GetReturnCodeMessage(code))
    except Exception:
        return "未知錯誤"


def ensure_success(center: Any, code: int, method: str) -> None:
    if int(code) != 0:
        raise RuntimeError(f"{method} 失敗：{code}（{return_message(center, int(code))}）")


def main() -> int:
    args = parser().parse_args()
    user_id, password = os.getenv("CAPITAL_USER_ID"), os.getenv("CAPITAL_PASSWORD")
    if not user_id or not password:
        print("請先設定 CAPITAL_USER_ID 與 CAPITAL_PASSWORD 環境變數。", file=sys.stderr)
        return 2
    if args.start_date > args.end_date:
        print("start-date 不可晚於 end-date。", file=sys.stderr)
        return 2

    import comtypes
    import comtypes.client
    import pythoncom

    project_root = Path(__file__).resolve().parents[2]
    capital_api_root = project_root / "capital_api"
    if str(capital_api_root) not in sys.path:
        sys.path.insert(0, str(capital_api_root))
    from app.services.capital_process_lock import get_capital_process_lock

    default_dll = project_root / "CapitalAPI_2.13.58_PythonExample" / "元件" / ("x64" if struct.calcsize("P") * 8 == 64 else "x86") / "SKCOM.dll"
    dll_path = Path(os.getenv("CAPITAL_SKCOM_DLL", default_dll))
    if not dll_path.is_file():
        print(f"找不到 SKCOM.dll：{dll_path}", file=sys.stderr)
        return 2

    # 依照群益 Python console 範例，pywin32 的訊息迴圈負責派送 COM event。
    pythoncom.CoInitialize()
    dll_handle = None
    reply_handler = quote_handler = None
    quote: Any | None = None
    process_lock_acquired = False
    try:
        get_capital_process_lock().acquire()
        process_lock_acquired = True
        if hasattr(os, "add_dll_directory"):
            dll_handle = os.add_dll_directory(str(dll_path.parent))
        from app.services.capital_dll_environment import ensure_dll_directory_on_path
        ensure_dll_directory_on_path(dll_path.parent)
        # 比照群益官方 Quote.py：先產生並載入 comtypes.gen.SKCOMLib。
        # 不使用 gen_dir=None，否則某些環境雖能呼叫方法，卻收不到事件。
        comtypes.client.GetModule(str(dll_path))
        sk = importlib.import_module("comtypes.gen.SKCOMLib")
        center = comtypes.client.CreateObject(sk.SKCenterLib, interface=sk.ISKCenterLib)
        quote = comtypes.client.CreateObject(sk.SKQuoteLib, interface=sk.ISKQuoteLib)
        reply = comtypes.client.CreateObject(sk.SKReplyLib, interface=sk.ISKReplyLib)

        class ReplyEvent:
            def OnReplyMessage(self, _user: str, message: str) -> int:
                print(f"[公告] {message}")
                return -1

        class QuoteEvent:
            def __init__(self) -> None:
                self.connection_events: list[list[int]] = []
                self.ready = False
                self.completed = False
                self.records: list[dict[str, str]] = []

            def OnConnection(self, kind: int, code: int) -> None:
                kind, code = int(kind), int(code)
                self.connection_events.append([kind, code])
                print(f"[OnConnection] {kind}, {code}")
                if kind == 3003 and code == 0:
                    self.ready = True

            def OnNotifyKLineData(self, stock_no: str, data: str) -> None:
                self.records.append({"symbol": str(stock_no), "data": str(data)})

            def OnKLineComplete(self, end_string: str) -> None:
                print(f"[OnKLineComplete] {end_string}")
                if str(end_string).startswith("##"):
                    self.completed = True

        reply_event, quote_event = ReplyEvent(), QuoteEvent()
        # 事件物件與 handler 都必須保持參考，且公告必須在 Login 前註冊。
        reply_handler = comtypes.client.GetEvents(reply, reply_event)
        quote_handler = comtypes.client.GetEvents(quote, quote_event)

        ensure_success(center, center.SKCenterLib_SetAuthority(args.authority), "SKCenterLib_SetAuthority")
        ensure_success(center, center.SKCenterLib_Login(user_id, password), "SKCenterLib_Login")
        ensure_success(center, quote.SKQuoteLib_EnterMonitorLONG(), "SKQuoteLib_EnterMonitorLONG")
        deadline = time.monotonic() + args.timeout
        while not quote_event.ready and time.monotonic() < deadline:
            pythoncom.PumpWaitingMessages()
            time.sleep(0.05)
        if not quote_event.ready:
            try:
                connection_state = int(quote.SKQuoteLib_IsConnected())
                connection_count, out_limit, status_code = quote.SKQuoteLib_GetQuoteStatus(0, False)
                diagnostic = f"IsConnected={connection_state}、GetQuoteStatus=({connection_count}, {out_limit}, {status_code})"
            except Exception as error:
                diagnostic = f"無法讀取報價連線診斷：{error}"
                connection_state = 0
            raise RuntimeError(
                "等待 OnConnection(3003, 0) 逾時；"
                f"收到事件：{quote_event.connection_events}；{diagnostic}。"
                "IsConnected=2 代表商品資料仍在下載，尚不可請求歷史 K 線。"
            )

        kline_type, minute_number = KLINE_TYPES[args.interval]
        ensure_success(
            center,
            quote.SKQuoteLib_RequestKLineAMByDate(
                args.symbol.upper(), kline_type, 1, 1 if args.am_session else 0,
                args.start_date, args.end_date, minute_number,
            ),
            "SKQuoteLib_RequestKLineAMByDate",
        )
        deadline = time.monotonic() + args.timeout
        while not quote_event.completed and time.monotonic() < deadline:
            pythoncom.PumpWaitingMessages()
            time.sleep(0.05)
        if not quote_event.completed:
            raise RuntimeError("等待 OnKLineComplete（##）逾時")

        output = args.output or Path.cwd() / f"kline_{args.symbol}_{args.interval}_{args.start_date}_{args.end_date}.json"
        output.write_text(json.dumps({"symbol": args.symbol.upper(), "interval": args.interval, "start_date": args.start_date, "end_date": args.end_date, "records": quote_event.records}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"成功接收 {len(quote_event.records)} 筆 K 線；已輸出：{output.resolve()}")
        return 0
    except Exception as error:
        print(f"測試失敗：{error}", file=sys.stderr)
        return 1
    finally:
        try:
            if quote is not None:
                quote.SKQuoteLib_LeaveMonitor()
        except Exception:
            pass
        del reply_handler, quote_handler
        if dll_handle is not None:
            dll_handle.close()
        pythoncom.CoUninitialize()
        if process_lock_acquired:
            get_capital_process_lock().release()


if __name__ == "__main__":
    raise SystemExit(main())
