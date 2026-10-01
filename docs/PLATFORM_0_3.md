# Geppetto 0.3 — 운영 Agent와 학습 관리

Kubernetes Agent를 실행 경로로 통일한다. 현재 운영 대상은 mlops의 A100 8개 서버 한 대이며,
작업마다 GPU 한 개를 Kubernetes device plugin으로 자동 할당한다. 여러 서버를 사용하려면
각 서버가 속한 클러스터에 Agent, 데이터/결과 PVC, 허용 이미지 digest를 연결한다.
동일 모델의 DDP 분산 학습 및 물리 GPU UUID 지정은 이번 실행 계약에 포함하지 않는다.

## 추가 기능

- **공 모델 epoch 재개**: 완료된 save callback에서 모델, optimizer, scheduler, AMP scaler,
  EMA, early-stopping 상태, Python/NumPy/Torch/CUDA RNG를 원자적으로 저장한다.
  중단/실패한 실행의 검증된 `resume.pt`만 사용하고 코드·데이터 버전·이미지 digest를 고정한다.
  이전 실행을 덮어쓰지 않고 같은 모델에 새 실행을 생성한다. 이미 전송 중인 DataLoader 배치는
  복원하지 않으므로 비트 단위로 중단 없는 실행과 동일하다는 보장은 하지 않는다.
  기존 외부 체크포인트와 등번호 모델 체크포인트를 완전 재개 가능하다고 추정하지 않는다.
- **자동 실험**: grid, seeded random, 명시적인 후보 집합에 대한 discrete TPE.
  초기 4개 성공 결과 이후 좋은/나쁜 후보의 범주형 Parzen 밀도 비율로 다음 후보를 고른다.
  실행 후보는 제출 전 검토한 최대 100개다. 연속 공간의 무제한 Bayesian 최적화가 아니다.
  비동기 successive-halving으로 낮은 중간 성능의 실행을 중단할 수 있다.
  Hyperband는 여러 bracket에 후보를 배분하고 bracket별로 비동기 successive-halving을 수행한다.
  최대 후보 수 안에서 bracket별 할당 수를 조정하며 현재 공 모델의 epoch index를 자원 단위로 사용한다.
  max_step=9이면 최종 학습 상한은 10 epochs다. 최고 rung에서는 중단하지 않고 정상 완료를 기다린다.
- **예산**: 실행 시작부터 종료까지 관측한 GPU 개수×초와 사용자가 지정한 시간당 요금 기반
  예상 비용 상한. Agent 폴링과 graceful shutdown 동안 초과할 수 있으며 청구 금액은 아니다.
  `limits: {gpu_seconds: 3600, hourly_rate: 2, cost: 2}`; 시간당 요금 없이는 비용 예산을 허용하지 않는다.
- **파이프라인**: 최대 50개 학습 단계의 DAG, 여러 선행 단계, 순환 참조 거부,
  전 단계의 frozen 설정 원자적 등록, 실패/중단 시 하위 단계 skipped.
  각 단계는 기준 실행의 모델·데이터·노드로 **새 학습**을 한다. 앞 단계의 출력 모델을 자동 입력으로
  전달하는 범용 ETL DAG가 아니다. 재실행은 검토 후 새 파이프라인 ID로 생성한다.
- **모델 레지스트리**: 이 카탈로그의 성공한 MLflow 모델 버전만 별칭 지정 대상으로 선택한다.
  현재 별칭과 대상 버전을 검토한 뒤 변경하고 전/후 버전·미확인 오류를 기록한다.
  이전 버전을 선택하여 롤백한다. 모델 서버를 배포하거나 서비스 트래픽을 변경하지 않는다.
  MLflow 외부 클라이언트와의 원자적 CAS는 제공하지 않으므로 외부 동시 변경은 관측 후 보고한다.
- **진단 시각화**: 히스토그램, 레이블 포함 2D 임베딩 좌표, CPU/GPU profiler duration trace.
  SDK 기록, JSON 가져오기, 실행별 SVG 표시와 JSON 내보내기를 제공한다.
  임베딩 차원 축소는 학습 환경에서 수행하고 projection 종류를 명시한다.
  `geppetto_sdk.tensorboard.import_events`는 선택 의존성 TensorBoard 이벤트의 scalar/histogram을,
  `import_chrome_trace`는 PyTorch/Chrome의 `ph=X` 이벤트를 가져온다.

## SDK

```python
run.log_histogram("weights", values, step=epoch, bins=32)
run.log_embedding("features", [{"x": 0.1, "y": 0.3, "label": "sample-1"}],
                  step=epoch, projection="pca")
run.log_trace("profile", [{"name": "forward", "lane": "GPU 0",
                           "start_us": 0, "duration_us": 1200}])
```

진단 한 항목은 histogram 256 bins, embedding 5,000 points, trace 10,000 events/128 lanes까지,
실행당 최대 1,000개이며 동일 이름·종류·step은 내용 불변이다. 실제 기록이 없으면 빈 상태로 표시한다.

## 운영 파일과 저장소

- `deploy/kubernetes.mlops.yaml`: 중앙 서버와 MLflow 동기화. 기존 운영 SQLite/PVC 유지.
- `deploy/agent.mlops.yaml` + `agent-execution.mlops.yaml` + `agent-execution.patch.yaml`: Agent 실행 권한,
  digest 허용 목록, 결과 PVC 읽기 전용 관측. Agent는 자기 namespace의 Job만 생성·중단한다.
- `deploy/agent-storage.mlops.yaml`: 현 단일 노드의 기존 `/datasets` 마운트를 local PV로 노출하고
  결과는 별도 PVC에 기록한다. 새 서버의 스토리지 구성은 해당 서버에 맞게 준비해야 한다.
- 이미지 레지스트리는 minikube 노드 안의 `127.0.0.1:5001`에만 바인딩된 Distribution registry다.
  데이터는 Docker named volume `geppetto-registry`에 유지한다. 외부 서버용 공유 레지스트리가 아니다.
- 모델 이미지는 `tools/build_model_image.py`로 소스 manifest에 있는 모든 파일을 검증하고 포함한다.
  같은 이름의 태그로 이미지를 덮어쓰지 않으며 실행은 digest를 고정한다.
- 데이터 내용 버전은 원본을 복사하지 않는다. 원본 변경 시 실행 전 재검증에서 거부한다.
  장기 재현성을 위해서는 운영 스토리지의 snapshot 또는 내용별 별도 경로를 유지해야 한다.

전체 서비스는 여전히 단일 중앙 SQLite 배포다. Postgres HA, 조직별 RBAC/SSO, 범용 ETL,
TensorBoard 전체 플러그인, 네 도구의 모든 기능에 대한 호환성을 제공한다고 주장하지 않는다.

## 검증 기록

최종 실제 실행 ID와 배포 이미지는 `KUBERNETES_OPERATIONS.md`의 0.3 운영 검증 항목에 기록한다.
운영 검증 데이터는 원본의 train 24장/validation 12장/test 12장으로 만든 별도 버전이며
모델의 전체 성능 비교나 품질 판단에 사용하지 않는다.
