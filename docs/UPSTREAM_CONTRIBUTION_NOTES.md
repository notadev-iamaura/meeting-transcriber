# Upstream Contribution Notes

로컬 운영 중 발견한 안정성 이슈를 upstream에 제안할 때 공유 가능한 범위와
패치 기준을 정리한다. 개인 회의 데이터, groundtruth 원문, 원본 오디오, raw 평가
JSON은 포함하지 않는다.

## STT HF Cache / Offline Loading

`HF_HUB_OFFLINE=1` 환경에서는 모델 파일이 로컬 Hugging Face 캐시에 있어도
`snapshot_download()` 또는 하위 로더가 cached snapshot을 찾지 못할 수 있다.
특히 `refs/main` 값의 trailing newline, ref 누락, 손상된 ref 때문에
`snapshots/{revision}` 경로 해석이 실패하는 사례가 있었다.

Upstream 패치 기준:

- `config.yaml`에 개인 snapshot 절대경로를 하드코딩하지 않는다.
- HF repo ID는 로컬 cached snapshot으로 자동 resolve하되, 캐시가 없으면 기존 HF
  repo ID를 그대로 반환한다.
- `refs/main`은 반드시 `strip()` 후 사용한다.
- `refs/main`이 없거나 유효하지 않으면 `config.json`과 `*.safetensors`가 있는 최신
  snapshot으로 fallback한다.
- `HF_HUB_CACHE`, `HF_HOME` 환경변수를 존중한다.
- SSL 검증 우회, 공개 미러 사용, 토큰 추측 같은 네트워크 우회는 하지 않는다.

## MLX Inference Serialization

MLX backend는 같은 프로세스에서 복수 태스크가 동시에 `generate()`/`chat()`/STT
추론을 호출하면 Metal command buffer assertion 또는 SIGABRT로 종료될 수 있다.
모델 로드 락만으로는 충분하지 않다. 모델이 로드된 뒤 실제 inference가 끝날
때까지 컨텍스트 전체가 직렬화되어야 한다.

Upstream 패치 기준:

- summarize API처럼 fire-and-forget 백그라운드 작업을 만드는 경로는 LLM 작업을
  하나씩 실행해야 한다.
- pipeline 내부 락만으로 보호되지 않는 chat, wiki, streaming 응답도 같은
  직렬화 규칙을 공유해야 한다.
- 락 획득과 inference 실행에는 하드 타임아웃을 두어 선행 작업 hang이 전체 서버를
  무기한 막지 않게 한다.

## Benchmark / Evaluation Sharing

공유 가능:

- 개인정보가 없는 benchmark script
- synthetic 또는 공개 샘플 기반 재현 절차
- 모델별 aggregate summary
- 환경 정보: Mac 칩, RAM, macOS, Python, MLX 버전

공유 금지:

- 회의 원문, 원본 오디오, 실제 참석자 이름
- raw transcript / correction / summary JSON
- groundtruth 원문 또는 개인 평가셋
- 로컬 절대경로, 토큰, 회사/조직명 같은 식별자

기존 스크립트 중 일반화 가능한 항목은 `scripts/benchmark_stt.py`,
`scripts/benchmark_llm.py`, `scripts/benchmark_llm_correct.py`를 우선 검토한다.
결과 문서는 raw 샘플 대신 평균, 표준편차, 실패 유형, 재현 명령 중심으로 작성한다.
