# 단계 0 계약 v0.1

기준 문서: `/home/eugene/sports-ml/mlops/GEPPETTO_DESIGN.md` (2026-09-26).
합의 사항은 유지하고, 아래 JSON 형식 및 Python 도구는 초기 구현 결정이다.
프런트엔드·서버 프레임워크·DB는 단계 1에서 결정한다.

## 모델 설정

`tools/export_jnr.py`는 모델 환경에서 실행하는 독립 어댑터다. 현재는 새 프로젝트의
전용 Python 환경에서 config.py 의존성만 설치해 검증했다. 실제 학습 이미지 검증은 미완료다.
전체 커밋과 설정·프리셋·학습 코드·src·tools 파일 해시를 기록한다.
원본 데이터는 코드 해시에 포함하지 않고 별도 manifest로 관리한다.
작업 트리가 dirty이거나 커밋이 다르면 추출을 거부한다. 서버는 모델 모듈을 import하지 않는다.
스키마 추출은 신뢰·검토된 코드 실행이며 보안 샌드박스가 아니다.

기본값 → 첫 YAML 프리셋 → 사용자 변경 → 관리 값 순으로 병합한다.
원본 YAML은 `safe_load`하며 모델의 `unsafe_load`는 호출하지 않는다.
JSON Schema는 모델 객체의 추가 필드를 거부한다. 모델이 허용하는 `model_kwargs` 사전은
열린 상태이므로 개별 아키텍처 지원 여부는 실행 이미지에서 별도 확인해야 한다.
Pydantic이 타입을 변환하거나 알 수 없는 필드를 버리기 전에 JSON Schema로 검사한다.
설정 파일에 없는 의미적 제약(예: 메모리 한계, 유효 아키텍처 조합)은 검증 완료가 아니다.

경로는 컨테이너 기준 절대 경로다. `OCR_DIR=/workspace/model`을 사용해 호스트 경로를
내보낸 기본값에 섞지 않는다. 사용자에게 원본 데이터·캐시·출력·추적 정책을 덮어쓰게 하지 않는다.
원본 프리셋의 W&B=true는 보존하고 제페토 미리보기에서 명시적으로 false로 확정한다.
MLflow 어댑터가 구현될 때까지 실제 학습 제출은 제공하지 않는다.
스키마 기본 layers=2/aux weight=0과 프리셋 layers=1/aux weight=0.1을 구분해 보존한다.

`preview`는 실행 승인이 아니며 `executable=false`다. 실제 제출에는 이미지 digest,
데이터 버전과 내용 검증, 관리 자원, 추적 ID, 실행 환경의 Config 재검증이 추가로 필요하다.
해시는 변경 탐지 수단이며 서명·신뢰성 보증이 아니다.

## 데이터 등록 경계

`inspect-data`는 지정된 루트의 필요한 하위 디렉터리 존재만 조회한다.
이미지·라벨 유효성이나 실제 필터 후 샘플 수를 확인했다고 표시하지 않는다.
선택 프리셋의 reid_all은 train/valid/test/challenge를 모두 포함한다.
`soccernet_test_sample`은 validation 역할이며 독립 최종 테스트 성능으로 간주하지 않는다.
실제 로더는 invalid label 필터, validation 팀 제외, 마스킹, 샘플 제한을 적용한다.
`train_val_split=0.8`만으로 실제 split을 추정하지 않는다.

향후 데이터 버전 레코드에는 원본 읽기 전용 위치, 이미지·라벨의 상대 경로와 콘텐츠 SHA-256,
명시적 split, 전처리·필터 조건, 코드 버전, 실제 사용 샘플 manifest를 저장한다.
전체 콘텐츠 스캔 비용은 데이터 위치와 크기 확인 후 결정한다. 캐시와 출력은 별도 쓰기 마운트다.
실행 직전 내용 변화를 검사하고 달라진 버전은 거부한다. 원본 불변 보존은 별도 운영 약속이다.

## 실행·결과 계약 (후속 구현 경계)

- 제페토: submission_id, idempotency_key, category_id, dataset_version_ids,
  template_version, code_commit, image_digest, schema_sha256, config_sha256, final_config,
  parent_execution_id, relation(train/evaluate/resume), schedule_id, stop_requested_at.
- Airflow 연결은 dag_id/run_id/task_id/map_index/try_number의 **배열**, MLflow run_id도 배열이다.
  제출 직후에는 두 배열 모두 비어 있을 수 있다.
- 상태는 원본 소유자별 값, observed_at, last_success_at, fetch_error를 함께 저장한다.
  조회 오류는 unknown/stale로 처리하며 failed로 변환하지 않는다.
- 지표는 이름, 방향, 단위, step 의미, 원본 값과 평가 조건을 보유한다. null과 0을 구분한다.
- 결과는 역할, 원본 실행, 저장소 내 상대 경로, 크기·해시, 완료 표시, 재개 가능 여부를 가진다.
  완성된 파일만 루트 안에서 제공하며 symlink/path traversal을 차단한다.
  현재 체크포인트 저장은 원자적 완료 계약이 없어 다운로드 제공 전 어댑터 보완이 필요하다.

이 실행·결과 계약은 아직 저장/API 구현이 없는 설계이며 단계 2에서 구체화한다.
현재 finetune_from은 새 optimizer를 쓰는 초기화다. save_optimizer 기본값=false이고
중단 저장은 global_step=-1 및 save_best_only=true 조합에서 파일이 남지 않을 수 있다.
완전 재개와 신뢰 가능한 중단 체크포인트 기능은 노출하지 않는다.

## 후속 구현 반영 (2026-09-26)

위 단계 0 기록 이후 실행·데이터·결과 계약의 SQLite/API/UI 구현을 추가했다.
구현된 필드와 상태 조정 정책은 [ARCHITECTURE.md](ARCHITECTURE.md), 실제 검증 범위는
[STATUS.md](STATUS.md)가 최신 기준이다. 초기 문단의 “아직 저장/API 구현 없음”은 단계 0 당시 상태다.
웹 UI의 live 템플릿 게이트와 컨테이너 어댑터는 구현되었지만 실제 GPU 실증은 여전히 미완료다.
