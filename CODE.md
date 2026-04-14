# Code Architecture & Design

`gpu_job_exporter.py` is designed for robust GPU job tracking with log-driven persistence. It prioritizes data integrity and high performance under Python 3.6.8.

## 1. Core Logic Overview

### High-Precision Job Identification
- **Job ID 규격**: `YYYYMMDD-HHMMSS_USER_PID`
- **Why?**: Ensures uniqueness even if the OS reuses PIDs or if a user restarts a process quickly.

### Metric Synchronisation (Log-Driven)
Unlike typical exporters that maintain state entirely in RAM, this exporter uses **JSONL logs as the Source of Truth**.
1. **Completion**: When a process ends, it is written to `jobs-YYYY-MM-DD.json`.
2. **Incremental Read**: `_sync_metrics_from_logs()` reads only the newly appended lines by tracking the file `offset` in `state.json`.
3. **Resiliency**: If the exporter crashes, it resumes reading from the exact byte it left off, ensuring Prometheus Counters are always accurate.

## 2. Main Components

### `ProcessEntry` Class
Represents an active job on a GPU.
- `baseline_cpu`: Initial CPU time recorded when the job is first seen.
- `accumulated_gpu_s`: Integrated GPU utilization (polling mode only).
- `last_util_ts`: Microsecond timestamp for differential polling.

### GPU Tracking Modes
1. **NVML Accounting Mode**:
   - High-precision hardware counters inside the driver.
   - Accessed via `nvmlDeviceGetAccountingStats`.
   - Requires `root` or special permissions to enable.
2. **Polling Fallback**:
   - Integrates `smUtil` samples over the polling interval.
   - Used when Accounting Mode is disabled or unsupported.

### Internal Persistence
- **`/var/lib/gpu_job_exporter/state.json`**:
  - `running_jobs`: Active process metadata for recovery after exporter restart.
  - `last_sync`: Keeps track of the last log file and byte offset read.
- **`/var/log/gpu_job_exporter/jobs-*.json`**:
  - Daily JSONL logs of every completed compute task.

## 3. Implementation Details

- **Atomic Writes**: `state.json` is written to a `.tmp` file and then renamed (`os.replace`) to prevent corruption during power failure.
- **Signal Handling**: Catches `SIGTERM` and `SIGINT` to save the current state before exiting.
- **Incremental Sync**: The loop calls `_sync_metrics_from_logs()` every cycle to update Prometheus counters in near-real-time.
