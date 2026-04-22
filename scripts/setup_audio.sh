#!/bin/bash
# =================================================================
# Meeting Transcriber — 오디오 녹음 환경 자동 셋업
#
# 목적: Aggregate Device 기반 양방향 녹음(본인 마이크 + 시스템 오디오)
#       을 위한 macOS 오디오 환경을 자동으로 구성한다.
#
# 수행 내용:
#   1. BlackHole 2ch 설치 확인 (없으면 `brew install blackhole-2ch` 안내)
#   2. Meeting Transcriber Aggregate Device 존재 확인
#   3. 없으면 scripts/create_aggregate_device.swift 컴파일·실행하여 자동 생성
#   4. ffmpeg 로 장치 목록 재조회하여 등록 확인
#
# 사용법:
#   bash scripts/setup_audio.sh          # 자동 셋업
#   bash scripts/setup_audio.sh --check  # 상태 점검만 (생성 안 함)
#   bash scripts/setup_audio.sh --force  # 기존 Aggregate 가 있어도 재생성
#
# 종료 코드:
#   0 - 성공 (이미 구성되어 있거나 새로 생성 성공)
#   1 - 에러 (의존성 미설치, 생성 실패 등)
#   2 - 상태 점검 결과 구성이 미완이거나 일부만 있음 (--check 모드)
#
# 전제:
#   - macOS
#   - Homebrew 설치됨 (BlackHole 설치 시 필요)
#   - Xcode Command Line Tools (swiftc 제공)
#
# 관리자 권한 불필요
# =================================================================
set -euo pipefail

# === 상수 ===
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly AGGREGATE_NAME="Meeting Transcriber Aggregate"
readonly SWIFT_SRC="${SCRIPT_DIR}/create_aggregate_device.swift"
readonly SWIFT_BIN="/tmp/meeting-transcriber-create-aggregate"

# === 색상 ===
readonly CLR_RED='\033[0;31m'
readonly CLR_GREEN='\033[0;32m'
readonly CLR_YELLOW='\033[0;33m'
readonly CLR_BLUE='\033[0;34m'
readonly CLR_RESET='\033[0m'

log_info()    { echo -e "${CLR_BLUE}[INFO]${CLR_RESET} $*"; }
log_ok()      { echo -e "${CLR_GREEN}[OK]${CLR_RESET} $*"; }
log_warn()    { echo -e "${CLR_YELLOW}[WARN]${CLR_RESET} $*"; }
log_err()     { echo -e "${CLR_RED}[ERROR]${CLR_RESET} $*" >&2; }

# === OS 체크 ===
check_macos() {
    if [[ "$(uname)" != "Darwin" ]]; then
        log_err "이 스크립트는 macOS 전용입니다 (감지: $(uname))."
        exit 1
    fi
}

# === BlackHole 설치 확인 ===
check_blackhole() {
    if ! command -v ffmpeg >/dev/null 2>&1; then
        log_err "ffmpeg 가 설치되지 않았습니다. 'brew install ffmpeg' 후 재시도."
        exit 1
    fi

    if ffmpeg -f avfoundation -list_devices true -i "" 2>&1 | grep -qi "blackhole"; then
        log_ok "BlackHole 2ch 감지됨"
        return 0
    fi

    log_warn "BlackHole 2ch 가 감지되지 않았습니다."
    if command -v brew >/dev/null 2>&1; then
        echo ""
        echo "  다음 명령으로 설치 후 이 스크립트를 다시 실행해 주세요:"
        echo "    brew install blackhole-2ch"
        echo ""
        echo "  설치 후 macOS 가 장치를 인식하려면 로그아웃·재로그인 또는"
        echo "  재부팅이 필요할 수 있습니다."
    else
        echo "  Homebrew 가 없습니다. https://brew.sh 에서 설치 후 진행해 주세요."
    fi
    exit 1
}

# === Aggregate Device 존재 확인 ===
aggregate_exists() {
    ffmpeg -f avfoundation -list_devices true -i "" 2>&1 \
        | grep -qi "${AGGREGATE_NAME}"
}

# === Swift 컴파일러 확인 ===
check_swiftc() {
    if ! command -v swiftc >/dev/null 2>&1; then
        log_err "swiftc 가 없습니다. 'xcode-select --install' 로 Command Line Tools 설치 후 재시도."
        exit 1
    fi
}

# === Aggregate Device 생성 ===
create_aggregate() {
    check_swiftc

    if [[ ! -f "${SWIFT_SRC}" ]]; then
        log_err "Swift 소스를 찾을 수 없습니다: ${SWIFT_SRC}"
        exit 1
    fi

    log_info "Swift 스크립트 컴파일 중..."
    if ! swiftc "${SWIFT_SRC}" -o "${SWIFT_BIN}" 2>&1; then
        log_err "Swift 컴파일 실패. Xcode Command Line Tools 상태를 확인하세요."
        exit 1
    fi

    log_info "Aggregate Device 생성 실행..."
    local output
    if ! output="$("${SWIFT_BIN}" 2>&1)"; then
        log_err "Aggregate Device 생성 실패:"
        echo "${output}" >&2
        exit 1
    fi

    case "${output}" in
        SUCCESS:*)
            log_ok "Aggregate Device 생성 완료 (UID=${output#SUCCESS:})"
            ;;
        SKIP:*)
            log_ok "이미 존재하는 Aggregate Device 를 사용합니다 (UID=${output#SKIP:})"
            ;;
        ERROR:*)
            log_err "생성 중 오류: ${output#ERROR:}"
            exit 1
            ;;
        *)
            log_err "예상치 못한 출력: ${output}"
            exit 1
            ;;
    esac
}

# === 최종 검증 ===
verify() {
    log_info "최종 검증: ffmpeg 장치 목록에 Aggregate 존재 여부 확인"
    if aggregate_exists; then
        local line
        line="$(ffmpeg -f avfoundation -list_devices true -i "" 2>&1 \
            | grep -i "${AGGREGATE_NAME}" | head -1)"
        log_ok "등록 확인: ${line// /}"
        return 0
    fi
    log_err "생성 직후 ffmpeg 목록에서 Aggregate 를 찾을 수 없습니다."
    log_err "로그아웃·재로그인 후 재시도하거나 Audio MIDI 설정에서 수동 확인 바랍니다."
    return 1
}

# === 명령 파싱 ===
MODE="setup"
for arg in "$@"; do
    case "${arg}" in
        --check) MODE="check" ;;
        --force) MODE="force" ;;
        --help|-h)
            grep -E "^#( |$)" "$0" | sed 's/^# \?//' | head -30
            exit 0
            ;;
        *)
            log_err "알 수 없는 옵션: ${arg}"
            exit 1
            ;;
    esac
done

# === 메인 ===
check_macos

case "${MODE}" in
    check)
        log_info "상태 점검 모드"
        rc=0
        ffmpeg -f avfoundation -list_devices true -i "" 2>&1 \
            | grep -qi "blackhole" \
            && log_ok "BlackHole 2ch 감지됨" \
            || { log_warn "BlackHole 2ch 미감지"; rc=2; }
        aggregate_exists \
            && log_ok "Aggregate Device 감지됨 (${AGGREGATE_NAME})" \
            || { log_warn "Aggregate Device 미생성"; rc=2; }
        exit "${rc}"
        ;;
    setup)
        log_info "오디오 환경 셋업 시작"
        check_blackhole
        if aggregate_exists; then
            log_ok "Aggregate Device 가 이미 존재합니다 (재생성 skip)."
            verify && exit 0 || exit 1
        fi
        create_aggregate
        verify && exit 0 || exit 1
        ;;
    force)
        log_info "강제 모드: 기존 Aggregate 가 있어도 새로 생성"
        check_blackhole
        # 기존이 있어도 Swift 스크립트 내부에서 skip 처리되므로, 실제 강제 재생성은
        # 사용자가 Audio MIDI 설정에서 삭제 후 --force 재실행이 필요하다.
        if aggregate_exists; then
            log_warn "기존 Aggregate 가 있어 skip 됩니다."
            log_warn "완전 재생성이 필요하면 'Audio MIDI 설정' 에서 기존 장치 삭제 후 다시 실행하세요."
        fi
        create_aggregate
        verify && exit 0 || exit 1
        ;;
esac
