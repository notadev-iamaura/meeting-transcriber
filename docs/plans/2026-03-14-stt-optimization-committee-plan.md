# ✅ RESOLVED — STT 최적화 위원회 합의안

> **상태 (2026-04-26)**: 본 합의안의 핵심 항목들은 이후 사이클에서 적용됨.
> - VAD/환각 필터: 운영 적용 (PR #15·#16 안정화 사이클)
> - STT 모델 선정: 6 회의 다중 파일 벤치마크 후 large-v3-turbo 채택 (PR #18)
> - LLM 보정 단계: 용어집 시스템 검증 완료
>
> 측정 매트릭스: `docs/BENCHMARK.md §1.1`. 본 문서는 의사결정 기록 보존용.

## 위원회 구성
1. **STT 모델 전문가** — 모델 파라미터, 디코딩 설정, 성능 최적화
2. **전처리/후처리 전문가** — VAD, 환각 필터링, 텍스트 후처리
3. **파이프라인 아키텍트** — 시스템 구조, 메모리 관리, 에러 복구
4. **품질 보증 전문가** — 테스트 커버리지, 엣지 케이스, 자동화

## 현황 요약

| 항목 | 현재 값 | 비고 |
|------|---------|------|
| STT 모델 | komixv2-mlx (medium) | CER 14.24% (Zeroth-Korean) |
| 디코딩 | greedy (beam 미지원) | mlx-whisper NotImplementedError |
| VAD | Silero VAD v5 활성화 | clip_timestamps 방식 |
| 숫자 정규화 | Level 1 (보수적) | 단위어 동반 시만 변환 |
| initial_prompt | null | 환각 유발 확인으로 비활성 |
| condition_on_previous_text | 기본값(True) | **명시적 설정 없음** |

## 합의된 개선안 (4명 중 3명 이상 찬성)

### 개선안 1: 환각 감지 및 필터링 (찬성 4/4)

**문제**: Whisper 모델은 무음/잡음 구간에서 환각(hallucination)을 생성한다.
ghost613은 물론, komixv2도 특정 조건에서 반복 패턴("감사합니다 감사합니다...")을 생성할 수 있다.

**구현**:
- `steps/hallucination_filter.py` 신규 모듈 생성
- `compression_ratio > 2.4` 기준 환각 세그먼트 필터링
- 반복 패턴 감지: 동일 문자열 3회 이상 연속 반복 감지
- `avg_logprob < -1.0` 저신뢰도 세그먼트 경고 로깅
- config.yaml에 `hallucination_filter` 섹션 추가 (독립 비활성화 가능)

**예상 효과**: 환각 세그먼트 제거로 전사 품질 안정성 향상
**구현 난이도**: 하
**리스크**: 낮음 — 기존 동작 변경 없이 후처리 레이어 추가

### 개선안 2: condition_on_previous_text=False 명시 설정 (찬성 4/4)

**문제**: 기본값 True는 이전 윈도우 텍스트를 다음 윈도우 prompt로 전달하여,
오류가 전파(cascading error)되는 위험이 있다. ghost613 조기 종료의 악화 요인이기도 했다.

**구현**:
- config.yaml의 `stt` 섹션에 `condition_on_previous_text: false` 추가
- `transcriber.py`의 `_build_transcribe_kwargs()`에서 해당 설정 전달
- 각 30초 윈도우가 독립적으로 전사됨

**예상 효과**: 오류 전파 방지, 개별 윈도우 독립 전사로 안정성 향상
**구현 난이도**: 하 (config 1줄 + 코드 2줄)
**리스크**: 낮음 — 문맥 연속성 약간 감소하나, VAD가 구간 분리를 담당

### 개선안 3: 후처리 텍스트 정리 파이프라인 (찬성 3/4)

**문제**: Whisper 출력에 불필요한 연속 공백, 줄바꿈 문자, 앞뒤 공백이 포함될 수 있다.

**구현**:
- `steps/text_postprocessor.py` 신규 모듈 생성
- 연속 공백 → 단일 공백 정규화
- 앞뒤 공백/줄바꿈 정리
- NFC 유니코드 정규화 (이미 transcriber에 있으나 일관성 위해 후처리에도)
- pipeline.py의 TRANSCRIBE 단계 직후에 호출
- config.yaml에 `text_postprocessing.enabled: true` 추가

**예상 효과**: 텍스트 정결성 향상, 후속 단계 처리 용이
**구현 난이도**: 하
**리스크**: 매우 낮음

### 개선안 4: corrector→summarizer 모델 유지 최적화 (찬성 3/4)

**문제**: corrector와 summarizer가 동일 LLM(EXAONE)을 사용하는데,
현재 corrector 종료 시 모델 언로드 → summarizer 시작 시 재로드하여 불필요한 오버헤드 발생.

**구현**:
- `core/pipeline.py`의 corrector 단계에서 `keep_loaded=True` 전달
- `ModelLoadManager.acquire(..., keep_loaded=True)` 이미 구현되어 있음
- summarizer 완료 후 자동 언로드

**예상 효과**: LLM 로드/언로드 1회 절약 (~30초, 메모리 안정)
**구현 난이도**: 하 (코드 1줄 변경)
**리스크**: 낮음 — skip_llm_steps=true 시 해당 없음

### 개선안 5: no_speech_threshold 활용 강화 (찬성 3/4)

**문제**: Whisper가 무음으로 판단한 세그먼트(no_speech_prob > 0.6)도 전사 결과에 포함될 수 있다.
현재 _parse_segments에서 이 값을 저장만 하고 필터링에 사용하지 않음.

**구현**:
- `hallucination_filter.py`에 no_speech_prob 기반 필터링 포함
- `no_speech_prob > 0.6` 세그먼트 제거 (기본 임계값)
- config.yaml의 `hallucination_filter.no_speech_threshold: 0.6`

**예상 효과**: 무음 구간 환각 세그먼트 추가 필터링
**구현 난이도**: 하
**리스크**: 낮음 — VAD와 이중 필터링으로 안전

## 기각된 개선안

| 개선안 | 기각 이유 | 투표 |
|--------|----------|------|
| 영어 혼합 발화 별도 처리 | 구현 복잡도 대비 효과 미미, komixv2가 영어도 인식 | 반대 3/4 |
| 숫자 정규화 Level 2 전환 | 오탈자 위험 증가 ("이 프로젝트" → "2 프로젝트") | 반대 3/4 |
| without_timestamps 모드 | 세그먼트 타임스탬프 상실 → 화자분리 병합 불가 | 반대 4/4 |
| beam_size 활성화 | mlx-whisper에서 NotImplementedError | 불가 |
| temperature fallback | komixv2에서 효과 미미, 복잡도 증가 | 보류 3/4 |
| VAD 파라미터 대폭 변경 | 현재 값이 합리적, 변경 시 회귀 리스크 | 보류 4/4 |

## 구현 순서

```
1. config.yaml 업데이트 (condition_on_previous_text, 신규 섹션)
2. steps/hallucination_filter.py 신규 생성
3. steps/text_postprocessor.py 신규 생성
4. steps/transcriber.py 수정 (condition_on_previous_text 전달)
5. core/pipeline.py 수정 (환각 필터 + 후처리 통합, keep_loaded)
6. 테스트 추가 (test_hallucination_filter.py, test_text_postprocessor.py)
7. 기존 테스트 전체 통과 확인
```

## 제약 조건

- 기존 테스트 1451개 전부 통과 유지
- RAM 9.5GB 피크 제한 유지
- 각 개선은 config로 독립 비활성화 가능
- 코딩 규칙 준수 (타입 힌트, 한국어 docstring, pathlib, logging)
