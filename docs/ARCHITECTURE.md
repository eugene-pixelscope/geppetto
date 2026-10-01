# 구조와 정책

## 구현 결정

개인 단일 인스턴스 앱으로 FastAPI + SQLite WAL + ES module/CSS를 선택했다.
별도 프런트 빌드나 외부 CDN 없이 동작하며, 한국어 글꼴도 로컬에서 제공한다.
운영 모델 패키지는 웹 프로세스에 설치하거나 import하지 않는다.
Pydantic은 HTTP 프레임워크의 의존성으로 사용되지만 모델 Config는 별도 환경에서만 실행한다.

- `geppetto/app.py`: HTTP·접근 통제·다운로드, 앱 수명주기에 속한 워커.
- `storage.py`: schema user_version=1, JSON 문서와 멱등성 키, 트랜잭션.
- `service.py`: immutable 설정 스냅샷, 데이터 등록, 제출, 예약, 비교.
- `demo.py`: **synthetic** 데이터·실험과 모의 자원 관리. 실제 학습이 아님.
- `adapters.py`, `worker.py`: 명시적 REST 연결, 전용 DAG 제출·조정·중단.
- `files.py`, `remote_files.py`: 내용 manifest, 루트 제한, 완료 파일 스풀.
- `contracts/templates/`: 핵심 코드를 수정하지 않고 추가하는 모델 계약.
- `integrations/model_runner.py`: 모델 이미지 안에서만 사용하는 공·등번호 어댑터.

## 저장과 상태

`documents(kind,id,body)`에 dataset/template/run/schedule을 저장한다.
`requests(key,digest,kind,id)`는 같은 키의 다른 요청을 거부한다.
BEGIN IMMEDIATE 트랜잭션에서 예약 occurrence 생성과 next_at 이동을 함께 커밋한다.
Airflow 제출 ID는 `geppetto-<submission_id>`로 결정적이며 409는 기존 실행 조회로 복구한다.
외부 호출 후 로컬 커밋 전 크래시가 발생해도 같은 DAG run을 재사용한다.
SQLite 백업은 DB 파일 단독 복사가 아닌 `tools/backup.py`의 online backup으로 수행한다.
여러 웹 worker/replica를 통한 분산 실행은 현재 배포 범위가 아니다.

제페토는 사용자 의도·설정과 예약 정의를, Airflow는 실제 작업 실행을 소유한다.
예약의 due occurrence를 제페토 워커가 Airflow에 제출하며, 학습 실행은 브라우저와 독립적이다.
MLflow에 기록한 값과 모델 결과 manifest를 화면용으로 복제하되 원본 run 연결을 유지한다.
조회 오류는 fetch_error와 observed_at에 기록하고 마지막 성공 상태를 failed로 바꾸지 않는다.

## 실행·예약 정책

- 즉시/지정시각/5필드 cron/선행 성공 후를 지원한다. 지정시각은 오프셋 필수다.
- 시간대는 IANA zone, 화면은 해당 예약 시간대로 다음 실행 시각을 표시한다.
- 반복 충돌은 skip, 서버 중단으로 여러 시각을 놓친 경우 coalesce(한 번)한다.
- 기본 동시 실행 상한 1, FIFO, 자동 모델 재시도 0. 통신 재시도는 같은 제출 ID를 유지한다.
- 데이터/코드/스키마/최종 설정은 예약 생성 시 고정하며 최신 버전을 자동 추종하지 않는다.
- 기존 실행을 선점하지 않는다. 요청 UUID는 임의 장치로 변경하지 않는다.
- 중단은 정확한 이름 확인 후 의도를 저장한다. Pod 삭제는 submission label과 UID 조건을 사용한다.
- 중단 intent는 재시작 후에도 조정하며 늦게 만들어진 동일 제출 Pod도 다시 확인한다.
- 미검증 resume/evaluation-only 컨테이너 실행은 거부한다. 초기화와 완전 재개를 혼동하지 않는다.

## 모델 이미지 계약

웹 템플릿의 `live_verified=false` 기본값을 임의로 바꾸지 않는다.
별도 이미지·격리 시험 증거가 준비되면 새 ID로 `tools/register_template.py`를 사용한다.
`code_files`, `image_digest`, `validation_evidence`, exec-form `entrypoint`,
Config schema/defaults를 포함해야 한다.
전용 DAG는 별도 allowlist의 **전체 템플릿 해시**, 이미지 digest, dataset 버전을 대조한다.
기본 예제 DAG는 단일 자동 배정 GPU만 받으며 UUID 선택은 실증 전 구현 경로에 연결하지 않는다.

컨테이너 어댑터는 내용 manifest를 재검증하고 소스 파일 해시와 Config를 검증한다.
공은 기존 YAML과 Ultralytics 콜백을 사용하며 명시적 split 목록을 생성한다.
등번호는 원본 모듈의 호출 경계를 감싸 validation 결과와 로그 지표를 MLflow에 기록한다.
로그에서 기록된 반올림 값을 수집할 수 있으며 미기록 배치 값을 복원하지 않는다.
필터 후 detection_df를 actual sample CSV로 기록한다. 이 경로는 실제 학습 이미지에서 검증 대기다.
W&B는 false로 고정되며 계정이 필요하지 않다. 다중 GPU 브리지는 실증 전 차단된다.
완료 콜백 이후 체크포인트를 별도 completed 디렉터리에 원자적으로 게시한다.
정상 완료와 finalized best 가중치가 모두 있을 때만 MLflow 모델 버전을 생성한다.
resume flag는 항상 false이며 optimizer/scheduler/AMP의 완전 복구 실증 후 별도 계약으로 추가한다.

## 데이터·파일

등록 루트는 GEPPETTO_DATA_ROOTS allowlist 안에 있어야 한다.
이미지·라벨 상대 경로와 split 목록을 받으며 양쪽 내용 SHA-256을 저장한다. 공유 CSV/추적/정제 파일은 source_files에 추가한다. 등번호 로더가 등록 외 샘플/split/라벨 원본을 사용하면 실행을 거부한다.
같은 이미지의 split 중복, 경로 탈출, 심볼릭 링크를 거부한다. 원본 불변 운영은 별도 필요하다.
공 라벨은 Ultralytics의 images→labels 위치 대응을 어댑터에서 확인한다.
실제 모델의 데이터 사용 여부는 필터 후 sample manifest와 별도로 대조해야 한다.

다운로드는 종료 실행의 complete 아티팩트만 허용한다. openat/O_NOFOLLOW로 경로를 열고
해시 확인을 마친 스풀에서 응답하므로 검증 이후 원본 변경이 응답 바이트를 바꾸지 않는다.
MLflow는 명시적으로 연결된 artifact proxy만 사용하고 임의 file/S3/HTTP URI를 열지 않는다.
기존 MLflow 파일은 종료 상태와 크기를 확인하지만 과거에 없던 원본 content hash는 복원하지 않는다.
원격 아티팩트는 다운로드 요청 시 캐시하며 자동으로 대용량 가중치를 전부 가져오지 않는다.

## API

인증이 켜지면 `/api/login`에서 개인 토큰으로 HttpOnly/SameSite cookie를 받거나 Bearer를 사용한다.
쓰기에는 JSON과 `X-Geppetto-Request: 1`, 동일 Origin을 요구한다. Host allowlist도 적용한다.

| API | 역할 |
|---|---|
| GET `/api/status` | 모드·지원 기능·연결 상태 |
| GET `/api/catalog/{kind}?category=&offset=&limit=` | 카탈로그 목록 (최대 100개/페이지) |
| GET `/api/catalog/{kind}/{id}` | dataset/template/run/schedule 상세 |
| POST `/api/datasets` | 원본 경로·manifest 기반 버전 등록 |
| POST `/api/preview` | 확정 설정과 confirmation_hash |
| POST `/api/submissions` | 최종 확인 해시와 Idempotency-Key로 제출 |
| POST `/api/runs/{id}/stop` | 이름 확인 후 중단 의도 저장 |
| PATCH `/api/schedules/{id}` | 시간 수정·활성화·비활성화·취소 |
| POST `/api/compare` | 같은 카테고리의 2–6개 비교 |
| POST `/api/integrations/sync` | 명시적으로 매핑한 MLflow 실험 읽기 |
| GET `/api/datasets/{id}/samples/{index}/{role}` | 이미지/라벨 원본 미리보기 |
| GET `/api/runs/{id}/artifacts/{artifact_id}` | 완료 파일 다운로드 |

API 참조: [MLflow 공식 REST](https://mlflow.org/docs/latest/api_reference/rest-api.html),
[Airflow 공식 REST](https://airflow.apache.org/docs/apache-airflow/stable/stable-rest-api-ref.html).
이번에는 운영 API에 연결하지 않았으며 가짜 HTTP transport로 요청·멱등성·페이지네이션을 검증했다.
배포 대상의 정확한 Airflow OpenAPI와 Kubernetes provider의 템플릿 렌더링은 격리 환경에서 추가 확인한다.
