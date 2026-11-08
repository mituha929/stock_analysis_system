"""Exercise the existing history-session idle timer without DLL/network access.

The production session loop is used unchanged. COM modules, the request queue,
clock and vendor request are replaced before the loop starts. No credentials,
database, production control file, or running API are accessed.
"""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from datetime import date
import json
import queue
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services import capital_history_service as history


def scenario(request_seconds: float, idle_seconds: float = 30.0) -> dict:
    clock = SimpleNamespace(now=0.0, finished=None, closed=None)
    request = history._HistoryRequest("OFFLINE", "day", date(2026, 1, 1), date(2026, 1, 1))

    class FakeQueue:
        delivered = False

        def get(self, timeout: float):
            if not self.delivered:
                self.delivered = True
                return request
            clock.now += timeout
            raise queue.Empty

        def get_nowait(self):
            raise queue.Empty

    class FakeCenter:
        def SKCenterLib_SetAuthority(self, _authority):
            return 0

        def SKCenterLib_Login(self, _user, _password):
            return 0

    class FakeQuote:
        def SKQuoteLib_LeaveMonitor(self):
            clock.closed = clock.now
            return 0

    fake_com = ModuleType("comtypes")
    fake_com.__path__ = []
    fake_client = ModuleType("comtypes.client")
    fake_com.client = fake_client
    fake_pythoncom = ModuleType("pythoncom")
    fake_pythoncom.CoInitialize = lambda: None
    fake_pythoncom.CoUninitialize = lambda: None
    fake_pythoncom.PumpWaitingMessages = lambda: None
    center, quote, reply = FakeCenter(), FakeQuote(), object()
    objects = {"center": center, "quote": quote, "reply": reply}
    fake_client.CreateObject = lambda kind, interface: objects[kind]
    fake_client.GetModule = lambda _path: None
    fake_client.GetEvents = lambda _source, _sink: object()
    fake_sk = SimpleNamespace(SKCenterLib="center", ISKCenterLib=None,
                             SKQuoteLib="quote", ISKQuoteLib=None,
                             SKReplyLib="reply", ISKReplyLib=None)

    def fake_request(*_args):
        clock.now += request_seconds
        clock.finished = clock.now
        return []

    session = history._ReusableHistoryComSession(
        Path(__file__).resolve(), ("offline-user", "offline-placeholder", 0), 60,
        SimpleNamespace(acquire=lambda: None, release=lambda: None),
        idle_timeout_seconds=idle_seconds,
    )
    session.requests = FakeQueue()
    with ExitStack() as stack:
        stack.enter_context(patch.dict(sys.modules, {
            "comtypes": fake_com, "comtypes.client": fake_client,
            "pythoncom": fake_pythoncom,
        }))
        stack.enter_context(patch.object(history.importlib, "import_module", return_value=fake_sk))
        stack.enter_context(patch.object(history.time, "monotonic", side_effect=lambda: clock.now))
        stack.enter_context(patch.object(history, "ensure_dll_directory_on_path", return_value=None))
        stack.enter_context(patch.object(history, "enter_history_monitor", return_value=None))
        stack.enter_context(patch.object(history, "request_history_candles", side_effect=fake_request))
        stack.enter_context(patch.object(history.gc, "collect", return_value=0))
        session._run()
    if session.startup_error is not None:
        raise RuntimeError("Offline session failed") from session.startup_error
    if not request.done.is_set() or clock.finished is None or clock.closed is None:
        raise RuntimeError("Offline scenario did not finish")
    return {
        "request_seconds": request_seconds,
        "idle_timeout_seconds": idle_seconds,
        "request_finished_at": round(clock.finished, 3),
        "session_closed_at": round(clock.closed, 3),
        "actual_idle_after_request_seconds": round(clock.closed - clock.finished, 3),
        "closed_before_full_idle_timeout": clock.closed - clock.finished < idle_seconds - 0.1,
    }


def main() -> int:
    results = [scenario(duration) for duration in (0.0, 5.0, 31.0)]
    # The long request may include vendor connection recovery. A session should
    # still receive a full idle interval AFTER that work finishes.
    reproduced = results[-1]["actual_idle_after_request_seconds"] < 0.1
    print(json.dumps({
        "real_dll_used": False, "network_used": False,
        "production_files_modified": False, "scenarios": results,
        "idle_timer_includes_active_request_reproduced": reproduced,
        "scope": "Proves premature idle expiry; does not measure native allocation or prove a DLL leak.",
    }, ensure_ascii=False, indent=2))
    return 0 if reproduced else 1


if __name__ == "__main__":
    raise SystemExit(main())
