# Recap 홍보문구

이 문서는 공개 README, GitHub About, SNS, 커뮤니티 글에 재사용할 수 있는 담백한 문구 모음이다. 과장된 "완전 자동", "완벽한 기억", "RAG 대체" 표현은 피하고, 현재 제품이 실제로 지향하는 범위를 설명한다.

## 한 줄 소개

한국어 회의를 로컬에서 녹음하고, 원문 근거가 있는 Decision Wiki로 정리하는 Apple Silicon용 오픈소스 도구.

## GitHub About

Local Korean meeting recorder and cited Decision Wiki for Apple Silicon.

## 짧은 설명

Recap은 Apple Silicon Mac에서 한국어 회의를 녹음하고, 전사·요약·검색·Decision Wiki 정리 흐름까지 로컬로 처리하는 오픈소스 프로젝트입니다. 회의가 끝난 뒤 사라지는 대화를 결정사항, 액션아이템, 원문 timestamp 근거와 함께 다시 찾을 수 있게 만드는 것이 목표입니다.

## README용 소개문

회의록은 남겨도 다시 찾기 어렵습니다. Recap은 회의 원문 전사와 RAG 검색을 유지하면서, 중요한 결정사항과 액션아이템을 별도의 Markdown Wiki 레이어로 정리합니다. 모든 처리는 Apple Silicon Mac에서 로컬로 실행되며, 데이터는 외부 서버로 전송되지 않습니다.

## SNS 짧은 문구

회의 전사문을 쌓아두는 것에서 한 걸음 더 나아가, 결정사항과 액션아이템을 위키로 정리하는 도구를 만들고 있습니다.

Recap은 한국어 회의를 로컬에서 녹음하고, 전사·요약·검색·Decision Wiki 정리 흐름까지 Apple Silicon Mac에서 처리하는 오픈소스 프로젝트입니다.

## SNS 긴 문구

Recap을 오픈소스로 준비하고 있습니다.

방향은 단순한 회의 전사 앱이 아니라, 회의가 끝난 뒤 결정사항과 액션아이템을 근거와 함께 다시 찾을 수 있는 로컬 Decision Wiki입니다.

- 한국어 회의 녹음/전사
- 화자 분리와 로컬 LLM 요약
- ChromaDB + FTS5 기반 회의 검색
- 원문 timestamp 근거가 있는 Decision Wiki
- Apple Silicon Mac에서 로컬 실행

아직 다듬는 중이지만, 회의 데이터를 외부 API에 보내기 어려운 팀과 개인에게 유용한 방향으로 만들고 있습니다.

## Hacker News / Reddit 초안

I am building Recap, an open-source local meeting transcriber for Korean meetings on Apple Silicon Macs.

The part I care about most is not just transcription. After a meeting is processed, Recap can organize decisions and action items into a local Markdown-based Decision Wiki with timestamp citations back to the original transcript. The goal is to make meetings searchable as working memory, not just store long transcripts.

It runs locally with MLX, mlx-whisper, pyannote, local LLMs, ChromaDB, and SQLite FTS5. The project is still early and Apple Silicon only, but I am trying to keep the positioning narrow: local Korean meeting memory with cited decisions.

## 게시 순서

1. GitHub README 정리 후 `v0.1.0-beta` 릴리즈 초안 게시
2. GeekNews / Disquiet / LinkedIn / X 에 한국어 소개글 게시
3. Velog 또는 개인 블로그에 "회의 전사보다 Decision Wiki가 필요한 이유" 글 작성
4. Hacker News `Show HN` 은 영어 README와 설치 안정성을 한 번 더 확인한 뒤 게시
5. Reddit은 `r/LocalLLaMA`, `r/selfhosted`, `r/MachineLearning` 중 규칙이 맞는 곳만 선택

## 피해야 할 표현

- "RAG를 대체합니다"
- "회의를 완벽하게 기억합니다"
- "100% 정확한 자동 위키"
- "엔터프라이즈급 검색"
- "설정 없이 모든 것이 자동으로 됩니다"

## 권장 표현

- "원문 근거가 있는 Decision Wiki"
- "회의 전사와 위키 레이어를 분리"
- "로컬에서 처리"
- "한국어 회의에 초점"
- "Apple Silicon 최적화"
- "자동 생성 결과는 원문 timestamp로 확인"
