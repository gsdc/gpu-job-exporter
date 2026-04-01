#!/usr/bin/env python3
"""
GPU Job Completion Exporter (with CPU Time tracking)
Monitors nvidia-smi and reports completed GPU process counts and CPU time via Prometheus.
"""

from __future__ import annotations

import os
import subprocess
import time
import logging
from dataclasses import dataclass
from datetime import datetime

import psutil
from prometheus_client import start_http_server, Counter, Summary

# --- Configuration (overridable via environment variables) ---
POLL_INTERVAL = float(os.environ.get("GPU_EXPORTER_POLL_INTERVAL", 2))
EXPORTER_PORT = int(os.environ.get("GPU_EXPORTER_PORT", 9101))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# --- Prometheus metrics ---
gpu_job_completed = Counter(
    "gpu_job_completed_total",
    "Total number of completed GPU compute jobs",
    ["gpu_uuid", "process_name"],
)
gpu_job_cpu_time_seconds = Counter(
    "gpu_job_cpu_time_seconds_total",
    "Total CPU time (user+system) consumed by completed GPU jobs, in seconds",
    ["gpu_uuid", "process_name"],
)
gpu_job_duration = Summary(
    "gpu_job_duration_seconds",
    "CPU time distribution of individual completed GPU jobs",
    ["gpu_uuid", "process_name"],
)


# --- State per tracked PID ---
@dataclass
class ProcessEntry:
    gpu_uuid: str
    process_name: str
    baseline_cpu: float   # cpu_times snapshot taken when the PID was first seen
    last_cpu: float       # most recent cpu_times snapshot (updated each poll)


def _read_cpu_time(pid: int) -> float | None:
    """Return user+system CPU seconds for *pid*, or None if unavailable."""
    try:
        p = psutil.Process(pid)
        t = p.cpu_times()
        return t.user + t.system
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None


def query_gpu_processes() -> dict[int, tuple[str, str]] | None:
    """
    Run nvidia-smi and return {pid: (gpu_uuid, process_name)}.
    Returns None on any nvidia-smi failure so the caller can skip the cycle.
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=gpu_uuid,pid,name",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        logger.error("nvidia-smi not found — is the NVIDIA driver installed?")
        return None
    except subprocess.TimeoutExpired:
        logger.error("nvidia-smi timed out.")
        return None
    except OSError as exc:
        logger.error("Failed to run nvidia-smi: %s", exc)
        return None

    if result.returncode != 0:
        logger.error(
            "nvidia-smi exited with code %d: %s",
            result.returncode,
            result.stderr.strip(),
        )
        return None

    processes: dict[int, tuple[str, str]] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3:
            logger.warning("Unexpected nvidia-smi output line: %r", line)
            continue
        gpu_uuid, pid_str, process_name = parts
        try:
            pid = int(pid_str)
        except ValueError:
            logger.warning("Non-integer PID in nvidia-smi output: %r", pid_str)
            continue
        processes[pid] = (gpu_uuid, process_name)

    return processes


def update_tracked(
    tracked: dict[int, ProcessEntry],
    current_pids: dict[int, tuple[str, str]],
) -> None:
    """
    For each PID currently visible on GPU:
    - Register new arrivals (record baseline CPU time).
    - Refresh last_cpu for existing entries.
    """
    for pid, (gpu_uuid, process_name) in current_pids.items():
        if pid not in tracked:
            baseline = _read_cpu_time(pid) or 0.0
            tracked[pid] = ProcessEntry(
                gpu_uuid=gpu_uuid,
                process_name=process_name,
                baseline_cpu=baseline,
                last_cpu=baseline,
            )
        else:
            fresh = _read_cpu_time(pid)
            if fresh is not None:
                tracked[pid].last_cpu = fresh


def process_finished(
    tracked: dict[int, ProcessEntry],
    current_pids: dict[int, tuple[str, str]],
) -> None:
    """
    Detect PIDs that disappeared, record metrics, log, then remove from tracked.
    """
    finished_pids = set(tracked) - set(current_pids)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for pid in finished_pids:
        entry = tracked.pop(pid)

        # Try one last read; fall back to last known value on failure.
        final_cpu = _read_cpu_time(pid)
        if final_cpu is None:
            final_cpu = entry.last_cpu

        cpu_used = max(0.0, final_cpu - entry.baseline_cpu)
        labels = {"gpu_uuid": entry.gpu_uuid, "process_name": entry.process_name}

        gpu_job_completed.labels(**labels).inc()
        gpu_job_cpu_time_seconds.labels(**labels).inc(cpu_used)
        gpu_job_duration.labels(**labels).observe(cpu_used)

        logger.info(
            "[%s] [Finished] PID: %d (%s) | GPU: %s | Used CPU Time: %.2fs",
            ts,
            pid,
            entry.process_name,
            entry.gpu_uuid,
            cpu_used,
        )


def main() -> None:
    start_http_server(EXPORTER_PORT)
    logger.info(
        "GPU Job Exporter started — metrics at http://0.0.0.0:%d/metrics",
        EXPORTER_PORT,
    )

    # pid -> ProcessEntry for every PID currently on a GPU
    tracked: dict[int, ProcessEntry] = {}
    first_cycle = True

    while True:
        current_pids = query_gpu_processes()

        if current_pids is None:
            # nvidia-smi failed: hold state, skip comparisons this cycle.
            time.sleep(POLL_INTERVAL)
            continue

        if not first_cycle:
            process_finished(tracked, current_pids)

        update_tracked(tracked, current_pids)
        first_cycle = False
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
