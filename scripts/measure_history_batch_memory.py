"""Sequential history GETs using the running API; never writes MySQL.

Does not change production code/configuration, log in, pause/resume, force GC,
or close the API's session. History GETs populate its bounded cache. Short
inter-request gaps permit the existing reusable history session to stay alive;
its identity/internal reconnect count is not exposed by the public endpoint.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import date, datetime
import json
import os
import statistics
import threading
import time
import urllib.parse

from measure_history_query_memory import Counters, get_json


def require_idle(control):
    if control.get("worker_active") or control.get("state") not in {
        "paused", "completed", "idle", "error", "limited",
    }:
        raise RuntimeError("Backfill is active/queued; no further history query will be sent")


def summarize_payload(payload, symbol, interval):
    if payload.get("symbol") != symbol or payload.get("interval") != interval:
        raise ValueError("Unexpected symbol/interval in response")
    timestamps = [row["timestamp"] for row in payload["data"]]
    if len(timestamps) != len(set(timestamps)):
        raise ValueError("Duplicate K-line timestamps")
    return dict(bars=len(timestamps), first=min(timestamps) if timestamps else None,
                last=max(timestamps) if timestamps else None)


def memory_pair(point):
    return dict(private_mib=round(point[1], 3), working_set_mib=round(point[2], 3))


def peak_pair(points):
    return dict(private_mib=round(max(s[1] for s in points), 3),
                working_set_mib=round(max(s[2] for s in points), 3))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--symbols", default="2330,2317,2454,2308,2303,2881,2882,2891,1301,2002")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--stop-private-mib", type=float, default=1536)
    parser.add_argument("--max-growth-mib", type=float, default=512)
    parser.add_argument("--observe-seconds", type=int, default=35)
    args = parser.parse_args()
    symbols = list(dict.fromkeys(s.strip().upper() for s in args.symbols.split(",") if s.strip()))
    base = args.base_url.rstrip("/")
    if os.name != "nt" or not symbols or len(symbols) > 20 or not 1 <= args.rounds <= 2:
        parser.error("Windows and 1-20 symbols, 1-2 rounds are required")
    if args.start > args.end or not 0 <= args.observe_seconds <= 330:
        parser.error("Invalid dates/observation duration")
    if urllib.parse.urlsplit(base).hostname not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("Only a local API is allowed")

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD)
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x410, False, args.pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    samples, sampling_errors, results = [], [], []
    stop = threading.Event()

    def read_memory():
        exit_code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        if exit_code.value != 259:
            raise RuntimeError("The measured API process exited; refusing further queries")
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return time.perf_counter(), counters.private / 1048576, counters.working / 1048576

    def sample():
        while not stop.is_set():
            try:
                samples.append(read_memory())
            except Exception as error:
                sampling_errors.append(str(error))
                return
            stop.wait(0.02)

    def emit(value):
        print(json.dumps(value, ensure_ascii=False), flush=True)

    sampler = None
    try:
        health, _ = get_json(base + "/api/v1/health")
        if not health.get("quote_ready"):
            raise RuntimeError("Quote connection is not ready; no history query sent")
        initial_control, _ = get_json(base + "/api/v1/backfill/control")
        require_idle(initial_control)
        initial = read_memory()
        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        emit(dict(event="baseline", api_pid=args.pid, memory=memory_pair(initial),
                  symbols=symbols, rounds=args.rounds, backfill_state=initial_control.get("state")))
        time.sleep(5)
        previous_finish = None
        failure = None
        try:
            for round_number in range(1, args.rounds + 1):
                for symbol in symbols:
                    control, _ = get_json(base + "/api/v1/backfill/control")
                    require_idle(control)
                    before = read_memory()
                    if before[1] >= args.stop_private_mib or before[1] - initial[1] >= args.max_growth_mib:
                        raise RuntimeError("Diagnostic memory safety limit reached; no further query sent")
                    if sampling_errors:
                        raise RuntimeError("Memory sampler failed; no further query sent")
                    query = urllib.parse.urlencode(dict(interval=args.interval,
                        start_date=args.start.isoformat(), end_date=args.end.isoformat()))
                    url = base + "/api/v1/history/candles/" + urllib.parse.quote(symbol, safe="") + "?" + query
                    begin = time.perf_counter()
                    payload, size = get_json(url, timeout=180)
                    finished = time.perf_counter()
                    after = read_memory()
                    summary = summarize_payload(payload, symbol, args.interval)
                    del payload
                    request_samples = [s for s in samples if begin <= s[0] <= finished] + [before, after]
                    result = dict(index=len(results) + 1, round=round_number, symbol=symbol,
                        **summary, response_bytes=size, seconds=round(finished - begin, 3),
                        gap_since_previous_seconds=round(begin - previous_finish, 3) if previous_finish else None,
                        before=memory_pair(before), peak=peak_pair(request_samples), after=memory_pair(after),
                        private_delta_mib=round(after[1] - before[1], 3))
                    results.append(result)
                    emit(dict(event="query", **result))
                    previous_finish = finished
                    if not summary["bars"]:
                        raise RuntimeError("Empty history response; stopping rather than continuing blindly")
        except Exception as error:
            failure = f"{type(error).__name__}: {error}"
            emit(dict(event="stopped", error=failure))
        end_batch = read_memory()
        emit(dict(event="batch_finished", completed_queries=len(results), error=failure,
                  memory=memory_pair(end_batch), idle_observation_seconds=args.observe_seconds))
        # Passive observation only: do not call any DLL endpoint or force GC.
        idle_samples = []
        for second in range(args.observe_seconds):
            time.sleep(1)
            if second + 1 in {1, 5, 15, 35, 310, args.observe_seconds}:
                idle_samples.append(dict(after_seconds=second + 1, **memory_pair(read_memory())))
                emit(dict(event="idle_memory", **idle_samples[-1]))
        settled = read_memory()
        final_control, _ = get_json(base + "/api/v1/backfill/control")
        stop.set()
        sampler.join(2)
        round_summaries = []
        for round_number in range(1, args.rounds + 1):
            rows = [r for r in results if r["round"] == round_number]
            if rows:
                round_summaries.append(dict(round=round_number, queries=len(rows),
                    total_bars=sum(r["bars"] for r in rows),
                    average_seconds=round(statistics.mean(r["seconds"] for r in rows), 3),
                    median_seconds=round(statistics.median(r["seconds"] for r in rows), 3),
                    after_private_delta_mib=round(rows[-1]["after"]["private_mib"] - rows[0]["before"]["private_mib"], 3)))
        later = results[1:]
        emit(dict(event="summary", measured_at=datetime.now().astimezone().isoformat(),
            api_pid=args.pid, interval=args.interval, start=args.start.isoformat(), end=args.end.isoformat(),
            requested_queries=len(symbols)*args.rounds, completed_queries=len(results), error=failure,
            initial=memory_pair(initial), peak=peak_pair(samples + [initial, settled]),
            after_batch=memory_pair(end_batch), settled=memory_pair(settled),
            retained_private_delta_mib=round(settled[1] - initial[1], 3),
            first_request_seconds=results[0]["seconds"] if results else None,
            later_average_seconds=round(statistics.mean(r["seconds"] for r in later), 3) if later else None,
            max_inter_request_gap_seconds=max((r["gap_since_previous_seconds"] or 0 for r in results), default=None),
            round_summaries=round_summaries, samples=len(samples), sampling_errors=sampling_errors,
            final_backfill_state=final_control.get("state"), final_worker_active=final_control.get("worker_active"),
            history_session_identity="not exposed; reuse expected from short gaps, not directly instrumented",
            measurement_scope="API process counters include COM/cache/background activity; no MySQL writes or forced cleanup; sampled peaks may miss brief spikes"))
        return 1 if failure or sampling_errors else 0
    finally:
        stop.set()
        if sampler is not None:
            sampler.join(2)
        kernel.CloseHandle(handle)


if __name__ == "__main__":
    raise SystemExit(main())
