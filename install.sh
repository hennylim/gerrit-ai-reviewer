#!/usr/bin/env bash
# =============================================================================
# install.sh — Gerrit AI 자동 코드 리뷰어 설치 스크립트
# =============================================================================
#
# 사용법:
#   ./install.sh [--gerrit-site <path>] [--no-venv] [--dry-run]
#
# 옵션:
#   --gerrit-site <path>   Gerrit 사이트 루트 (기본: $GERRIT_SITE 또는 직접 입력)
#   --no-venv              virtualenv 생성 건너뜀 (시스템 Python 사용)
#   --dry-run              실제 파일 조작 없이 수행 내용만 출력
# =============================================================================

set -euo pipefail

# ── 색상 출력 ─────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${BLUE}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
step()    { echo -e "\n${BOLD}▶ $*${RESET}"; }

# ── 프로젝트 루트 결정 ────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"

# ── 인수 파싱 ─────────────────────────────────────────────────────────────────
GERRIT_SITE="${GERRIT_SITE:-}"
USE_VENV=true
DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --gerrit-site) GERRIT_SITE="$2"; shift 2 ;;
        --no-venv)     USE_VENV=false;   shift ;;
        --dry-run)     DRY_RUN=true;     shift ;;
        -h|--help)
            grep '^#' "$0" | grep -v '^#!/' | sed 's/^# \?//'
            exit 0 ;;
        *) error "알 수 없는 옵션: $1"; exit 1 ;;
    esac
done

run() {
    if [[ "$DRY_RUN" == "true" ]]; then
        echo -e "  ${YELLOW}[DRY-RUN]${RESET} $*"
    else
        eval "$@"
    fi
}

# ──────────────────────────────────────────────────────────────────────────────
echo -e "\n${BOLD}========================================${RESET}"
echo -e "${BOLD}  Gerrit AI 자동 코드 리뷰어 설치${RESET}"
echo -e "${BOLD}========================================${RESET}"
info "프로젝트 경로: $PROJECT_DIR"
[[ "$DRY_RUN" == "true" ]] && warn "DRY-RUN 모드 — 실제 변경 없음"

# ── STEP 1: Python 확인 ───────────────────────────────────────────────────────
step "Python 환경 확인"

PYTHON=""
for cmd in python3.12 python3.11 python3.10 python3 python; do
    if command -v "$cmd" &>/dev/null; then
        VER=$("$cmd" -c "import sys; print('%d.%d'%sys.version_info[:2])")
        MAJOR="${VER%%.*}"; MINOR="${VER#*.}"
        if [[ "$MAJOR" -ge 3 && "$MINOR" -ge 10 ]]; then
            PYTHON=$(command -v "$cmd")
            success "$cmd ($VER) 사용"
            break
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    error "Python 3.10 이상이 필요합니다."
    error "설치: https://www.python.org/downloads/"
    exit 1
fi

# ── STEP 2: virtualenv 생성 ───────────────────────────────────────────────────
step "Python 가상환경 설정"
VENV_DIR="${PROJECT_DIR}/venv"

if [[ "$USE_VENV" == "true" ]]; then
    if [[ -d "$VENV_DIR" ]]; then
        warn "기존 venv 발견: $VENV_DIR (재사용)"
    else
        info "virtualenv 생성: $VENV_DIR"
        run "$PYTHON -m venv '$VENV_DIR'"
    fi
    PYTHON_EXEC="${VENV_DIR}/bin/python3"
    PIP_EXEC="${VENV_DIR}/bin/pip"
    success "venv 활성화 완료"
else
    PYTHON_EXEC="$PYTHON"
    PIP_EXEC="pip3"
    warn "--no-venv: 시스템 Python 사용 ($PYTHON)"
fi

# ── STEP 3: 의존성 설치 ───────────────────────────────────────────────────────
step "Python 패키지 설치"
REQ_FILE="${PROJECT_DIR}/requirements.txt"

if [[ -f "$REQ_FILE" ]]; then
    run "$PIP_EXEC install --upgrade pip -q"
    run "$PIP_EXEC install -r '$REQ_FILE' -q"
    success "패키지 설치 완료"
else
    warn "requirements.txt 없음 — 수동 설치 필요"
fi

# ── STEP 4: 디렉토리 초기화 ───────────────────────────────────────────────────
step "출력/로그 디렉토리 생성"
for d in output logs; do
    DIR="${PROJECT_DIR}/$d"
    if [[ ! -d "$DIR" ]]; then
        run "mkdir -p '$DIR'"
        success "$DIR 생성"
    else
        info "$DIR 이미 존재"
    fi
done

# __init__.py 확인
INIT="${PROJECT_DIR}/scripts/__init__.py"
if [[ ! -f "$INIT" ]]; then
    run "touch '$INIT'"
    info "scripts/__init__.py 생성"
fi

# ── STEP 5: 설정 파일 안내 ────────────────────────────────────────────────────
step "설정 파일 확인"
API_KEYS="${PROJECT_DIR}/config/api_keys.json"
REVIEWER_CFG="${PROJECT_DIR}/config/reviewer_config.json"

check_placeholder() {
    local file="$1"
    local key="$2"
    if grep -q "$key" "$file" 2>/dev/null; then
        warn "$file 에 '$key' 플레이스홀더가 있습니다. 실제 값으로 교체하세요."
        return 1
    fi
    return 0
}

CONFIG_OK=true
check_placeholder "$API_KEYS"     "YOUR_" || CONFIG_OK=false
check_placeholder "$REVIEWER_CFG" "YOUR_" || CONFIG_OK=false
check_placeholder "$REVIEWER_CFG" "gerrit.example.com" || CONFIG_OK=false

if [[ "$CONFIG_OK" == "true" ]]; then
    success "설정 파일 정상"
else
    echo ""
    info "설정이 필요한 파일:"
    echo "  1. ${API_KEYS}"
    echo "     → claude/gemini/openai API 키 입력"
    echo "  2. ${REVIEWER_CFG}"
    echo "     → gerrit.url, gerrit.username, gerrit.password 설정"
fi

# ── STEP 6: Gerrit Hook 설치 ──────────────────────────────────────────────────
step "Gerrit Hook 설치"
HOOK_SRC="${PROJECT_DIR}/hooks/patchset-created"
run "chmod +x '$HOOK_SRC'"

if [[ -n "$GERRIT_SITE" ]]; then
    HOOK_DST="${GERRIT_SITE}/hooks/patchset-created"
    run "mkdir -p '$(dirname "$HOOK_DST")'"

    # 기존 hook 백업
    if [[ -f "$HOOK_DST" ]] && [[ "$DRY_RUN" != "true" ]]; then
        BAK="${HOOK_DST}.bak.$(date +%Y%m%d%H%M%S)"
        run "cp '$HOOK_DST' '$BAK'"
        warn "기존 hook 백업: $BAK"
    fi

    run "cp '$HOOK_SRC' '$HOOK_DST'"
    run "chmod +x '$HOOK_DST'"
    success "Hook 설치 완료: $HOOK_DST"
else
    warn "GERRIT_SITE 미지정 — hook 파일을 수동으로 복사하세요:"
    echo ""
    echo "  cp '${HOOK_SRC}' '<gerrit_site>/hooks/patchset-created'"
    echo "  chmod +x '<gerrit_site>/hooks/patchset-created'"
fi

# ── STEP 7: 연결 테스트 ───────────────────────────────────────────────────────
step "설치 검증"
TEST_SCRIPT="${PROJECT_DIR}/scripts/gerrit_reviewer.py"

if [[ "$CONFIG_OK" == "true" && "$DRY_RUN" != "true" ]]; then
    info "dry-run 테스트 실행 (change=1 patchset=1 --dry-run)..."
    if "$PYTHON_EXEC" "$TEST_SCRIPT" --change 1 --patchset 1 --dry-run 2>&1 | grep -q "AI 코드 리뷰"; then
        success "기본 동작 검증 완료"
    else
        warn "테스트 실행 중 문제 발생 — 로그 확인: ${PROJECT_DIR}/logs/"
    fi
else
    info "설정 미완료 또는 DRY-RUN — 검증 건너뜀"
fi

# ── 완료 요약 ─────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}========================================${RESET}"
echo -e "${GREEN}${BOLD}  설치 완료!${RESET}"
echo -e "${BOLD}========================================${RESET}"
echo ""
echo "  다음 단계:"
echo "  1. config/api_keys.json      → AI API 키 설정"
echo "  2. config/reviewer_config.json → Gerrit URL/계정 설정"
echo ""
echo "  테스트 실행:"
echo "  ${PYTHON_EXEC} scripts/gerrit_reviewer.py --change <번호> --patchset 1 --dry-run"
echo ""
echo "  Gerrit hook 수동 설치 (GERRIT_SITE 미지정 시):"
echo "  cp hooks/patchset-created <gerrit_site>/hooks/"
echo "  chmod +x <gerrit_site>/hooks/patchset-created"
echo ""
