"""One explicit local API connect/disconnect test using .env credentials.

Credentials are read inside this process, sent only in a local HTTP JSON body,
and redacted from output. No history requests or MySQL writes are performed.
Backfill is first paused to prevent automatic recovery from contaminating the
measurement and remains paused afterwards. Never retries a timed-out POST.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request

from dotenv import dotenv_values
from measure_native_worker_recycling import windows_api, memory, system_memory, probe_existing_dll_lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--observe-seconds", type=int, default=35)
    args = parser.parse_args()
    if not 15 <= args.observe_seconds <= 60:
        parser.error("Observation must be 15-60 seconds")
    env = dotenv_values(args.env_file)
    user_id, password = env.get("CAPITAL_USER_ID"), env.get("CAPITAL_PASSWORD")
    if not user_id or not password:
        raise SystemExit("CAPITAL_USER_ID or CAPITAL_PASSWORD is missing; no request sent")
    authority = int(env.get("CAPITAL_AUTHORITY") or "0")
    secrets = (user_id, password)

    def redact(value):
        if isinstance(value, dict):
            return {key: ("[REDACTED]" if key.lower() in {"user_id", "password", "input"}
                          else redact(item)) for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, str):
            for secret in secrets:
                value = value.replace(secret, "[REDACTED]")
        return value

    def emit(event, **values):
        now = datetime.now(timezone(timedelta(hours=8))).isoformat()
        print(json.dumps(redact({"event": event, "at": now, **values}), ensure_ascii=False), flush=True)

    kernel, psapi = windows_api()
    handle = kernel.OpenProcess(0x410, False, args.pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    base = "http://127.0.0.1:8000"
    samples, sampling_errors = [], []
    stop = threading.Event()
    login_attempted = False
    disconnect_attempted = False
    disconnect_succeeded = False
    sampler = None

    def alive():
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        if code.value != 259:
            raise RuntimeError("Measured API process exited; no more API requests will be sent")

    def http(path, payload=None, timeout=6):
        alive()
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(base + path, data=data,
            headers={"Content-Type": "application/json"}, method="POST" if data is not None else "GET")
        begin = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as error:
            raw, status = error.read(), error.code
        try:
            result = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            result = {"body": raw[:1000].decode("utf-8", errors="replace")}
        return {"http_status": status, "seconds": round(time.monotonic() - begin, 3), "response": result}

    def health():
        result = http("/api/v1/health")
        if result["http_status"] != 200:
            raise RuntimeError("Health request failed")
        wanted = ("capital_status", "capital_message", "quote_ready", "quote_connection_state",
                  "process_memory_private_mb", "process_memory_working_set_mb", "memory_protection_status")
        return {key: result["response"].get(key) for key in wanted}

    def snapshot(stage):
        alive()
        point = {"stage": stage, "memory": memory(kernel, psapi, handle), "health": health(),
                 "dll_lock": probe_existing_dll_lock(Path(__file__).resolve().parents[1] / "data" / "capital_skcom.lock"),
                 "system": system_memory(kernel)}
        emit("measurement", **point)
        return point

    def sample():
        while not stop.wait(0.05):
            try:
                alive()
                samples.append((time.monotonic(), memory(kernel, psapi, handle)))
            except Exception as error:
                sampling_errors.append(type(error).__name__)
                break

    def disconnect():
        nonlocal disconnect_attempted, disconnect_succeeded
        disconnect_attempted = True
        # A timeout leaves the server-side synchronous call potentially active;
        # do not resend the POST or terminate the API to obtain a nicer result.
        result = http("/api/v1/capital/disconnect", {}, timeout=25)
        disconnect_succeeded = result["http_status"] == 200
        emit("disconnect_response", **result)
        return result

    try:
        initial = snapshot("before_connect")
        if initial["health"]["quote_ready"]:
            raise RuntimeError("API is already connected; refusing to alter another active login")
        control = http("/api/v1/backfill/control")["response"]
        if control.get("worker_active"):
            raise RuntimeError("Backfill is active; no connect request sent")
        if not control.get("pause_requested"):
            pause = http("/api/v1/backfill/pause", {})
            if pause["http_status"] != 200:
                raise RuntimeError("Unable to pause automatic backfill before login")
            emit("backfill_paused", pause_requested=True)
        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        time.sleep(2)
        login_attempted = True
        connect = http("/api/v1/capital/connect", {
            "user_id": user_id, "password": password, "authority": authority,
        }, timeout=80)
        emit("connect_response", **connect)
        ready = False
        if connect["http_status"] == 200:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if sampling_errors:
                    raise RuntimeError("Memory sampling failed")
                point = health()
                if point["quote_ready"] and point["quote_connection_state"] == "connected":
                    ready = True
                    break
                time.sleep(1)
        connected = snapshot("after_connect_ready" if ready else "connect_not_ready")
        if ready:
            time.sleep(5)
            connected = snapshot("connected_after_5_seconds")
        disconnect_result = disconnect()
        post_points = [snapshot("after_disconnect_immediate")]
        begin = time.monotonic()
        for seconds in sorted({5, 15, args.observe_seconds}):
            remaining = seconds - (time.monotonic() - begin)
            if remaining > 0:
                time.sleep(remaining)
            post_points.append(snapshot(f"after_disconnect_{seconds}_seconds"))
        final = post_points[-1]
        final_control = http("/api/v1/backfill/control")["response"]
        peak_private = max((p["private_mib"] for _, p in samples), default=initial["memory"]["private_mib"])
        lock_released = final["dll_lock"].get("available") is True
        connection_closed = final["health"]["quote_ready"] is False and final["health"]["quote_connection_state"] == "disconnected"
        passed = ready and disconnect_succeeded and connection_closed and lock_released and not sampling_errors
        emit("summary", passed=passed, api_pid=args.pid,
             connect_ready=ready, connect_http_status=connect["http_status"],
             disconnect_http_status=disconnect_result["http_status"],
             disconnect_connection_closed=connection_closed, dll_lock_released=lock_released,
             before_connect=initial["memory"], connected=connected["memory"], after_disconnect=final["memory"],
             observed_peak_private_mib=round(peak_private, 3),
             connect_private_delta_mib=round(connected["memory"]["private_mib"]-initial["memory"]["private_mib"], 3),
             disconnect_private_delta_mib=round(final["memory"]["private_mib"]-connected["memory"]["private_mib"], 3),
             sample_count=len(samples), sampling_errors=sampling_errors,
             backfill_pause_requested=final_control.get("pause_requested"),
             backfill_worker_active=final_control.get("worker_active"),
             scope="One real connect and one real disconnect; no history requests, database writes, process restart or memory trimming; backfill left paused")
        return 0 if passed else 1
    except Exception as error:
        emit("test_error", error_type=type(error).__name__, message=str(error),
             login_attempted=login_attempted, disconnect_attempted=disconnect_attempted)
        if login_attempted and not disconnect_attempted:
            try:
                disconnect()
            except Exception as cleanup_error:
                emit("disconnect_cleanup_error", error_type=type(cleanup_error).__name__, message=str(cleanup_error))
        try:
            snapshot("after_test_error")
        except Exception as status_error:
            emit("status_check_error", error_type=type(status_error).__name__, message=str(status_error))
        return 1
    finally:
        stop.set()
        if sampler is not None:
            sampler.join(timeout=2)
        kernel.CloseHandle(handle)


if __name__ == "__main__":
    raise SystemExit(main())
