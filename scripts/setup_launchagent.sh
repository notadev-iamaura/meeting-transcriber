#!/bin/bash
# =================================================================
# macOS LaunchAgent 설정 스크립트
#
# 목적: 로그인 시 한국어 로컬 AI 회의 전사 시스템을 자동으로 시작한다.
# 사용법:
#   bash scripts/setup_launchagent.sh           # LaunchAgent 등록
#   bash scripts/setup_launchagent.sh --unload  # LaunchAgent 해제
#   bash scripts/setup_launchagent.sh --status  # LaunchAgent 상태 확인
#
# 관리자 권한 불필요 (~/Library/LaunchAgents/ 사용)
# =================================================================
set -euo pipefail

# === 상수 정의 ===
readonly PLIST_LABEL="com.meeting-transcriber"
readonly PLIST_DIR="${HOME}/Library/LaunchAgents"
readonly PLIST_PATH="${PLIST_DIR}/${PLIST_LABEL}.plist"

# 프로젝트 경로 자동 감지 (BASH_SOURCE 사용 — source 시에도 올바른 경로)
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly MAIN_PY="${PROJECT_DIR}/main.py"

# venv 경로 자동 감지 (프로젝트 .venv → 글로벌 ~/.meeting-transcriber-venv)
if [[ -f "${PROJECT_DIR}/.venv/bin/python" ]]; then
    readonly VENV_DIR="${PROJECT_DIR}/.venv"
elif [[ -f "${HOME}/.meeting-transcriber-venv/bin/python" ]]; then
    readonly VENV_DIR="${HOME}/.meeting-transcriber-venv"
elif [[ -n "${VIRTUAL_ENV:-}" ]]; then
    readonly VENV_DIR="${VIRTUAL_ENV}"
else
    readonly VENV_DIR="${PROJECT_DIR}/.venv"
fi
readonly PYTHON_BIN="${VENV_DIR}/bin/python"

# 로그 경로
readonly LOG_DIR="${HOME}/.meeting-transcriber/logs"
readonly STDOUT_LOG="${LOG_DIR}/launchagent.stdout.log"
readonly STDERR_LOG="${LOG_DIR}/launchagent.stderr.log"

# === 유틸리티 함수 ===

# 색상 출력 (터미널 지원 시)
_info() {
    echo "[정보] $1"
}

_warn() {
    echo "[경고] $1" >&2
}

_error() {
    echo "[오류] $1" >&2
}

_success() {
    echo "[완료] $1"
}

# === 사전 검증 ===

validate_environment() {
    # macOS 확인
    if [[ "$(uname -s)" != "Darwin" ]]; then
        _error "이 스크립트는 macOS에서만 실행할 수 있습니다."
        exit 1
    fi

    # Python venv 확인
    if [[ ! -f "${PYTHON_BIN}" ]]; then
        _error "Python 가상환경을 찾을 수 없습니다: ${VENV_DIR}"
        _error "먼저 가상환경을 생성해주세요:"
        _error "  python3 -m venv ${VENV_DIR}"
        _error "  source ${VENV_DIR}/bin/activate"
        _error "  pip install -r requirements.txt"
        exit 1
    fi

    # main.py 확인
    if [[ ! -f "${MAIN_PY}" ]]; then
        _error "main.py를 찾을 수 없습니다: ${MAIN_PY}"
        exit 1
    fi

    # LaunchAgents 디렉토리 확인/생성
    if [[ ! -d "${PLIST_DIR}" ]]; then
        mkdir -p "${PLIST_DIR}"
        _info "LaunchAgents 디렉토리 생성: ${PLIST_DIR}"
    fi
}

# === plist 생성 ===

generate_plist() {
    # Homebrew 경로 감지 (Apple Silicon / Intel)
    local brew_prefix
    if [[ -d "/opt/homebrew" ]]; then
        brew_prefix="/opt/homebrew"
    elif [[ -d "/usr/local/Homebrew" ]]; then
        brew_prefix="/usr/local"
    else
        brew_prefix="/opt/homebrew"  # 기본값
        _warn "Homebrew 경로를 감지하지 못했습니다. 기본값 사용: ${brew_prefix}"
    fi

    # 로그 디렉토리 생성
    mkdir -p "${LOG_DIR}"

    # plist XML 생성
    cat > "${PLIST_PATH}" << PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <!-- 서비스 식별자 -->
    <key>Label</key>
    <string>${PLIST_LABEL}</string>

    <!-- 실행할 프로그램과 인자 -->
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON_BIN}</string>
        <string>${MAIN_PY}</string>
        <string>--log-file</string>
        <string>${LOG_DIR}/app.log</string>
    </array>

    <!-- 작업 디렉토리 -->
    <key>WorkingDirectory</key>
    <string>${PROJECT_DIR}</string>

    <!-- 로그인 시 자동 시작 -->
    <key>RunAtLoad</key>
    <true/>

    <!-- 크래시 시 자동 재시작 비활성화 (안전) -->
    <key>KeepAlive</key>
    <false/>

    <!-- 백그라운드 프로세스 (리소스 우선순위 낮춤) -->
    <key>ProcessType</key>
    <string>Background</string>

    <!-- I/O 우선순위 낮춤 (팬리스 MacBook Air 대응) -->
    <key>LowPriorityBackgroundIO</key>
    <true/>

    <!-- 표준 출력/에러 로그 -->
    <key>StandardOutPath</key>
    <string>${STDOUT_LOG}</string>
    <key>StandardErrorPath</key>
    <string>${STDERR_LOG}</string>

    <!-- 환경변수: PATH에 Homebrew + venv 포함 -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${VENV_DIR}/bin:${brew_prefix}/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>LANG</key>
        <string>ko_KR.UTF-8</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
</dict>
</plist>
PLIST_EOF

    # plist 파일 권한 설정 (644: 소유자 rw, 그룹/기타 r)
    chmod 644 "${PLIST_PATH}"
}

# === LaunchAgent 등록 ===

load_agent() {
    validate_environment

    # 기존 plist가 로드되어 있으면 먼저 언로드
    if launchctl list "${PLIST_LABEL}" &>/dev/null; then
        _info "기존 LaunchAgent를 먼저 해제합니다..."
        launchctl unload "${PLIST_PATH}" 2>/dev/null || true
    fi

    # plist 생성
    _info "plist 파일 생성 중..."
    generate_plist
    _info "plist 생성 완료: ${PLIST_PATH}"

    # plist 문법 검증
    if ! plutil -lint "${PLIST_PATH}" &>/dev/null; then
        _error "plist 문법 오류가 발견되었습니다."
        plutil -lint "${PLIST_PATH}"
        exit 1
    fi
    _info "plist 문법 검증 통과"

    # LaunchAgent 등록
    launchctl load "${PLIST_PATH}"
    _success "LaunchAgent 등록 완료!"
    _info ""
    _info "설정 요약:"
    _info "  레이블:    ${PLIST_LABEL}"
    _info "  plist:     ${PLIST_PATH}"
    _info "  Python:    ${PYTHON_BIN}"
    _info "  main.py:   ${MAIN_PY}"
    _info "  앱 로그:   ${LOG_DIR}/app.log"
    _info "  stdout:    ${STDOUT_LOG}"
    _info "  stderr:    ${STDERR_LOG}"
    _info ""
    _info "다음 로그인부터 자동으로 시작됩니다."
    _info "지금 바로 시작하려면: launchctl start ${PLIST_LABEL}"
}

# === LaunchAgent 해제 ===

unload_agent() {
    if [[ ! -f "${PLIST_PATH}" ]]; then
        _warn "plist 파일이 존재하지 않습니다: ${PLIST_PATH}"
        return 0
    fi

    # 언로드
    if launchctl list "${PLIST_LABEL}" &>/dev/null; then
        launchctl unload "${PLIST_PATH}"
        _info "LaunchAgent 해제 완료"
    else
        _info "LaunchAgent가 로드되어 있지 않습니다."
    fi

    # plist 파일 삭제
    rm -f "${PLIST_PATH}"
    _success "plist 파일 삭제 완료: ${PLIST_PATH}"
    _info "다음 로그인부터 자동 시작되지 않습니다."
}

# === LaunchAgent 상태 확인 ===

check_status() {
    _info "LaunchAgent 상태 확인"
    _info "---"

    # plist 파일 존재 여부
    if [[ -f "${PLIST_PATH}" ]]; then
        _info "plist 파일: 존재 (${PLIST_PATH})"
    else
        _warn "plist 파일: 없음"
        return 0
    fi

    # 로드 여부
    if launchctl list "${PLIST_LABEL}" &>/dev/null; then
        _info "로드 상태: 로드됨"

        # 프로세스 실행 여부
        local pid
        pid=$(launchctl list "${PLIST_LABEL}" 2>/dev/null | awk '{print $1}')
        if [[ "${pid}" != "-" && -n "${pid}" ]]; then
            _info "프로세스: 실행 중 (PID: ${pid})"
        else
            _info "프로세스: 실행되지 않음 (대기 중)"
        fi
    else
        _info "로드 상태: 로드되지 않음"
    fi

    # 로그 파일 확인
    if [[ -f "${STDERR_LOG}" ]]; then
        _info "최근 에러 로그 (마지막 5줄):"
        tail -5 "${STDERR_LOG}" 2>/dev/null || true
    fi
}

# === 메인 ===

main() {
    local command="${1:-}"

    case "${command}" in
        --unload|unload|--remove|remove)
            unload_agent
            ;;
        --status|status)
            check_status
            ;;
        --help|-h|help)
            echo "사용법: $(basename "$0") [옵션]"
            echo ""
            echo "옵션:"
            echo "  (없음)      LaunchAgent를 등록합니다 (로그인 시 자동 시작)"
            echo "  --unload    LaunchAgent를 해제하고 plist를 삭제합니다"
            echo "  --status    LaunchAgent 상태를 확인합니다"
            echo "  --help      이 도움말을 표시합니다"
            ;;
        "")
            load_agent
            ;;
        *)
            _error "알 수 없는 옵션: ${command}"
            _error "도움말: $(basename "$0") --help"
            exit 1
            ;;
    esac
}

# 직접 실행될 때만 main 호출 (source 시에는 함수만 로드)
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
