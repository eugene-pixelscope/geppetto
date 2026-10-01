# Kubernetes 병행 운영

2026-09-26 배포 완료. 기존 `mlops` namespace의 대시보드, Airflow, MLflow, 학습 Deployment/Pod는 변경하지 않았다.

| 구분 | 주소/위치 |
|---|---|
| 기존 대시보드 | http://localhost:8090 |
| 개발용 제페토 | http://localhost:8091 · 호스트 코드와 local/app |
| 배포용 제페토 | https://localhost:8092 · geppetto-system namespace |
| 조회 전용 Agent | geppetto-agent namespace |

서버에서 loopback 8092 → 새 ClusterIP Service 443을 tmux `geppetto-k8s-ui`로 전달한다. 원격 PC는 VS Code에서 8092 포트를 전달하거나 `ssh -N -L 8092:127.0.0.1:8092 eugene@서버주소`를 사용한다. 외부 Ingress는 열지 않았다.

로그인 토큰: `/home/eugene/geppetto/local/kubernetes/secrets/access-token`. 토큰 값은 문서/소스에 기록하지 않는다. 내부 CA 인증서: `local/kubernetes/secrets/ca.crt`. 브라우저는 이 내부 CA를 신뢰하도록 설정해야 인증서 경고 없이 접속할 수 있다. 서버 인증서 만료는 2027-09-26이므로 그 전에 갱신한다. TLS 검증을 끄지 않고 Agent가 CA와 서비스 DNS를 검증한다.

## 개발과 배포 분리

- 앱 이미지 `geppetto:20260927-09`, image ID `sha256:80bf179db2cfa9a1bde519810e25cd390abefa0dbf493b73b7a537384798c90b`. Agent는 변경 없이 `geppetto:20260926-01`을 유지한다.
- 앱 코드를 bind mount하지 않는다. 호스트 파일 수정은 배포 화면에 즉시 반영되지 않는다.
- 배포 DB와 인덱스는 전용 5Gi PVC, 개발 DB는 `local/app`이다. 최초 SQLite backup으로 복사했으며 이후 양방향 동기화하지 않는다.
- 원본 `/datasets`는 앱에 읽기 전용으로 마운트한다. 개발 클러스터의 기존 `/datasets` 노드 마운트를 사용하며 새 호스트 마운트 설정은 하지 않았다.
- 앱과 MLflow 조회 컨테이너의 합계 CPU limit 1.25, 메모리 1.25Gi. Agent limit 0.25 CPU/256Mi. GPU 요청 없음.
- 배포는 catalog 모드다. 학습 조회는 가능하지만 새 학습 제출/중단 제어는 아직 연결하지 않았다.
- MLflow 조회 컨테이너가 기존 실제 run 3개만 60초마다 읽는다. 진행 중 P2는 실제 Pod의 nodeName을 확인해 고래에 연결했다. 과거 평가 2건은 실행 노드가 확인되지 않아 `local` 작업 공간에 보존했다.

## 업데이트

1. 로컬에서 검증한다. `.venv/bin/python -m unittest discover -s tests -q`, `node tests/workshop.mjs`.
2. 현재 배포 PVC의 DB를 SQLite backup API로 별도 백업한다. 개발 DB를 다시 덮어쓰지 않는다.
3. SDK wheel을 `uv build --wheel --out-dir dist sdk`로 먼저 빌드한 뒤 새 고유 태그로 이미지 빌드: `minikube -p mlops image build -t geppetto:NEW_VERSION -f deploy/Dockerfile .`. 기존 태그를 재사용하지 않는다.
4. `deploy/kubernetes.mlops.yaml`의 app/mlflow-sync 이미지를 새 태그로 바꿔 해당 manifest만 apply한다. Agent 변경이 있을 때만 Agent도 새 태그로 적용한다.
5. rollout 상태, TLS, 로그인, Agent heartbeat, 실제 기록, 이미지/아티팩트를 확인한다. Recreate 배포이므로 새 제페토는 짧은 재시작 구간이 있고 기존 대시보드와 학습은 계속 실행된다.

앱 롤백: `kubectl --context=mlops -n geppetto-system rollout undo deployment/geppetto`. DB 스키마가 바뀐 배포는 호환 백업도 확인한다. PVC나 namespace를 삭제하는 방식으로 롤백하지 않는다.

## 배포 파일과 인증

- `deploy/kubernetes.mlops.yaml`: 앱, 조회 sidecar, PVC, Service.
- `deploy/agent.mlops.yaml`: Agent, 전용 ServiceAccount, 조회 RBAC, 별도 상태 PVC.
- Secret과 내부 CA ConfigMap은 위 파일과 별도로 준비한다. 민감 파일은 gitignore된 `local/kubernetes/secrets`에만 보존한다.
- Agent는 nodes get/list, kube-system namespace get, 자기 namespace PVC get/list만 허용한다. 실제 RBAC에서 기존 mlops Pod 삭제는 거부됨을 확인했다.
- Agent가 보고한 kube-system UID를 실제 클러스터 UID와 대조해 등록을 승인했다. 연결 ID `774203a240b64afeb08165c4e2074aa4`.
- Agent 등록 토큰은 최초 등록용이며, 인증된 Agent identity는 전용 PVC에 보존한다.
- 포트 전달 tmux는 SSH 종료 후에도 유지되지만 호스트 재부팅 후에는 다시 실행해야 한다. Kubernetes Deployment는 클러스터가 실행 중이면 재조정된다.

## 검증

실제 배포에서 HTTPS 인증서 검증, 로그인, Agent 연결, 노드 조회, 고래 등록, 인덱스 255,996장 조회, 실제 이미지 읽기, MLflow 결과 다운로드, 375/1440px 화면을 확인했다. 기존 P2 Pod `train-ball-4c5tw6vo`는 배포 전후 Running, 재시작 0이었다.

모델 1:1 관계·이름 변경·프로젝트 비교는 [모델과 프로젝트](MODELS_AND_PROJECTS.md)를 참고한다. 변경 전 백업은 배포 PVC `/state/backups/before-model-projects-20260926.sqlite3`에 보존했다.

Python SDK는 [적용 명세](PYTHON_SDK.md)를 참고한다. Settings에서 프로젝트별 키 발급·폐기와 wheel 다운로드를 제공한다. SDK 배포 전 DB 백업은 `/state/backups/before-sdk-20260927.sqlite3`이다. SDK 파일은 동일 PVC의 `/state/sdk-artifacts`, 기본 예약 용량 한도는 1 GiB이다.

## 2026-09-27 학습 종료 및 오류 분석

사용자 요청으로 `mlops/train-ball-4c5tw6vo`를 종료했고, 대응 MLflow run `7db09f122b5247b4b206224b4140ad12`를 KILLED로 반영했다. Geppetto에서는 stopped로 표시한다. 기존 ball-results PVC/체크포인트는 보존했으며 새 학습을 시작하지 않았다.

동일 이미지 런타임의 임시 평가 Pod를 geppetto-system에 만들고 데이터/기존 결과를 읽기 전용으로 마운트해 validation 128장을 평가했다. 예측 JSON 회수 후 Pod와 ConfigMap을 삭제했다. 운영 모델의 오류 분석에 보고서 `cfa7c58c9633fb25b2ef3b27`을 연결했다. 자세한 조건은 ERROR_ANALYSIS.md 참고. 전체 Python 테스트 79개 및 실제 보고서의 브라우저 탐색/확대/모바일 확인 완료.

## 0.3 운영 적용 및 실제 GPU 검증 — 2026-09-27

앞 절의 20260927-09 이미지 및 읽기 전용 Agent 설명은 이전 운영 상태다.
현재 중앙 서버·MLflow sync·Agent 이미지는 **`geppetto:20260927-16`**, API/SDK 버전은 **0.3.0**이다.
운영 DB는 변경 전에 `/state/backups/pre-agent-*.sqlite3`로 SQLite backup을 만들었으며 기존 기록과 PVC를 유지했다.

Agent namespace `geppetto-agent`에서 실행한다. `agent-execution.mlops.yaml` RBAC/허용 목록과
`agent-execution.patch.yaml`의 결과 RO 마운트를 적용했다. `agent-storage.mlops.yaml`의 데이터 local PV는
현재 mlops 노드에 고정되어 있고, 결과 PVC는 `geppetto-results`다. 원본 데이터는 학습 컨테이너에서 RO다.
Agent는 5초 간격으로 관측하며 재시작 시 PVC의 Job UID/sequence ledger를 이어받는다.

기본 학습 이미지:
`localhost:5001/geppetto-ball@sha256:bad1a6f25015beb7e6cf548349d3fda7a56a05ce8cbb2113e69476d00273f0e0`

이전 검증 실행의 재개를 위해
`localhost:5001/geppetto-ball@sha256:df87eb70593853519fb675e7c02935af08a369d75f77f0d6e1c38caa5ff8c526`
도 허용 목록에 유지한다. 재개는 현재 기본 이미지가 바뀌어도 원본 실행의 digest를 고정하며,
원본 이미지가 허용 목록에서 제거되면 Agent가 거부한다. 기본 동시 실행 상한은 검증 종료 후 8개다.
물리 GPU UUID를 요청으로 지정하지 않고 NVIDIA device plugin이 GPU를 배정한다.
실제 동시 실행에서 서로 다른 GPU 두 개, 컨테이너당 visible device 1개를 확인했다.

전체 데이터 검증 작업 `cc0b2456c8de4dee862e081401bd9146`가 **255,996장 전체**의 이미지·라벨 해시와
분할 간 동일 이미지 중복 검사를 마쳤다. 새 내용 버전 `content-4f47f703d7541b23e5573782991d8894`를
IDC 노드의 `geppetto-datasets` PVC에 연결하고 `/datasets/geppetto/datasets.json`으로 실행 manifest를
내보냈다. 원본 indexed 버전은 유지한다. 내용 버전은 스토리지 snapshot 자체가 아니며 실행 시작 시
파일 바이트를 다시 확인한다.

검증용 별도 버전 `aed8427e980d44799aed9c9afe0d6965`는 실제 train 24장/validation 12장/test 12장이다.
검증 실행은 `operational-validation`, `small-dataset` 태그로 표시한다. 성능 벤치마크가 아니다.

- 정상 학습: `de889274e9a64411aa14d6b23101dc88`
- 체크포인트 저장 후 중단·GPU 반환: `7f6da7b16eb04ffd802c20419bc88590`
- AdamW 상태 재개: `5567b6a39c664cd1a7f7bac5720d9fec`
  - start_epoch=3 (표시상 4번째 epoch), optimizer state 619개, scheduler epoch=2.
  - 모델·optimizer·scheduler·scaler의 저장/복원 값 일치 검사 모두 true.
- 별도 test 평가: `43e5c61f6f284526be318b9fe55c9306`
- 선행 단계 DAG: `fcaf6d102a36401ca3f7fcab3e2f9dba`
- 두 후보 grid sweep: `befb1078591848338ca89b61a0728f5c`
- MLflow `ball-p2-v1@operational-validation` 별칭을 version 1 → 2 → 1로 변경/롤백하고 감사 기록 확인.
  운영 서비스용 champion/production 별칭은 만들지 않았다.

실제 실행 중 확인하고 해결한 문제는 CPU 할당 대비 과도한 Torch 스레드,
AMP 검사 가중치의 쓰기 경로, MLflow의 루트 artifact path와 지표 이름 제약,
GPU UUID 접두사 처리, 없는 모델 별칭 조회 응답이다. 초기 실패 기록도 숨기거나 지우지 않았다.
실패·중단·성공한 작업의 GPU 반환을 확인하고 완료 기록은 결과 PVC/MLflow에 보존한다.

기계 판독 가능한 실행 상태·이미지 digest·artifact hash 검증·재개 증거는
[`validation-0.3.json`](validation-0.3.json)에 있다. `tools/verify_deployment.py`로 다시 조회할 수 있다.
GPU 프로세스가 남지 않았는지 `nvidia-smi` 및 Agent namespace의 실행 Pod로 확인한다.
장시간의 전체 데이터 학습은 검증 과정에서 시작하지 않았다.

자동 검증: Python 104 tests, scalar/workshop JavaScript 검증, 모의 카탈로그의 제출/평가 및
histogram/embedding/trace/관리 화면 브라우저 검증, 실제 HTTPS 운영 화면 7개 탭과 실행 결과 조회.

## 2026-09-27 계정 로그인 전환 (0.3.1)

중앙 app 및 MLflow 동기화 컨테이너를 `geppetto:20260927-18`로 갱신했다. Agent는 변경 없이 `geppetto:20260927-16`을 유지한다. 기존 데이터와 실행 기록을 유지한 채 카탈로그 DB에 별도 인증 테이블을 추가했다. 적용 전 `/state/backups/pre-accounts-*.sqlite3`에 온라인 SQLite 백업을 저장했다.

접속 주소는 https://localhost:8092 이며 웹에서는 토큰 대신 계정으로 로그인한다. 관리자 계정 `admin`의 임시 비밀번호는 `local/kubernetes/secrets/admin-login.txt`에 0600 권한으로 저장되어 있다. 첫 로그인 시 비밀번호를 변경한다. 사용자 추가와 복구 방법은 [계정 안내](ACCOUNTS.md)를 참고한다.

Python 테스트 112개, 계정/기존 관리/진단/Agent 제출 브라우저 테스트 및 JS 차트/워크숍 테스트 통과. 운영 HTTPS에서 로그인·강제 비밀번호 변경·로그아웃 후 세션 폐기를 검증했다. 기존 Agent snapshot/job poll은 정상 응답을 유지한다. 학습 작업의 생성·중단 없이 배포했다.

## 2026-09-27 작업 이력 관리 (0.3.2)

중앙 app/동기화 이미지를 `geppetto:20260927-19`로 배포했다. 적용 전 `/state/backups/pre-history-*.sqlite3`에 카탈로그를 백업했다. [작업 이력 안내](RUN_HISTORY.md) 참조.

Runs 목록과 상세에서 이력을 삭제하고 삭제된 이력에서 복원할 수 있다. 기본 목록 조회는 삭제된 이력을 제외한다. 원본 실행·파일·모델·데이터는 유지하며, Agent와 스케줄러의 실행 기록은 계속 보존된다. 운영 기록은 임의로 삭제하지 않았다.

Python 테스트 118개 및 실제 브라우저에서 개별/선택 삭제·취소·복원·모바일 화면, 기존 관리 기능, 계정 인증을 검증했다. 운영 HTTPS에서는 삭제 확인 창을 열어 취소하고 삭제된 이력 화면과 API를 확인했다. 기존 로그인 정보는 변경하지 않았다.

## 2026-09-27 모델에서 직접 삭제 (0.3.3)

중앙 app/동기화 이미지를 `geppetto:20260927-20`으로 갱신했다. 적용 전 `/state/backups/pre-model-delete-*.sqlite3`에 백업했다. 모델 카드 오른쪽과 상세 상단의 **삭제**가 모델 및 연결 작업 이력을 함께 목록에서 제거한다. Runs의 삭제 버튼은 제거했다. 활성 작업/미반환 자원/활성 예약을 확인하고 삭제를 차단한다. 삭제된 모델에 대한 제출과 예약 재활성화도 거부한다.

원본 모델·실행·파일은 유지하고 `model_history` 표시에 따라 목록을 구성한다. 노드의 **삭제된 모델**에서 일괄 복원하며, 기존 개별 삭제 기록은 그대로 유지한다. Python 테스트 123개, 모델 삭제/복원 및 기존 이력 복원 브라우저 테스트 통과. 운영 모델은 삭제하지 않고 버튼·확인 창·취소 동작만 확인했다.

## 2026-09-27 Epoch·오류 시각화·샘플 정리 (0.3.4)

중앙 app/동기화 이미지를 `geppetto:20260927-21`로 배포했다. 스칼라/비교 차트는 Epoch 고정이며 검증된 YOLO26x CSV 이력의 epoch를 동기화한다. 오류 분석은 연속적인 오류 위치 밀도 히트맵을 제공한다(Grad-CAM 아님). 정답은 항상 초록, 미탐은 별도 빨강 외곽선, 예측/오탐은 파랑/주황 점선이며 썸네일과 확대 뷰어를 통일했다.

`pre-sample-cleanup-*.sqlite3` 백업 후 태그/생성 기록이 확인된 검증용 실행 18개와 모델 14개를 목록에서 정리했다. 5개 모델은 이미 삭제되어 있었고 9개를 추가 삭제했다. 사용자 기존 실제 실행과 삭제 상태는 유지했다. 대상은 `sample-cleanup.json`에 기록했다. 원본 파일을 지우거나 학습을 실행하지 않았다.

Python 124개 테스트 및 JS/브라우저 시각화 검증 통과. 운영 HTTPS에서 Epoch 차트와 실제 이력의 epoch 동기화, 검증용 실행이 기본 목록에서 사라진 상태를 확인했다. 상세는 [시각화 안내](DETECTION_VISUALS.md)를 참고한다.

## 2026-09-29 · 8091 제페토 0.4 적용

사용 대상은 호스트 `geppetto-catalog`의 8091 앱이다. 기존 Kubernetes 중앙 앱의 카탈로그를
덮어쓰거나 기존 학습을 옮기지 않았다. 8091의 기존 계정·MLflow 기록·원본 데이터는 유지한다.

- 적용 전 DB: `local/app/backups/before-orchestration-20260929.sqlite3` (SQLite online backup).
- `geppetto-agent-gateway.service`: bridge IP `192.168.49.1:8094`의 TLS 수신 경로.
  Agent의 enroll/snapshot/jobs poll/observe만 8091에 전달한다. 브라우저와 관리 API는 노출하지 않는다.
  인증서·등록 자격증명은 `local/agent-gateway/`에 보관하고 이미지나 Git에 넣지 않는다.
- 별도 `geppetto-local-agent` Deployment 및 state PVC로 8091에 연결했다.
  기존 중앙 앱의 Agent와 진행 중인 mlops 학습은 유지했다.
- 8091에 `IDC · GPU 학습` 노드와 검증된 전체 공 데이터셋 255,996장을 연결했다.
  내용 인덱스와 manifest만 `local/app/connected-data/`로 복사했다. 학습 이미지가 참조하는
  원본 데이터 PVC와 버전은 동일하다. 과거 검증용 실행/48장 데이터셋은 가져오지 않았다.
- Agent 이미지: `geppetto:20260929-03`, 노드 수집기 이미지: `geppetto:20260929-02`.
  학습 이미지: `localhost:5001/geppetto-ball@sha256:0b92fdba941f110b600c5b52e38982a62a1cba5f0195c0dc0043ba8371b539e3`.
  새 이미지는 기존 모델 소스와 의존성을 유지하고 실행 브리지를 갱신한다.
- 실행 프로필은 단일 GPU만 활성화한다 (`max_gpus=1`). 다중 GPU/노드 코드는 포함하지만
  운영 GPU 검증 전 이를 활성화하지 않는다. 요청된 기능·제약은 [0.4 문서](PLATFORM_0_4.md).
- 노드 수집기 DaemonSet은 GPU 자원을 요청하지 않는다. 실제 8개 GPU 측정값과
  CPU·메모리·디스크·네트워크, 클러스터 전체 요청량을 확인했다.
- 기존 mlops 학습 Pod 2개는 Running/restart 0 유지. 이번 검증에서 학습 작업을 제출하지 않았다.

검증: Python 전체 146개 통과, 신규 관리 UI의 실제 폼 제출과 네 화면 폭(격리 카탈로그),
차트·진행 표시 검사, 8091 실제 인증 화면의 읽기 검증 통과.
`tools/verify_local_orchestration.py`는 임시 인증 세션만 만들고 종료 시 폐기하며 학습을 제출하지 않는다.
실제 다중 GPU/노드 실행과 분산 체크포인트 재개는 미검증이다.
