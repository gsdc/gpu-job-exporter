#!/usr/bin/env python3
"""
GPU Job Completion Exporter (with JSON Logging & Historical Calculation)
Python 3.6.8 compatible.
"""

import json
import os
import signal
import subprocess
import time
import logging
import glob
from datetime import datetime
from typing import Dict, Tuple, Optional, Any

import psutil
from prometheus_client import start_http_server, Counter, Gauge, Summary

try:
    import pynvml
    _NVML_AVAILABLE = True
except ImportError:
    pynvml = None
    _NVML_AVAILABLE = False

# --- Configuration ---
POLL_INTERVAL: float = float(os.environ.get("GPU_EXPORTER_POLL_INTERVAL", 2))
EXPORTER_PORT: int = int(os.environ.get("GPU_EXPORTER_PORT", 9101))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

STATE_FILE: str = os.environ.get(
    "GPU_EXPORTER_STATE_FILE",
    "/var/lib/gpu_job_exporter/state.json",
)
LOG_DIR: str = os.environ.get(
    "GPU_EXPORTER_LOG_DIR",
    "/var/log/gpu_job_exporter",
)
_SAVE_EVERY: int = max(1, int(os.environ.get("GPU_EXPORTER_SAVE_INTERVAL_CYCLES", 30)))

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

gpu_job_running = Gauge("gpu_job_running", "Number of GPU jobs currently running", ["gpu_uuid", "process_name", "username"])
gpu_job_running_cpu_time_seconds = Gauge("gpu_job_running_cpu_time_seconds", "CPU time consumed so far", ["gpu_uuid", "process_name", "username", "pid"])
gpu_job_running_gpu_time_seconds = Gauge("gpu_job_running_gpu_time_seconds", "GPU time consumed so far", ["gpu_uuid", "process_name", "username", "pid"])

gpu_job_completed = Counter("gpu_job_completed_total", "Total completed GPU jobs", ["gpu_uuid", "process_name", "username"])
gpu_job_cpu_time_seconds = Counter("gpu_job_cpu_time_seconds_total", "Total CPU time consumed", ["gpu_uuid", "process_name", "username"])
gpu_job_gpu_time_seconds = Counter("gpu_job_gpu_time_seconds_total", "Total GPU time consumed", ["gpu_uuid", "process_name", "username"])
gpu_job_cpu_duration = Summary("gpu_job_cpu_duration_seconds", "CPU time distribution", ["gpu_uuid", "process_name", "username"])
gpu_job_gpu_duration = Summary("gpu_job_gpu_duration_seconds", "GPU active time distribution", ["gpu_uuid", "process_name", "username"])

# ---------------------------------------------------------------------------
# State & Logging Logic
# ---------------------------------------------------------------------------

_nvml_handle_cache: Dict[str, Any] = {}
_accounting_enabled: Dict[str, bool] = {}
_totals: Dict[str, Dict[str, float]] = {"completed": {}, "cpu_time": {}, "gpu_time": {}}

class ProcessEntry:
    def __init__(self, gpu_uuid: str, process_name: str, username: str,
                 baseline_cpu: float, last_cpu: float,
                 accumulated_gpu_s: float = 0.0, last_util_ts: int = 0,
                 start_time: float = 0.0):
        self.gpu_uuid = gpu_uuid
        self.process_name = process_name
        self.username = username
        self.baseline_cpu = baseline_cpu
        self.last_cpu = last_cpu
        self.accumulated_gpu_s = accumulated_gpu_s
        self.last_util_ts = last_util_ts
        self.start_time = start_time if start_time > 0.0 else time.time()

def _label_key(gpu_uuid: str, process_name: str, username: str) -> str:
    return f"{gpu_uuid}|{process_name}|{username}"

def _log_finished_job(entry: ProcessEntry, cpu_used: float, gpu_used: float, mode: str) -> None:
    """작업 종료 시 JSON 로그 파일에 기록 (JSON Lines 포맷)"""
    os.makedirs(LOG_DIR, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = os.path.join(LOG_DIR, f"jobs-{today}.json")
    
    record = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "gpu_uuid": entry.gpu_uuid,
        "process_name": entry.process_name,
        "username": entry.username,
        "cpu_time_s": round(cpu_used, 3),
        "gpu_time_s": round(gpu_used, 3),
        "mode": mode
    }
    
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning(f"Failed to write job log: {exc}")

def _recalculate_totals_from_logs() -> Dict[str, Dict[str, float]]:
    """로그 파일을 전체 스캔하여 누적 통계 계산"""
    new_totals = {"completed": {}, "cpu_time": {}, "gpu_time": {}}
    if not os.path.isdir(LOG_DIR):
        return new_totals
    
    log_files = sorted(glob.glob(os.path.join(LOG_DIR, "jobs-*.json")))
    if not log_files:
        return new_totals

    logger.info(f"Recalculating totals from {len(log_files)} log files...")
    for path in log_files:
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip(): continue
                    data = json.loads(line)
                    key = _label_key(data["gpu_uuid"], data["process_name"], data["username"])
                    
                    new_totals["completed"][key] = new_totals["completed"].get(key, 0) + 1
                    new_totals["cpu_time"][key] = new_totals["cpu_time"].get(key, 0) + data.get("cpu_time_s", 0)
                    new_totals["gpu_time"][key] = new_totals["gpu_time"].get(key, 0) + data.get("gpu_time_s", 0)
        except Exception as exc:
            logger.warning(f"Error reading log file {path}: {exc}")
            
    return new_totals

def _save_state(tracked: Dict[int, ProcessEntry]) -> None:
    """현재 실행 중인 작업(tracked) 상태만 저장"""
    state = {
        "tracked": {
            str(pid): {
                "gpu_uuid": e.gpu_uuid, "process_name": e.process_name, "username": e.username,
                "baseline_cpu": e.baseline_cpu, "last_cpu": e.last_cpu,
                "accumulated_gpu_s": e.accumulated_gpu_s, "last_util_ts": e.last_util_ts,
                "start_time": e.start_time,
            } for pid, e in tracked.items()
        },
    }
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    tmp = f"{STATE_FILE}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except OSError as exc:
        logger.warning(f"Failed to save state: {exc}")

def _load_tracked_state() -> Dict[int, ProcessEntry]:
    """이전 기동에서 실행 중이었던 작업 정보 복구"""
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        tracked = {}
        for pid_str, e in state.get("tracked", {}).items():
            tracked[int(pid_str)] = ProcessEntry(
                gpu_uuid=e["gpu_uuid"], process_name=e["process_name"], username=e.get("username", "unknown"),
                baseline_cpu=e["baseline_cpu"], last_cpu=e["last_cpu"],
                accumulated_gpu_s=e["accumulated_gpu_s"], last_util_ts=e["last_util_ts"],
                start_time=e.get("start_time", 0.0),
            )
        return tracked
    except Exception:
        return {}

def _restore_prometheus_counters() -> None:
    """_totals 데이터를 Prometheus Counter에 주입"""
    for key, val in _totals["completed"].items():
        if val > 0:
            gpu_uuid, proc, user = _parse_label_key(key)
            gpu_job_completed.labels(gpu_uuid=gpu_uuid, process_name=proc, username=user).inc(val)
    for key, val in _totals["cpu_time"].items():
        if val > 0:
            gpu_uuid, proc, user = _parse_label_key(key)
            gpu_job_cpu_time_seconds.labels(gpu_uuid=gpu_uuid, process_name=proc, username=user).inc(val)
    for key, val in _totals["gpu_time"].items():
        if val > 0:
            gpu_uuid, proc, user = _parse_label_key(key)
            gpu_job_gpu_time_seconds.labels(gpu_uuid=gpu_uuid, process_name=proc, username=user).inc(val)

def _parse_label_key(key: str) -> Tuple[str, str, str]:
    parts = key.split("|", 2)
    return (parts[0], parts[1], parts[2]) if len(parts) == 3 else (parts[0], parts[1], "unknown")

# --- NVML & Process Helpers (Existing logic optimized for 3.6) ---

def _nvml_init() -> None:
    global _NVML_AVAILABLE
    if not _NVML_AVAILABLE: return
    try:
        pynvml.nvmlInit()
        logger.info(f"NVML initialised (driver {pynvml.nvmlSystemGetDriverVersion()}).")
        for i in range(pynvml.nvmlDeviceGetCount()):
            h = pynvml.nvmlDeviceGetHandleByIndex(i)
            u = pynvml.nvmlDeviceGetUUID(h)
            u = u.decode() if isinstance(u, bytes) else u
            _nvml_handle_cache[u] = h
            try:
                pynvml.nvmlDeviceSetAccountingMode(h, pynvml.NVML_FEATURE_ENABLED)
                _accounting_enabled[u] = True
                logger.info(f"GPU[{i}] {u}: accounting mode ENABLED (set successfully)")
            except Exception as set_err:
                try:
                    mode = pynvml.nvmlDeviceGetAccountingMode(h)
                    enabled = (mode == pynvml.NVML_FEATURE_ENABLED)
                    _accounting_enabled[u] = enabled
                    logger.warning(
                        f"GPU[{i}] {u}: accounting mode set FAILED ({set_err}), "
                        f"current state: {'ENABLED' if enabled else 'DISABLED'} — "
                        f"{'using accounting for final values' if enabled else 'falling back to utilization sampling'}"
                    )
                except Exception as get_err:
                    _accounting_enabled[u] = False
                    logger.warning(
                        f"GPU[{i}] {u}: accounting mode UNKNOWN (set failed: {set_err}, get failed: {get_err}) — "
                        f"falling back to utilization sampling"
                    )
    except Exception as e:
        logger.warning(f"NVML init failed: {e}")
        _NVML_AVAILABLE = False

def _get_nvml_handle(gpu_uuid: str):
    return _nvml_handle_cache.get(gpu_uuid)

def _read_gpu_time_accounting(handle, pid):
    try: return pynvml.nvmlDeviceGetAccountingStats(handle, pid).time / 1000.0
    except: return 0.0

def _sample_gpu_active_seconds(handle, pid, last_util_ts):
    try:
        samples = pynvml.nvmlDeviceGetProcessUtilizationSample(handle, last_util_ts)
        pid_samples = sorted((s for s in samples if s.pid == pid), key=lambda s: s.timeStamp)
        gpu_active, prev_ts = 0.0, last_util_ts
        for s in pid_samples:
            interval = (s.timeStamp - prev_ts) / 1e6
            if interval > 0: gpu_active += (s.smUtil / 100.0) * interval
            prev_ts = s.timeStamp
        return gpu_active, prev_ts
    except: return 0.0, last_util_ts

def _read_cpu_time(pid):
    try:
        t = psutil.Process(pid).cpu_times()
        return t.user + t.system
    except: return None

def _get_username(pid):
    try:
        u = psutil.Process(pid).username()
        return u() if callable(u) else u
    except: return "unknown"

def query_gpu_processes():
    try:
        res = subprocess.run(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,name", "--format=csv,noheader,nounits"], 
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=10)
        if res.returncode != 0: return None
        procs = {}
        for line in res.stdout.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) == 3: procs[int(parts[1])] = (parts[0], parts[2])
        return procs
    except: return None

# --- Main Loop Logic ---

def update_tracked(tracked: Dict[int, ProcessEntry], current_pids: Dict[int, Tuple[str, str]]) -> None:
    now_us = int(time.time() * 1e6)
    for pid, (gpu_uuid, process_name) in current_pids.items():
        if pid not in tracked:
            baseline = _read_cpu_time(pid) or 0.0
            username = _get_username(pid)
            tracked[pid] = ProcessEntry(gpu_uuid, process_name, username, baseline, baseline, 0.0, now_us)
            gpu_job_running.labels(gpu_uuid=gpu_uuid, process_name=process_name, username=username).inc()
            logger.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [Started ] PID: {pid} ({process_name}) | GPU: {gpu_uuid} | User: {username}")
        else:
            entry = tracked[pid]
            fresh_cpu = _read_cpu_time(pid)
            if fresh_cpu is not None: entry.last_cpu = fresh_cpu
            # accounting mode 여부와 관계없이 항상 sampling 누적
            # (nvmlDeviceGetAccountingStats는 실행 중인 프로세스에서 0을 반환할 수 있음)
            delta, new_ts = _sample_gpu_active_seconds(_get_nvml_handle(gpu_uuid), pid, entry.last_util_ts)
            if delta > 0 or new_ts != entry.last_util_ts:
                entry.accumulated_gpu_s += delta
                entry.last_util_ts = new_ts
                if _accounting_enabled.get(gpu_uuid, False):
                    logger.debug(
                        f"PID {pid} GPU sample: +{delta:.3f}s (total_sampled={entry.accumulated_gpu_s:.3f}s)"
                    )

        entry = tracked[pid]
        cpu_val = max(0.0, entry.last_cpu - entry.baseline_cpu)
        # 실시간 게이지는 sampling 누적값 사용 (accounting은 완료 시에만 신뢰)
        gpu_val = entry.accumulated_gpu_s
        lbls = {"gpu_uuid": gpu_uuid, "process_name": process_name, "username": entry.username}
        gpu_job_running_cpu_time_seconds.labels(**lbls, pid=str(pid)).set(cpu_val)
        gpu_job_running_gpu_time_seconds.labels(**lbls, pid=str(pid)).set(gpu_val)

def process_finished(tracked: Dict[int, ProcessEntry], current_pids: Dict[int, Tuple[str, str]]) -> None:
    for pid in (set(tracked) - set(current_pids)):
        entry = tracked.pop(pid)
        cpu_used = max(0.0, (_read_cpu_time(pid) or entry.last_cpu) - entry.baseline_cpu)
        
        handle = _get_nvml_handle(entry.gpu_uuid)
        pid_lifetime = time.time() - entry.start_time
        if _accounting_enabled.get(entry.gpu_uuid, False):
            acct_val = _read_gpu_time_accounting(handle, pid)
            if acct_val > 0:
                gpu_used, mode = acct_val, "accounting"
                logger.info(
                    f"PID {pid}: accounting gpu_time={acct_val:.3f}s "
                    f"(sampled={entry.accumulated_gpu_s:.3f}s, lifetime={pid_lifetime:.3f}s)"
                )
            else:
                # accounting=0 → sampling 누적값 우선, 그것도 0이면 PID 생존 시간으로 폴백
                delta, _ = _sample_gpu_active_seconds(handle, pid, entry.last_util_ts)
                sampled = entry.accumulated_gpu_s + delta
                if sampled > 0:
                    gpu_used, mode = sampled, "accounting(0)-fallback-sampling"
                    logger.warning(
                        f"PID {pid}: accounting returned 0, using sampled={gpu_used:.3f}s "
                        f"(lifetime={pid_lifetime:.3f}s)"
                    )
                else:
                    gpu_used, mode = pid_lifetime, "accounting(0)-fallback-lifetime"
                    logger.warning(
                        f"PID {pid}: accounting=0 and sampling=0, "
                        f"using pid lifetime={gpu_used:.3f}s as gpu_time estimate"
                    )
        else:
            delta, _ = _sample_gpu_active_seconds(handle, pid, entry.last_util_ts)
            sampled = entry.accumulated_gpu_s + delta
            if sampled > 0:
                gpu_used, mode = sampled, "sampling"
            else:
                gpu_used, mode = pid_lifetime, "sampling(0)-fallback-lifetime"
                logger.warning(
                    f"PID {pid}: sampling=0, using pid lifetime={gpu_used:.3f}s as gpu_time estimate"
                )

        # 로그 기록
        _log_finished_job(entry, cpu_used, gpu_used, mode)

        # Prometheus & internal totals 업데이트
        lbls = {"gpu_uuid": entry.gpu_uuid, "process_name": entry.process_name, "username": entry.username}
        gpu_job_completed.labels(**lbls).inc()
        gpu_job_cpu_time_seconds.labels(**lbls).inc(cpu_used)
        gpu_job_gpu_time_seconds.labels(**lbls).inc(gpu_used)
        gpu_job_cpu_duration.labels(**lbls).observe(cpu_used)
        gpu_job_gpu_duration.labels(**lbls).observe(gpu_used)

        key = _label_key(entry.gpu_uuid, entry.process_name, entry.username)
        _totals["completed"][key] = _totals["completed"].get(key, 0) + 1
        _totals["cpu_time"][key] = _totals["cpu_time"].get(key, 0) + cpu_used
        _totals["gpu_time"][key] = _totals["gpu_time"].get(key, 0) + gpu_used

        gpu_job_running.labels(**lbls).dec()
        gpu_job_running_cpu_time_seconds.remove(entry.gpu_uuid, entry.process_name, entry.username, str(pid))
        gpu_job_running_gpu_time_seconds.remove(entry.gpu_uuid, entry.process_name, entry.username, str(pid))
        logger.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [Finished] PID: {pid} ({entry.process_name}) | CPU: {cpu_used:.2f}s | GPU: {gpu_used:.2f}s")

def main():
    global _totals
    _nvml_init()
    
    # 1. 과거 로그로부터 통계 복구
    _totals = _recalculate_totals_from_logs()
    _restore_prometheus_counters()
    
    # 2. 실행 중이었던 작업 복구
    tracked = _load_tracked_state()
    for e in tracked.values():
        gpu_job_running.labels(gpu_uuid=e.gpu_uuid, process_name=e.process_name, username=e.username).inc()
    
    start_http_server(EXPORTER_PORT)
    logger.info(f"Exporter started on port {EXPORTER_PORT} (Logs: {LOG_DIR})")

    def _on_exit(s, f):
        _save_state(tracked)
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _on_exit)
    signal.signal(signal.SIGINT, _on_exit)

    first_cycle, save_cycle = True, 0
    while True:
        pids = query_gpu_processes()
        if pids is not None:
            if not first_cycle: process_finished(tracked, pids)
            update_tracked(tracked, pids)
            first_cycle = False
            save_cycle += 1
            if save_cycle >= _SAVE_EVERY:
                _save_state(tracked)
                save_cycle = 0
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
