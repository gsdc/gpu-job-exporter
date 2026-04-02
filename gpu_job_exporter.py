#!/usr/bin/env python3
"""
GPU Job Completion Exporter (with CPU Time and GPU Time tracking)
Monitors GPU processes and reports job counts, CPU time, and GPU active time
via Prometheus — both for currently running jobs and for completed jobs.

GPU time tracking mode is determined per-GPU at startup:
  - Accounting mode  : driver records exact GPU active time per process.
                       Queried once at process exit via nvmlDeviceGetAccountingStats.
                       Also readable for live processes (isRunning=1).
                       Requires root to enable; automatically attempted at startup.
  - Polling mode     : SM utilisation samples are integrated over each poll interval.
                       Used as fallback when accounting mode is unavailable.
"""

from __future__ import annotations

import os
import subprocess
import time
import logging
from dataclasses import dataclass
from datetime import datetime

import psutil
from prometheus_client import start_http_server, Counter, Gauge, Summary

try:
    import pynvml
    _NVML_AVAILABLE = True
except ImportError:
    pynvml = None  # type: ignore[assignment]
    _NVML_AVAILABLE = False

# --- Configuration (overridable via environment variables) ---
POLL_INTERVAL = float(os.environ.get("GPU_EXPORTER_POLL_INTERVAL", 2))
EXPORTER_PORT = int(os.environ.get("GPU_EXPORTER_PORT", 9101))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

# -- Running jobs --
gpu_job_running = Gauge(
    "gpu_job_running",
    "Number of GPU jobs currently running",
    ["gpu_uuid", "process_name"],
)
gpu_job_running_cpu_time_seconds = Gauge(
    "gpu_job_running_cpu_time_seconds",
    "CPU time (user+system) consumed so far by each running GPU job, in seconds",
    ["gpu_uuid", "process_name", "pid"],
)
gpu_job_running_gpu_time_seconds = Gauge(
    "gpu_job_running_gpu_time_seconds",
    "GPU active time consumed so far by each running GPU job, in seconds",
    ["gpu_uuid", "process_name", "pid"],
)

# -- Completed jobs --
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
gpu_job_gpu_time_seconds = Counter(
    "gpu_job_gpu_time_seconds_total",
    "Total GPU active time consumed by completed GPU jobs, in seconds",
    ["gpu_uuid", "process_name"],
)
gpu_job_cpu_duration = Summary(
    "gpu_job_cpu_duration_seconds",
    "CPU time distribution of individual completed GPU jobs",
    ["gpu_uuid", "process_name"],
)
gpu_job_gpu_duration = Summary(
    "gpu_job_gpu_duration_seconds",
    "GPU active time distribution of individual completed GPU jobs",
    ["gpu_uuid", "process_name"],
)

# ---------------------------------------------------------------------------
# NVML state
# ---------------------------------------------------------------------------

_nvml_handle_cache: dict[str, object] = {}   # gpu_uuid -> handle
_accounting_enabled: dict[str, bool] = {}    # gpu_uuid -> True/False


def _nvml_init() -> None:
    """
    Initialise NVML, populate handle cache, and determine per-GPU tracking mode.

    For each GPU, accounting mode is attempted first (requires root).
    If activation fails, the current mode is read back; if it was already
    enabled by the user, accounting mode is still used.  Otherwise the GPU
    falls back to polling-based GPU time estimation.
    """
    global _NVML_AVAILABLE
    if not _NVML_AVAILABLE:
        logger.warning("pynvml not installed — GPU time tracking disabled.")
        return
    try:
        pynvml.nvmlInit()
        driver = pynvml.nvmlSystemGetDriverVersion()
        logger.info("NVML initialised (driver %s).", driver)
    except pynvml.NVMLError as exc:
        logger.warning("NVML init failed (%s) — GPU time tracking disabled.", exc)
        _NVML_AVAILABLE = False
        return

    count = pynvml.nvmlDeviceGetCount()
    for i in range(count):
        handle = pynvml.nvmlDeviceGetHandleByIndex(i)
        uuid = pynvml.nvmlDeviceGetUUID(handle)
        _nvml_handle_cache[uuid] = handle

        # Try to enable accounting mode.
        try:
            pynvml.nvmlDeviceSetAccountingMode(handle, pynvml.NVML_FEATURE_ENABLED)
            _accounting_enabled[uuid] = True
            logger.info("GPU %s — accounting mode: ENABLED (activated now).", uuid)
            continue
        except pynvml.NVMLError:
            pass  # may already be on, or no permission — check below

        # Activation failed: read the current state.
        try:
            mode = pynvml.nvmlDeviceGetAccountingMode(handle)
            _accounting_enabled[uuid] = (mode == pynvml.NVML_FEATURE_ENABLED)
        except pynvml.NVMLError:
            _accounting_enabled[uuid] = False

        if _accounting_enabled[uuid]:
            logger.info("GPU %s — accounting mode: ENABLED (was already on).", uuid)
        else:
            logger.warning(
                "GPU %s — accounting mode: UNAVAILABLE — falling back to polling.", uuid
            )


def _get_nvml_handle(gpu_uuid: str) -> object | None:
    """Return a cached pynvml device handle for *gpu_uuid*, or None on error."""
    if not _NVML_AVAILABLE:
        return None
    if gpu_uuid in _nvml_handle_cache:
        return _nvml_handle_cache[gpu_uuid]
    try:
        handle = pynvml.nvmlDeviceGetHandleByUUID(gpu_uuid.encode())
        _nvml_handle_cache[gpu_uuid] = handle
        return handle
    except pynvml.NVMLError as exc:
        logger.warning("Cannot get NVML handle for %s: %s", gpu_uuid, exc)
        _nvml_handle_cache[gpu_uuid] = None
        return None


# ---------------------------------------------------------------------------
# GPU time helpers
# ---------------------------------------------------------------------------

def _read_gpu_time_accounting(handle: object | None, pid: int) -> float:
    """
    Return GPU active time in seconds for *pid* via NVML accounting stats.
    Works for both running and completed processes.
    Returns 0.0 if unavailable.
    """
    if handle is None:
        return 0.0
    try:
        stats = pynvml.nvmlDeviceGetAccountingStats(handle, pid)
        return stats.time / 1000.0  # ms → s
    except pynvml.NVMLError as exc:
        logger.debug("Accounting stats unavailable for PID %d: %s", pid, exc)
        return 0.0


def _sample_gpu_active_seconds(
    handle: object | None,
    pid: int,
    last_util_ts: int,
) -> tuple[float, int]:
    """
    Fetch SM utilisation samples for *pid* that arrived after *last_util_ts*
    (microseconds since epoch) and compute the weighted active seconds.

    Returns (gpu_active_seconds_delta, updated_last_util_ts).
    """
    if handle is None:
        return 0.0, last_util_ts
    try:
        samples = pynvml.nvmlDeviceGetProcessUtilizationSample(handle, last_util_ts)
    except pynvml.NVMLError:
        return 0.0, last_util_ts

    pid_samples = sorted(
        (s for s in samples if s.pid == pid),
        key=lambda s: s.timeStamp,
    )
    if not pid_samples:
        return 0.0, last_util_ts

    gpu_active = 0.0
    prev_ts = last_util_ts
    for s in pid_samples:
        interval_s = (s.timeStamp - prev_ts) / 1e6  # µs → s
        if interval_s > 0:
            gpu_active += (s.smUtil / 100.0) * interval_s
        prev_ts = s.timeStamp

    return gpu_active, prev_ts


# ---------------------------------------------------------------------------
# State per tracked PID
# ---------------------------------------------------------------------------

@dataclass
class ProcessEntry:
    gpu_uuid: str
    process_name: str
    # CPU time
    baseline_cpu: float        # cpu_times snapshot when PID was first seen
    last_cpu: float            # most recent cpu_times snapshot
    # GPU time — polling mode only (ignored when accounting mode is active)
    accumulated_gpu_s: float = 0.0
    last_util_ts: int = 0      # µs since epoch; set to now on first arrival


def _read_cpu_time(pid: int) -> float | None:
    """Return user+system CPU seconds for *pid*, or None if unavailable."""
    try:
        p = psutil.Process(pid)
        t = p.cpu_times()
        return t.user + t.system
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None


# ---------------------------------------------------------------------------
# Core loop helpers
# ---------------------------------------------------------------------------

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
    - Register new arrivals (record baseline CPU time, increment running gauge).
    - Refresh last_cpu for existing entries.
    - For polling-mode GPUs, accumulate GPU active time.
    - Update running job gauges (cpu/gpu time so far).
    """
    now_us = int(time.time() * 1e6)

    for pid, (gpu_uuid, process_name) in current_pids.items():
        if pid not in tracked:
            baseline = _read_cpu_time(pid) or 0.0
            tracked[pid] = ProcessEntry(
                gpu_uuid=gpu_uuid,
                process_name=process_name,
                baseline_cpu=baseline,
                last_cpu=baseline,
                accumulated_gpu_s=0.0,
                last_util_ts=now_us,
            )
            gpu_job_running.labels(gpu_uuid=gpu_uuid, process_name=process_name).inc()
            logger.info(
                "[%s] [Started ] PID: %d (%s) | GPU: %s",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                pid,
                process_name,
                gpu_uuid,
            )
        else:
            entry = tracked[pid]

            # Refresh CPU snapshot.
            fresh_cpu = _read_cpu_time(pid)
            if fresh_cpu is not None:
                entry.last_cpu = fresh_cpu

            # Polling mode: accumulate GPU utilisation.
            if not _accounting_enabled.get(gpu_uuid, False):
                handle = _get_nvml_handle(gpu_uuid)
                gpu_delta, new_ts = _sample_gpu_active_seconds(
                    handle, pid, entry.last_util_ts
                )
                entry.accumulated_gpu_s += gpu_delta
                entry.last_util_ts = new_ts

        # Update live gauges for this PID.
        entry = tracked[pid]
        pid_str = str(pid)
        cpu_elapsed = max(0.0, entry.last_cpu - entry.baseline_cpu)

        if _accounting_enabled.get(gpu_uuid, False):
            handle = _get_nvml_handle(gpu_uuid)
            gpu_elapsed = _read_gpu_time_accounting(handle, pid)
        else:
            gpu_elapsed = entry.accumulated_gpu_s

        gpu_job_running_cpu_time_seconds.labels(
            gpu_uuid=gpu_uuid, process_name=process_name, pid=pid_str
        ).set(cpu_elapsed)
        gpu_job_running_gpu_time_seconds.labels(
            gpu_uuid=gpu_uuid, process_name=process_name, pid=pid_str
        ).set(gpu_elapsed)


def process_finished(
    tracked: dict[int, ProcessEntry],
    current_pids: dict[int, tuple[str, str]],
) -> None:
    """
    Detect PIDs that disappeared, record completed metrics, clean up running
    gauges, log, then remove from tracked.
    """
    finished_pids = set(tracked) - set(current_pids)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for pid in finished_pids:
        entry = tracked.pop(pid)
        pid_str = str(pid)

        # --- CPU time ---
        final_cpu = _read_cpu_time(pid)
        if final_cpu is None:
            final_cpu = entry.last_cpu
        cpu_used = max(0.0, final_cpu - entry.baseline_cpu)

        # --- GPU time ---
        handle = _get_nvml_handle(entry.gpu_uuid)
        if _accounting_enabled.get(entry.gpu_uuid, False):
            gpu_used = _read_gpu_time_accounting(handle, pid)
            gpu_mode_tag = "accounting"
        else:
            gpu_delta, _ = _sample_gpu_active_seconds(handle, pid, entry.last_util_ts)
            gpu_used = entry.accumulated_gpu_s + gpu_delta
            gpu_mode_tag = "polling"

        # Record completed-job metrics.
        labels = {"gpu_uuid": entry.gpu_uuid, "process_name": entry.process_name}
        gpu_job_completed.labels(**labels).inc()
        gpu_job_cpu_time_seconds.labels(**labels).inc(cpu_used)
        gpu_job_gpu_time_seconds.labels(**labels).inc(gpu_used)
        gpu_job_cpu_duration.labels(**labels).observe(cpu_used)
        gpu_job_gpu_duration.labels(**labels).observe(gpu_used)

        # Clean up running-job gauges.
        gpu_job_running.labels(**labels).dec()
        gpu_job_running_cpu_time_seconds.remove(entry.gpu_uuid, entry.process_name, pid_str)
        gpu_job_running_gpu_time_seconds.remove(entry.gpu_uuid, entry.process_name, pid_str)

        logger.info(
            "[%s] [Finished] PID: %d (%s) | GPU: %s | "
            "CPU time: %.2fs | GPU active: %.2fs [%s]",
            ts,
            pid,
            entry.process_name,
            entry.gpu_uuid,
            cpu_used,
            gpu_used,
            gpu_mode_tag,
        )


def main() -> None:
    _nvml_init()
    start_http_server(EXPORTER_PORT)
    logger.info(
        "GPU Job Exporter started — metrics at http://0.0.0.0:%d/metrics",
        EXPORTER_PORT,
    )

    tracked: dict[int, ProcessEntry] = {}
    first_cycle = True

    while True:
        current_pids = query_gpu_processes()

        if current_pids is None:
            time.sleep(POLL_INTERVAL)
            continue

        if not first_cycle:
            process_finished(tracked, current_pids)

        update_tracked(tracked, current_pids)
        first_cycle = False
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
