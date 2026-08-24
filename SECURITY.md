# 보안 정책 (Security Policy)

## 지원 버전

현재 보안 패치를 지원하는 버전 목록입니다.

| 버전 | 지원 여부 |
|------|-----------|
| 0.1.x | 지원됨 ✅ |
| 0.0.x (사전 출시) | 지원 안 됨 ❌ |

---

## 취약점 신고

**보안 취약점을 발견하셨다면 GitHub Issues를 통해 신고해 주세요.**

> 이 프로젝트는 이메일 기반 취약점 신고를 운영하지 않습니다.
> 모든 신고는 GitHub 저장소의 Issues 탭을 이용해 주세요.

**신고 방법:**

1. [GitHub Issues](https://github.com/notadev-iamaura/meeting-transcriber/issues/new)에서 새 이슈를 생성합니다.
2. 이슈 제목 앞에 `[보안]` 태그를 붙여 주세요. 예: `[보안] 파일 경로 순회 취약점`
3. 아래 **신고 양식**에 따라 내용을 작성해 주세요.

> **참고**: 민감한 보안 취약점의 경우 GitHub의 [비공개 취약점 신고(Private Vulnerability Reporting)](https://docs.github.com/ko/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability) 기능을 사용할 수 있습니다.

### 신고 양식

```
## 취약점 요약
[한 줄 요약]

## 영향 받는 버전
[예: 0.1.0]

## 재현 방법
1. ...
2. ...
3. ...

## 예상 동작
[어떻게 동작해야 하는지]

## 실제 동작
[현재 어떻게 동작하는지]

## 심각도 (본인 평가)
[치명적 / 높음 / 중간 / 낮음]

## 추가 정보
[스크린샷, 로그, POC 코드 등]
```

---

## 대응 기한

| 단계 | 기한 |
|------|------|
| 신고 확인 및 초기 응답 | **신고 후 7일 이내** |
| 취약점 분류 및 심각도 평가 | **신고 후 14일 이내** |
| 패치 개발 완료 | **신고 후 30일 이내** |
| 패치 릴리스 | **개발 완료 후 7일 이내** |

심각도가 **치명적(Critical)**으로 분류된 취약점은 위 기한보다 빠르게 대응합니다.

---

## 보안 설계 원칙

### 로컬 우선 처리 (Local-First Processing)

이 프로젝트의 가장 근본적인 보안 원칙입니다.

- **기본값은 로컬**: 새 설치의 전사 provider는 `local`이며 자동 cloud fallback이 없습니다. OpenAI를 선택하지 않으면 음성 데이터, 전사 텍스트, 회의 내용은 외부 AI API로 전송되지 않습니다.
- **명시적 OpenAI 예외**: 설정에서 OpenAI를 기본 전사로 선택하려면 한 번의 업로드 동의가 필요하며, 로컬로 되돌리기 전까지 이후 새 전사 음성이 `api.openai.com`으로 전송됩니다.
- **회의별 1회 선택**: 녹음 완료 회의의 `전사 시작`에서 전역 기본값을 바꾸지 않고 해당 회의만 OpenAI로 선택할 수 있습니다. 외부 모델을 선택할 때마다 해당 음성 파일의 전송 동의를 다시 요구하며, 공개 모델 ID 화이트리스트와 loopback 요청 검증 뒤에만 작업 큐에 저장합니다.
- **파일별 동의와 비파괴 실행**: 회의별 **다른 모델로 텍스트 변환하기…**는 실행할 때마다 별도 동의를 요구하고 기존 전사·요약·검색 인덱스를 교체하지 않는 격리 A/B 작업으로 저장됩니다.
- **LLM 로컬 실행**: Gemma 4(기본) 또는 EXAONE 3.5 모델은 MLX(in-process) 백엔드에서 실행되며, 선택적으로 Ollama(localhost)를 사용할 수 있습니다.
- **임베딩 로컬 실행**: `multilingual-e5-small` 임베딩 모델은 로컬 Apple Silicon GPU(MPS)에서 실행됩니다.
- **인터넷 연결 선택적**: 로컬 전사에는 초기 모델 다운로드 이후 인터넷 연결이 필요하지 않습니다. OpenAI 전사를 명시적으로 사용할 때만 연결이 필요합니다.
- **웹 서버 localhost 전용**: FastAPI 서버는 `127.0.0.1`(loopback)에서만 실행됩니다. 외부 네트워크에 노출되지 않습니다.

### 최소 권한 원칙 (Principle of Least Privilege)

- **관리자 권한 불필요**: 애플리케이션 설치 및 실행에 `sudo` 또는 관리자 권한이 필요하지 않습니다.
- **데이터 디렉토리 격리**: 모든 회의 데이터는 `~/.meeting-transcriber/`에 저장되며, 해당 디렉토리에 `chmod 700`을 적용하여 현재 사용자만 접근할 수 있습니다.
- **Spotlight 색인 제외**: 데이터 디렉토리에 `.noindex` 파일을 생성하여 macOS Spotlight 검색 색인에서 제외합니다.

### 데이터 보안

- **암호화 정책**: 이 프로젝트는 파일 시스템 수준의 암호화를 별도로 구현하지 않습니다. macOS의 FileVault(전체 디스크 암호화)를 활성화하여 저장 데이터를 보호할 것을 권장합니다.
- **데이터 수명주기**: `security/lifecycle.py`를 통해 회의 데이터의 보관 기간과 삭제 정책을 관리합니다. hot(활성) → warm(보관) → cold(아카이브) 단계별 정책을 `config.yaml`에서 설정할 수 있습니다.
- **HuggingFace 토큰**: pyannote 화자분리 모델의 초기 다운로드에 필요한 HuggingFace 토큰은 환경변수(`HUGGINGFACE_TOKEN`)로만 관리합니다. 토큰을 코드나 설정 파일에 직접 기록하지 마십시오.
- **OpenAI API 키**: 일반 사용에서는 별도 자격 증명 API를 통해 macOS Keychain에 저장합니다. 키 값은 설정 응답, DOM 재표시, YAML, SQLite, 체크포인트, 로그에 기록하지 않습니다. `OPENAI_API_KEY`는 개발/CI용 읽기 전용 폴백입니다.
- **OpenAI 업로드 최소화**: 원본 파일명·회의 ID·제목·로컬 prompt를 보내지 않고 generic MP3 파일과 `model`, `response_format=diarized_json`, `chunking_strategy=auto` multipart 필드만 전송합니다. 지원되지 않는 `language`, `prompt`, `timestamp_granularities` 필드는 보내지 않으며, 25MB 제한보다 작은 로컬 청크를 고정 HTTPS endpoint로 순차 업로드합니다.

### 오디오 녹음 보안

- **스테이징 격리**: 녹음 중인 파일은 `recordings_temp/`에 임시 저장됩니다. 녹음이 정상 완료된 후에만 `audio_input/`으로 이동하여 파이프라인을 트리거합니다. 불완전한 녹음 파일이 처리 대상에 포함되는 것을 방지합니다.
- **ffmpeg 안전 종료**: 녹음 프로세스 종료는 stdin 'q' 신호 → graceful 타임아웃(10초) → SIGTERM → SIGKILL 순서로 진행합니다. 데이터 손실과 좀비 프로세스를 최소화합니다.

### 코드 보안 관행

- **bare except 금지**: 모든 예외는 구체적인 타입으로 처리합니다. 예외를 조용히 무시하는 코드를 허용하지 않습니다.
- **로깅 정책**: 민감한 데이터(토큰, 파일 내용 등)는 로그에 기록하지 않습니다.
- **경로 처리**: `pathlib.Path`를 사용하여 파일 경로를 처리합니다. 경로 순회(path traversal) 공격을 방지하기 위해 사용자 입력 기반 경로는 항상 검증합니다.
- **설정 하드코딩 금지**: 포트 번호, 경로 등 모든 설정값은 `config.yaml`에서 관리합니다.

---

## 권장 보안 설정

이 프로젝트를 더 안전하게 사용하기 위한 권장 설정입니다.

```bash
# macOS FileVault 활성화 여부 확인 (권장)
fdesetup status

# 데이터 디렉토리 권한 확인
ls -la ~/.meeting-transcriber/

# 데이터 디렉토리 권한이 700이 아닌 경우 수동 설정
chmod 700 ~/.meeting-transcriber/

# HuggingFace 토큰을 ~/.zshrc에 저장할 때는 파일 권한 확인
chmod 600 ~/.zshrc
```

---

## 알려진 보안 제약사항

이 프로젝트의 설계상 의도된 보안 제약사항입니다. 취약점이 아닙니다.

| 제약사항 | 설명 |
|----------|------|
| 파일 시스템 암호화 미구현 | macOS FileVault 사용 권장. 별도 구현 계획 없음 |
| 네트워크 인증 없음 | localhost 전용 서버이므로 별도 인증 미구현. 다중 사용자 환경에서는 사용 주의 |
| HuggingFace 초기 연결 | 최초 1회 모델 다운로드 시 HuggingFace 서버에 연결. 이후 오프라인 동작 |
| 선택적 OpenAI 데이터 처리 | OpenAI 전사를 선택한 음성은 OpenAI의 데이터 처리 정책과 네트워크 가용성의 영향을 받음. 민감 회의는 로컬 기본값 유지 권장 |
| OpenAI 키 유효성 | 앱은 저장 시 키 형식과 존재 여부만 확인하며 실제 API 유효성은 첫 전사 요청에서 확인됨 |

---

## 보안 관련 문의

보안 정책이나 설계에 대한 일반적인 문의는 [GitHub Discussions](https://github.com/notadev-iamaura/meeting-transcriber/discussions)를 이용해 주세요.
