# Geppetto Python SDK v0.2

학습 코드에서 모델과 실행을 등록하고, 설정·epoch·지표·결과 파일을 기록합니다.
Python 3.9 이상, 외부 런타임 의존성 없음. PyTorch/Ultralytics는 기존 학습 환경의 것을 사용합니다.

## 설치와 인증

Settings → Python SDK에서 wheel을 다운로드하고 프로젝트 및 Environment를 선택해 API 키를 발급합니다.
키는 한 번만 표시되며 30일 후 만료됩니다. Settings에서 폐기·재발급할 수 있습니다.
실행 중인 SDK 키를 교체하려면 기존 기록의 run ID를 보존하고 새 키로 `attach()`합니다.

```bash
pip install geppetto_sdk-*.whl
export GEPPETTO_URL='https://your-geppetto.example'
export GEPPETTO_API_KEY='발급받은 프로젝트 키'
# 자체 인증서 사용 시 학습 호스트에 전달한 CA 파일:
export GEPPETTO_CA_CERT='/path/to/ca.crt'
```

이 설치에는 PyPI 배포가 필요하지 않습니다. 소스 checkout에서는 `pip install ./sdk`도 가능합니다.
원격 호스트/Pod에서는 해당 실행 환경에서 도달 가능한 HTTPS 주소와 그 주소에 유효한 인증서를 사용해야 합니다.
SDK는 TLS 인증서를 검증하고 리다이렉트를 거부합니다. HTTP는 loopback에서만 허용합니다.
키를 코드·Git·config에 넣지 말고 환경변수 또는 Kubernetes Secret으로 전달합니다.

## 학습 코드

```python
import geppetto_sdk as geppetto

config = {"epochs": 245, "imgsz": 1920, "batch": 16}
with geppetto.init(
    project="project-ball-detection",   # 발급한 키의 프로젝트 ID 또는 이름
    node_id="실제 Environment ID",     # Settings에서 생성한 코드에 포함
    name="YOLO26 P2",
    architecture="YOLO26 P2",
    dataset_id="등록한 데이터 버전 ID",
    config=config,
) as run:
    print(run.id, run.model_id)         # 재연결에 사용할 run ID를 보존
    for epoch in range(1, config["epochs"] + 1):
        run.progress(epoch, total_epochs=config["epochs"])
        loss, map50 = train_one_epoch(epoch)  # 사용자의 실제 학습 함수
        run.log({"train/loss": loss, "metrics/mAP50(B)": map50}, step=epoch, epoch=epoch)
    run.log_artifact("best.pt", role="inference_weights")
```

`init()`마다 독립 모델 한 개와 실행 한 개를 만듭니다. `name`은 표시 이름, `architecture`는 아키텍처 식별 정보입니다.
Environment 지정은 기록의 소속을 정하며, 해당 노드에 프로세스를 배치하거나 GPU를 예약하지 않습니다.
`dataset_id`를 지정하면 서버에 등록된 버전 식별자를 고정합니다. 파일 경로만 config에 넣은 경우 데이터 검증을 주장하지 않습니다.
config는 JSON 객체이며 64 KiB 이하여야 합니다. config·데이터 버전은 실행 중 변경할 수 없습니다.
같은 프로젝트에서 같은 이름의 지표를 기록하면 기존 D3 그래프에서 비교할 수 있습니다.

## 호출 계약

| API | 동작 |
| --- | --- |
| `init(...)` | 새 학습 등록. `project`, `node_id`, `dataset_id`, `architecture`, `name`, `config` 지정 |
| `run.log(metrics, step=..., epoch=...)` | 유한한 숫자 지표 기록. step 생략 시 0부터 증가. epoch는 명시한 경우만 표시 |
| `run.progress(epoch, total_epochs, phase="training")` | 실제 현재 epoch 기록. phase는 `training` 또는 `validation` |
| `run.log_artifact(path, role=...)` | 파일 바이트 업로드, 크기·SHA-256 검증 후 게시 |
| `run.finish("succeeded" / "failed" / "stopped")` | 실행 종료. 학습 프로세스를 종료하지 않음 |
| `attach(run_id, ...)` | 기존 실행에 다시 연결. 자동 step은 마지막 기록 다음 값 |

step·epoch는 역행할 수 없습니다. 같은 metric/step의 값은 바꿀 수 없습니다.
같은 step에 서로 다른 metric을 추가하는 것은 가능합니다. epoch 0은 준비 단계, 1부터 실제 학습 epoch로 사용하면 됩니다.
한 호출 최대 100개 지표, 실행당 최대 200개 지표명, 지표당 최대 100,000개 점을 받습니다.
미니배치마다 모든 값을 보내기보다 집계한 값이나 epoch 지표를 기록하세요.

컨텍스트 정상 종료는 succeeded, 예외 종료는 failed로 기록합니다. 실패 기록 자체가 안 되면 원래 학습 예외를 보존합니다.
`with` 없이 사용할 때는 반드시 `finish()`를 호출하세요. 자동 heartbeat는 30초 간격이며
프로세스가 갑자기 죽으면 상태를 성공/실패로 추측하지 않습니다. 오래된 heartbeat는 화면의 실행 애니메이션을 멈춥니다.
일반 기록 실패는 `TrackingError`를 발생시킵니다. 오프라인 큐는 없습니다.
네트워크 오류와 HTTP 429/502/503/504는 동일 요청 ID로 최대 세 번 전송하므로 응답 유실로 모델·지표가 중복되지 않습니다.

## 재연결·재개·평가

```python
# 원래 학습 프로세스가 종료된 것을 확인한 뒤, SIGKILL 등으로 남은 상태를 정리:
geppetto.attach(previous_run_id, heartbeat_interval=0).finish("failed", message="Process exited")

# 체크포인트 로딩과 optimizer/scheduler 복구는 학습 코드가 수행:
with geppetto.init(job_type="resume", parent_run_id=previous_run_id,
                   name="YOLO26 P2 · 체크포인트 복구 학습") as run:
    train_from_checkpoint(run)  # 사용자의 실제 복구 함수

# 같은 모델의 평가 기록:
with geppetto.init(job_type="evaluate", parent_run_id=completed_run_id) as run:
    run.log(evaluate_checkpoint(), step=0)
```

resume/evaluate는 상위 실행의 모델 ID를 유지합니다. resume는 실패·중단된 SDK 실행에만 가능하며,
기존 설정·데이터 버전·아키텍처·Environment를 유지합니다. 같은 모델의 학습이 이미 진행 중이면 거부합니다.
새 resume 실행의 step은 별도 축입니다. 실제 이어지는 epoch를 코드에서 보내세요.
평가도 같은 아키텍처·Environment를 사용하며 별도 평가 데이터 버전을 지정할 수 있습니다.
MLflow에서 가져온 기존 실행은 SDK가 수정하지 않습니다. 최초 SDK 적용 학습부터 이 계약을 사용합니다.

## 파일과 분산 학습

artifact role: `checkpoint`, `inference_weights`, `evaluation`, `configuration`.
파일당 최대 4 GiB, 1 MiB 단위 업로드, 서버 전체 SDK 파일 예약 용량 기본 1 GiB입니다.
서버 `GEPPETTO_SDK_QUOTA_BYTES`로 용량을 조정할 수 있으며 PVC의 실제 여유 공간을 고려해야 합니다.
검증이 완료되지 않은 업로드는 결과 목록에 나오지 않습니다. 결과 다운로드는 실행 종료 후 가능합니다.
업로드 완료는 optimizer 등 완전 재개 상태를 보증하지 않으므로 자동 재개 가능 표시를 하지 않습니다.

DDP에서 각 trainer 프로세스가 코드를 호출할 경우 `RANK=0`(또는 단일 프로세스의 -1)만 기록합니다.
다른 rank는 no-op Run을 받습니다. `enabled=False`로 명시적으로 끌 수도 있습니다.
여러 프로세스가 같은 run에 쓰면 안 됩니다. `attach()`도 원래 writer가 종료된 뒤 사용하세요.
Ultralytics 자동 subprocess 실행에서는 상위 프로세스의 콜백이 자식에게 전달된다고 가정하지 말고,
실제 trainer 프로세스 안에서 SDK를 초기화해야 합니다.

```python
from geppetto_sdk.ultralytics import attach
with geppetto.init(name="YOLO26 P2", architecture="YOLO26 P2", config=config) as run:
    attach(model, run)  # 기존 YOLO 인스턴스; torch/ultralytics를 SDK가 설치하지 않음
    model.train(**config)
    if run.enabled:
        run.log_artifact(model.trainer.best, role="inference_weights")
```

콜백은 train loss, validation 지표, learning rate, epoch를 기록합니다.
최종 검증에서 같은 epoch 콜백이 반복되는 것은 중복 기록하지 않습니다.
일반 루프/YOLO 통합 예제는 `examples/sdk/train.py`, `examples/sdk/yolo.py`에 있습니다.
현재 설치된 학습 코드와 실제 GPU 학습을 변경하거나 실행하지 않고 콜백 계약을 테스트했습니다.

## HTTP 명세와 권한

모든 요청은 JSON, `Authorization: Bearer <SDK token>`, `X-Geppetto-Request: 1`입니다.
쓰기 body의 `request_id`는 32자리 소문자 UUID hex이며 동일 재전송은 동일 body를 사용합니다.

- `POST /api/tracking/runs`: init 인자와 request_id → id, model_id, project_id, state, last_step.
- `POST /api/tracking/runs/{id}/inspect`: body `{}` → 위 실행 정보.
- `POST /api/tracking/runs/{id}/events`: kind=log/progress/heartbeat/finish 및 해당 API 인자.
- `POST /api/tracking/runs/{id}/artifacts`: action=start(name,size,sha256,role), chunk(offset,data:base64), complete.
  action=abort는 미완료 업로드를 삭제하고 예약 용량을 반환합니다.
  동일 업로드는 같은 request_id를 사용합니다. 재전송 chunk의 바이트가 다르면 거부합니다.

SDK 키는 한 프로젝트의 SDK 기록에만 접근합니다. 관리자 API·원본 MLflow 기록·학습 중단 권한은 없습니다.
Settings의 키 발급/조회/폐기는 기존 관리자 로그인이 필요합니다. 서버에는 키의 SHA-256만 저장합니다.
이 SDK는 코드에서 실행 중인 학습을 추적합니다. UI의 원격 Start training은 별도의 실행 어댑터 연결 후 활성화됩니다.

## 타입별 평가 기록 (SDK 0.2)

`run.log_evaluation(dataset_id=..., type="classification", policy=..., samples=...)`로
평가 결과를 등록합니다. `policy`에는 `evaluator_version`, `split`, `checkpoint_sha256`을 지정하고,
샘플은 등록 데이터의 `index`, `target`, `prediction`, `confidence`, 선택적 `slice`를 포함합니다.
`type="keypoints"`는 두 좌표를 `[x,y]`로 제공하며 `coordinate_unit`도 지정합니다.
Manage → 평가 분석에서 샘플·혼동 건수·조건별 성능을 확인합니다.
이는 제공된 예측 기록이며 SDK가 추론이나 체크포인트 복원을 실행하지 않습니다.


## 0.3 진단 기록

`Run.log_histogram`, `Run.log_embedding`, `Run.log_trace`와 선택형 TensorBoard/Chrome trace import를 지원합니다. 입력 계약과 예시는 [PLATFORM_0_3.md](PLATFORM_0_3.md)를 참조하세요.
