# MacWhisper vs meeting-transcriber 비교 분석

## 1. MacWhisper 아키텍처 요약

MacWhisper는 Argmax의 WhisperKit 프레임워크 기반 macOS 네이티브 STT 애플리케이션.
OpenAI Whisper를 CoreML로 변환하여 Apple Neural Engine에서 실행.

### 핵심 구성요소

| 컴포넌트 | 기술 | 비고 |
|---------|------|------|
| **STT 엔진** | WhisperKit CoreML (3종: WhisperKit, WhisperKit Pro, ParakeetKit Pro) | CoreML 3-Stage: MelSpectrogram → AudioEncoder → TextDecoder |
| **화자분리** | SpeakerKit (Pyannote CoreML 포팅, W8A16 양자화) | Segmenter → Embedder → Clusterer |
| **데이터** | SQLite + GRDB (54개 migration), FTS5 2개 인덱스 | 세션별 full-text + 받아쓰기 검색 |
| **오디오** | 멀티트랙 분리 녹음 (앱/마이크/merged) | 3개 트랙 동시 저장 |
| **UI** | SwiftUI 네이티브 macOS 앱 | |
| **모델 선택** | 칩셋 자동 감지 (A12~M4), 양자화 레벨별 모델 | |

### 모델 크기 (large-v3)

| 컴포넌트 | 크기 |
|---------|------|
| MelSpectrogram | 1KB |
| AudioEncoder | 1.2GB |
| TextDecoder | 1.5GB |
| **합계** | ~2.7GB (full), ~626MB (양자화) |

### SpeakerKit 모델

| 컴포넌트 | 양자화 | 이유 |
|---------|--------|------|
| Segmenter | W8A16 | 속도 우선 |
| Embedder | W8A16 | 속도 우선 |
| Clusterer | W32A32 (full) | 거리 계산 정밀도 필요 |

### 사용 통계 (실제 데이터 기준)

- 252개 세션, 140,080 전사 라인, 1,358 화자
- 97.6% diarization 적용
- 주 사용: 회의 녹음 (ko/auto)

---

## 2. 비교 분석

### 2.1 STT 엔진

| 항목 | MacWhisper | meeting-transcriber |
|------|-----------|-------------------|
| **프레임워크** | WhisperKit (CoreML) | mlx-whisper (MLX) |
| **기본 모델** | whisper-large-v3 (M1+) | whisper-medium-ko-zeroth |
| **모델 크기** | ~2.7GB (full) / 626MB (양자화) | ~1.5GB |
| **가속** | Neural Engine + GPU | GPU (MLX Metal) |
| **한국어** | 자동 감지 or 수동 설정 | 한국어 전용 파인튜닝 모델 |
| **양자화** | 칩셋별 자동 선택 (full/626MB/547MB) | 없음 (full precision) |
| **배치** | 스트리밍 디코딩 | batch_size=12 |

**분석**: MacWhisper는 large-v3로 범용 정확도가 높지만, meeting-transcriber는 한국어 파인튜닝 모델(whisper-medium-ko-zeroth)로 한국어 특화. medium 모델이라 메모리 절약.

### 2.2 화자분리

| 항목 | MacWhisper | meeting-transcriber |
|------|-----------|-------------------|
| **엔진** | SpeakerKit (Pyannote CoreML) | pyannote-audio 3.1 (Python) |
| **양자화** | W8A16 (Segmenter/Embedder) | 없음 (full precision) |
| **Device** | Neural Engine/GPU | CPU 강제 (MPS 버그) |
| **속도** | 빠름 (CoreML 최적화) | 느림 (CPU, 타임아웃 10분) |
| **정확도** | 양자화로 약간 손실 가능 | full precision으로 최대 정확도 |

**분석**: MacWhisper가 CoreML 최적화로 압도적으로 빠름. meeting-transcriber는 CPU 강제라 느리지만 정확도 면에서 우위. pyannote MPS 버그 해결되면 큰 개선 가능.

### 2.3 오디오 캡처

| 항목 | MacWhisper | meeting-transcriber |
|------|-----------|-------------------|
| **방식** | macOS 네이티브 API | ffmpeg avfoundation |
| **트랙** | 멀티트랙 (앱+마이크+merged) | 싱글 트랙 (모노 16kHz) |
| **시스템 오디오** | 네이티브 지원 | BlackHole 의존 |
| **장점** | 고품질 분리 녹음 | 설치 간편, ffmpeg 범용 |

**분석**: MacWhisper의 멀티트랙 분리 녹음은 STT 품질에 큰 이점. 앱 오디오(상대방)와 마이크(사용자)를 분리하면 화자분리 정확도가 크게 향상. meeting-transcriber는 모노 믹스라 화자분리에 불리.

### 2.4 데이터/검색

| 항목 | MacWhisper | meeting-transcriber |
|------|-----------|-------------------|
| **DB** | SQLite + GRDB (54 migrations) | SQLite (job_queue) + JSON 체크포인트 |
| **전문검색** | FTS5 (2개 인덱스) | FTS5 (unicode61) + ChromaDB 벡터 |
| **벡터검색** | 없음 | ChromaDB (multilingual-e5-small, 384차원) |
| **RAG** | AI 요약만 (외부 API?) | 로컬 EXAONE 3.5 + RRF 하이브리드 검색 |

**분석**: meeting-transcriber가 검색/RAG에서 압도적 우위. 벡터+FTS5 하이브리드 검색 + 로컬 LLM 채팅은 MacWhisper에 없는 기능.

### 2.5 LLM 후처리

| 항목 | MacWhisper | meeting-transcriber |
|------|-----------|-------------------|
| **발화 보정** | 없음 (또는 외부 API) | EXAONE 3.5 7.8B 로컬 |
| **요약** | AI Summary (외부 API 추정) | EXAONE 로컬 마크다운 회의록 |
| **RAG 채팅** | 없음 | 로컬 LLM + 하이브리드 검색 |

**분석**: MacWhisper는 STT 특화, meeting-transcriber는 전사 후 AI 활용까지 포괄.

### 2.6 모델 관리

| 항목 | MacWhisper | meeting-transcriber |
|------|-----------|-------------------|
| **전략** | 칩셋별 자동 모델 선택 + 다운로드 | 고정 모델 + 뮤텍스 순차 로드 |
| **메모리** | CoreML 최적화 (OS 관리) | 수동 gc.collect() + Metal 캐시 정리 |
| **양자화** | 모델별 다단계 양자화 | 없음 |
| **동시 로드** | CoreML이 자동 관리 | 한 번에 하나만 (asyncio.Lock) |

### 2.7 UI/UX

| 항목 | MacWhisper | meeting-transcriber |
|------|-----------|-------------------|
| **UI** | SwiftUI 네이티브 | 웹 SPA (HTML/CSS/JS) + rumps 메뉴바 |
| **접근성** | macOS 전용 | 브라우저 범용 |
| **실시간** | 네이티브 바인딩 | WebSocket + REST 폴링 |

---

## 3. meeting-transcriber 개선 기회 (MacWhisper 참고)

### 3.1 높은 우선순위

| 개선 | 현재 | MacWhisper 참고 | 기대 효과 |
|------|------|---------------|----------|
| **pyannote MPS 지원** | CPU 강제 (느림) | CoreML로 GPU 활용 | 화자분리 속도 5~10x 향상 |
| **모델 양자화** | full precision | W8A16 양자화 | 메모리 50% 절감, 속도 향상 |
| **whisper-large-v3 지원** | medium 고정 | 칩셋별 자동 선택 | 정확도 향상 (M4 16GB에서 가능) |

### 3.2 중간 우선순위

| 개선 | 현재 | MacWhisper 참고 | 기대 효과 |
|------|------|---------------|----------|
| **멀티트랙 녹음** | 모노 싱글 트랙 | 앱+마이크 분리 | STT/화자분리 정확도 향상 |
| **Turbo 모델 지원** | medium 고정 | large-v3_turbo | 디코딩 속도 향상 (KV-cache prefill) |
| **DB 마이그레이션** | JSON 체크포인트 | GRDB 54개 migration | 데이터 무결성 + 스키마 진화 |

### 3.3 낮은 우선순위 (차별화 유지)

| 항목 | 현재 우위 | 유지 전략 |
|------|----------|----------|
| **하이브리드 검색** | 벡터+FTS5 RRF | MacWhisper에 없는 핵심 기능 |
| **로컬 LLM** | EXAONE 채팅/보정/요약 | 외부 API 없는 완전 로컬 |
| **파이프라인 회복성** | JSON 체크포인트 재개 | 이미 잘 구현됨 |
| **서멀 관리** | 2건/3분 쿨다운 | 팬리스 환경 필수 |

---

## 4. 핵심 결론

### MacWhisper 강점 (우리가 배울 점)
1. **CoreML 최적화**: Neural Engine 활용으로 STT/화자분리 속도 극대화
2. **멀티트랙 녹음**: 앱/마이크 분리로 전사 품질 향상
3. **칩셋별 모델 적응**: 디바이스 성능에 맞는 자동 모델 선택
4. **양자화 스펙트럼**: 동일 모델의 다단계 양자화 (메모리/성능 트레이드오프)

### meeting-transcriber 강점 (우리만의 차별화)
1. **로컬 LLM 통합**: 발화 보정 + 회의록 요약 + RAG 채팅 (MacWhisper에 없음)
2. **하이브리드 검색**: 벡터+FTS5 의미 검색 (MacWhisper는 키워드만)
3. **한국어 특화**: whisper-medium-ko 파인튜닝 모델
4. **파이프라인 회복성**: 체크포인트 기반 실패 재개
5. **온디맨드 LLM**: 전사만 기본 실행, 요약은 사용자 요청 시
6. **오픈소스 스택**: 벤더 종속 없음 (MacWhisper는 Argmax 독점)

### 전략적 포지셔닝
```
MacWhisper = "빠르고 정확한 전사" (STT 특화)
meeting-transcriber = "전사 + AI 인텔리전스" (엔드투엔드 회의 분석)
```

MacWhisper가 STT 속도/UX에서 앞서지만, meeting-transcriber는 전사 이후의 AI 활용(LLM 보정, 하이브리드 검색, RAG 채팅)에서 차별화. 두 시스템은 경쟁이 아니라 **다른 사용 시나리오**를 타겟.
