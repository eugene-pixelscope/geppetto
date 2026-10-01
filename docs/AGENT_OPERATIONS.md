# 중앙 제페토–Agent 연결

현재 구현: **조회 전용 Agent 등록·인증·승인·노드 발견·고래 등록**.
Agent를 통한 학습 Job 실행, 원격 파일 미리보기, 모델 이미지 배포는 아직 연결하지 않았다.
기존 Airflow 실행 경로와 중복 제출하지 않는다. Kubernetes에 설치/apply한 것은 아니다.

## 중앙

기본 모드는 `catalog`다. 카탈로그·클러스터 연결은 관리할 수 있지만 모의/실제 학습 제출은 받지 않는다.
`readonly`는 사용자 변경을 막는다. 기존 `live` 실행은 검증된 전용 Airflow 계약을 계속 사용한다.
외부에 중앙을 제공하려면 HTTPS, `GEPPETTO_TOKEN`, `GEPPETTO_ALLOWED_HOSTS`와 영속 상태 볼륨을 구성한다.
Agent에서 중앙 주소에 접근할 수 있어야 한다. 현재 로컬 서버 127.0.0.1:8091은 다른 Pod에서 접근할 중앙 주소가 아니다.

1. ‘클러스터 연결’ → ‘Kubernetes 연결 추가’에서 표시 이름을 입력한다.
2. 클러스터별 등록 토큰이 한 번 표시된다. 토큰은 24시간 유효하며 첫 Agent 등록 시 소모된다.
3. 실제 중앙 HTTPS 주소를 입력해 설치 매니페스트를 받는다.
4. `REPLACE_WITH_IMMUTABLE_GEPPETTO_IMAGE`를 새로 빌드한 이미지의 고정 digest로 바꾼다.
5. 대상 클러스터에서 별도로 배포한다. 기본 예제는 `geppetto-agent` namespace에 Agent를 설치한다.
6. 중앙에 나타난 Agent를 확인하고 ‘Agent 승인’을 누른다.
7. 다음 조회 이후 실제 Node UID/Ready/스케줄링 상태/allocatable을 확인하고 ‘고래로 등록’을 누른다.

설치 예제: `deploy/agent.example.yaml`. 동일 앱 이미지에 `python -m geppetto.agent` 명령을 사용한다.
Agent 전용 상태 PVC가 필요하다. `/state/identity.json`에 Agent ID·인증 토큰·순서를 저장하며 권한은 0600이다.
새 클러스터에는 새 연결을 만든다. Agent 상태 볼륨을 잃으면 중앙에서 토큰을 재발급한 뒤 다시 등록한다.

## 인증과 통신

- 중앙 사용자 토큰과 Agent 토큰은 별도다. Agent 토큰으로 중앙 관리 API에 접근할 수 없다.
- 최초 등록 시 Agent가 별도의 무작위 지속 토큰을 만든다. 중앙에는 토큰 해시만 보관한다.
- 최초 등록 요청의 응답 유실은 같은 Agent ID/클러스터 UID/지속 토큰으로 재시도할 수 있다.
- 연결 폐기는 즉시 기존 Agent 인증을 거부한다. 재발급도 기존 인증을 무효화하고 승인부터 다시 진행한다.
- 지속 토큰은 365일 유효하다. 만료 전에 재등록한다. API의 일반 목록에 토큰·해시를 노출하지 않는다.
- Agent는 20초마다 중앙으로 HTTPS 요청한다. CA 검증을 끄는 옵션은 없다.
  사설 CA는 Agent에 인증서 파일을 마운트하고 `GEPPETTO_SERVER_CA`를 지정한다.
- Kubernetes API 접근은 Pod의 ServiceAccount token/CA를 사용한다. 토큰 파일은 요청마다 다시 읽는다.
  [Kubernetes 공식 안내](https://kubernetes.io/docs/tasks/run-application/access-api-from-pod/).

## 관측과 권한

노드 목록은 페이지네이션을 모두 읽은 뒤 한 번의 snapshot으로 전달한다.
클러스터 정체성은 kube-system Namespace UID, 노드는 Node UID로 구분한다.
중앙은 단조 증가하는 sequence와 snapshot hash로 중복/오래된 관측을 처리한다.
같은 이름의 새 Node UID는 별도 고래가 되고 기존 노드 이력은 보존된다.

Agent heartbeat와 Node Ready는 구분된다. 갱신 60초 초과는 지연, 180초 초과는 연결 끊김이다.
Kubernetes 조회 실패 시 이전 노드 목록을 보존하고 조회 오류를 표시한다.
성공한 전체 snapshot에서 사라진 노드는 미발견으로 표시하며 작업을 실패/중단으로 바꾸지 않는다.
최신 성공 조회가 없으면 노드를 새 고래로 등록하지 않는다.

CPU·메모리·GPU는 capacity/allocatable 값이고 사용률이 아니다.
PVC는 Agent namespace의 이름·상태만 수집하며 파일이나 학습 데이터 접근 가능성을 주장하지 않는다.
노드/PVC 읽기 및 kube-system 식별 읽기만 RBAC로 부여한다. Job/Pod 생성·삭제, Secret 조회, cluster-admin은 부여하지 않는다.
[RBAC 공식 안내](https://kubernetes.io/docs/reference/access-authn-authz/rbac/).

## 로컬 검증

`tests/test_clusters.py`에서 실제 중앙 API와 Agent cycle을 연결해 등록·재시도·승인·snapshot·폐기·재발급·노드 교체를 검증한다.
Kubernetes API는 MockTransport로 대체한다. 실제 클러스터 권한이나 GPU는 사용하지 않는다.

`tools/browser_clusters.py`는 **별도의 빈 catalog 서버 8092**만 대상으로 한다.
등록 → 설치 파일 다운로드 → Agent 등록 → 승인 → 노드 발견 → 고래 등록을 확인한다.
사용자의 8091 카탈로그에 테스트 연결을 만들지 않는다.

## 예제 기록 정리

`tools/clean_synthetic.py`는 기본적으로 목록만 출력한다. `--apply`는 SQLite online backup 후 명시적인 synthetic 기록을 정리한다.
실제/imported 기록, 그 기록이 참조하는 데이터·모델·상위 작업은 보호한다.
이미지·라벨·가중치·로그 파일을 삭제하지 않는다. 테스트 결과 파일은 카탈로그에서만 제거한다.
정리한 카탈로그에는 `demo_disabled` 표시를 저장해 demo 모드로 재시작해도 예제를 다시 만들지 않는다.
이후 합성 테스트는 별도 HOME/포트를 사용한다. 기존 smoke 도구는 사용자 포트 8091을 거부한다.
