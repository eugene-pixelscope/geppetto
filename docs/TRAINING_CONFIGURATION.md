# New training

현재 배포에서는 상단 `New training`으로 학습 설정을 준비한다.

1. Environment & architecture: 환경, 프로젝트, 아키텍처, 학습 이름.
2. Dataset: 선택한 환경·분류의 데이터셋과 분할/버전 확인.
3. Parameters: epochs, batch, 입력 크기, learning rate, 지원하는 경우 patience.
4. Review: 설정 저장, JSON 다운로드. Saved configurations에서 다시 열어 수정 가능.

설정 저장은 `training_draft` 문서만 기록하며 학습 run이나 모델을 만들지 않는다. 저장한 설정은 준비용 명세이며 실행 중인 학습 설정을 수정하지 않는다. 실제 제출 어댑터·모델 이미지·데이터 검증이 준비되지 않아 현재 `Start training`은 비활성화다. 이 문서에서 다루는 준비용 아키텍처 선택은 실행 템플릿의 live_verified 승인과 별개다.

아키텍처 선택: YOLO11x, YOLO26x, YOLO26 P2, YOLO26 P2 custom(CBAM + ESMoE_DCN + NWD), Jersey ViT. GPU 수/실제 실행 장치 선택과 임의 코드 업로드는 준비용 폼에 포함하지 않는다. 표시 기본값은 편집 가능한 초기 설정이지 GPU 메모리/실행 성공 보장이 아니다. 실제 제출 연결 시 설정을 다시 검증해야 한다.

API: GET `/api/training/options`, POST `/api/training/preview`, POST `/api/training/drafts`. 서버가 프로젝트/아키텍처/데이터 분류, 환경 소속, 숫자 범위, YOLO 입력 크기(32 배수)를 확인한다. 초안 업데이트는 기존 ID로 수행하며 없는 ID는 거부한다. readonly 모드에서는 preview/save를 거부한다.

환경 화면은 Home/Environments 명칭과 흑백 팔레트로 정리했고 로고의 내부 사각형을 제거했다. `layout.css`가 공통 폰트·버튼·입력·카드 크기를 관리한다.
