"""Measure real Windows native-memory reclamation across worker exits.

This is an isolated process-boundary prototype, NOT a Capital DLL benchmark.
Each child commits and touches a bounded VirtualAlloc region, deliberately
retains it until a normal process exit, and is observed by the parent. No DLL
login, database write, production restart, or working-set trimming is done.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from measure_history_query_memory import Counters, get_json

MIB = 1024 * 1024


class GlobalMemory(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
        (name, ctypes.c_ulonglong) for name in (
            "total_phys", "avail_phys", "total_commit", "avail_commit",
            "total_virtual", "avail_virtual", "avail_extended_virtual",
        )
    ]


def windows_api():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.GlobalMemoryStatusEx.argtypes = (ctypes.POINTER(GlobalMemory),)
    kernel.GlobalMemoryStatusEx.restype = wintypes.BOOL
    kernel.VirtualAlloc.argtypes = (ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD)
    kernel.VirtualAlloc.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD)
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    return kernel, psapi


def memory(kernel, psapi, handle=None):
    point = Counters()
    point.cb = ctypes.sizeof(point)
    if not psapi.GetProcessMemoryInfo(handle or kernel.GetCurrentProcess(), ctypes.byref(point), point.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return {"private_mib": round(point.private / MIB, 3),
            "working_set_mib": round(point.working / MIB, 3)}


def system_memory(kernel):
    point = GlobalMemory()
    point.length = ctypes.sizeof(point)
    if not kernel.GlobalMemoryStatusEx(ctypes.byref(point)):
        raise ctypes.WinError(ctypes.get_last_error())
    return {"available_ram_mib": round(point.avail_phys / MIB, 3),
            "commit_used_mib": round((point.total_commit - point.avail_commit) / MIB, 3),
            "available_commit_mib": round(point.avail_commit / MIB, 3)}


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def child_main(payload_mib):
    kernel, psapi = windows_api()
    emit({"event": "child_baseline", "pid": os.getpid(), "memory": memory(kernel, psapi)})
    size = payload_mib * MIB
    address = kernel.VirtualAlloc(None, size, 0x1000 | 0x2000, 0x04)
    if not address:
        raise ctypes.WinError(ctypes.get_last_error())
    ctypes.memset(address, 0x3D, size)  # Commit physical pages, not just reserve addresses.
    emit({"event": "child_allocated", "pid": os.getpid(), "memory": memory(kernel, psapi),
          "native_payload_mib": payload_mib})
    if sys.stdin.readline().strip() != "stop":
        return 2
    # Intentionally no VirtualFree: the buffer is still owned immediately before
    # a NORMAL exit, reproducing the boundary needed for retained native memory.
    emit({"event": "child_exiting", "pid": os.getpid(), "memory": memory(kernel, psapi)})
    return 0


def read_message(messages, timeout=15):
    try:
        line = messages.get(timeout=timeout)
    except queue.Empty as error:
        raise RuntimeError("Test child did not respond within 15 seconds") from error
    if line is None:
        raise RuntimeError("Test child exited before completing the protocol")
    return json.loads(line)


def live_api_snapshot(base):
    health, _ = get_json(base + "/api/v1/health", timeout=8)
    control, _ = get_json(base + "/api/v1/backfill/control", timeout=8)
    return {
        "quote_ready": health.get("quote_ready"),
        "private_mib": health.get("process_memory_private_mb"),
        "backfill_state": control.get("state"), "backfill_worker_active": control.get("worker_active"),
        "backfill_pause_kind": control.get("pause_kind"), "api_pid_from_control": control.get("worker_pid"),
    }


def probe_existing_dll_lock(path):
    """Briefly test availability; never create, overwrite or break the lock."""
    import msvcrt
    if not path.is_file():
        return {"checked": False, "reason": "lock file does not exist"}
    with path.open("r+b") as handle:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return {"checked": True, "available": False}
        try:
            return {"checked": True, "available": True}
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def parent_main(args):
    kernel, psapi = windows_api()
    initial_system = system_memory(kernel)
    required_free = max(512, args.payload_mib * 4)
    if min(initial_system["available_ram_mib"], initial_system["available_commit_mib"]) < required_free:
        raise RuntimeError(f"Less than {required_free} MiB available; no child was started")
    base = args.api_base_url.rstrip("/") if args.api_base_url else None
    if base and base not in {"http://127.0.0.1:8000", "http://localhost:8000"}:
        raise ValueError("Only the local production API may be passively observed")
    api_before = live_api_snapshot(base) if base else None
    lock = probe_existing_dll_lock(Path(__file__).resolve().parents[1] / "data" / "capital_skcom.lock")
    parent_before = memory(kernel, psapi)
    rows = []
    emit({"event": "test_baseline", "supervisor_pid": os.getpid(), "supervisor": parent_before,
          "system": initial_system, "production_api": api_before, "dll_lock": lock})
    for number in range(1, args.rounds + 1):
        safety = system_memory(kernel)
        if min(safety["available_ram_mib"], safety["available_commit_mib"]) < required_free:
            raise RuntimeError("Available memory fell below the safety limit; no new child started")
        before = memory(kernel, psapi)
        process = subprocess.Popen(
            [sys.executable, "-B", str(Path(__file__).resolve()), "--child", "--payload-mib", str(args.payload_mib)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW,
        )
        messages = queue.Queue()

        def reader(stream=process.stdout, target=messages):
            for line in stream:
                target.put(line)
            target.put(None)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        handle = None
        try:
            baseline, allocated = read_message(messages), read_message(messages)
            if baseline.get("event") != "child_baseline" or allocated.get("event") != "child_allocated":
                raise RuntimeError("Unexpected test child protocol")
            # A Windows venv python.exe may be a small launcher with a second
            # interpreter process. Measure the PID reported by the interpreter
            # owning VirtualAlloc, rather than accidentally measuring its launcher.
            worker_pid = allocated["pid"]
            if baseline["pid"] != worker_pid:
                raise RuntimeError("The child interpreter PID changed unexpectedly")
            handle = kernel.OpenProcess(0x410, False, worker_pid)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            external_peak = memory(kernel, psapi, handle)
            if abs(external_peak["private_mib"] - allocated["memory"]["private_mib"]) > 4:
                raise RuntimeError("External counters do not match the allocating interpreter")
            parent_during = memory(kernel, psapi)
            system_during = system_memory(kernel)
            time.sleep(0.5)
            process.stdin.write("stop\n")
            process.stdin.flush()
            exiting = read_message(messages)
            if exiting.get("event") != "child_exiting":
                raise RuntimeError("Test child did not acknowledge a normal exit")
            code = process.wait(timeout=10)
            exit_code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                raise ctypes.WinError(ctypes.get_last_error())
            if code != 0 or exit_code.value != 0:
                raise RuntimeError(f"Test child did not exit normally: {code}")
            after = memory(kernel, psapi)
            retained_delta = allocated["memory"]["private_mib"] - baseline["memory"]["private_mib"]
            result = {
                "round": number, "child_pid": worker_pid, "launcher_pid": process.pid,
                "native_payload_mib": args.payload_mib,
                "child_before": baseline["memory"], "child_after_allocation": allocated["memory"],
                "child_external_measurement": external_peak, "child_before_exit": exiting["memory"],
                "child_private_growth_mib": round(retained_delta, 3),
                "child_exit_code": code, "child_exit_verified": True,
                "supervisor_before": before, "supervisor_during": parent_during, "supervisor_after_exit": after,
                "private_footprint_drop_after_exit_mib": round(parent_during["private_mib"] + external_peak["private_mib"] - after["private_mib"], 3),
                "system_during": system_during, "system_after_exit": system_memory(kernel),
            }
            if retained_delta < args.payload_mib * 0.95:
                raise RuntimeError("The child did not commit the expected native payload")
            rows.append(result)
            emit({"event": "round_completed", **result})
        finally:
            # Only the child created above can ever be terminated by this test.
            if process.poll() is None:
                try:
                    process.stdin.write("stop\n")
                    process.stdin.flush()
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    process.kill()
                    process.wait(timeout=5)
            if handle:
                kernel.CloseHandle(handle)
            thread.join(timeout=1)
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()
    api_after = live_api_snapshot(base) if base else None
    parent_after = memory(kernel, psapi)
    supervisor_growth = round(parent_after["private_mib"] - parent_before["private_mib"], 3)
    api_state_preserved = not base or all(api_before[k] == api_after[k] for k in (
        "quote_ready", "backfill_state", "backfill_worker_active", "backfill_pause_kind", "api_pid_from_control",
    ))
    passed = len(rows) == args.rounds and supervisor_growth < 16 and api_state_preserved
    emit({"event": "summary", "passed": passed, "supervisor_pid": os.getpid(),
          "supervisor_before": parent_before, "supervisor_after": parent_after,
          "supervisor_private_growth_mib": supervisor_growth,
          "rounds": len(rows), "native_payload_mib_per_round": args.payload_mib,
          "total_native_payload_allocated_mib": len(rows) * args.payload_mib,
          "production_api_before": api_before, "production_api_after": api_after,
          "production_api_state_preserved": api_state_preserved,
          "production_dll_lock": lock, "final_system": system_memory(kernel),
          "real_windows_native_allocation": True, "real_capital_dll_used": False,
          "production_processes_stopped": False, "database_written": False,
          "limitation": "Proves native-memory isolation and normal-exit reclamation, not Capital login/reconnect, subscriptions, checkpoints, or long-running DLL memory behavior."})
    return 0 if passed else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--payload-mib", type=int, default=128)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--api-base-url")
    args = parser.parse_args()
    if os.name != "nt" or not 32 <= args.payload_mib <= 256 or not 1 <= args.rounds <= 5:
        parser.error("Requires Windows, payload 32-256 MiB, and 1-5 rounds")
    return child_main(args.payload_mib) if args.child else parent_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
