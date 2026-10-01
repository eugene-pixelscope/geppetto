# Geppetto

Geppetto는 모델 프로젝트의 데이터, 학습 설정, 실행 이력, 지표와 결과물을 한곳에서 관리하는 모델 운영 플랫폼입니다. FastAPI 기반 서버, 브라우저 UI, Kubernetes Agent, Python SDK를 제공합니다.

Geppetto는 모델 학습 코드를 소유하지 않습니다. 각 모델 저장소가 자신의 학습 코드와 컨테이너 이미지를 관리하고 Geppetto SDK/API에 모델과 실행 정보를 등록합니다. 저장소 책임과 sports-ml 연동 방향은 [저장소 경계](docs/REPOSITORY_BOUNDARIES.md)에 정리되어 있습니다.

## 주요 기능

- 모델, 데이터 버전, 학습·평가 실행 카탈로그
- 실행 설정 고정, 예약, 중단, 재개 및 파이프라인 연결
- Kubernetes Agent를 통한 격리된 작업 실행
- 지표, 체크포인트, 평가 결과와 오류 분석
- 프로젝트 범위 API 키와 Python SDK
- MLflow 실행 가져오기와 기존 기록 보존

기본 실행 모드는 catalog입니다. Agent와 실행 정책이 명시적으로 연결되기 전에는 실제 학습을 제출하지 않습니다.

## 빠른 시작

요구 사항은 Python 3.12와 [uv](https://docs.astral.sh/uv/)입니다.

    git clone git@github.com:eugene-pixelscope/geppetto.git
    cd geppetto
    uv venv --python python3.12 .venv
    uv pip sync --python .venv/bin/python requirements-dev.lock
    uv build --wheel --out-dir dist sdk
    .venv/bin/python -m geppetto.app --host 127.0.0.1 --port 8091

브라우저에서 <http://127.0.0.1:8091>을 엽니다. 기본 상태는 local/app/에 저장되며 Git에서 제외됩니다. 다른 위치를 사용하려면 GEPPETTO_HOME을 지정합니다.

루프백이 아닌 주소에 바인딩할 때는 GEPPETTO_TOKEN과 GEPPETTO_ALLOWED_HOSTS를 반드시 설정해야 합니다. 운영 배포는 [배포 안내](docs/DEPLOYMENT.md)를 참고하세요.

tools/serve_local_catalog.sh는 선택적으로 local/runtime/catalog.env를 읽습니다. 이 파일은 호스트별 MLflow 주소와 실행 ID 같은 운영 설정을 위한 것이며 Git에 포함되지 않습니다.

## 모델 프로젝트 연결

모델 저장소는 Geppetto SDK wheel을 설치하고 학습 코드에서 실행 상태를 기록합니다.

    import geppetto_sdk as geppetto

    with geppetto.init(
        project="ball-detection",
        name="YOLO experiment",
        architecture="YOLO",
        dataset_id="registered-dataset-version",
        config={"epochs": 100},
    ) as run:
        for epoch in range(1, 101):
            metrics = train_one_epoch(epoch)
            run.progress(epoch, total_epochs=100)
            run.log(metrics, step=epoch, epoch=epoch)
        run.log_artifact("best.pt", role="inference_weights")

SDK는 학습을 대신 실행하지 않습니다. 학습 코드가 진행률, 지표와 결과물을 Geppetto에 기록하도록 연결합니다. 전체 계약은 [Python SDK](docs/PYTHON_SDK.md)를 참고하세요.

원격 실행이 필요하면 모델 프로젝트가 불변 이미지 digest, 설정 JSON Schema, 데이터·결과물 역할, 실행 진입점, 코드 해시와 검증 근거를 준비해 Geppetto에 등록합니다. Geppetto Agent는 등록된 명세만 실행하며 모델 저장소의 로컬 경로나 Python 모듈을 직접 가져오지 않아야 합니다.

## 저장소 구조

    geppetto/       서버, 카탈로그, 스케줄러와 Agent
    sdk/            모델 코드가 사용하는 독립 Python SDK
    web/            빌드 단계 없는 브라우저 UI
    contracts/      모델 실행 계약과 템플릿
    integrations/   모델 컨테이너 안에서 사용하는 호환 어댑터
    deploy/         범용 예제와 현재 운영 배포 manifest
    tests/          Python 및 JavaScript 회귀 테스트
    tools/          등록, 검증, 백업과 이전 도구
    docs/           설계, 운영 및 기능 문서

local/, 가상환경, 브라우저 바이너리, 빌드 결과, wheel과 데이터베이스는 소스가 아니며 Git에서 제외됩니다. 실제 데이터셋, 체크포인트, 토큰과 인증서를 저장소에 커밋하지 마세요.

현재 contracts/, integrations/, deploy의 mlops 전용 파일, 일부 tools/에는 기존 스포츠 모델 운영을 위한 호환 코드가 남아 있습니다. 이는 코어 API가 의존해야 할 범용 코드가 아니며 검증된 순서에 따라 모델 저장소로 이전할 대상입니다.

## 테스트

    .venv/bin/python -m unittest discover -s tests -q
    node tests/workshop.mjs
    node tests/charts.mjs
    node tests/detection-visuals.mjs

브라우저 검증은 사용자 카탈로그와 분리된 임시 GEPPETTO_HOME에서만 실행해야 합니다.

## SDK와 컨테이너 빌드

SDK wheel은 생성물이므로 Git에 커밋하지 않습니다.

    uv build --wheel --out-dir dist sdk
    docker build -t geppetto:dev -f deploy/Dockerfile .

Docker 빌드는 SDK wheel을 별도 빌드 단계에서 자동 생성합니다. 서버의 SDK 다운로드 화면은 빌드된 wheel을 제공합니다.

## 문서

- [저장소 경계와 모델 연동](docs/REPOSITORY_BOUNDARIES.md)
- [아키텍처](docs/ARCHITECTURE.md)
- [Agent 운영](docs/AGENT_OPERATIONS.md)
- [모델·데이터 계약](docs/CONTRACTS.md)
- [Python SDK](docs/PYTHON_SDK.md)
- [배포](docs/DEPLOYMENT.md)
- [Kubernetes 운영](docs/KUBERNETES_OPERATIONS.md)
- [구현·검증 상태](docs/STATUS.md)

운영 기록은 특정 시점의 증거이며 현재 동작 계약을 대신하지 않습니다. 변경할 때는 테스트와 현재 계약 문서를 함께 갱신하세요.
