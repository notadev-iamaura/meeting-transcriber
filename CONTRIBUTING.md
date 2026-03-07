# 기여 가이드

Meeting Transcriber에 기여해 주셔서 감사합니다!

## 개발 환경 설정

```bash
# 1. 저장소 포크 및 클론
git clone https://github.com/<your-username>/meeting-transcriber.git
cd meeting-transcriber

# 2. 가상환경 생성
python3 -m venv .venv
source .venv/bin/activate

# 3. 개발 의존성 설치
pip install -e ".[dev]"

# 4. pre-commit 훅 설치
pre-commit install
```

## 편의 명령어

```bash
make help          # 사용 가능한 명령어 목록
make lint          # 린트 검사
make format        # 코드 포맷팅
make fix           # 린트 자동 수정 + 포맷팅
make test          # 전체 테스트
make test-cov      # 커버리지 포함 테스트
make test-quick    # 빠른 테스트
make clean         # 캐시 정리
```

## 개발 워크플로우

1. `main` 브랜치에서 새 브랜치를 생성합니다.
   ```bash
   git checkout -b feature/기능-이름
   ```

2. 코드를 수정합니다.

3. 린트 및 테스트를 실행합니다.
   ```bash
   make fix           # 린트 자동 수정 + 포맷팅
   make test          # 전체 테스트
   ```

4. 커밋 후 PR을 생성합니다.
   ```bash
   git add .
   git commit -m "기능: 새로운 기능 설명"
   git push origin feature/기능-이름
   ```

> **참고**: pre-commit 훅이 설치되어 있으면, 커밋 시 자동으로 ruff 린트/포맷이 실행됩니다.

## 코드 스타일

- **Python 3.11+** 기능을 사용합니다.
- **타입 힌트**를 모든 함수에 작성합니다.
- **한국어 주석**: 파일 헤더, 함수 docstring, 복잡한 로직에 한국어 주석을 작성합니다.
- **pydantic v2**: 설정 및 데이터 모델은 pydantic BaseModel을 사용합니다.

## 커밋 메시지 규칙

유다시티(Udacity) 스타일을 한국어로 사용합니다:

| 접두사 | 용도 |
|--------|------|
| `기능:` | 새로운 기능 추가 |
| `수정:` | 버그 수정 |
| `리팩터:` | 코드 리팩토링 |
| `테스트:` | 테스트 추가/수정 |
| `문서:` | 문서 수정 |
| `스타일:` | 코드 포맷팅 |

## 테스트

```bash
# 전체 테스트
pytest tests/ -v

# 특정 모듈 테스트
pytest tests/test_config.py -v

# 커버리지 포함
pytest tests/ -v --cov=. --cov-report=term-missing
```

## 이슈 리포팅

버그를 발견하셨나요? [Issues](https://github.com/notadev-iamaura/meeting-transcriber/issues)에 등록해 주세요.

이슈 작성 시 포함해 주세요:
- macOS 버전 및 칩 종류 (M1/M2/M3/M4)
- Python 버전
- 오류 메시지 전문
- 재현 단계

## 라이선스

이 프로젝트에 기여하면 [MIT 라이선스](LICENSE)에 동의한 것으로 간주합니다.
