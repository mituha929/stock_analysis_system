"""One explicit, non-MySQL history GET with external Windows memory sampling.

Uses the existing API login. Does not change settings, log in, resume jobs,
restart the server, force GC, or write reports. The GET does replace the
API's in-memory history cache for the chosen symbol/interval.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from collections import Counter
from datetime import date, datetime
import json
import os
import threading
import time
import urllib.parse
import urllib.request


class Counters(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in (
            "peak_working", "working", "peak_paged", "paged", "peak_nonpaged",
            "nonpaged", "pagefile", "peak_pagefile", "private",
        )
    ]


def get_json(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")), len(raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--symbol", default="2330")
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Windows process memory counters are required")
    if args.start > args.end:
        parser.error("start must not be later than end")
    base = args.base_url.rstrip("/")
    if urllib.parse.urlsplit(base).hostname not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("Only a local API is allowed")

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD)
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x410, False, args.pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    samples, errors = [], []
    stop = threading.Event()

    def read_memory():
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return (time.perf_counter(), counters.private / 1048576, counters.working / 1048576)

    def sample():
        while not stop.is_set():
            try:
                samples.append(read_memory())
            except OSError as error:
                errors.append(str(error))
                return
            stop.wait(0.02)

    sampler = None
    try:
        health, _ = get_json(base + "/api/v1/health")
        control, _ = get_json(base + "/api/v1/backfill/control")
        if not health.get("quote_ready"):
            raise RuntimeError("Group quote connection is not ready; no history request sent")
        if control.get("worker_active") or control.get("state") not in {"paused", "completed", "idle", "error", "limited"}:
            raise RuntimeError("Backfill is active or queued; no history request sent")
        print(json.dumps({"stage": "baseline", "api_pid": args.pid,
                          "private_mib": read_memory()[1], "backfill_state": control.get("state")}), flush=True)
        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        time.sleep(5)
        # Recheck immediately before the only history query. Never pause someone
        # else's work automatically to make room for this measurement.
        control, _ = get_json(base + "/api/v1/backfill/control")
        if control.get("worker_active") or control.get("state") not in {"paused", "completed", "idle", "error", "limited"}:
            raise RuntimeError("Backfill changed state during baseline; no history request sent")
        query = urllib.parse.urlencode({"interval": args.interval, "start_date": args.start.isoformat(),
                                       "end_date": args.end.isoformat()})
        url = base + "/api/v1/history/candles/" + urllib.parse.quote(args.symbol, safe="") + "?" + query
        begin = time.perf_counter()
        before = read_memory()
        payload, response_bytes = get_json(url, timeout=180)
        finished = time.perf_counter()
        after = read_memory()
        data = payload["data"]
        timestamps = [row["timestamp"] for row in data]
        print(json.dumps({"stage": "query_finished", "bars": len(data),
                          "elapsed_seconds": round(finished - begin, 3), "observe_seconds": 35}), flush=True)
        time.sleep(35)
        settled = read_memory()
        stop.set()
        sampler.join(2)
        baseline = [s for s in samples if s[0] < begin]
        request_samples = [s for s in samples if begin <= s[0] <= finished] + [before, after]
        post_samples = [s for s in samples if s[0] >= begin] + [settled]
        def memory_pair(point):
            return dict(private_mib=round(point[1], 3), working_set_mib=round(point[2], 3))
        def peak_pair(points):
            return dict(private_mib=round(max(s[1] for s in points), 3),
                        working_set_mib=round(max(s[2] for s in points), 3))
        result = dict(
            measured_at=datetime.now().astimezone().isoformat(), api_pid=args.pid,
            url=url, requested_interval=args.interval, returned_interval=payload.get("interval"),
            bars=len(data), at_least_1200=len(data) >= 1200,
            unique_timestamps=len(set(timestamps)), duplicate_timestamps=len(timestamps) - len(set(timestamps)),
            first=min(timestamps) if timestamps else None, last=max(timestamps) if timestamps else None,
            per_day=dict(sorted(Counter(t[:10] for t in timestamps).items())),
            response_bytes=response_bytes, http_elapsed_seconds=round(finished - begin, 3),
            baseline_range=dict(private_min_mib=round(min(s[1] for s in baseline), 3),
                                private_max_mib=round(max(s[1] for s in baseline), 3)),
            before=memory_pair(before), request_peak=peak_pair(request_samples),
            after_response=memory_pair(after), after_35_seconds=memory_pair(settled),
            observed_peak_including_post=peak_pair(post_samples),
            request_peak_private_delta_mib=round(max(s[1] for s in request_samples) - before[1], 3),
            settled_private_delta_mib=round(settled[1] - before[1], 3),
            samples=len(samples), sampling_period_seconds=0.02, sampling_errors=errors,
            scope="One HTTP GET; no MySQL write; includes COM startup/cache/serialization and other API activity; sampled peak is a lower bound, not per-candle heap size",
        )
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 0 if data else 1
    finally:
        stop.set()
        if sampler is not None:
            sampler.join(2)
        kernel.CloseHandle(handle)


if __name__ == "__main__":
    raise SystemExit(main())
