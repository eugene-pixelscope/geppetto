> 이 문서는 0.2 개발 시점 기록입니다. 실제 운영 적용과 추가 기능은 [PLATFORM_0_3.md](PLATFORM_0_3.md) 및 [운영 검증](KUBERNETES_OPERATIONS.md)의 0.3 항목을 참조하세요.

# 제페토 0.2 · 여러 노드의 독립 학습

중앙 서버에서 여러 Kubernetes 노드를 관리하고, 각 노드에서는 **서로 다른 학습 Job**을 실행한다.
하나의 학습을 여러 노드에 분산하는 DDP 구성은 이번 실행 모델에 포함하지 않는다.
기존 Airflow 경로와 Agent 경로는 실행별 `frozen.execution.backend`로 분리한다.
Airflow worker는 Agent 작업을 제출하거나 중단하지 않는다.

이 변경은 소스 구현이다. 운영 서버 이미지·Agent·RBAC·학습 작업은 변경하지 않았다.

## 추가된 사용 흐름

메뉴 **Manage / 학습 관리**:

1. **노드 실행 설정**: 발견된 노드별 모델 이미지 digest, namespace, 데이터/결과 PVC, 동시 실행 수를 등록한다.
2. **데이터 버전**: 전체 이미지·라벨 검증을 요청하고 완료된 새 버전을 확인한다. 여러 노드의 데이터 위치를 같은 버전에 연결한다.
3. **평가 분석**: 분류/등번호 인식, 2차원 좌표 평가 JSON을 가져온다. 샘플·조건·혼동 건수·정확도/위치 오차를 탐색한다.
4. **자동 실험**: 기준 실행에서 grid/random 탐색 설정, seed, 최대 실험 수, 동시 실행 수와 목표 지표를 지정한다. 생성될 설정들을 확인한 뒤 시작한다.
5. **지표 정의**: 프로젝트별 지표 이름, 단위, 개선 방향, 마지막/최고/평균 대표값을 정의한다. 실험 비교표에 적용된다.

실행 상세의 메모와 태그는 저장되며 전체 Runs 검색에 포함된다.
기존 예약·반복·선행 성공 실행과 다중 실행 곡선 비교는 유지한다.
실행 설정이 활성화된 catalog 모드에서는 기존 최종 확인 학습 생성 화면을 사용할 수 있다.

## Agent 실행 설치

기존 `deploy/agent.example.yaml`은 조회 전용 기본값을 유지한다.
실행 기능은 **중앙 실행 설정과 Agent의 이미지/PVC 허용 목록을 모두** 구성해야 동작한다.

필요한 준비:

- 격리 검증한 학습 이미지를 레지스트리에 올리고 `repository@sha256:...`로 지정한다.
- 모델 이미지는 기존 `deploy/model.Dockerfile` 및 `integrations/model_runner.py` 계약을 따른다.
- 데이터 PVC는 모든 대상 노드에서 읽을 수 있어야 한다. 결과 PVC는 학습 노드와 Agent에서 동시에 접근 가능해야 한다.
- 여러 노드에서는 NFS/CSI 등의 **ReadWriteMany 공유 결과 PVC**를 사용한다. 단일 노드용 RWO 볼륨으로 대체했다고 가정하지 않는다.
- Agent는 자신과 같은 namespace의 Job만 관리한다. CPU/GPU 장치 할당은 Kubernetes scheduler/device plugin이 담당한다.
- 모든 학습은 중앙과 같은 논리적 MLflow 저장소를 사용한다. 중앙의 `GEPPETTO_MLFLOW_URL`은 다운로드 가능한 artifact proxy를 가리켜야 한다.

실행 권한 예제는 `deploy/agent-execution.example.yaml`, Deployment 변경 예제는
`deploy/agent-execution.patch.yaml`이다. namespace/PVC/image 항목을 실제 환경에 맞춰 채운다.
Role은 해당 namespace에서 Job get/create/delete, Pod 및 Pod log 읽기만 허용한다.
Job 생성 권한은 namespace 권한이므로 신뢰할 수 있는 전용 namespace를 사용한다.
Agent는 `/results`를 읽기 전용으로 마운트한다. 학습 Pod에는 ServiceAccount 토큰을 마운트하지 않는다.

### 데이터 등록 파일

중앙에 등록된 데이터의 전체 내용 검증을 완료한 후:

```bash
.venv/bin/python tools/export_dataset_registry.py \
  --database /path/to/catalog.sqlite3 \
  --dataset content-VERSION_ID \
  --container-root /datasets/soccer/ball-v1 \
  --output /staging/geppetto/datasets.json
```

이 도구는 데이터 원본을 복사하지 않고 manifest 메타데이터만 내보낸다.
데이터 PVC에 `soccer/ball-v1/` 원본과 `geppetto/datasets.json`을 배치한다.
중앙의 데이터 위치 연결에는 `root=soccer/ball-v1`을 지정한다.
학습 컨테이너는 등록 파일의 버전·경로와 원본 이미지·라벨 해시를 모두 검증한다.
각 노드에서 다른 경로를 쓸 경우 해당 실행의 등록 파일도 그 경로에 맞게 준비한다.

원격 전용 원본의 중앙 미리보기는 중앙에도 읽기 전용 공유 데이터 마운트가 필요하다.
Agent가 임의의 호스트 경로를 중앙에 공개하거나 원격 파일 탐색 프록시를 제공하지는 않는다.

## 실행과 장애 처리

- 제출/예약은 SQLite에 저장되고, Agent가 나가는 HTTPS 연결로 작업 의도를 조회한다.
- Job 이름은 중앙 run ID에서 결정된다. POST 응답 유실·중복 polling은 기존 Job과 spec hash를 확인한다.
- Agent는 Job UID를 로컬 상태 PVC에 저장한다. 중앙도 마지막 UID와 관측 sequence를 보관한다.
- 이미 관측한 Job이 사라지면 상태 확인 오류를 표시한다. 임의로 다시 학습하지 않는다.
- 노드명과 Node UID, PVC UID를 제출 전에 대조하고 노드 affinity를 적용한다.
- 연결 오류는 마지막 실행 상태를 보존한다. 중단 완료는 Job과 활성 Pod의 종료를 관측한 후 기록한다.
- 중단은 일치하는 소유 label/spec 및 UID 조건을 사용한 foreground Job 삭제다.
- 재시도 backoff는 0이다. 실패한 학습을 새 run으로 다시 실행하는 것은 명시적인 사용자 동작이다.
- 정상 Job 종료 뒤 `result.json` 게시까지 확인해야 성공으로 표시한다. 완료 파일은 MLflow proxy에서 해시를 검증해 다운로드한다.
- 모델 runner는 컨테이너의 주 프로세스로 실행하여 종료 신호를 직접 받는다.

현재 각 관리형 학습은 GPU 1장을 요청한다. 물리 GPU UUID 지정과 완전한 optimizer 재개는
검증된 계약을 추가하기 전까지 차단한다. 공 모델의 실행 상세에서는 완료된 체크포인트를 선택해
validation/test 평가 전용 Job을 만들 수 있다. 체크포인트 SHA-256, 데이터 버전, 해상도,
confidence/IoU, FP32 정책을 고정하고 원래 모델에 평가 이력을 연결한다. 학습 루프는 실행하지 않는다.
등번호 등 다른 모델의 평가기는 API/SDK로 이미 수행한 평가를 기록한다.

## 데이터 검증과 보존

검증은 별도 로컬 인덱스에서 64샘플씩 처리하며 재시작 후 이어서 처리한다.
원래 indexed 버전은 수정하지 않고 새 `content-*` 버전을 만든다.
이미지/라벨 SHA-256, 전체 manifest SHA-256, 분할 간 동일 이미지 내용, 명시된 YOLO 형식의
이미지 디코딩/box 라벨을 검사한다. pose keypoint 의미나 모든 모델의 사용자 라벨 스키마까지 검증하지는 않는다.
학습 시작 시 원본 변경을 다시 검사한다. 해시는 원본 보존을 대체하지 않으므로
재현에 필요한 원본 버전은 공유 저장소에서 불변으로 보존해야 한다.

## 평가 입력 예제

```json
{
  "run_id": "RUN_ID",
  "dataset_id": "DATASET_VERSION_ID",
  "type": "classification",
  "policy": {
    "evaluator_version": "jersey-evaluator-v1",
    "split": "test",
    "checkpoint_sha256": "64-character-lowercase-sha256"
  },
  "samples": [
    {"index": 0, "target": "12", "prediction": "21", "confidence": 0.82, "slice": "occluded"}
  ]
}
```

`index`는 등록된 해당 데이터 버전의 샘플 인덱스다. 같은 샘플을 중복 집계하지 않는다.
가져온 target/prediction은 평가기가 제공한 값이며 서버가 모델 추론을 수행했다는 의미가 아니다.
일부 샘플만 평가한 결과에는 전체 분할 대비 평가 범위를 표시한다.
`keypoints`는 target/prediction을 `[x,y]`로 제공하고 policy에 `coordinate_unit=pixels|normalized`,
선택적으로 `tolerance`를 지정한다. 서버는 평균/중앙값/P90/최대 위치 오차와 tolerance 이내 비율을 계산한다.
분류는 정확도, 정답→예측 혼동 건수, 클래스별 precision/recall/F1을 반환한다.
좌표 평가는 2차원 단일 점 기준이며 다중 관절·가림 masking 정책은 별도 평가기로 계산해야 한다.

## 검증과 현재 한계

자동 검증은 임시 DB/파일과 모의 Kubernetes HTTP API, 실제 로컬 HTTP/Chromium으로 수행한다.
`tests/test_platform.py`는 노드별 독립 dispatch, 동시 실행 상한, UID 충돌, 재시작, 중단 확인,
Agent 인증/sequence, sweep 멱등성, 내용 버전 생성·변조를 검증한다.
`tools/browser_platform.py`는 평가 가져오기·지표 등록·sweep 생성과 5개 탭 × 4개 화면 폭을 확인한다.

운영 Kubernetes/GPU/CSI/이미지 레지스트리의 실제 연결 검증은 남아 있다.
중앙 서버는 단일 인스턴스 SQLite 구성이다. 여러 중앙 replica/HA/PostgreSQL 이관은 포함하지 않는다.
자동 탐색은 grid/random과 trial 수 예산을 지원한다. Bayesian/Hyperband, 자동 조기 중단,
금액·GPU 시간 예산, 분산 학습, TensorBoard histogram/embedding/profiler의 전체 복제는 포함하지 않는다.
MLflow 모델 버전 등록은 기존 runner를 사용하며, 자동 운영 배포 승격은 수행하지 않는다.

Kubernetes 동작 참고: [Jobs](https://kubernetes.io/docs/concepts/workloads/controllers/job/),
[Node affinity](https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/).
