"""Small Windows-native memory monitor without an additional dependency."""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from ctypes import wintypes


@dataclass(frozen=True)
class ProcessMemory:
    working_set_mb: float | None
    private_usage_mb: float | None


class _ProcessMemoryCountersEx(ctypes.Structure):
    # ctypes caches pointer types by structure identity.  This structure must
    # be shared across measurements rather than redefined on every call.
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


def _megabytes(value: int) -> float:
    return round(value / 1024 / 1024, 1)


def current_process_memory() -> ProcessMemory:
    """Return this API process's resident and private memory on Windows."""
    if os.name != "nt":
        return ProcessMemory(None, None)

    counters = _ProcessMemoryCountersEx()
    counters.cb = ctypes.sizeof(counters)
    try:
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = (
            wintypes.HANDLE, ctypes.POINTER(_ProcessMemoryCountersEx), wintypes.DWORD,
        )
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        success = psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), ctypes.sizeof(counters),
        )
        if not success:
            return ProcessMemory(None, None)
    except OSError:
        return ProcessMemory(None, None)
    return ProcessMemory(_megabytes(counters.WorkingSetSize), _megabytes(counters.PrivateUsage))


def process_memory_for_pid(pid: int) -> ProcessMemory:
    """Read an owned worker's private memory; unavailable is never zero."""
    if os.name != "nt":
        return ProcessMemory(None, None)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ProcessMemoryCountersEx), wintypes.DWORD)
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x410, False, pid)
    if not handle:
        return ProcessMemory(None, None)
    try:
        counters = _ProcessMemoryCountersEx()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return ProcessMemory(None, None)
        return ProcessMemory(_megabytes(counters.WorkingSetSize), _megabytes(counters.PrivateUsage))
    finally:
        kernel.CloseHandle(handle)


def memory_limit_mb(name: str, default: int) -> int:
    """Read a positive MiB limit from the environment with a safe fallback."""
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def memory_status(memory: ProcessMemory, warning_mb: int, stop_mb: int) -> str:
    value = memory.private_usage_mb if memory.private_usage_mb is not None else memory.working_set_mb
    if value is None:
        return "unavailable"
    if value >= stop_mb:
        return "backfill_stopped"
    if value >= warning_mb:
        return "warning"
    return "normal"
