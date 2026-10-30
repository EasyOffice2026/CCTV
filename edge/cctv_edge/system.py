"""Device stats for the heartbeat. Jetson exposes GPU load and temperatures in sysfs."""

import shutil
from pathlib import Path

import psutil

from cctv_contracts.v1 import SystemStats

THERMAL_ZONES = Path("/sys/class/thermal")
# Jetson Orin: GPU load in tenths of a percent.
JETSON_GPU_LOAD = Path("/sys/devices/platform/gpu.0/load")


def _read_number(path: Path) -> float | None:
    try:
        return float(path.read_text().strip())
    except (OSError, ValueError):
        return None


def max_temperature_c(root: Path = THERMAL_ZONES) -> float | None:
    readings = [_read_number(p) for p in root.glob("thermal_zone*/temp")]
    values = [r / 1000 for r in readings if r is not None and r > 0]
    return round(max(values), 1) if values else None


def gpu_percent(path: Path = JETSON_GPU_LOAD) -> float | None:
    load = _read_number(path)
    return None if load is None else min(100.0, load / 10)


def collect(data_dir: Path, outbox_pending: int) -> SystemStats:
    return SystemStats(
        cpu_percent=psutil.cpu_percent(interval=None),
        gpu_percent=gpu_percent(),
        temperature_c=max_temperature_c(),
        disk_free_bytes=shutil.disk_usage(data_dir).free,
        outbox_pending=outbox_pending,
    )
