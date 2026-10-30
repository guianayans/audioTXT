"""Leitura de métricas de CPU e GPU (NVIDIA via NVML)."""
from __future__ import annotations

import os
import time
import psutil

DATA_DIR = os.environ.get("AUDIOTXT_DATA_DIR", "/data")

try:
    import pynvml  # fornecido por nvidia-ml-py
    pynvml.nvmlInit()
    _NVML = True
except Exception:  # pragma: no cover - sem GPU/NVML
    _NVML = False

# Primeira chamada de cpu_percent apenas inicializa o contador.
psutil.cpu_percent(interval=None)


def gpu_available() -> bool:
    if not _NVML:
        return False
    try:
        return pynvml.nvmlDeviceGetCount() > 0
    except Exception:
        return False


def _bytes_to_mb(value: int) -> float:
    return round(value / (1024 * 1024), 1)


def read_gpus() -> list[dict]:
    if not _NVML:
        return []
    gpus = []
    try:
        count = pynvml.nvmlDeviceGetCount()
    except Exception:
        return []
    for i in range(count):
        try:
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(h)
            if isinstance(name, bytes):
                name = name.decode("utf-8", "replace")
            mem = pynvml.nvmlDeviceGetMemoryInfo(h)
            util = pynvml.nvmlDeviceGetUtilizationRates(h)
            try:
                temp = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
            except Exception:
                temp = None
            try:
                power = round(pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0, 1)
            except Exception:
                power = None
            try:
                power_limit = round(
                    pynvml.nvmlDeviceGetEnforcedPowerLimit(h) / 1000.0, 1
                )
            except Exception:
                power_limit = None
            gpus.append(
                {
                    "index": i,
                    "name": name,
                    "util": util.gpu,
                    "mem_util": util.memory,
                    "mem_used_mb": _bytes_to_mb(mem.used),
                    "mem_total_mb": _bytes_to_mb(mem.total),
                    "mem_percent": round(mem.used / mem.total * 100, 1)
                    if mem.total
                    else 0,
                    "temp": temp,
                    "power": power,
                    "power_limit": power_limit,
                }
            )
        except Exception:
            continue
    return gpus


def read_disk() -> dict:
    path = DATA_DIR if os.path.isdir(DATA_DIR) else "/"
    try:
        du = psutil.disk_usage(path)
        return {
            "used_gb": round(du.used / (1024 ** 3), 1),
            "total_gb": round(du.total / (1024 ** 3), 1),
            "percent": du.percent,
        }
    except Exception:
        return {"used_gb": 0, "total_gb": 0, "percent": 0}


def read_metrics() -> dict:
    vm = psutil.virtual_memory()
    return {
        "ts": time.time(),
        "disk": read_disk(),
        "cpu": {
            "percent": psutil.cpu_percent(interval=None),
            "per_core": psutil.cpu_percent(interval=None, percpu=True),
            "cores": psutil.cpu_count(logical=True),
            "freq_mhz": round(psutil.cpu_freq().current, 0)
            if psutil.cpu_freq()
            else None,
            "load_avg": [round(x, 2) for x in psutil.getloadavg()]
            if hasattr(psutil, "getloadavg")
            else None,
        },
        "mem": {
            "percent": vm.percent,
            "used_mb": _bytes_to_mb(vm.used),
            "total_mb": _bytes_to_mb(vm.total),
        },
        "gpus": read_gpus(),
    }
