# GPU Job Exporter

Prometheus exporter for GPU compute job completions. Monitors GPU processes, records detailed job logs, and exposes metrics for individual compute tasks including CPU and GPU active time.

## Key Features
- **Standardized Job ID**: `YYYYMMDD-HHMMSS_USER_PID` for unique task tracking.
- **Source of Truth (JSONL)**: Completed jobs are stored in date-partitioned JSONL files (`/var/log/gpu_job_exporter/jobs-YYYY-MM-DD.json`).
- **Incremental Log Sync**: Metrics are driven by reading log deltas, ensuring no data loss during restarts.
- **Python 3.6+ Support**: Fully compatible with CentOS 7 default `python3`.
- **Dual GPU Tracking**: Supports both NVML Accounting Mode (high precision) and Polling fallback.

## Requirements
- **OS**: CentOS 7+ (Linux)
- **Python**: 3.6.8 or higher
- **NVIDIA Driver**: 450.x or higher recommended
- **Dependencies**: `psutil`, `prometheus_client`, `pynvml` (automatically vendored in RPM)

## Exposed Metrics
- `gpu_job_running`: Gauge of active GPU processes.
- `gpu_job_completed_total`: Counter of finished tasks (synced from logs).
- `gpu_job_cpu_time_seconds_total`: Cumulative CPU time (user+system).
- `gpu_job_gpu_time_seconds_total`: Cumulative GPU active time.
- `gpu_job_cpu_duration_seconds`: Summary distribution of CPU usage per job.
- `gpu_job_gpu_duration_seconds`: Summary distribution of GPU usage per job.

## Data Structure
### 1. Running Jobs (`/var/lib/gpu_job_exporter/state.json`)
Stores the real-time state of active processes and the last log sync offset.
```json
{
  "running_jobs": {
    "1234": { "job_id": "20260414-143005_user_1234", ... }
  },
  "last_sync": { "file": "jobs-2026-04-14.json", "offset": 5421 }
}
```

### 2. Job Logs (`/var/log/gpu_job_exporter/jobs-YYYY-MM-DD.json`)
Append-only JSONL files for completed tasks.
```jsonl
{"job_id": "20260414-143005_user_1234", "cpu_time_s": 12.5, "gpu_time_s": 45.1, "mode": "accounting"}
```

## Build & Install
```bash
# Build RPM
bash build_rpm.sh

# Install
sudo yum install ~/rpmbuild/RPMS/x86_64/gpu-job-exporter-1.0.0-1.el7.x86_64.rpm
```
