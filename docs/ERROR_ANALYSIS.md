# 공 모델 오류 분석

피노키오의 학습 작업 상세의 **오류 분석** 탭에서 이미지별 예측 JSON을 가져온다.
원본 학습 코드·데이터는 수정하지 않으며 가져오기는 추론을 실행하지 않는다.
실제 예측이 없으면 빈 상태를 표시한다. 합성 데이터의 학습 기록에서는 명시적인 합성 예시를 생성할 수 있다.

## 입력 계약

`POST /api/runs/{run_id}/error-analysis` (`X-Geppetto-Request: 1` 필요):

```json
{
  "dataset_id": "등록된 데이터 id",
  "dataset_version": "전체 버전 문자열",
  "checkpoint": "평가에 사용한 가중치 SHA-256 또는 식별자",
  "split": "validation",
  "prediction_floor": 0.01,
  "images": [
    {"index": 208457, "predictions": [{"box": [0.5, 0.5, 0.02, 0.02], "score": 0.8}]},
    {"index": 208458, "predictions": []}
  ]
}
```

- `split`: validation 또는 test. 학습 split을 평가 결과로 표시하지 않는다.
- `index`: `GET /api/datasets/{id}/samples?split=validation&offset=0&limit=12`의 전역 index.
  위 index는 형식 예시다. 데이터 버전과 split에 속하는지 서버에서 확인한다.
- `box`: 원본 이미지 크기로 정규화한 cx, cy, width, height. letterbox 좌표라면 먼저 원본 좌표로 복원해야 한다.
- 단일 공 클래스(0)만 지원한다. 클래스별 분석·추적·pose 오차는 포함하지 않는다.
- `predictions: []`는 평가했으나 검출하지 못한 이미지다. 결과에서 누락된 이미지는 평가 범위에 포함하지 않는다.
- 최대 100,000장, 이미지당 예측 300개, 요청 16 MiB. 큰 평가를 나누어 가져오면 각각 별도 보고서이며 합산하지 않는다.
- `prediction_floor`: 예측을 내보낼 때 적용한 최소 점수. 이보다 낮은 기준은 분석할 수 없다.
- checkpoint 식별자는 제출자의 기록이며 서버가 모델 가중치와 실제 추론의 대응을 인증하지 않는다.

정답은 등록된 원본 YOLO/YOLO pose 라벨에서 읽는다. 빈 라벨 파일은 음성 이미지로,
없는 라벨 파일은 오류로 취급한다. 가져온 이미지·라벨 내용 해시를 보고서에 고정하고,
실패 이미지 조회 시 변경된 이미지 제공을 거부한다. 전체 데이터셋의 내용 검증 상태를 변경하지 않는다.
원본 라벨·박스·예측·카메라·이미지 크기는 로컬 카탈로그 evaluation 문서에 저장된다.

## 분석 규칙과 해석

예측 점수 내림차순으로 가장 높은 IoU의 미사용 정답과 일대일로 매칭한다.
기준 이상의 짝은 TP, 미매칭 정답은 FN, 미매칭 예측은 FP다. 중복 검출은 FP가 된다.
위치 오차로 IoU 기준에 못 미친 경우 FN과 FP에 동시에 기여한다.
표시한 검출률·정밀도는 고정 기준의 진단 지표로, 모델 평가기의 mAP를 대체하지 않는다.

- 히트맵: 원본 화면을 8×8로 나누고 FN은 정답 중심, FP는 예측 중심으로 집계한다.
- FN 비율 = FN / 해당 영역 정답 수. FP 비율 = FP / 해당 영역 예측 수(배경 전체에 대한 FPR가 아님).
- 분모 0은 —, 표본 20개 미만은 점선. 건수와 비율은 별도 표시한다.
- 히트맵 영역 클릭은 실패 장면만 필터링한다. 카메라 필터는 히트맵·통계 전체에 적용한다.
- 크기별 검출률: 원본 픽셀 √(bbox 면적)의 <16, 16–32, 32–64, ≥64 구간. 리사이즈 후 입력 크기가 아니다.
- 카메라: 파일명의 `___` 앞부분으로 추정한 그룹. 없는 경우 미기록. 촬영 시간/날씨는 추측하지 않는다.
- 이미지 좌표를 합치는 방식이며 경기장 호모그래피 히트맵은 아니다. 카메라별 필터를 함께 사용한다.
- 정답·예측 박스와 점수, 미검출·오검출 구분을 실패 이미지 위에 겹친다.
- 평가 범위와 split 전체 수를 표시한다. 부분 평가 또는 필터된 결과를 전체 모델 성능으로 해석하면 안 된다.

## 검증

`tests/test_error_analysis.py`: 중복 매칭·음성 이미지·무검출·크기·카메라·영역 필터·분모 0·점수 하한,
버전/split/중복 index/잘못된 예측 거부, 이미지 변조 검사, 합성 표시.

`tools/browser_error_analysis.py`: 합성 결과 생성·JSON 파일 가져오기·히트맵·영역 선택·점수 변경·부분 평가·반응형.
GPU 추론이나 실제 학습은 실행하지 않는다.

## 실패 이미지 탐색과 실제 평가 (2026-09-27)

- 히트맵 영역 선택 → 해당 영역의 실패 이미지만 표시.
- 오류 많은 순 / 원본 순 / 오탐 점수 높은 순 정렬. 영역 선택 시 선택 영역의 오류 수로 정렬한다.
- `확대해서 확인`은 선택한 오류 박스 주변을 먼저 보여준다. 정답/예측 표시 전환, 박스별 확대, 드래그 이동, 확대/축소, 전체 보기, 현재 페이지 내 이전/다음 이미지를 제공한다.
- 평가 데이터 버전을 보고서에 별도로 고정한다. 기존 MLflow 실행에 학습 데이터 버전이 누락되어도 등록된 검증/테스트 데이터로 평가 결과를 가져올 수 있다. 학습 당시 데이터였다고 추정하거나 원본 학습 설정을 수정하지 않는다.
- 조회 API에 `sort=errors|index|confidence`가 추가되었다. 기존 confidence/IoU/camera/kind/cell/offset 필터와 조합한다.

실제 P2 학습은 사용자 요청으로 종료했다. 마지막 체크포인트
`/results/cff429d2c2694646991235059cdcfcb7/train/weights/last.pt`를 읽기 전용으로 사용해
등록된 validation 목록에서 균등 간격으로 선택한 128장을 imgsz=1920, export confidence=0.01로 추론했다.
이는 23,187장 중 일부의 진단 결과이며 전체 성능이나 랜덤 표본의 불편 추정치가 아니다.
confidence=0.5 / IoU=0.5에서 정답 139개, TP 101, FN 38, FP 6이다.
원본 파일 경로에 images/train이 있어도 평가 split은 등록된 recording-timestamp 분할 목록으로 판정한다.
평가용 Pod/ConfigMap은 삭제했고 새 학습을 시작하지 않았다.

`tools/export_ball_predictions.py`는 기존 Ultralytics 런타임에서 실제 예측 JSON을 만드는 도구다.
manifest에는 등록된 `dataset_id`, `dataset_version`, `root`, `split`과 샘플 API의 `samples`(전역 index, image, split)를 넣는다.
없는 예측도 빈 배열로 포함하고, 원본 이미지 좌표계의 xywhn 및 가중치 SHA-256을 기록한다.

```bash
python tools/export_ball_predictions.py --manifest manifest.json --weights last.pt \
  --output predictions.json --imgsz 1920 --device 0 --floor 0.01 \
  --ultralytics-root /app/ball/ultralytics-src
```

원본 모델에 맞는 Ultralytics/커스텀 레이어 런타임을 사용해야 한다. 기본 device는 CPU이며 GPU 사용은 명시한다.
