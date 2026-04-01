# 코드 상세 설명 — gpu_job_exporter.py

---

## 전체 흐름

```
main()
 │
 ├─ start_http_server(EXPORTER_PORT)       # Prometheus 엔드포인트 개시
 │
 └─ 무한 루프 (POLL_INTERVAL 초 간격)
      │
      ├─ query_gpu_processes()             # nvidia-smi 실행 → 현재 PID 목록
      │     └─ 실패 시 None 반환 → 이번 사이클 skip (상태 보존)
      │
      ├─ process_finished()               # 사라진 PID 감지 → 메트릭 기록
      │
      └─ update_tracked()                 # 신규 PID 등록 / 기존 PID CPU time 갱신
```

---

## 설정값

```python
POLL_INTERVAL = float(os.environ.get("GPU_EXPORTER_POLL_INTERVAL", 2))
EXPORTER_PORT = int(os.environ.get("GPU_EXPORTER_PORT", 9101))
```

환경변수가 없으면 기본값(`2`초, `9101`포트)을 사용합니다.  
systemd 유닛 파일의 `Environment=` 지시어로 주입됩니다.

---

## 상태 관리 자료구조

### `ProcessEntry` (dataclass)

```python
@dataclass
class ProcessEntry:
    gpu_uuid:      str    # GPU 식별자 (nvidia-smi 제공)
    process_name:  str    # 프로세스 이름 (nvidia-smi 제공)
    baseline_cpu:  float  # 이 PID를 처음 발견했을 때의 누적 CPU time (초)
    last_cpu:      float  # 가장 최근에 읽은 누적 CPU time (종료 시 fallback용)
```

### `tracked` 딕셔너리

```
tracked: dict[int, ProcessEntry]
  key   = PID (int)
  value = ProcessEntry
```

메인 루프 전체에서 공유되는 단일 딕셔너리입니다.  
신규 PID는 `update_tracked()`에서 추가되고, 종료 PID는 `process_finished()`에서 제거됩니다.

---

## 함수별 설명

### `_read_cpu_time(pid) → float | None`

```python
p = psutil.Process(pid)
t = p.cpu_times()
return t.user + t.system
```

- `/proc/<pid>/stat` 을 읽어 해당 프로세스의 누적 CPU time(user + system)을 초 단위로 반환합니다.
- `NoSuchProcess` / `AccessDenied` / `ZombieProcess` 예외는 모두 `None` 반환으로 처리하여 호출부에서 분기합니다.
- 커널 스레드(uid=0)가 아닌 일반 GPU 작업은 `/proc/<pid>/stat` 이 world-readable이므로 `gpu-exporter` 계정으로도 읽을 수 있습니다.

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
  └─ ProcessEntry 생성 후 tracked에 추가

기존 PID (tracked에 이미 있음)
  └─ _read_cpu_time() 로 last_cpu 갱신
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

4. Prometheus 메트릭 업데이트
     gpu_job_completed_total          += 1
     gpu_job_cpu_time_seconds_total   += cpu_used
     gpu_job_duration_seconds.observe(cpu_used)

5. 로그 출력
     [2026-04-01 15:10:22] [Finished] PID: 18432 (python3) | GPU: GPU-a1b2... | Used CPU Time: 120.53s
```

`max(0.0, ...)` 을 사용하는 이유:  
PID 재사용(pid wrap-around) 등 극히 드문 상황에서 `final_cpu < baseline_cpu` 가 되는 것을 방지합니다.

---

### `main()`

```python
first_cycle = True

while True:
    current_pids = query_gpu_processes()

    if current_pids is None:          # nvidia-smi 실패 → skip
        time.sleep(POLL_INTERVAL)
        continue

    if not first_cycle:               # 첫 사이클은 비교 대상 없으므로 skip
        process_finished(tracked, current_pids)

    update_tracked(tracked, current_pids)
    first_cycle = False
    time.sleep(POLL_INTERVAL)
```

`first_cycle` 플래그가 필요한 이유:  
스크립트 시작 시점에 이미 실행 중인 프로세스들은 "이전 사이클에 있었다"는 기록이 없습니다.  
첫 사이클에서 `process_finished()`를 호출하면 이 프로세스들이 전부 종료된 것으로 잘못 집계됩니다.

---

## Prometheus 메트릭 상세

### `gpu_job_completed_total` (Counter)

| 항목 | 내용 |
|---|---|
| 타입 | Counter |
| 라벨 | `gpu_uuid`, `process_name` |
| 의미 | 해당 GPU에서 해당 이름의 프로세스가 종료된 누적 횟수 |

### `gpu_job_cpu_time_seconds_total` (Counter)

| 항목 | 내용 |
|---|---|
| 타입 | Counter |
| 라벨 | `gpu_uuid`, `process_name` |
| 의미 | 종료된 작업들이 소모한 CPU time의 누적 합계 (초) |
| 활용 | `rate(gpu_job_cpu_time_seconds_total[5m])` → 분당 평균 CPU 사용률 추이 |

### `gpu_job_duration_seconds` (Summary)

| 항목 | 내용 |
|---|---|
| 타입 | Summary |
| 라벨 | `gpu_uuid`, `process_name` |
| 파생 메트릭 | `_count` (종료 건수), `_sum` (CPU time 합계) |
| 의미 | 개별 작업의 CPU time 분포 파악 |
| 활용 | `gpu_job_duration_seconds_sum / gpu_job_duration_seconds_count` → 작업당 평균 CPU time |

> Summary는 Exporter 프로세스 내에서만 분위수(quantile)를 계산합니다.  
> 서버 간 집계가 필요하다면 Prometheus 서버에서 `histogram_quantile()` 을 쓸 수 있는 Histogram 타입으로 교체를 검토하세요.

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

---

## 의존 라이브러리

| 라이브러리 | 용도 | 설치 위치 |
|---|---|---|
| `psutil` | `/proc/<pid>/stat` 읽기, CPU time 계산 | RPM 내 vendoring (`lib/`) |
| `prometheus_client` | HTTP 서버 및 메트릭 타입 제공 | RPM 내 vendoring (`lib/`) |

두 라이브러리 모두 `build_rpm.sh` 빌드 단계에서 `pip install --target` 으로 `/usr/libexec/gpu-job-exporter/lib/` 에 vendoring됩니다.  
시스템 Python 환경을 오염시키지 않으며, EPEL 등 별도 저장소 설정이 필요하지 않습니다.
