#!/usr/bin/env bash
# =============================================================================
# run_review.sh — Gerrit AI 코드 리뷰 Shell 래퍼
# =============================================================================
#
# Python 경로, 프로젝트 루트, virtualenv 활성화를 자동으로 처리합니다.
# 어떤 경로에서 실행해도 정상 동작합니다.
#
# 사용법:
#   ./run_review.sh --change 12345 --patchset 1
#   ./run_review.sh --change 12345 --patchset 1 --dry-run
#   ./run_review.sh --change 12345 --patchset 1 --no-post --verbose
#   ./run_review.sh --batch --changes 100 101 102
#   ./run_review.sh --test                    # 연결 테스트
#   ./run_review.sh --batch --query "status:open project:my-project"
# =============================================================================

set -euo pipefail

# ── 프로젝트 루트: 이 스크립트 위치 기준 ──────────────────────────────────────
SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
PROJECT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"

# ── 색상 ──────────────────────────────────────────────────────────────────────
GRN='\033[0;32m'; YLW='\033[1;33m'; RED='\033[0;31m'; BLU='\033[0;34m'
BLD='\033[1m'; RST='\033[0m'

log_info()  { echo -e "${BLU}[run_review]${RST} $*"; }
log_ok()    { echo -e "${GRN}[run_review]${RST} $*"; }
log_warn()  { echo -e "${YLW}[run_review]${RST} $*" >&2; }
log_error() { echo -e "${RED}[run_review]${RST} $*" >&2; }

# ── 도움말 ────────────────────────────────────────────────────────────────────
usage() {
    cat <<EOF

${BLD}Gerrit AI 자동 코드 리뷰어${RST}

${BLD}사용법:${RST}
  $(basename "$0") --change <번호> --patchset <번호> [옵션]
  $(basename "$0") --batch --changes <번호...>       [옵션]
  $(basename "$0") --test [--change <번호>]

${BLD}단일 리뷰 옵션:${RST}
  --change   <N>   Gerrit Change 번호  (필수)
  --patchset <N>   Patchset 번호       (필수)

${BLD}배치 리뷰 옵션 (--batch):${RST}
  --changes  <N...>  Change 번호 목록
  --file     <path>  리뷰 목록 파일
  --query    <str>   Gerrit 쿼리 문자열
  --workers  <N>     병렬 워커 수 (기본 1)

${BLD}공통 옵션:${RST}
  --dry-run          AI/Gerrit 실제 호출 없이 테스트
  --no-post          AI 리뷰 수행 후 Gerrit 등록 안함
  --verbose          DEBUG 상세 로그 출력
  --provider <name>  AI 제공자 오버라이드 (claude/gemini/openai)
  --model    <name>  AI 모델 오버라이드
  --config   <path>  설정 디렉토리 경로
  --output   <path>  결과 출력 디렉토리
  --test             연결 테스트 실행
  -h, --help         이 도움말 출력

${BLD}예시:${RST}
  # 실제 등록
  $(basename "$0") --change 12345 --patchset 1

  # 디버깅 단계 1: AI 없이 전체 흐름 확인
  $(basename "$0") --change 12345 --patchset 1 --dry-run --verbose

  # 디버깅 단계 2: AI 리뷰만, Gerrit 미등록
  $(basename "$0") --change 12345 --patchset 1 --no-post

  # 배치 처리
  $(basename "$0") --batch --changes 100 101 102 --no-post

  # 연결 테스트
  $(basename "$0") --test
  $(basename "$0") --test --change 12345

EOF
}

# ── 인수 파싱 ─────────────────────────────────────────────────────────────────
BATCH_MODE=false
TEST_MODE=false
PYTHON_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)    usage; exit 0 ;;
        --batch)      BATCH_MODE=true; shift ;;
        --test)       TEST_MODE=true;  shift ;;
        *)            PYTHON_ARGS+=("$1"); shift ;;
    esac
done

# ── Python 인터프리터 탐색 ───────────────────────────────────────────────────
PYTHON=""
# 1순위: 프로젝트 내 virtualenv
if [[ -f "${PROJECT_DIR}/venv/bin/python3" ]]; then
    PYTHON="${PROJECT_DIR}/venv/bin/python3"
    log_info "virtualenv 사용: $PYTHON"
# 2순위: REVIEWER_PYTHON 환경변수
elif [[ -n "${REVIEWER_PYTHON:-}" ]]; then
    PYTHON="$REVIEWER_PYTHON"
    log_info "REVIEWER_PYTHON 사용: $PYTHON"
# 3순위: 시스템 Python
else
    for cmd in python3.12 python3.11 python3.10 python3 python; do
        if command -v "$cmd" &>/dev/null; then
            VER=$("$cmd" -c "import sys; print('%d.%d'%sys.version_info[:2])" 2>/dev/null || echo "0.0")
            MAJOR="${VER%%.*}"; MINOR="${VER#*.}"
            if [[ "$MAJOR" -ge 3 && "$MINOR" -ge 10 ]]; then
                PYTHON=$(command -v "$cmd")
                log_info "시스템 Python 사용: $PYTHON ($VER)"
                break
            fi
        fi
    done
fi

if [[ -z "$PYTHON" ]]; then
    log_error "Python 3.10 이상을 찾을 수 없습니다."
    log_error "  설치 후 재시도하거나 REVIEWER_PYTHON 환경변수를 설정하세요."
    exit 1
fi

# ── PYTHONPATH 에 프로젝트 루트 추가 (이중 안전망) ────────────────────────────
export PYTHONPATH="${PROJECT_DIR}:${PYTHONPATH:-}"

# ── 실행 분기 ─────────────────────────────────────────────────────────────────
if [[ "$TEST_MODE" == "true" ]]; then
    log_info "연결 테스트 실행..."
    exec "$PYTHON" "${PROJECT_DIR}/scripts/test_connection.py" "${PYTHON_ARGS[@]}"

elif [[ "$BATCH_MODE" == "true" ]]; then
    log_info "배치 리뷰 실행..."
    exec "$PYTHON" "${PROJECT_DIR}/scripts/batch_reviewer.py" "${PYTHON_ARGS[@]}"

else
    # 단일 리뷰 — --change / --patchset 필수 확인
    if ! printf '%s\n' "${PYTHON_ARGS[@]}" | grep -q -- '--change'; then
        log_error "--change 옵션이 필요합니다."
        usage
        exit 1
    fi
    log_info "단일 리뷰 실행..."
    exec "$PYTHON" "${PROJECT_DIR}/scripts/gerrit_reviewer.py" "${PYTHON_ARGS[@]}"
fi
