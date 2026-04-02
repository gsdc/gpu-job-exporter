# 코드 상세 설명 — gpu_job_exporter.py

---

## 전체 흐름

```
main()
 │
 ├─ _nvml_init()                           # NVML 초기화 및 GPU별 추적 모드 결정
 ├─ _load_state()                          # 디스크에서 이전 상태 복구
 ├─ _restore_counters()                    # 복구된 카운터를 Prometheus에 재현
 ├─ start_http_server(EXPORTER_PORT)       # Prometheus 엔드포인트 개시
 │
 └─ 무한 루프 (POLL_INTERVAL 초 간격)
      │
      ├─ query_gpu_processes()             # nvidia-smi 실행 → 현재 PID 목록
      │     └─ 실패 시 None 반환 → 이번 사이클 skip (상태 보존)
      │
      ├─ process_finished()               # 사라진 PID 감지 → 완료 메트릭 기록
      │
      ├─ update_tracked()                 # 신규 PID 등록 / 기존 PID CPU·GPU time 갱신
      │
      └─ _save_state() (주기적)           # 카운터·추적 상태를 디스크에 저장
```

---

## 설정값

```python
POLL_INTERVAL          = float(os.environ.get("GPU_EXPORTER_POLL_INTERVAL", 2))
EXPORTER_PORT          = int(os.environ.get("GPU_EXPORTER_PORT", 9101))
STATE_FILE             = os.environ.get("GPU_EXPORTER_STATE_FILE",
                             "/var/lib/gpu_job_exporter/state.json")
_SAVE_EVERY            = max(1, int(os.environ.get("GPU_EXPORTER_SAVE_INTERVAL_CYCLES", 30)))
```

| 환경변수 | 기본값 | 설명 |
|---|---|---|
| `GPU_EXPORTER_POLL_INTERVAL` | `2` | nvidia-smi 폴링 간격 (초) |
| `GPU_EXPORTER_PORT` | `9101` | Prometheus 메트릭 노출 포트 |
| `GPU_EXPORTER_STATE_FILE` | `/var/lib/gpu_job_exporter/state.json` | 상태 영속성 파일 경로 |
| `GPU_EXPORTER_SAVE_INTERVAL_CYCLES` | `30` | 상태 저장 주기 (폴 사이클 수, 기본 ~60초) |

---

## GPU 시간 추적 모드

GPU별로 시작 시점에 모드가 결정됩니다.

| 모드 | 설명 |
|---|---|
| **Accounting mode** | NVML 드라이버가 프로세스별 정확한 GPU 활성 시간을 기록. 프로세스 종료 후 `nvmlDeviceGetAccountingStats`로 조회. root 권한으로 자동 활성화 시도. |
| **Polling mode** | Accounting 모드 미지원 시 fallback. SM 사용률 샘플을 폴 간격마다 적분하여 누적. |

---

## 상태 관리 자료구조

### `ProcessEntry` (dataclass)

```python
@dataclass
class ProcessEntry:
    gpu_uuid:          str    # GPU 식별자 (nvidia-smi 제공)
    process_name:      str    # 프로세스 이름 (nvidia-smi 제공)
    baseline_cpu:      float  # 이 PID를 처음 발견했을 때의 누적 CPU time (초)
    last_cpu:          float  # 가장 최근에 읽은 누적 CPU time (종료 시 fallback용)
    accumulated_gpu_s: float  # polling 모드에서 누적된 GPU 활성 시간 (초)
    last_util_ts:      int    # polling 모드의 마지막 샘플 타임스탬프 (µs)
```

### `tracked` 딕셔너리

```
tracked: dict[int, ProcessEntry]
  key   = PID (int)
  value = ProcessEntry
```

메인 루프 전체에서 공유되는 단일 딕셔너리입니다.  
신규 PID는 `update_tracked()`에서 추가되고, 종료 PID는 `process_finished()`에서 제거됩니다.

### `_totals` 딕셔너리

```
_totals: { "completed": {"uuid|name": float},
           "cpu_time":  {"uuid|name": float},
           "gpu_time":  {"uuid|name": float} }
```

Prometheus Counter와 동기화되는 내부 누적값입니다.  
상태 파일 저장 및 재시작 후 카운터 복구에 사용됩니다.

### NVML 캐시

```python
_nvml_handle_cache: dict[str, object]  # gpu_uuid → NVML handle
_accounting_enabled: dict[str, bool]   # gpu_uuid → accounting 모드 여부
```

---

## 함수별 설명

### `_nvml_init()`

NVML을 초기화하고 GPU별 핸들을 캐시에 등록합니다.  
각 GPU에 대해 `nvmlDeviceSetAccountingMode(ENABLED)` 를 시도하고, 실패 시 현재 모드를 읽어 `_accounting_enabled` 를 결정합니다.  
`pynvml` 미설치 또는 드라이버 오류 시 `_NVML_AVAILABLE = False` 로 설정하고 polling 모드로 폴백합니다.

---

### `_get_nvml_handle(gpu_uuid) → object | None`

`_nvml_handle_cache` 에서 핸들을 반환합니다.  
캐시에 없으면 `nvmlDeviceGetHandleByUUID` 로 조회하여 저장합니다.  
실패 시 `None` 을 반환하여 GPU time을 `0.0` 으로 처리합니다.

---

### `_read_gpu_time_accounting(handle, pid) → float`

```python
stats = pynvml.nvmlDeviceGetAccountingStats(handle, pid)
return stats.time / 1000.0  # ms → s
```

Accounting 모드에서 실행 중·종료된 프로세스 모두에 대해 GPU 활성 시간을 반환합니다.  
실패 시 `0.0` 반환.

---

### `_sample_gpu_active_seconds(handle, pid, last_util_ts) → (float, int)`

SM 사용률 샘플 (`nvmlDeviceGetProcessUtilizationSample`) 을 `last_util_ts` 이후 분을 필터링하여 가중 적분합니다:

```
gpu_active += (smUtil / 100.0) * interval_s
```

`(gpu_active_delta, updated_last_util_ts)` 튜플을 반환합니다.  
Polling 모드 전용이며, 샘플이 없으면 `(0.0, last_util_ts)` 반환.

---

### `_save_state(tracked)`

카운터 누적값(`_totals`)과 현재 추적 중인 PID 상태를 JSON으로 원자적(atomic) 저장합니다.

```
tmp 파일에 쓰기 → os.replace()로 교체
```

`GPU_EXPORTER_SAVE_INTERVAL_CYCLES` 사이클마다 호출되며, SIGTERM/SIGINT 수신 시에도 즉시 호출됩니다.

---

### `_load_state() → (totals_dict, {pid: ProcessEntry})`

STATE_FILE 이 존재하면 읽어서 `(_totals, tracked)` 를 복구합니다.  
파일이 없거나 파싱 실패 시 빈 값을 반환하여 처음부터 시작합니다.

---

### `_restore_counters(saved_totals)`

로드된 누적값을 Prometheus Counter에 `inc()` 로 재현합니다.  
재시작 이후에도 카운터가 연속적으로 보이도록 합니다.

---

### `_read_cpu_time(pid) → float | None`

```python
p = psutil.Process(pid)
t = p.cpu_times()
return t.user + t.system
```

- `/proc/<pid>/stat` 을 읽어 누적 CPU time(user + system)을 초 단위로 반환합니다.
- `NoSuchProcess` / `AccessDenied` / `ZombieProcess` 예외는 모두 `None` 반환으로 처리합니다.

---

### `query_gpu_processes() → dict[int, tuple[str, str]] | None`

```bash
nvidia-smi --query-compute-apps=gpu_uuid,pid,name --format=csv,noheader,nounits
```

출력 예시:
```
GPU-a1b2c3d4-..., 18432, python3
GPU-a1b2c3d4-..., 18500, torch_train
```

- 각 줄을 파싱하여 `{pid: (gpu_uuid, process_name)}` 딕셔너리로 변환합니다.
- 다음 상황에서 `None`을 반환하고 해당 사이클을 skip합니다:
  - `nvidia-smi` 실행 파일 없음 (`FileNotFoundError`)
  - 10초 초과 응답 없음 (`TimeoutExpired`)
  - 0이 아닌 exit code (드라이버 오류 등)
  - 기타 `OSError`
- `None` 반환 시 `tracked` 딕셔너리를 건드리지 않으므로 **카운터가 오염되지 않습니다**.

---

### `update_tracked(tracked, current_pids)`

현재 GPU에 올라와 있는 모든 PID를 순회합니다.

```
신규 PID (tracked에 없음)
  └─ _read_cpu_time() 로 baseline_cpu 기록
  └─ last_util_ts = now_us (µs) 설정
  └─ ProcessEntry 생성 후 tracked에 추가
  └─ gpu_job_running 게이지 +1

기존 PID (tracked에 이미 있음)
  └─ _read_cpu_time() 로 last_cpu 갱신
  └─ polling 모드: _sample_gpu_active_seconds() 로 accumulated_gpu_s 누적

공통
  └─ gpu_job_running_cpu_time_seconds 게이지 갱신
  └─ gpu_job_running_gpu_time_seconds 게이지 갱신
       accounting: _read_gpu_time_accounting()
       polling:    accumulated_gpu_s
```

`baseline_cpu`를 기록하는 이유:  
프로세스는 GPU에 올라오기 전부터 CPU를 사용했을 수 있습니다.  
종료 시점의 누적값에서 `baseline_cpu`를 빼야 **GPU 작업 기간 동안의 순수 CPU time**을 구할 수 있습니다.

---

### `process_finished(tracked, current_pids)`

```python
finished_pids = set(tracked) - set(current_pids)
```

이전 사이클에는 있었으나 현재 사이클에 없는 PID가 종료된 작업입니다.

종료된 PID마다 아래 순서로 처리합니다:

```
1. tracked에서 entry 꺼내기 (pop → 이후 해당 PID 추적 중단)

2. _read_cpu_time(pid) 마지막 시도
     성공 → final_cpu = 방금 읽은 값
     실패 → final_cpu = entry.last_cpu  (fallback)

3. cpu_used = max(0.0, final_cpu - entry.baseline_cpu)

4. GPU time 결정
     accounting 모드 → _read_gpu_time_accounting(handle, pid)
     polling 모드   → accumulated_gpu_s + 마지막 샘플 delta

5. Prometheus 완료 메트릭 업데이트
     gpu_job_completed_total          += 1
     gpu_job_cpu_time_seconds_total   += cpu_used
     gpu_job_gpu_time_seconds_total   += gpu_used
     gpu_job_cpu_duration_seconds.observe(cpu_used)
     gpu_job_gpu_duration_seconds.observe(gpu_used)

6. _totals 내부 누적값 동기화

7. 실행 중 게이지 정리
     gpu_job_running               -1
     gpu_job_running_cpu_time_seconds  레이블 제거
     gpu_job_running_gpu_time_seconds  레이블 제거

8. 로그 출력
     [2026-04-01 15:10:22] [Finished] PID: 18432 (python3) | GPU: GPU-a1b2... |
     CPU time: 120.53s | GPU active: 45.20s [accounting]
```

---

### `main()`

```python
_nvml_init()
saved_totals, saved_tracked = _load_state()
_totals = saved_totals
_restore_counters(saved_totals)
start_http_server(EXPORTER_PORT)

# 저장된 PID 복구
for pid, entry in saved_tracked.items():
    tracked[pid] = entry
    gpu_job_running.labels(...).inc()
    gpu_job_running_cpu_time_seconds.labels(...).set(cpu_elapsed)
    gpu_job_running_gpu_time_seconds.labels(...).set(entry.accumulated_gpu_s)

# SIGTERM / SIGINT → _save_state() 후 SystemExit
signal.signal(signal.SIGTERM, _on_exit)
signal.signal(signal.SIGINT, _on_exit)

first_cycle = len(tracked) == 0   # 복구된 PID 있으면 즉시 종료 감지

while True:
    current_pids = query_gpu_processes()
    if current_pids is None:
        time.sleep(POLL_INTERVAL); continue

    if not first_cycle:
        process_finished(tracked, current_pids)

    update_tracked(tracked, current_pids)
    first_cycle = False

    save_cycle += 1
    if save_cycle >= _SAVE_EVERY:
        _save_state(tracked)
        save_cycle = 0

    time.sleep(POLL_INTERVAL)
```

`first_cycle` 플래그가 필요한 이유:  
스크립트 시작 시 이미 실행 중인 프로세스는 "이전 사이클에 있었다"는 기록이 없습니다.  
첫 사이클에서 `process_finished()`를 호출하면 이 프로세스들이 전부 종료된 것으로 잘못 집계됩니다.  
단, 저장된 PID가 복구된 경우에는 `first_cycle = False` 로 시작하여 다운타임 동안 종료된 프로세스를 즉시 감지합니다.

---

## Prometheus 메트릭 상세

### 실행 중 작업 (Gauge)

| 메트릭 | 라벨 | 의미 |
|---|---|---|
| `gpu_job_running` | `gpu_uuid`, `process_name` | 현재 실행 중인 GPU 작업 수 |
| `gpu_job_running_cpu_time_seconds` | `gpu_uuid`, `process_name`, `pid` | 실행 중 작업의 현재까지 CPU 사용 시간 (초) |
| `gpu_job_running_gpu_time_seconds` | `gpu_uuid`, `process_name`, `pid` | 실행 중 작업의 현재까지 GPU 활성 시간 (초) |

### 완료된 작업 (Counter / Summary)

| 메트릭 | 타입 | 라벨 | 의미 |
|---|---|---|---|
| `gpu_job_completed_total` | Counter | `gpu_uuid`, `process_name` | 종료된 GPU 작업의 누적 건수 |
| `gpu_job_cpu_time_seconds_total` | Counter | `gpu_uuid`, `process_name` | 종료 작업의 CPU 사용 시간 누적 합계 (초) |
| `gpu_job_gpu_time_seconds_total` | Counter | `gpu_uuid`, `process_name` | 종료 작업의 GPU 활성 시간 누적 합계 (초) |
| `gpu_job_cpu_duration_seconds` | Summary | `gpu_uuid`, `process_name` | 개별 작업의 CPU time 분포 |
| `gpu_job_gpu_duration_seconds` | Summary | `gpu_uuid`, `process_name` | 개별 작업의 GPU time 분포 |

**활용 예시:**

```promql
# 최근 5분 평균 CPU 사용률 추이
rate(gpu_job_cpu_time_seconds_total[5m])

# 최근 5분 평균 GPU 활성 시간
rate(gpu_job_gpu_time_seconds_total[5m])

# 작업당 평균 CPU time
gpu_job_cpu_duration_seconds_sum / gpu_job_cpu_duration_seconds_count

# 작업당 평균 GPU active time
gpu_job_gpu_duration_seconds_sum / gpu_job_gpu_duration_seconds_count
```

> Summary는 Exporter 프로세스 내에서만 분위수(quantile)를 계산합니다.  
> 서버 간 집계가 필요하다면 `histogram_quantile()` 을 쓸 수 있는 Histogram 타입으로 교체를 검토하세요.

---

## 에러 처리 전략 요약

| 상황 | 처리 방식 |
|---|---|
| `nvidia-smi` 실행 불가 | `None` 반환 → 사이클 skip, 기존 `tracked` 보존 |
| `nvidia-smi` timeout (10초) | 동일 |
| `nvidia-smi` 비정상 종료 | 동일 |
| PID의 `/proc` 접근 불가 (권한, 소멸) | `None` 반환 → `last_cpu` fallback 사용 |
| CPU time 역전 (PID 재사용) | `max(0.0, ...)` 으로 음수 방지 |
| 스크립트 기동 시 이미 실행 중인 프로세스 | `first_cycle` 플래그로 오탐 방지 |
| `pynvml` 미설치 | `_NVML_AVAILABLE = False` → GPU time = 0.0 |
| NVML 초기화 실패 | 동일 |
| Accounting 모드 활성화 권한 없음 | polling 모드로 폴백 |
| NVML handle 조회 실패 | `None` 반환 → GPU time = 0.0 |
| 상태 파일 없음 / 파싱 실패 | 경고 로그 후 빈 상태로 시작 |
| 상태 파일 쓰기 실패 | 경고 로그, 서비스 계속 실행 |

---

## 의존 라이브러리

| 라이브러리 | 용도 | 설치 위치 |
|---|---|---|
| `psutil` | `/proc/<pid>/stat` 읽기, CPU time 계산 | RPM 내 vendoring (`lib/`) |
| `prometheus_client` | HTTP 서버 및 메트릭 타입 제공 | RPM 내 vendoring (`lib/`) |
| `pynvml` | GPU 시간 추적 (accounting/polling 모드) | RPM 내 vendoring (`lib/`) |

모든 라이브러리는 `build_rpm.sh` 빌드 단계에서 `pip install --target` 으로 `/usr/libexec/gpu-job-exporter/lib/` 에 vendoring됩니다.  
시스템 Python 환경을 오염시키지 않으며, EPEL 등 별도 저장소 설정이 필요하지 않습니다.  
`pynvml` 은 선택적 의존성으로, 미설치 시 GPU time 추적 기능만 비활성화되고 나머지 기능은 정상 동작합니다.
