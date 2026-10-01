# 독립 배포·VPN·복구

2026-09-26 새 제페토와 조회 전용 Agent를 별도 namespace에 배포했다.
실제 주소·인증·개발 분리·업데이트 절차는 [Kubernetes 운영](KUBERNETES_OPERATIONS.md)을 따른다.
기존 학습/대시보드는 유지하며 새 학습 제출 연동은 아직 미완료다. 아래 example 파일은 참고용이다.

## 구성 파일

- `deploy/Dockerfile`: 앱 전용 이미지, 비특권 사용자, 기본 catalog 모드.
- `deploy/mlops-compat.Dockerfile`: 기존 ball MLflow 실행을 주기적으로 가져오는
  `sync_real_ball.py`만 기본 앱 이미지 위에 추가하는 opt-in 호환 이미지.
  범용 배포에서는 빌드하거나 실행하지 않는다.
- `deploy/compose.yaml`: loopback 8091, 새 상태 볼륨, 토큰 필수.
- `deploy/kubernetes.example.yaml`: 별도 geppetto-validation namespace용 앱/PVC/ClusterIP 예제.
  namespace와 token Secret은 자동 생성하지 않는다. 이미지 digest를 먼저 확정한다.
- `deploy/model.Dockerfile`: 기존 모델 이미지를 부모로 한 별도 연동 이미지 예제.
  모델 코드는 MODEL_SOURCE로 지정하고 새 이미지에서 MLflow/Config validator를 추가한다. 이 의존성 조합의 실제 학습 검증은 별도 필요하다.
- `integrations/geppetto_training_dag.py`: 전용 DAG 예제. 기존 DAG/Secret/Helm 값은 수정하지 않는다.
  템플릿 allowlist, dataset registry, 새 결과 PVC, pinned init image가 준비되어야 한다.
  Airflow/provider가 설치되지 않은 로컬에서는 문법 검사만 수행했다.

## 환경 변수

| 변수 | 용도 |
|---|---|
| GEPPETTO_HOME | 새 카탈로그 DB·예제 데이터 위치, 기본 local/app |
| GEPPETTO_MODE | catalog / demo / readonly / live; 기본 catalog |
| GEPPETTO_TOKEN | 개인 접근 토큰, 비-loopback 바인딩 시 필수 |
| GEPPETTO_ALLOWED_HOSTS | 접속할 hostname/IP 쉼표 목록 |
| GEPPETTO_SECURE_COOKIE | TLS 사용 시 1 |
| GEPPETTO_DATA_ROOTS | 등록 허용 원본 루트, OS path separator로 구분 |
| GEPPETTO_MLFLOW_URL/TOKEN | 명시적 MLflow 연결; 토큰 선택적 |
| GEPPETTO_MLFLOW_UI_URL / GEPPETTO_AIRFLOW_UI_URL | 원본 관리 UI로 연결할 내부 브라우저 주소 |
| GEPPETTO_EXPERIMENT_MAP | 예: `{"17":"ball-detection","18":"jersey-recognition"}` |
| GEPPETTO_AIRFLOW_URL/TOKEN/DAG | 전용 geppetto_ DAG와 인증 토큰 |
| GEPPETTO_KUBE_URL/TOKEN/CA/NAMESPACE | 지정 namespace의 대상 Pod 조회·중단 확인 |
| GEPPETTO_RESULTS_ROOT | 새 결과 PVC의 읽기 전용 마운트 루트 |

live 모드만으로 실행이 열리지 않는다. 코드·이미지·데이터 버전과 실제 검증 증거를 담은
새 템플릿 계약을 등록하고 별도 DAG allowlist에 동일 해시를 승인해야 한다.
기본 템플릿은 live_verified=false이며 새 서버 시작 시 항상 이 기본값으로 로드된다.
Airflow JWT가 만료되면 조정에 fetch_error가 남는다. 운영용 장기 인증/갱신 방법은 배포 환경에서
결정하고 기존 Airflow 서명 키/Secret을 UI 개발 때문에 바꾸지 않는다.

## 전환 순서

1. 별도 호스트에서 GPU 격리 조건을 마련하고 [GPU 실증](GPU_VALIDATION.md)을 수행한다.
2. 실제 데이터 위치·manifest·필터 후 사용 샘플을 대조하고 원본 read-only 마운트/쓰기 캐시를 분리한다.
3. 공·등번호 각각의 이미지 digest를 고정해 작은 데이터로 완료/실패/중단·반환을 검증한다.
4. 전용 DAG 렌더링과 dataset PVC/subPath, 모델 코드 해시, 로그/MLflow/결과 연결을 확인한다.
5. 앱은 먼저 readonly로 배포해 기존 실험을 명시적으로 매핑하고 원본 지표·파일을 대조한다.
6. 새 포트/내부 도메인에서 테스트한다. 기존 UI와 DNS·포트를 교체하지 않는다.
7. 해당 시점의 사용자 승인 범위를 확인하고 새 제출만 새 앱으로 전환한다. 기존 학습은 그대로 둔다.

개인 접근 토큰은 Secret/환경으로 전달하고 Git이나 프런트 JavaScript에 넣지 않는다.
Kubernetes 앱 기본 manifest는 service account token 자동 마운트를 끈다.
제어 연결을 추가할 때는 별도 namespace의 pods get/list/delete 및 pods/log get 권한만 부여한다.
Secret 조회, pod exec, 임의 Pod 생성 권한은 웹 앱에 필요하지 않다.

## WireGuard 실제 접속 확인

모바일이 VPN에서 도달할 수 있는 내부 IP/도메인과 포트를 먼저 확인한다.
localhost 바인딩 서비스는 휴대폰에서 직접 접근할 수 없다.
승인된 내부 주소 바인딩 또는 TLS 내부 reverse proxy를 사용하고 방화벽 범위를 VPN으로 제한한다.
Host allowlist에 해당 내부 이름을 추가한다. 외부 공개 ingress는 만들지 않는다.

실제 휴대폰에서 VPN 연결 → 로그인 → 데이터 이미지/라벨 → 학습 최종 확인 → 진행/중단
→ 결과 다운로드 → 비교를 확인한다. 현재 Playwright 모바일 폭 검증은 이 네트워크 실증을 대신하지 않는다.

## 백업·롤백

```bash
.venv/bin/python tools/backup.py local/app/catalog.sqlite3 local/catalog-backup.sqlite3
```

실행 중에도 SQLite backup API로 일관된 복사본을 만든다. 결과 파일과 원본 데이터는 별도로 보존한다.
앱 롤백은 새 제출을 끄고 readonly 모드로 전환한 뒤 카탈로그를 백업하고 이전 앱 이미지를 복원한다.
이전 앱이 DB user_version보다 오래되면 기동을 거부한다. 이 경우 별도로 보존한 호환 DB를 사용한다.
이미 제출된 Airflow 실행과 모델 결과는 앱 롤백 때문에 삭제하거나 종료하지 않는다.
이전 sports-ml UI는 수정하지 않았으므로 기존 주소로 돌아갈 수 있다.
새 예약은 비활성화하되 과거 제출 ID/예약 occurrence는 보존해 재시작 중복을 방지한다.

## 운영 전 확인이 필요한 한계

- 별도 GPU 호스트, 실제 데이터, WireGuard 접속 주소 미제공.
- 모델 이미지 내부의 실제 Config/의존성/학습 콜백 검증은 아직 수행하지 않음.
- 앱과 조회 Agent는 build/apply 및 연결 검증 완료. 새 학습 제출용 모델/DAG 배포는 미실행.
- GPU UUID DRA 경로, 다중 GPU, 완전 재개는 미검증이므로 차단 상태.
- 등번호 탐지 모델 계약은 아직 없으며 새로운 모델 저장소/스키마가 필요함.
- 결과 PVC 용량·MLflow artifact 캐시 보존 기간은 운영 데이터 크기에 맞춰 정해야 함.

검증된 새 템플릿과 실제 데이터를 등록한 후 `tools/export_runtime.py`로 DAG allowlist와 컨테이너 dataset registry를 내보낼 수 있다. 이 도구는 이미지 빌드나 배포를 하지 않는다.

## 중앙–Agent

`deploy/agent.example.yaml`은 조회 전용 Agent와 최소 RBAC/PVC 예제다. 설치 절차와 범위는 [Agent 운영 문서](AGENT_OPERATIONS.md)를 따른다.
