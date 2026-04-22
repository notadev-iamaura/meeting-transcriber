#!/bin/bash
# =================================================================
# 한국어 로컬 AI 회의 전사 시스템 — 설치 스크립트
#
# 목적: 클린 macOS 환경에서 모든 의존성을 일괄 설치한다.
# 사용법:
#   bash scripts/install.sh              # 전체 설치
#   bash scripts/install.sh --check      # 설치 상태 확인만
#   bash scripts/install.sh --help       # 도움말
#
# 설치 항목:
#   1. Homebrew (없을 시 안내)
#   2. Python 3.11+ (brew install python@3.11)
#   3. ffmpeg (brew install ffmpeg)
#   4. Python 가상환경 (~/.meeting-transcriber-venv)
#   5. pip 패키지 (requirements.txt)
#   6. Ollama 설치 확인
#   7. EXAONE 3.5 모델 pull
#   8. 데이터 디렉토리 생성 + 보안 설정
#
# 관리자 권한 불필요
# =================================================================
set -euo pipefail

# === 상수 정의 ===
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# venv 경로 (CLAUDE.md 기준)
readonly VENV_DIR="${HOME}/.meeting-transcriber-venv"
readonly PYTHON_BIN="${VENV_DIR}/bin/python"
readonly PIP_BIN="${VENV_DIR}/bin/pip"

# 데이터 디렉토리 (config.yaml 기준)
readonly DATA_DIR="${HOME}/.meeting-transcriber"

# Ollama 모델 (config.yaml 기준)
readonly EXAONE_MODEL="exaone3.5:7.8b-instruct-q4_K_M"
readonly OLLAMA_HOST="http://127.0.0.1:11434"

# 최소 디스크 여유 공간 (GB)
readonly MIN_DISK_FREE_GB=20

# 필수 Python 버전
readonly MIN_PYTHON_MAJOR=3
readonly MIN_PYTHON_MINOR=11

# requirements.txt 경로
readonly REQUIREMENTS_FILE="${PROJECT_DIR}/requirements.txt"

# === 유틸리티 함수 ===

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

_step() {
    echo ""
    echo "=========================================="
    echo "  $1"
    echo "=========================================="
}

# === 사전 검증 ===

check_macos() {
    # macOS 확인
    if [[ "$(uname -s)" != "Darwin" ]]; then
        _error "이 스크립트는 macOS에서만 실행할 수 있습니다."
        return 1
    fi
    _info "macOS 확인 완료: $(sw_vers -productVersion 2>/dev/null || echo '버전 확인 불가')"
    return 0
}

check_apple_silicon() {
    # Apple Silicon (ARM64) 확인 — mlx-whisper 필수
    local arch
    arch="$(uname -m)"
    if [[ "${arch}" != "arm64" ]]; then
        _warn "Apple Silicon이 아닙니다 (${arch}). mlx-whisper가 동작하지 않을 수 있습니다."
        return 1
    fi
    _info "Apple Silicon 확인 완료: ${arch}"
    return 0
}

check_disk_space() {
    # 디스크 여유 공간 확인
    local free_gb
    free_gb=$(df -g "${HOME}" 2>/dev/null | awk 'NR==2 {print $4}')
    if [[ -z "${free_gb}" ]]; then
        _warn "디스크 여유 공간을 확인할 수 없습니다."
        return 0
    fi
    if [[ "${free_gb}" -lt "${MIN_DISK_FREE_GB}" ]]; then
        _error "디스크 여유 공간 부족: ${free_gb}GB (최소 ${MIN_DISK_FREE_GB}GB 필요)"
        _error "PyTorch (~2GB), EXAONE 모델 (~5GB), 기타 패키지를 위해 공간이 필요합니다."
        return 1
    fi
    _info "디스크 여유 공간: ${free_gb}GB (최소 ${MIN_DISK_FREE_GB}GB)"
    return 0
}

# === 1단계: Homebrew 확인 ===

check_homebrew() {
    if command -v brew &>/dev/null; then
        _info "Homebrew 설치 확인 완료: $(brew --version 2>/dev/null | head -1)"
        return 0
    fi
    _error "Homebrew가 설치되어 있지 않습니다."
    _error "다음 명령어로 설치해주세요:"
    _error '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
    return 1
}

# === 2단계: Python 3.11+ 확인/설치 ===

check_python_version() {
    # 시스템 또는 brew에서 Python 3.11+ 찾기
    local python_cmd=""
    local version_output=""
    local major=""
    local minor=""

    # python3.11, python3.12, python3.13 순서로 확인
    for candidate in python3.11 python3.12 python3.13 python3; do
        if command -v "${candidate}" &>/dev/null; then
            version_output=$("${candidate}" --version 2>&1 || true)
            major=$(echo "${version_output}" | grep -oE '[0-9]+\.[0-9]+' | head -1 | cut -d. -f1)
            minor=$(echo "${version_output}" | grep -oE '[0-9]+\.[0-9]+' | head -1 | cut -d. -f2)
            if [[ -n "${major}" && -n "${minor}" ]]; then
                if [[ "${major}" -ge "${MIN_PYTHON_MAJOR}" && "${minor}" -ge "${MIN_PYTHON_MINOR}" ]]; then
                    python_cmd="${candidate}"
                    break
                fi
            fi
        fi
    done

    if [[ -n "${python_cmd}" ]]; then
        _info "Python 확인 완료: ${version_output} (${python_cmd})"
        echo "${python_cmd}"
        return 0
    fi

    return 1
}

install_python() {
    _info "Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+ 설치 중..."
    brew install python@3.11
    _success "Python 3.11 설치 완료"
}

# === 3단계: ffmpeg 확인/설치 ===

check_ffmpeg() {
    if command -v ffmpeg &>/dev/null; then
        _info "ffmpeg 확인 완료: $(ffmpeg -version 2>&1 | head -1)"
        return 0
    fi
    return 1
}

install_ffmpeg() {
    _info "ffmpeg 설치 중..."
    brew install ffmpeg
    _success "ffmpeg 설치 완료"
}

# === 4단계: 가상환경 생성 ===

create_venv() {
    local python_cmd="$1"

    if [[ -f "${PYTHON_BIN}" ]]; then
        _info "가상환경 이미 존재: ${VENV_DIR}"
        return 0
    fi

    _info "가상환경 생성 중: ${VENV_DIR}"
    "${python_cmd}" -m venv "${VENV_DIR}"
    _success "가상환경 생성 완료"
}

# === 5단계: pip 패키지 설치 ===

generate_requirements() {
    # requirements.txt가 없으면 pyproject.toml 참조 스텁 생성
    if [[ -f "${REQUIREMENTS_FILE}" ]]; then
        _info "requirements.txt 발견: ${REQUIREMENTS_FILE}"
        return 0
    fi

    _info "requirements.txt 생성 중 (pyproject.toml 참조)..."
    cat > "${REQUIREMENTS_FILE}" << 'REQ_EOF'
# 한국어 로컬 AI 회의 전사 시스템 — 의존성 목록
#
# SSOT: pyproject.toml [project.dependencies]
# 이 파일은 하위 호환성을 위해 유지합니다.
# 권장 설치 방법: pip install -e .
-e .
REQ_EOF
    _success "requirements.txt 생성 완료"
}

install_pip_packages() {
    _info "pip 업그레이드 중..."
    "${PIP_BIN}" install --upgrade pip --quiet

    _info "pip 패키지 설치 중... (시간이 소요될 수 있습니다)"
    # pyproject.toml을 SSOT로 사용 (dev 의존성 포함)
    if [[ -f "${PROJECT_DIR}/pyproject.toml" ]]; then
        "${PIP_BIN}" install -e "${PROJECT_DIR}[dev]"
    else
        "${PIP_BIN}" install -r "${REQUIREMENTS_FILE}"
    fi
    _success "pip 패키지 설치 완료"
}

# === 6단계: Ollama 확인 ===

check_ollama() {
    if command -v ollama &>/dev/null; then
        _info "Ollama 확인 완료: $(ollama --version 2>&1 || echo '버전 확인 불가')"
        return 0
    fi
    _error "Ollama가 설치되어 있지 않습니다."
    _error "https://ollama.com 에서 macOS 앱을 다운로드하여 설치해주세요."
    return 1
}

# === 7단계: EXAONE 모델 pull ===

check_ollama_running() {
    # Ollama 서버가 실행 중인지 확인
    if curl -s --connect-timeout 3 "${OLLAMA_HOST}/api/tags" &>/dev/null; then
        return 0
    fi
    return 1
}

check_exaone_model() {
    # EXAONE 모델이 이미 다운로드되어 있는지 확인
    if ollama list 2>/dev/null | grep -q "exaone3.5"; then
        _info "EXAONE 모델 이미 존재"
        return 0
    fi
    return 1
}

pull_exaone_model() {
    if ! check_ollama_running; then
        _warn "Ollama 서버가 실행 중이 아닙니다."
        _warn "Ollama 앱을 실행한 후 다음 명령어를 수동으로 실행하세요:"
        _warn "  ollama pull ${EXAONE_MODEL}"
        return 1
    fi

    if check_exaone_model; then
        return 0
    fi

    _info "EXAONE 3.5 모델 다운로드 중... (약 5GB, 시간이 소요됩니다)"
    ollama pull "${EXAONE_MODEL}"
    _success "EXAONE 모델 다운로드 완료"
}

# === 8단계: 디렉토리 생성 + 보안 설정 ===

setup_directories() {
    local dirs=(
        "${DATA_DIR}"
        "${DATA_DIR}/audio_input"
        "${DATA_DIR}/outputs"
        "${DATA_DIR}/checkpoints"
        "${DATA_DIR}/chroma_db"
        "${DATA_DIR}/logs"
    )

    for dir in "${dirs[@]}"; do
        if [[ ! -d "${dir}" ]]; then
            mkdir -p "${dir}"
            _info "디렉토리 생성: ${dir}"
        fi
    done

    # 보안 설정: chmod 700 (소유자만 접근)
    chmod 700 "${DATA_DIR}"
    _info "디렉토리 권한 설정: chmod 700 ${DATA_DIR}"

    # Spotlight 인덱싱 제외
    local never_index="${DATA_DIR}/.metadata_never_index"
    if [[ ! -f "${never_index}" ]]; then
        touch "${never_index}"
        _info "Spotlight 인덱싱 제외 설정"
    fi

    # .gitignore 생성
    local gitignore="${DATA_DIR}/.gitignore"
    if [[ ! -f "${gitignore}" ]]; then
        echo "*" > "${gitignore}"
        _info ".gitignore 생성"
    fi

    _success "디렉토리 구조 및 보안 설정 완료"
}

# === 설치 상태 확인 ===

run_check() {
    _step "설치 상태 확인"

    local all_ok=true

    # macOS
    if check_macos; then
        _success "macOS: OK"
    else
        _error "macOS: 실패"
        all_ok=false
    fi

    # Apple Silicon
    if check_apple_silicon; then
        _success "Apple Silicon: OK"
    else
        _warn "Apple Silicon: 비 ARM64 아키텍처"
    fi

    # Homebrew
    if check_homebrew; then
        _success "Homebrew: OK"
    else
        _error "Homebrew: 미설치"
        all_ok=false
    fi

    # Python
    local python_cmd
    python_cmd=$(check_python_version 2>/dev/null) || true
    if [[ -n "${python_cmd}" ]]; then
        _success "Python 3.11+: OK (${python_cmd})"
    else
        _error "Python 3.11+: 미설치"
        all_ok=false
    fi

    # ffmpeg
    if check_ffmpeg; then
        _success "ffmpeg: OK"
    else
        _error "ffmpeg: 미설치"
        all_ok=false
    fi

    # venv
    if [[ -f "${PYTHON_BIN}" ]]; then
        _success "가상환경: OK (${VENV_DIR})"
    else
        _error "가상환경: 미생성"
        all_ok=false
    fi

    # Ollama
    if check_ollama; then
        _success "Ollama: OK"
    else
        _error "Ollama: 미설치"
        all_ok=false
    fi

    # EXAONE 모델
    if check_exaone_model 2>/dev/null; then
        _success "EXAONE 모델: OK"
    else
        _warn "EXAONE 모델: 미다운로드"
    fi

    # 데이터 디렉토리
    if [[ -d "${DATA_DIR}" ]]; then
        local perms
        perms=$(stat -f "%Lp" "${DATA_DIR}" 2>/dev/null || echo "알 수 없음")
        _success "데이터 디렉토리: OK (권한: ${perms})"
    else
        _error "데이터 디렉토리: 미생성"
        all_ok=false
    fi

    # 디스크 공간
    check_disk_space || all_ok=false

    echo ""
    if [[ "${all_ok}" == "true" ]]; then
        _success "모든 항목이 정상입니다!"
        return 0
    else
        _warn "일부 항목이 누락되어 있습니다. 'bash scripts/install.sh'로 설치하세요."
        return 1
    fi
}

# === 전체 설치 실행 ===

run_install() {
    _step "한국어 로컬 AI 회의 전사 시스템 설치"
    _info "프로젝트 경로: ${PROJECT_DIR}"
    echo ""

    # 0. 사전 검증
    _step "0단계: 사전 검증"
    check_macos || exit 1
    check_apple_silicon || true  # 경고만 (실패해도 계속)
    check_disk_space || exit 1

    # 1. Homebrew 확인
    _step "1단계: Homebrew 확인"
    check_homebrew || exit 1

    # 2. Python 3.11+ 확인/설치
    _step "2단계: Python 3.11+ 확인"
    local python_cmd
    python_cmd=$(check_python_version 2>/dev/null) || true
    if [[ -z "${python_cmd}" ]]; then
        install_python
        python_cmd=$(check_python_version 2>/dev/null) || true
        if [[ -z "${python_cmd}" ]]; then
            _error "Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+ 설치에 실패했습니다."
            exit 1
        fi
    fi

    # 3. ffmpeg 확인/설치
    _step "3단계: ffmpeg 확인"
    if ! check_ffmpeg; then
        install_ffmpeg
    fi

    # 4-5. 가상환경 + pip 패키지 (이미 활성화된 venv이 있으면 건너뜀)
    if [[ -n "${VIRTUAL_ENV:-}" ]]; then
        _step "4-5단계: Python 환경 (건너뜀)"
        _info "이미 활성화된 가상환경 감지: ${VIRTUAL_ENV}"
        _info "pip install -e . 으로 패키지를 설치한 경우 이 단계를 건너뜁니다."
    else
        _step "4단계: Python 가상환경"
        create_venv "${python_cmd}"

        _step "5단계: Python 패키지 설치"
        generate_requirements
        install_pip_packages
    fi

    # 6. Ollama 확인
    _step "6단계: Ollama 확인"
    check_ollama || {
        _warn "Ollama 설치 후 다시 실행하세요."
    }

    # 7. EXAONE 모델 pull
    _step "7단계: EXAONE 모델"
    pull_exaone_model || true  # 실패해도 계속 (나중에 수동 가능)

    # 8. 디렉토리 + 보안 설정
    _step "8단계: 디렉토리 및 보안 설정"
    setup_directories

    # 9. 오디오 환경 (선택)
    _step "9단계: 오디오 녹음 환경 (선택 사항)"
    setup_audio_environment

    # 완료 요약
    print_summary
}

# === 9단계: 오디오 환경 셋업 ===
# BlackHole 2ch + Aggregate Device 를 자동 셋업하여 본인 마이크 + 시스템 오디오
# 양방향 녹음이 가능하도록 한다. 실패해도 설치 전체가 중단되지는 않는다 (선택 사항).
setup_audio_environment() {
    if [[ "${SKIP_AUDIO_SETUP:-0}" == "1" ]]; then
        _info "SKIP_AUDIO_SETUP=1 감지 — 오디오 환경 셋업 건너뜀"
        return 0
    fi

    local audio_script="${SCRIPT_DIR}/setup_audio.sh"
    if [[ ! -x "${audio_script}" ]]; then
        _warn "오디오 셋업 스크립트 누락 또는 실행 불가: ${audio_script}"
        _warn "수동 안내: docs/AGGREGATE_DEVICE_SETUP.md 참고"
        return 0
    fi

    _info "오디오 환경 셋업 스크립트 실행..."
    if bash "${audio_script}"; then
        _success "오디오 환경 자동 구성 완료 (Aggregate Device 준비됨)"
    else
        _warn "오디오 환경 자동 구성 실패 — 수동 안내: docs/AGGREGATE_DEVICE_SETUP.md"
        _warn "(전사 파이프라인 자체는 영향 없음. 녹음 품질만 영향)"
    fi
}

# === 완료 요약 ===

print_summary() {
    echo ""
    echo "=========================================="
    echo "  설치 완료!"
    echo "=========================================="
    echo ""
    _info "가상환경 경로:  ${VENV_DIR}"
    _info "데이터 경로:    ${DATA_DIR}"
    _info "프로젝트 경로:  ${PROJECT_DIR}"
    echo ""
    _info "다음 단계:"
    echo "  1. 가상환경 활성화:"
    echo "     source ${VENV_DIR}/bin/activate"
    echo ""
    echo "  2. HuggingFace 토큰 설정 (화자분리에 필요):"
    echo "     export HUGGINGFACE_TOKEN=hf_xxxxx"
    echo "     (https://huggingface.co/settings/tokens 에서 발급)"
    echo ""
    echo "  3. Ollama 앱 실행 확인 후 모델 다운로드:"
    echo "     ollama pull ${EXAONE_MODEL}"
    echo ""
    echo "  4. 애플리케이션 실행:"
    echo "     python main.py"
    echo ""
    echo "  5. (선택) 로그인 시 자동 시작 설정:"
    echo "     bash scripts/setup_launchagent.sh"
    echo ""
    echo "  6. 설치 상태 확인:"
    echo "     bash scripts/install.sh --check"
    echo ""
}

# === 메인 ===

main() {
    local command="${1:-}"

    case "${command}" in
        --check|check)
            run_check
            ;;
        --help|-h|help)
            echo "사용법: $(basename "$0") [옵션]"
            echo ""
            echo "한국어 로컬 AI 회의 전사 시스템의 의존성을 일괄 설치합니다."
            echo ""
            echo "옵션:"
            echo "  (없음)      전체 설치를 실행합니다"
            echo "  --check     설치 상태를 확인합니다"
            echo "  --help      이 도움말을 표시합니다"
            echo ""
            echo "설치 항목:"
            echo "  - Homebrew (확인만, 미설치 시 안내)"
            echo "  - Python 3.11+ (brew install)"
            echo "  - ffmpeg (brew install)"
            echo "  - Python 가상환경 (~/.meeting-transcriber-venv)"
            echo "  - pip 패키지 (requirements.txt)"
            echo "  - Ollama (확인만, 미설치 시 안내)"
            echo "  - EXAONE 3.5 모델 (ollama pull)"
            echo "  - 데이터 디렉토리 + 보안 설정"
            ;;
        "")
            run_install
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
