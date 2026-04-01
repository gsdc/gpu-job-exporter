#!/usr/bin/env python3
"""
10분간 GPU를 사용하는 테스트 작업.
행렬 곱셈을 반복하여 GPU를 점유합니다.
"""

import time
import sys

DURATION_SECONDS = 10 * 60  # 10분

try:
    import torch
except ImportError:
    print("PyTorch가 설치되어 있지 않습니다. pip install torch 로 설치하세요.")
    sys.exit(1)

if not torch.cuda.is_available():
    print("CUDA를 사용할 수 없습니다. GPU 드라이버/PyTorch CUDA 빌드를 확인하세요.")
    sys.exit(1)

device = torch.device("cuda:0")
print(f"GPU: {torch.cuda.get_device_name(0)}")
print(f"테스트 시작 — {DURATION_SECONDS // 60}분간 행렬 곱셈을 반복합니다.")

# 큰 행렬 할당 (VRAM 점유)
SIZE = 4096
a = torch.randn(SIZE, SIZE, device=device)
b = torch.randn(SIZE, SIZE, device=device)

start = time.time()
iterations = 0

while True:
    elapsed = time.time() - start
    if elapsed >= DURATION_SECONDS:
        break

    # 행렬 곱셈 반복
    c = torch.matmul(a, b)
    torch.cuda.synchronize()
    iterations += 1

    remaining = int(DURATION_SECONDS - elapsed)
    print(f"\r  경과: {int(elapsed)}s / {DURATION_SECONDS}s | 남은 시간: {remaining}s | 반복: {iterations}회", end="", flush=True)

print(f"\n완료 — 총 {iterations}회 반복, 소요 시간: {time.time() - start:.1f}s")
