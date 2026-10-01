# 실제 공 모델 기록

2026-09-26 확인. 기존 학습/클러스터는 변경하지 않습니다.

- YOLO11x 1920: 기존 기준 가중치 평가 완료, box mAP50–95 0.973393.
- YOLO26x 1920: 100 epoch 모델 평가 완료, box mAP50–95 0.922529.
- YOLO26 P2 1920: 실제 복구 학습 진행 중. 확인 시 16/245 epoch, GPU 0.
- 두 완료 모델은 과거 test-as-val 평가이며 P2의 timestamp 분할과 직접 비교하지 않습니다.
- 가져온 완료 기록은 기존 가중치의 평가 작업입니다. 원본 학습 시각/epoch 곡선을 만들어 표시하지 않습니다.
- 현재 로컬 고래는 외부 기록의 카탈로그 작업 공간입니다. 실제 Kubernetes Agent 연결 완료를 뜻하지 않습니다.

`tools/sync_real_ball.py`는 검증한 MLflow run 3개만 읽고, 명시적으로 선택하지 않은 smoke/잘못된 해상도/중단 실험을 가져오지 않습니다. `--watch`는 60초 주기 조회이며 실패하면 마지막 관측을 유지하고 조회 오류를 표시합니다. 모델 ID는 갱신 시 보존합니다.

실행 중 tmux: `geppetto-catalog`(8091), `geppetto-real-sync`(조회만). 서버 재시작 스크립트: `tools/serve_local_catalog.sh`. 재부팅 후 자동 시작 서비스는 설치하지 않았습니다.

사용자 지정 순서: YOLO11x (1920) → YOLO26x → custom layer. 앞선 두 모델이 이미 존재하므로 중복 학습하지 않았습니다. 현재 P2를 중단하지 않습니다. custom 단계는 CBAM/ESMoE_DCN/NWD 전체 설정 또는 단계별 비교 중 선택 대기이며 아직 제출/예약하지 않았습니다.
