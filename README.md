# gpu-job-exporter

NVIDIA GPU 서버에서 실행 중인 프로세스를 감시하여, 작업이 종료될 때마다 종료 건수와 CPU 사용 시간을 Prometheus 메트릭으로 노출하는 경량 Python Exporter입니다.

---

## 파일 구성

```
gpu_job_exporter/
├── gpu_job_exporter.py       # 메인 Python 스크립트
├── gpu-job-exporter.service  # systemd 유닛 파일
├── gpu-job-exporter.spec     # RPM 스펙 파일
├── build_rpm.sh              # RPM 빌드 자동화 스크립트
├── requirements.txt          # pip 의존성 목록
├── README.md                 # 빌드 및 운영 가이드 (이 파일)
└── CODE.md                   # 코드 상세 설명
```

---

## 사전 요구사항

| 항목 | 최소 버전 | 비고 |
|---|---|---|
| OS | AlmaLinux / RHEL / CentOS 9 | `el9` 기준 |
| Python | 3.9 이상 | `python3` |
| NVIDIA Driver | 드라이버 설치 및 `nvidia-smi` 사용 가능 상태 | |
| rpm-build | — | RPM 빌드 시 필요 |
| python3-pip | — | 의존성 vendoring 시 필요 |

---

## RPM 빌드

### 1. 빌드 도구 설치 (최초 1회)

```bash
sudo dnf install -y rpm-build python3-pip
```

### 2. RPM 빌드 실행

```bash
cd gpu_job_exporter/
bash build_rpm.sh
```

빌드 스크립트가 자동으로 수행하는 작업:

1. `prometheus_client`, `psutil` 을 `lib/` 디렉터리에 vendoring
2. 소스 타르볼 (`gpu-job-exporter-1.0.0.tar.gz`) 생성
3. `~/rpmbuild/` 트리에 파일 배치
4. `rpmbuild -ba` 실행

빌드 완료 시 출력 예시:

```
=== Build complete ===
  ~/rpmbuild/RPMS/noarch/gpu-job-exporter-1.0.0-1.el9.noarch.rpm
  ~/rpmbuild/SRPMS/gpu-job-exporter-1.0.0-1.el9.src.rpm
```

---

## 설치

### 단일 서버

```bash
sudo dnf install -y ~/rpmbuild/RPMS/noarch/gpu-job-exporter-1.0.0-1.el9.noarch.rpm
```

### 14대 서버 일괄 배포

RPM 파일을 빌드 서버에서 한 번만 빌드한 뒤 각 서버에 복사하여 설치합니다.

```bash
# 각 서버에 RPM 파일 복사
for i in $(seq -w 1 14); do
    scp ~/rpmbuild/RPMS/noarch/gpu-job-exporter-1.0.0-1.el9.noarch.rpm \
        user@gpu-server-${i}:
done

# 각 서버에서 설치 및 서비스 시작
for i in $(seq -w 1 14); do
    ssh user@gpu-server-${i} \
        "sudo dnf install -y ./gpu-job-exporter-1.0.0-1.el9.noarch.rpm && \
         sudo systemctl enable --now gpu-job-exporter"
done
```

설치 후 자동으로 생성되는 항목:

- 실행 파일: `/usr/bin/gpu-job-exporter`
- 메인 스크립트: `/usr/libexec/gpu-job-exporter/gpu_job_exporter.py`
- 의존 라이브러리: `/usr/libexec/gpu-job-exporter/lib/`
- systemd 유닛: `/usr/lib/systemd/system/gpu-job-exporter.service`
- 서비스 계정: `gpu-exporter` (시스템 유저, 자동 생성)

---

## 서비스 관리

```bash
# 시작 / 중지 / 재시작
sudo systemctl start   gpu-job-exporter
sudo systemctl stop    gpu-job-exporter
sudo systemctl restart gpu-job-exporter

# 부팅 시 자동 시작 등록 / 해제
sudo systemctl enable  gpu-job-exporter
sudo systemctl disable gpu-job-exporter

# 상태 확인
sudo systemctl status  gpu-job-exporter

# 실시간 로그 확인
sudo journalctl -fu gpu-job-exporter
```

---

## 환경변수 설정

`/usr/lib/systemd/system/gpu-job-exporter.service` 에 기본값이 지정되어 있습니다.

| 환경변수 | 기본값 | 설명 |
|---|---|---|
| `GPU_EXPORTER_POLL_INTERVAL` | `2` | nvidia-smi 폴링 간격 (초, 소수점 허용) |
| `GPU_EXPORTER_PORT` | `9101` | Prometheus 메트릭 노출 포트 |

### 값 변경 방법 (권장 — 패키지 업그레이드 후에도 유지됨)

```bash
sudo systemctl edit gpu-job-exporter
```

편집기에 아래 내용을 입력하고 저장합니다:

```ini
[Service]
Environment=GPU_EXPORTER_POLL_INTERVAL=5
Environment=GPU_EXPORTER_PORT=9200
```

```bash
sudo systemctl daemon-reload
sudo systemctl restart gpu-job-exporter
```

> `systemctl edit` 은 `/etc/systemd/system/gpu-job-exporter.service.d/override.conf` 파일을 생성합니다.
> 패키지를 재설치하거나 업그레이드해도 이 파일은 삭제되지 않습니다.

---

## 메트릭 확인

```bash
curl -s http://localhost:9101/metrics | grep gpu_job
```

출력 예시:

```
# HELP gpu_job_completed_total Total number of completed GPU compute jobs
# TYPE gpu_job_completed_total counter
gpu_job_completed_total{gpu_uuid="GPU-a1b2c3d4",process_name="python3"} 5.0

# HELP gpu_job_cpu_time_seconds_total Total CPU time consumed by completed GPU jobs
# TYPE gpu_job_cpu_time_seconds_total counter
gpu_job_cpu_time_seconds_total{gpu_uuid="GPU-a1b2c3d4",process_name="python3"} 612.4

# HELP gpu_job_duration_seconds CPU time distribution of individual completed GPU jobs
# TYPE gpu_job_duration_seconds summary
gpu_job_duration_seconds_count{gpu_uuid="GPU-a1b2c3d4",process_name="python3"} 5.0
gpu_job_duration_seconds_sum{gpu_uuid="GPU-a1b2c3d4",process_name="python3"} 612.4
```

---

## 제거

```bash
sudo systemctl disable --now gpu-job-exporter
sudo dnf remove gpu-job-exporter
# override.conf 도 삭제할 경우
sudo rm -rf /etc/systemd/system/gpu-job-exporter.service.d/
sudo systemctl daemon-reload
```
# gpu-job-exporter
