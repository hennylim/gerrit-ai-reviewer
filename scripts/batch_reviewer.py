"""
batch_reviewer.py
-----------------
여러 Gerrit Change 를 일괄로 AI 코드 리뷰하는 배치 처리 스크립트.

사용 예시:
  # 변경 번호 직접 지정
  python scripts/batch_reviewer.py --changes 100 101 102

  # 파일에서 읽기 (한 줄에 "change_number patchset" 형식)
  python scripts/batch_reviewer.py --file review_list.txt

  # Gerrit 쿼리 기반 (status:open + 특정 프로젝트)
  python scripts/batch_reviewer.py --query "status:open project:myproject"

  # 동시 실행 (워커 수 조정)
  python scripts/batch_reviewer.py --changes 100 101 102 --workers 3

  # 결과를 별도 디렉토리에 저장
  python scripts/batch_reviewer.py --changes 100 101 102 --output /tmp/batch_output
"""

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

SCRIPT_DIR  = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))

from scripts.gerrit_reviewer import run_review, load_config, setup_logging


# ──────────────────────────────────────────────────────────────────────────────
# 배치 대상 파싱
# ──────────────────────────────────────────────────────────────────────────────

def parse_review_list_file(filepath: str) -> list[tuple[int, int]]:
    """
    파일에서 리뷰 대상 목록을 읽습니다.
    형식 (한 줄에 하나):
      12345         → change=12345, patchset=1 (기본값)
      12345 2       → change=12345, patchset=2
      12345,3       → change=12345, patchset=3
      # 주석 줄 무시
    """
    pairs = []
    path  = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"리뷰 목록 파일 없음: {filepath}")

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.replace(",", " ").split()
        change   = int(parts[0])
        patchset = int(parts[1]) if len(parts) > 1 else 1
        pairs.append((change, patchset))
    return pairs


def query_gerrit_changes(query: str, cfg: dict) -> list[tuple[int, int]]:
    """Gerrit 쿼리로 변경사항 목록을 조회합니다."""
    from scripts.gerrit_client import GerritClient

    gcfg   = cfg["gerrit"]
    client = GerritClient(
        base_url   = gcfg["url"],
        username   = gcfg["username"],
        password   = gcfg["password"],
        auth_type  = gcfg.get("auth_type", "basic"),
        verify_ssl = gcfg.get("verify_ssl", True),
    )

    data = client._get(
        "changes/",
        params={"q": query, "o": "CURRENT_REVISION", "n": 100}
    )
    pairs = []
    for change in data:
        cn  = change.get("_number", 0)
        rev = change.get("revisions", {})
        ps  = max(
            (v.get("_number", 1) for v in rev.values()),
            default=1
        )
        if cn:
            pairs.append((cn, ps))

    logging.getLogger("batch_reviewer").info(
        "Gerrit 쿼리 '%s' → %d 건", query, len(pairs)
    )
    return pairs


# ──────────────────────────────────────────────────────────────────────────────
# 단일 리뷰 실행 (스레드풀용)
# ──────────────────────────────────────────────────────────────────────────────

def _review_task(
    change:    int,
    patchset:  int,
    cfg:       dict,
    project_dir: Path,
    kwargs:    dict,
) -> dict:
    """단일 리뷰 실행 후 요약 딕셔너리 반환"""
    logger = logging.getLogger("batch_reviewer")
    logger.info("▶ 리뷰 시작: change=#%d ps=%d", change, patchset)
    t0 = time.time()
    try:
        result = run_review(
            change_number   = change,
            patchset_number = patchset,
            cfg             = cfg,
            project_dir     = project_dir,
            **kwargs,
        )
        elapsed = time.time() - t0
        return {
            "change":    change,
            "patchset":  patchset,
            "success":   result.success,
            "score":     result.overall_score,
            "posted":    result.gerrit_posted,
            "elapsed":   elapsed,
            "error":     result.error,
        }
    except Exception as exc:
        elapsed = time.time() - t0
        logger.exception("리뷰 실패: change=#%d — %s", change, exc)
        return {
            "change":   change,
            "patchset": patchset,
            "success":  False,
            "score":    0,
            "posted":   False,
            "elapsed":  elapsed,
            "error":    str(exc),
        }


# ──────────────────────────────────────────────────────────────────────────────
# 배치 실행
# ──────────────────────────────────────────────────────────────────────────────

def run_batch(
    pairs:       list[tuple[int, int]],
    cfg:         dict,
    project_dir: Path,
    workers:     int  = 1,
    interval:    float = 2.0,
    output_dir:  Path = None,
    **review_kwargs,
) -> list[dict]:
    logger = logging.getLogger("batch_reviewer")

    if output_dir:
        cfg.setdefault("output", {})["dir"] = str(output_dir)

    logger.info("=" * 60)
    logger.info("배치 AI 코드 리뷰 시작")
    logger.info("  대상: %d 건  워커: %d  간격: %.1fs", len(pairs), workers, interval)
    logger.info("=" * 60)

    results   = []
    total     = len(pairs)

    if workers <= 1:
        # 순차 실행
        for i, (change, patchset) in enumerate(pairs, 1):
            logger.info("[%d/%d] 처리 중: #%d ps%d", i, total, change, patchset)
            r = _review_task(change, patchset, cfg, project_dir, review_kwargs)
            results.append(r)
            if i < total and interval > 0:
                logger.debug("다음 요청까지 %.1f초 대기...", interval)
                time.sleep(interval)
    else:
        # 병렬 실행
        logger.info("병렬 실행 (workers=%d)", workers)
        futures = {}
        with ThreadPoolExecutor(max_workers=workers) as executor:
            for change, patchset in pairs:
                f = executor.submit(
                    _review_task, change, patchset, cfg, project_dir, review_kwargs
                )
                futures[f] = (change, patchset)

            for done in as_completed(futures):
                r = done.result()
                results.append(r)
                status = "✅" if r["success"] else "❌"
                logger.info(
                    "%s #%d ps%d  score=%+d  elapsed=%.1fs",
                    status, r["change"], r["patchset"], r["score"], r["elapsed"]
                )

    return results


# ──────────────────────────────────────────────────────────────────────────────
# 배치 결과 요약 저장
# ──────────────────────────────────────────────────────────────────────────────

def save_batch_summary(
    results:    list[dict],
    output_dir: Path,
) -> Path:
    """배치 실행 결과 요약을 JSON + TXT로 저장합니다."""
    ts     = datetime.now().strftime("%Y%m%d_%H%M%S")
    ok     = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"]]
    total_elapsed = sum(r["elapsed"] for r in results)

    summary = {
        "timestamp":     datetime.now().isoformat(),
        "total":         len(results),
        "success":       len(ok),
        "failed":        len(failed),
        "total_elapsed": round(total_elapsed, 2),
        "results":       results,
        "notes": {
            "retention": "코멘트 없는 파일은 자동 제외됨, 배치 잘림 시 재시도 적용",
        },
    }

    output_dir.mkdir(parents=True, exist_ok=True)

    # JSON
    json_path = output_dir / f"batch_summary_{ts}.json"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))

    # TXT 요약
    lines = [
        "=" * 60,
        f"  배치 AI 코드 리뷰 요약",
        f"  실행 시각: {summary['timestamp']}",
        "-" * 60,
        f"  총 건수  : {len(results)}",
        f"  성공     : {len(ok)}",
        f"  실패     : {len(failed)}",
        f"  소요 시간: {total_elapsed:.1f}초",
        "=" * 60,
        "",
        "[ 처리 정책 ]",
        f"  · 코멘트 없는 파일: 결과에서 자동 제외",
        f"  · 배치 잘림 감지: 반으로 분할 → 재시도 → 개별 호출 폴백",
        f"  · 재시도 레벨: 최대 3단계 (배치 → 반분할 → 개별)",
        "",
        "[ 결과 목록 ]",
    ]
    for r in results:
        status = "OK" if r["success"] else "NG"
        posted = "posted" if r.get("posted") else "no-post"
        err    = f"  ERROR: {r['error']}" if r.get("error") else ""
        lines.append(
            f"  [{status}] #%d ps%d  score=%+d  %s  %.1fs%s"
            % (r["change"], r["patchset"], r["score"], posted, r["elapsed"], err)
        )

    if failed:
        lines += ["", "[ 실패 목록 ]"]
        for r in failed:
            lines.append(f"  ❌ #%d ps%d — %s" % (r["change"], r["patchset"], r.get("error","")))

    txt_path = output_dir / f"batch_summary_{ts}.txt"
    txt_path.write_text("\n".join(lines), encoding="utf-8")

    logger = logging.getLogger("batch_reviewer")
    logger.info("배치 요약 저장: %s", json_path)
    logger.info("배치 요약 저장: %s", txt_path)
    return json_path


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Gerrit AI 코드 리뷰 배치 처리",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  # 번호 직접 지정
  python scripts/batch_reviewer.py --changes 100 101 102

  # 파일에서 읽기
  python scripts/batch_reviewer.py --file review_list.txt

  # Gerrit 쿼리 기반
  python scripts/batch_reviewer.py --query "status:open project:my-project branch:main"

  # 병렬 실행 (워커 3개)
  python scripts/batch_reviewer.py --changes 100 101 102 --workers 3

  # DRY-RUN + NO-POST 디버깅
  python scripts/batch_reviewer.py --changes 100 101 --dry-run

  # NO-POST (AI 리뷰만, Gerrit 미등록)
  python scripts/batch_reviewer.py --changes 100 101 --no-post
""",
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--changes",  type=int, nargs="+", metavar="N",
                     help="리뷰할 Change 번호 목록 (patchset은 최신 사용)")
    src.add_argument("--file",     type=str, metavar="PATH",
                     help="리뷰 목록 파일 (한 줄에 'change [patchset]')")
    src.add_argument("--query",    type=str, metavar="QUERY",
                     help="Gerrit 검색 쿼리 (예: status:open project:foo)")

    p.add_argument("--patchset",  type=int, default=1,
                   help="--changes 사용 시 patchset 번호 (기본: 1)")
    p.add_argument("--workers",   type=int, default=1,
                   help="병렬 처리 워커 수 (기본: 1, 순차)")
    p.add_argument("--interval",  type=float, default=2.0,
                   help="순차 처리 시 요청 간격 초 (기본: 2.0)")
    p.add_argument("--config",    type=str, default=None,
                   help="설정 디렉토리 경로")
    p.add_argument("--output",    type=str, default=None,
                   help="결과 출력 디렉토리")
    p.add_argument("--provider",  type=str, default=None,
                   help="AI 제공자 오버라이드")
    p.add_argument("--model",     type=str, default=None,
                   help="AI 모델 오버라이드")
    p.add_argument("--dry-run",   action="store_true",
                   help="테스트 모드 (AI/Gerrit 실제 호출 없음)")
    p.add_argument("--no-post",   action="store_true",
                   help="AI 리뷰 후 Gerrit 미등록")
    p.add_argument("--verbose",   action="store_true",
                   help="상세 로그")
    return p


def main():
    args = build_parser().parse_args()

    config_dir  = Path(args.config) if args.config else PROJECT_DIR / "config"
    output_dir  = Path(args.output) if args.output else PROJECT_DIR / "output"
    log_dir     = PROJECT_DIR / "logs"

    logger = setup_logging(log_dir, verbose=args.verbose)
    logger.info("배치 리뷰어 시작")

    # 설정 로드
    try:
        cfg = load_config(config_dir)
    except FileNotFoundError as exc:
        logger.error("설정 파일 없음: %s", exc)
        sys.exit(1)

    # 대상 목록 구성
    pairs: list[tuple[int, int]] = []
    if args.changes:
        pairs = [(c, args.patchset) for c in args.changes]
    elif args.file:
        try:
            pairs = parse_review_list_file(args.file)
        except (FileNotFoundError, ValueError) as exc:
            logger.error("리뷰 목록 파일 오류: %s", exc)
            sys.exit(1)
    elif args.query:
        try:
            pairs = query_gerrit_changes(args.query, cfg)
        except Exception as exc:
            logger.error("Gerrit 쿼리 실패: %s", exc)
            sys.exit(1)

    if not pairs:
        logger.warning("리뷰 대상이 없습니다.")
        sys.exit(0)

    logger.info("리뷰 대상: %d 건 — %s", len(pairs),
                ", ".join(f"#{c}" for c, _ in pairs[:10]) + ("..." if len(pairs) > 10 else ""))

    # 배치 실행
    results = run_batch(
        pairs        = pairs,
        cfg          = cfg,
        project_dir  = PROJECT_DIR,
        workers      = args.workers,
        interval     = args.interval,
        output_dir   = output_dir,
        dry_run              = args.dry_run,
        no_post              = args.no_post,
        verbose              = args.verbose,
        ai_provider_override = args.provider,
        ai_model_override    = args.model,
    )

    # 요약 저장
    save_batch_summary(results, output_dir)

    # 종료 코드
    failed = sum(1 for r in results if not r["success"])
    if failed:
        logger.warning("실패: %d / %d 건", failed, len(results))
        sys.exit(1)
    else:
        logger.info("전체 성공: %d 건", len(results))
        sys.exit(0)


if __name__ == "__main__":
    main()
