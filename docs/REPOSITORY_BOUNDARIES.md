# 저장소 경계와 모델 연동

## 원칙

Geppetto는 모델 관리 플랫폼이고 sports-ml은 Geppetto가 관리하는 모델 프로젝트 중 하나다.

의존 방향은 다음과 같다.

    sports-ml ── SDK/API/등록 명세 ──> Geppetto

Geppetto 코어는 sports-ml의 체크아웃 위치, 가상환경, 학습 모듈 또는 데이터 경로를 import하거나 직접 참조하지 않는다. 모델 프로젝트가 자신의 코드와 실행 환경을 소유하고 Geppetto의 공개 계약을 사용한다.

원격 학습에서는 제어 흐름이 반대로 보일 수 있다.

    사용자 → Geppetto → Agent → 등록된 모델 이미지

이는 Geppetto가 sports-ml 소스에 의존한다는 의미가 아니다. Agent는 sports-ml이 사전에 등록한 불변 이미지와 실행 명세를 전달받아 실행할 뿐이다.

## 각 저장소의 책임

### Geppetto

- 웹 UI, API, 인증과 프로젝트 권한
- 모델, 데이터 버전, 실행과 결과 카탈로그
- 예약, 중단 의도, 리소스 정책과 Agent 프로토콜
- 범용 설정·실행·결과 계약
- Python SDK와 업로드 API
- 모델 종류와 무관한 시각화 및 비교 기능

Geppetto 저장소에는 실제 데이터, 모델 가중치, 모델별 가상환경, 학습 소스 사본, 운영 토큰 또는 클러스터 비밀값을 저장하지 않는다.

### sports-ml

- 공·등번호 등 실제 모델의 학습 및 평가 코드
- 모델별 의존성, 설정과 데이터 로더
- 컨테이너 이미지와 재현 가능한 빌드 정의
- Geppetto에 등록할 템플릿과 데이터 manifest
- SDK를 통한 진행률, 지표와 결과물 기록
- 모델별 검증 및 재개 정책

## 연동 방식

### 1. SDK 기록

학습 코드가 geppetto_sdk를 설치하고 init, progress, log, log_artifact, finish를 호출한다. SDK는 학습 프로세스를 배치하거나 GPU를 예약하지 않는다.

### 2. 관리형 실행

모델 프로젝트는 다음 내용을 가진 버전 고정 템플릿을 등록한다.

- image_digest: 태그가 아닌 sha256 digest
- schema와 defaults: 사용자 설정 계약
- entrypoint 또는 adapter 식별자
- metrics와 artifact_roles
- code_files와 validation_evidence
- 지원 기능: train, resume, evaluate, multi-GPU

Agent는 허용된 이미지, PVC, namespace와 리소스 정책을 확인한 뒤 작업을 생성한다. Geppetto 서버는 모델 패키지를 import하지 않는다.

### 3. 기존 기록 가져오기

MLflow 같은 기존 추적 시스템의 실행은 명시적으로 선택한 범위만 가져온다. 원본 실행 ID와 artifact URI를 유지하며 Geppetto가 과거 학습을 수행한 것처럼 표시하지 않는다.

## 현재 남아 있는 결합

현재 저장소에는 초기 운영 검증 때문에 다음 sports-ml 전용 호환 코드가 남아 있다.

- contracts/templates/ball.json과 jersey.json
- contracts/ball-source.json 및 uncertainty-jnr 계약
- integrations/model_runner.py와 distributed_ball.py
- 공·등번호 데이터 가져오기 및 MLflow 동기화 도구
- deploy 디렉터리의 mlops 전용 manifest
- 특정 sports-ml 경로가 등장하는 운영 기록

이 파일들은 현재 배포와 회귀 테스트가 사용하므로 한 번에 삭제하지 않는다. Geppetto 코어와 분리된 호환 계층으로 취급하고 아래 순서로 이전한다.

## 이전 순서

1. sports-ml에서 SDK 기록을 적용하고 새 실행의 지표·결과물 일치 여부를 검증한다.
2. 모델 템플릿과 이미지 빌드를 sports-ml에서 생성하고 Geppetto 등록 API로 게시한다.
3. 모델 컨테이너가 범용 entrypoint 계약을 직접 구현하도록 바꾼다.
4. 기존 실행의 중단·재개·평가와 artifact 접근을 회귀 검증한다.
5. mlops 전용 manifest와 import 도구를 sports-ml로 옮긴다.
6. Geppetto의 모델 전용 내장 템플릿과 어댑터를 제거한다.

각 단계는 새 이미지 digest와 템플릿 ID를 사용한다. 실행 중인 Job의 이미지나 계약을 제자리에서 바꾸지 않는다.

## 운영 안전 규칙

- 활성 학습이 있을 때는 Agent, 실행 정책, 모델 이미지와 결과 PVC를 변경하지 않는다.
- 학습 중단은 Geppetto의 stop 경로를 사용하고 상태·체크포인트·GPU 반환을 확인한다.
- 배포 전에 Python/JavaScript 테스트와 격리된 smoke test를 실행한다.
- 데이터베이스는 SQLite online backup으로 보존한다.
- local/, 데이터셋, 결과 PVC와 인증서는 Git 정리 대상이 아니다.
- 소스 정리와 실제 배포를 같은 단계에서 수행하지 않는다.

## 완료 조건

경계 정리가 완료되면 다음 조건을 만족해야 한다.

- 새 Geppetto checkout은 sports-ml 경로 없이 실행되고 테스트된다.
- sports-ml 학습은 SDK/API만으로 등록 및 추적된다.
- 모델 템플릿과 이미지 버전의 소유자가 sports-ml로 명확하다.
- Geppetto 코어 테스트가 공 또는 등번호 모델 소스를 요구하지 않는다.

## Template entrypoint 계약

새 live template은 컨테이너가 실행할 명령을 문자열 배열로 명시한다.

    "entrypoint": ["python", "/app/train.py"]

Agent는 shell을 거치지 않고 이 배열을 Kubernetes container `command`로 전달한다.
인자는 1~32개, 각 인자는 비어 있지 않은 4 KiB 이하 문자열이어야 한다. 이미지 내부 경로와
실행 모듈의 소유자는 모델 프로젝트다.

`entrypoint`가 없던 기존 frozen run은 재개·재시도를 위해 과거 호환 entrypoint를 사용한다.
새 live template 등록에는 명시적 entrypoint가 필수이며 호환 fallback에 의존하지 않는다.
