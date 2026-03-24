"""
test_connection.py
------------------
Gerrit 서버 연결 및 AI API 키를 사전에 검증하는 진단 도구.

설치 직후, 또는 문제 발생 시 먼저 이 스크립트를 실행하여
연결 상태를 확인하세요.

사용법:
  python scripts/test_connection.py                  # 전체 검사
  python scripts/test_connection.py --gerrit-only    # Gerrit 연결만 검사
  python scripts/test_connection.py --ai-only        # AI API만 검사
  python scripts/test_connection.py --change 12345   # 특정 Change 접근 테스트
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

# 어느 경로에서 실행해도 동작하도록 경로 설정
SCRIPT_DIR  = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))

# ── 색상 출력 헬퍼 ────────────────────────────────────────────────────────────
def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"

OK   = lambda t: print(f"  {_c('32;1', '✓')} {t}")
FAIL = lambda t: print(f"  {_c('31;1', '✗')} {t}")
WARN = lambda t: print(f"  {_c('33;1', '!')} {t}")
INFO = lambda t: print(f"  {_c('34',   '·')} {t}")
HEAD = lambda t: print(f"\n{_c('1', t)}")


# ──────────────────────────────────────────────────────────────────────────────
# 설정 파일 검증
# ──────────────────────────────────────────────────────────────────────────────

def check_config_files(config_dir: Path) -> dict:
    HEAD("[ 1 / 4 ]  설정 파일 검증")
    results = {}

    # reviewer_config.json
    cfg_path = config_dir / "reviewer_config.json"
    if not cfg_path.exists():
        FAIL(f"reviewer_config.json 없음: {cfg_path}")
        results["reviewer_config"] = False
        return results

    try:
        with cfg_path.open() as f:
            cfg = json.load(f)
        OK(f"reviewer_config.json 파싱 성공")
        results["config"] = cfg
    except json.JSONDecodeError as e:
        FAIL(f"reviewer_config.json JSON 오류: {e}")
        results["reviewer_config"] = False
        return results

    # 필수 키 확인
    required = [
        ("gerrit", "url"),
        ("gerrit", "username"),
        ("gerrit", "password"),
    ]
    config_ok = True
    for section, key in required:
        val = cfg.get(section, {}).get(key, "")
        if not val or "example.com" in str(val) or val.startswith("YOUR_"):
            FAIL(f"설정 미완료: {section}.{key} = \"{val}\"")
            config_ok = False
        else:
            INFO(f"{section}.{key} = \"{val[:30]}{'...' if len(str(val)) > 30 else ''}\"")
    results["reviewer_config"] = config_ok

    # api_keys.json
    api_path = config_dir / "api_keys.json"
    if not api_path.exists():
        FAIL(f"api_keys.json 없음: {api_path}")
        results["api_keys"] = False
        return results

    try:
        with api_path.open() as f:
            keys = json.load(f)
        provider = cfg.get("ai", {}).get("provider", "claude")
        api_key  = keys.get(provider, {}).get("api_key", "")
        if not api_key or api_key.startswith("YOUR_"):
            FAIL(f"api_keys.json: '{provider}' API 키 미설정")
            results["api_keys"] = False
        else:
            masked = api_key[:8] + "..." + api_key[-4:]
            OK(f"api_keys.json: {provider} API 키 확인 ({masked})")
            results["api_keys"] = True
    except json.JSONDecodeError as e:
        FAIL(f"api_keys.json JSON 오류: {e}")
        results["api_keys"] = False

    if results.get("reviewer_config") and results.get("api_keys"):
        OK("설정 파일 전체 정상")
    return results


# ──────────────────────────────────────────────────────────────────────────────
# Gerrit 연결 테스트
# ──────────────────────────────────────────────────────────────────────────────

def check_gerrit(cfg: dict, change_number: int = None) -> bool:
    HEAD("[ 2 / 4 ]  Gerrit 서버 연결 테스트")

    try:
        from scripts.gerrit_client import GerritClient
    except ImportError as e:
        FAIL(f"gerrit_client 임포트 실패: {e}")
        return False

    gcfg   = cfg.get("gerrit", {})
    url    = gcfg.get("url", "")
    user   = gcfg.get("username", "")
    passwd = gcfg.get("password", "")

    INFO(f"접속: {url}  (user: {user})")

    client = GerritClient(
        base_url   = url,
        username   = user,
        password   = passwd,
        auth_type  = gcfg.get("auth_type", "basic"),
        timeout    = gcfg.get("timeout", 30),
        verify_ssl = gcfg.get("verify_ssl", True),
    )

    t0 = time.time()
    ok = client.test_connection()
    elapsed = time.time() - t0

    if ok:
        OK(f"Gerrit 연결 성공  ({elapsed:.2f}s)")
        INFO(f"버전 정보: {client.caps.summary()}")
    else:
        FAIL(f"Gerrit 연결 실패  ({elapsed:.2f}s)")
        WARN("확인 사항:")
        WARN("  1. gerrit.url 이 올바른지 확인")
        WARN("  2. Gerrit HTTP Password 를 재발급해 설정")
        WARN("  3. 자체 서명 인증서라면 verify_ssl: false 설정")
        return False

    # 특정 Change 접근 테스트
    if change_number:
        INFO(f"Change #{change_number} 접근 테스트...")
        try:
            change = client.get_change(change_number, 1)
            OK(f"Change #{change_number}: [{change.project}] {change.subject[:50]}")
        except Exception as exc:
            FAIL(f"Change #{change_number} 조회 실패: {exc}")
            return False

    return True


# ──────────────────────────────────────────────────────────────────────────────
# AI API 테스트
# ──────────────────────────────────────────────────────────────────────────────

def check_ai(cfg: dict, config_dir: Path) -> bool:
    HEAD("[ 3 / 4 ]  AI API 연결 테스트")

    try:
        from ai_chat import create_ai
    except ImportError as e:
        FAIL(f"ai_chat 임포트 실패: {e}")
        return False

    provider = cfg.get("ai", {}).get("provider", "claude")
    model    = cfg.get("ai", {}).get("model")
    api_cfg  = config_dir / "api_keys.json"

    INFO(f"제공자: {provider}  모델: {model or '기본값'}")

    # dry_run=True 로 먼저 임포트/초기화 확인
    try:
        ai = create_ai(
            provider    = provider,
            config_path = str(api_cfg),
            model       = model,
            dry_run     = True,
        )
        OK(f"AI 인스턴스 생성 성공: {ai.provider_name} / {ai.model}")
    except Exception as e:
        FAIL(f"AI 초기화 실패: {e}")
        return False

    # 실제 API 호출 (짧은 프롬프트)
    INFO("실제 API 호출 테스트 (짧은 프롬프트)...")
    try:
        ai_real = create_ai(
            provider    = provider,
            config_path = str(api_cfg),
            model       = model,
            dry_run     = False,
            retry_count = 1,
            retry_delay = 2.0,
        )
        t0 = time.time()
        resp = ai_real.chat("다음 Python 코드의 문제점을 한 줄로 설명하세요:\nx=1/0")
        elapsed = time.time() - t0

        if resp.success:
            preview = resp.answer[:80].replace("\n", " ")
            OK(f"AI 응답 수신 ({elapsed:.2f}s): {preview}...")
            return True
        else:
            FAIL(f"AI API 오류: {resp.error}")
            WARN("API 키가 올바른지, 크레딧이 남아있는지 확인하세요.")
            return False

    except Exception as e:
        FAIL(f"API 호출 중 예외: {e}")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# 환경 / 의존성 검사
# ──────────────────────────────────────────────────────────────────────────────

def check_environment() -> bool:
    HEAD("[ 4 / 4 ]  실행 환경 및 의존성 검사")
    import sys
    v = sys.version_info
    if v >= (3, 10):
        OK(f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        FAIL(f"Python {v.major}.{v.minor} — 3.10 이상 필요")
        return False

    packages = {
        "requests":   "Gerrit REST API",
        "anthropic":  "Claude AI",
        "google.genai": "Gemini AI",
        "openai":     "OpenAI",
    }
    missing = []
    for pkg, desc in packages.items():
        try:
            __import__(pkg.split(".")[0])
            OK(f"{pkg}  ({desc})")
        except ImportError:
            WARN(f"{pkg} 미설치  ({desc}) — 사용하지 않는다면 무시 가능")
            missing.append(pkg)

    # requests 는 필수
    if "requests" in missing:
        FAIL("requests 패키지는 필수입니다: pip install requests")
        return False

    return True


# ──────────────────────────────────────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Gerrit AI 리뷰어 연결 진단 도구",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  python scripts/test_connection.py
  python scripts/test_connection.py --gerrit-only
  python scripts/test_connection.py --ai-only
  python scripts/test_connection.py --change 12345
""",
    )
    p.add_argument("--gerrit-only", action="store_true", help="Gerrit 연결만 검사")
    p.add_argument("--ai-only",     action="store_true", help="AI API만 검사")
    p.add_argument("--change",      type=int, default=None, help="접근 테스트할 Change 번호")
    p.add_argument("--config",      type=str, default=None, help="설정 디렉토리 경로")
    return p


def main():
    logging.basicConfig(level=logging.WARNING)   # 진단 중 ai_chat 내부 로그 억제
    args = build_parser().parse_args()

    config_dir = Path(args.config) if args.config else PROJECT_DIR / "config"

    print(_c("1;34", "\n══════════════════════════════════════════"))
    print(_c("1;34", "  Gerrit AI 리뷰어 — 연결 진단"))
    print(_c("1;34", "══════════════════════════════════════════"))
    print(f"  프로젝트: {PROJECT_DIR}")
    print(f"  설정 경로: {config_dir}")

    all_ok   = True
    cfg      = {}

    # 환경 체크는 항상
    if not (args.gerrit_only or args.ai_only):
        if not check_environment():
            all_ok = False

    # 설정 파일
    cfg_results = check_config_files(config_dir)
    cfg = cfg_results.get("config", {})

    if not cfg:
        FAIL("설정 로드 실패 — 이후 검사를 건너뜁니다.")
        sys.exit(1)

    # Gerrit
    if not args.ai_only:
        if not check_gerrit(cfg, change_number=args.change):
            all_ok = False

    # AI
    if not args.gerrit_only:
        if not check_ai(cfg, config_dir):
            all_ok = False

    # 최종 요약
    print()
    print(_c("1", "══════════════════════════════════════════"))
    if all_ok:
        print(_c("32;1", "  ✅ 전체 검사 통과 — 리뷰어 실행 준비 완료"))
        print()
        print("  다음 명령으로 테스트 리뷰를 실행하세요:")
        print(f"  python scripts/gerrit_reviewer.py --change <번호> --patchset 1 --no-post")
    else:
        print(_c("31;1", "  ❌ 일부 검사 실패 — 위 오류를 먼저 해결하세요"))
        print()
        print("  도움말: README.md의 '문제 해결' 섹션을 참고하세요.")
    print(_c("1", "══════════════════════════════════════════\n"))

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
