"""Нагрузка этого ПК: CPU, RAM, диск. Без сторонних пакетов."""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

_last_cpu: tuple[float, float] | None = None


def collect(disk_path: str | Path | None = None) -> dict:
    cpu = round(_cpu_percent(), 1)
    ram = _ram()
    disk = _disk(disk_path)
    return {
        "cpu_percent": cpu,
        "cpu_count": os.cpu_count() or 0,
        "ram_percent": ram["percent"],
        "ram_used_gb": ram["used_gb"],
        "ram_total_gb": ram["total_gb"],
        "disk_percent": disk["percent"],
        "disk_used_gb": disk["used_gb"],
        "disk_total_gb": disk["total_gb"],
    }


def _cpu_percent() -> float:
    global _last_cpu
    idle, total = _cpu_times()
    if total <= 0:
        return 0.0
    if _last_cpu is None:
        _last_cpu = (idle, total)
        time.sleep(0.12)
        idle2, total2 = _cpu_times()
        di, dt = idle2 - idle, total2 - total
        _last_cpu = (idle2, total2)
        if dt <= 0:
            return 0.0
        return max(0.0, min(100.0, (1.0 - di / dt) * 100.0))
    di, dt = idle - _last_cpu[0], total - _last_cpu[1]
    _last_cpu = (idle, total)
    if dt <= 0:
        return 0.0
    return max(0.0, min(100.0, (1.0 - di / dt) * 100.0))


def _cpu_times() -> tuple[float, float]:
    if os.name == "nt":
        return _cpu_times_windows()
    return _cpu_times_linux()


def _cpu_times_linux() -> tuple[float, float]:
    try:
        with open("/proc/stat", encoding="utf-8") as fh:
            parts = fh.readline().split()
        nums = [float(x) for x in parts[1:8]]
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        total = sum(nums)
        return idle, total
    except Exception:
        return 0.0, 0.0


def _cpu_times_windows() -> tuple[float, float]:
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    idle = FILETIME()
    kernel = FILETIME()
    user = FILETIME()
    if not ctypes.windll.kernel32.GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
    ):
        return 0.0, 0.0

    def _n(ft: FILETIME) -> int:
        return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

    idle_n, kernel_n, user_n = _n(idle), _n(kernel), _n(user)
    total = float(kernel_n + user_n)
    return float(idle_n), total


def _ram() -> dict:
    if os.name == "nt":
        data = _ram_windows()
        if data:
            return data
    return _ram_linux()


def _ram_linux() -> dict:
    info: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                info[key] = int(rest.strip().split()[0]) * 1024
    except Exception:
        return {"percent": 0.0, "used_gb": 0.0, "total_gb": 0.0}
    total = info.get("MemTotal") or 0
    avail = info.get("MemAvailable") or info.get("MemFree") or 0
    used = max(0, total - avail)
    return _pack_bytes(used, total)


def _ram_windows() -> dict | None:
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        return None
    used = int(stat.ullTotalPhys - stat.ullAvailPhys)
    return _pack_bytes(used, int(stat.ullTotalPhys))


def _disk(path: str | Path | None) -> dict:
    target = Path(path or Path.cwd()).resolve()
    try:
        usage = shutil.disk_usage(target.anchor or str(target))
        return _pack_bytes(usage.used, usage.total)
    except Exception:
        return {"percent": 0.0, "used_gb": 0.0, "total_gb": 0.0}


def _pack_bytes(used: int, total: int) -> dict:
    if total <= 0:
        return {"percent": 0.0, "used_gb": 0.0, "total_gb": 0.0}
    return {
        "percent": round(used / total * 100.0, 1),
        "used_gb": round(used / (1024 ** 3), 1),
        "total_gb": round(total / (1024 ** 3), 1),
    }
