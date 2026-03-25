"""
gerrit_reviewer.py
------------------
Gerrit AI 자동 코드 리뷰 메인 실행 스크립트.

동작 흐름:
  1. Gerrit 에서 변경사항 정보 및 diff 조회
  2. AI 를 통해 코드 리뷰 생성
  3. 결과를 TEXT / MD / JSON / HTML 파일로 저장
  4. (--no-post 없으면) Gerrit 에 리뷰 코멘트 등록

사용 예시:
  python gerrit_reviewer.py --change 12345 --patchset 1
  python gerrit_reviewer.py --change 12345 --patchset 1 --dry-run
  python gerrit_reviewer.py --change 12345 --patchset 1 --no-post
"""

import argparse
from dataclasses import dataclass, field
import json
import logging
import os
import sys
import time
from pathlib import Path
from datetime import datetime

# ── 경로 설정: 어느 경로에서 실행해도 동작 ────────────────────────────────────
SCRIPT_DIR  = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))       # ai_chat 패키지 탐색 경로

from ai_chat import create_ai
from scripts.gerrit_client   import GerritClient, ReviewInput
from scripts.review_formatter import ReviewFormatter, ReviewResult

logger = logging.getLogger("gerrit_reviewer")

# ──────────────────────────────────────────────────────────────────────────────
# 로깅 설정
# ──────────────────────────────────────────────────────────────────────────────

def setup_logging(log_dir: Path, verbose: bool = False) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file  = log_dir / f"reviewer_{ts}.log"

    level     = logging.DEBUG if verbose else logging.INFO
    fmt       = "[%(asctime)s] %(levelname)-8s %(name)s — %(message)s"
    datefmt   = "%Y-%m-%d %H:%M:%S"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)   # 핸들러가 필터링

    # 파일 핸들러: DEBUG 이상 모두 기록
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(fmt, datefmt))
    root.addHandler(fh)

    # 콘솔 핸들러: verbose 여부에 따라 레벨 조정
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(logging.Formatter(fmt, datefmt))
    root.addHandler(ch)

    logger = logging.getLogger("gerrit_reviewer")
    logger.info("로그 파일: %s", log_file)
    return logger


# ──────────────────────────────────────────────────────────────────────────────
# 설정 로더
# ──────────────────────────────────────────────────────────────────────────────

def load_config(config_dir: Path) -> dict:
    """reviewer_config.json 을 로드합니다. 환경 변수로 값 오버라이드 가능."""
    config_file = config_dir / "reviewer_config.json"
    if not config_file.exists():
        raise FileNotFoundError(f"설정 파일이 없습니다: {config_file}")

    with config_file.open(encoding="utf-8") as f:
        cfg = json.load(f)

    # 환경 변수 오버라이드 (CI/CD 파이프라인 친화적)
    env_map = {
        "GERRIT_URL":      ("gerrit", "url"),
        "GERRIT_USER":     ("gerrit", "username"),
        "GERRIT_PASSWORD": ("gerrit", "password"),
        "AI_PROVIDER":     ("ai",     "provider"),
        "AI_MODEL":        ("ai",     "model"),
    }
    for env_key, (section, key) in env_map.items():
        val = os.environ.get(env_key)
        if val:
            cfg.setdefault(section, {})[key] = val
            logging.getLogger("gerrit_reviewer").debug(
                "환경 변수 오버라이드: %s.%s", section, key
            )
    return cfg


# ──────────────────────────────────────────────────────────────────────────────
# 프롬프트 빌더
# ──────────────────────────────────────────────────────────────────────────────

def build_inline_review_prompt(
    subject:      str,
    project:      str,
    branch:       str,
    diff:         "FileDiff",        # FileDiff 객체
    prompt_cfg:   dict,
) -> str:
    """
    AI 에게 파일별 리뷰를 JSON 구조로 반환하도록 요청하는 프롬프트를 생성합니다.

    AI 응답 예시 (반드시 아래 JSON 형식으로만 반환):
    {
      "file_summary": "전체 파일에 대한 요약...",
      "inline_comments": [
        {
          "line": 47,
          "side": "RIGHT",
          "severity": "CRITICAL",
          "category": "Security",
          "message": "SQL Injection 취약점: f-string 쿼리 직접 조합은 위험합니다.\n수정: cursor.execute('...', (param,))"
        }
      ]
    }

    side: "RIGHT" = 변경 후 코드(신규 줄), "LEFT" = 변경 전 코드(삭제 줄)
    severity: CRITICAL / MAJOR / MINOR / INFO
    line: diff 에서 +로 시작하는 줄의 실제 라인 번호 (1-based)
    """
    focus_areas = prompt_cfg.get("focus_areas", [])
    language    = prompt_cfg.get("language", "Korean")

    focus_str = ""
    if focus_areas:
        focus_str = "중점 리뷰 영역:\n" + "\n".join(f"  - {a}" for a in focus_areas)

    # diff 에서 추가된 줄(+)의 라인 번호 목록을 힌트로 제공
    added_lines = _extract_added_lines(diff.diff_content)
    line_hint   = ""
    if added_lines:
        line_hint = f"\n추가된 라인 번호 목록 (RIGHT side): {added_lines[:40]}"

    return f"""당신은 10년 이상 경력의 시니어 소프트웨어 엔지니어입니다.
아래 코드 diff를 리뷰하고, 반드시 JSON 형식으로만 응답하세요.
JSON 외 다른 텍스트(설명, 마크다운 코드블록 등)는 절대 포함하지 마세요.

프로젝트: {project}  브랜치: {branch}
변경 제목: {subject}
파일: {diff.filename}  (변경 유형: {diff.change_type}, +{diff.lines_inserted}/-{diff.lines_deleted})
{focus_str}{line_hint}

응답 JSON 스키마:
{{
  "file_summary": "<파일 전체 리뷰 요약 - {language}로 작성>",
  "inline_comments": [
    {{
      "line": <라인 번호(정수)>,
      "side": "RIGHT",
      "severity": "<CRITICAL|MAJOR|MINOR|INFO>",
      "category": "<Security|Bug|Performance|Style|Test|Design>",
      "message": "<{language}로 작성한 구체적 코멘트. 문제 설명 + 수정 방법 포함>"
    }}
  ]
}}

규칙:
- inline_comments 는 실제 문제가 있는 라인에만 작성 (문제 없으면 빈 배열 [])
- line 은 diff 에서 + 로 시작하는 줄의 실제 파일 라인 번호
- message 는 300자 이내로 간결하게 (줄바꿈은 \\n 사용)
- 심각도별 기준: CRITICAL=보안/데이터손실, MAJOR=버그/성능, MINOR=코드품질, INFO=개선제안

--- DIFF START ---
{diff.diff_content}
--- DIFF END ---"""


def _extract_added_lines(diff_content: str) -> list[int]:
    """
    unified diff 에서 추가된 줄(+)의 실제 파일 라인 번호를 추출합니다.
    @@ -a,b +c,d @@ 헤더를 파싱해 라인 번호를 추적합니다.
    """
    import re
    lines   = diff_content.splitlines()
    result  = []
    cur_new = 0   # 현재 NEW 파일 라인 번호

    for line in lines:
        hunk = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if hunk:
            cur_new = int(hunk.group(1)) - 1
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            cur_new += 1
            result.append(cur_new)
        elif line.startswith("-"):
            pass   # 삭제 줄: NEW 파일 번호 증가 없음
        else:
            cur_new += 1   # 컨텍스트 줄

    return result


def parse_inline_comments(ai_response: str, filename: str) -> tuple[str, list[dict]]:
    """
    AI JSON 응답을 파싱해 (file_summary, inline_comments) 를 반환합니다.

    Returns:
        file_summary   : 파일 전체 요약 텍스트
        inline_comments: [{"line": N, "side": "RIGHT", "severity": "...", "message": "..."}]
    """
    import re
    import json as _json

    # JSON 블록 추출 (```json ... ``` 마크다운 감싸기 대응)
    text = ai_response.strip()
    md_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if md_match:
        text = md_match.group(1)

    # 첫 번째 { ~ 마지막 } 범위만 추출 (앞뒤 잡음 제거)
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        text = brace_match.group(0)

    try:
        data = _json.loads(text)
    except (_json.JSONDecodeError, ValueError) as exc:
        logger.warning("파일 '%s' AI 응답 JSON 파싱 실패: %s", filename, exc)
        logger.debug("원본 응답:\n%s", ai_response[:500])
        # 파싱 실패 시 전체 텍스트를 요약으로 사용
        return ai_response, []

    file_summary    = data.get("file_summary", "")
    raw_comments    = data.get("inline_comments", [])
    inline_comments = []

    for c in raw_comments:
        try:
            line = int(c.get("line", 0))
            if line <= 0:
                continue
            severity = c.get("severity", "INFO").upper()
            category = c.get("category", "")
            message  = c.get("message", "").strip()
            side     = c.get("side", "RIGHT").upper()

            if not message:
                continue

            # severity 아이콘 매핑
            icon = {"CRITICAL": "🔴", "MAJOR": "🟠", "MINOR": "🟡", "INFO": "🔵"}.get(severity, "⚪")
            cat_str = f"[{category}] " if category else ""

            formatted_msg = f"{icon} [{severity}] {cat_str}{message}"

            inline_comments.append({
                "line":     line,
                "side":     side,
                "severity": severity,
                "message":  formatted_msg,
            })
        except (TypeError, ValueError) as exc:
            logger.debug("인라인 코멘트 항목 파싱 오류: %s — %s", c, exc)
            continue

    logger.debug(
        "  파일 '%s': 인라인 코멘트 %d개 파싱 완료", filename, len(inline_comments)
    )
    return file_summary, inline_comments


def build_gerrit_comments(file_reviews: list[dict]) -> dict:
    """
    file_reviews 목록에서 Gerrit REST API CommentInput 형식의 dict를 생성합니다.

    Gerrit CommentInput 형식:
      {
        "filename": [
          {
            "line":       <int>,
            "side":       "REVISION" | "PARENT",
            "message":    "<str>",
            "unresolved": true
          }
        ]
      }

    side 변환: RIGHT → REVISION (변경 후), LEFT → PARENT (변경 전)
    """
    comments: dict[str, list[dict]] = {}

    for fr in file_reviews:
        filename        = fr.get("filename", "")
        inline_comments = fr.get("inline_comments", [])

        if not inline_comments:
            continue

        file_comments = []
        for c in inline_comments:
            side = "REVISION" if c.get("side", "RIGHT") == "RIGHT" else "PARENT"
            file_comments.append({
                "line":       c["line"],
                "side":       side,
                "message":    c["message"],
                "unresolved": c.get("severity") in ("CRITICAL", "MAJOR"),
            })

        if file_comments:
            comments[filename] = file_comments
            logger.debug(
                "Gerrit 코멘트 구성: %s — %d개", filename, len(file_comments)
            )

    total = sum(len(v) for v in comments.values())
    logger.info("인라인 코멘트 총 %d개 (파일 %d개)", total, len(comments))
    return comments


def build_fallback_message(
    review_summary: str,
    file_reviews:   list[dict],
) -> str:
    """
    인라인 코멘트 미지원 Gerrit 에서 Change 레벨 코멘트 하나에
    전체 요약 + 파일별 리뷰 내용 + 인라인 코멘트를 모두 담아 반환합니다.

    포맷:
      [전체 요약]
      ---
      ### 파일명 (+N/-M)
      <파일 요약>
      Line 47  [CRITICAL]  메시지
      Line 53  [MAJOR]     메시지
      ---
      ...
    """
    SEP  = "=" * 60
    THIN = "-" * 60
    sev_label = {
        "CRITICAL": "[CRITICAL]", "MAJOR": "[MAJOR  ]",
        "MINOR":    "[MINOR  ]",  "INFO":  "[INFO   ]",
    }

    lines = [review_summary, "", SEP, "📂 파일별 상세 리뷰", SEP, ""]

    for fr in file_reviews:
        fname   = fr.get("filename", "?")
        summary = (fr.get("file_summary") or fr.get("review_text", "")).strip()
        inlines = fr.get("inline_comments", [])
        ins     = fr.get("lines_ins", 0)
        dels    = fr.get("lines_del", 0)

        lines.append(f"### {fname}  (+{ins}/-{dels})")
        lines.append(THIN)
        if summary:
            lines.append(summary)

        if inlines:
            lines.append("")
            lines.append("  [인라인 코멘트]")
            for c in inlines:
                sev   = c.get("severity", "INFO")
                label = sev_label.get(sev, f"[{sev}]")
                msg   = c.get("message", "").replace("\n", " ")
                lines.append(f"  Line {c['line']:4d}  {label}  {msg}")

        lines.append("")

    return "\n".join(lines)


def build_summary_prompt(
    file_reviews: list[dict],
    subject:      str,
    prompt_cfg:   dict,
) -> str:
    """개별 파일 리뷰를 종합해 전체 요약 프롬프트를 생성합니다."""
    # file_summary 를 우선 사용, 없으면 review_text 폴백
    reviews_text = "\n\n".join(
        f"### {fr['filename']}\n{fr.get('file_summary') or fr.get('review_text', '')}"
        for fr in file_reviews
    )
    return (
        f"다음은 '{subject}' 변경사항의 파일별 코드 리뷰 결과입니다.\n\n"
        f"{reviews_text}\n\n"
        "위 리뷰를 바탕으로:\n"
        "1. 전체적인 변경사항에 대한 종합 평가를 작성해 주세요(최대한 요약해서 작성).\n"
        "2. 주요 문제점과 개선 권고사항을 정리해 주세요(최대한 요약해서 작성).\n"
        "3. Code-Review 점수를 -1(수정필요) ~ +1(승인) 범위에서 추천하고 그 이유를 설명해 주세요.\n"
        "4. 마지막 줄에 반드시 'SCORE: <숫자>' 형식으로 점수를 명시해 주세요. 예: SCORE: 1\n"
    )


def extract_score(text: str) -> int:
    """AI 응답에서 SCORE: N 패턴을 파싱합니다."""
    import re
    match = re.search(r"SCORE:\s*([+-]?\d)", text, re.IGNORECASE)
    if match:
        score = int(match.group(1))
        return max(-2, min(2, score))
    return 0


# ──────────────────────────────────────────────────────────────────────────────
# 파일 필터링
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class FilterResult:
    """파일 필터링 결과"""
    review_files:   list          # 리뷰할 FileDiff 목록
    skipped_files:  list[str]     # 건너뛴 파일 경로 목록
    skip_reasons:   dict[str, str]# {파일경로: 건너뛴 이유}
    total_skipped:  int           # 건너뛴 파일 수
    skip_whole_change: bool       # True이면 변경 전체를 건너뜀
    whole_skip_reason: str        # 전체 건너뜀 이유


def filter_files(
    diffs:      list,
    review_cfg: dict,
    total_file_count: int,
) -> FilterResult:
    """
    설정 기반으로 리뷰 대상 파일을 필터링합니다.

    전체 건너뜀 조건 (skip_whole_change=True):
      - 총 파일 수 > skip_if_total_files_over
      - 총 변경 줄 수 > skip_if_total_lines_over

    파일별 건너뜀 조건:
      - 확장자가 skip_extensions 에 포함
      - 경로가 skip_path_patterns 에 매칭
      - 변경 줄 수 > max_lines_per_file

    모두 0 또는 빈 목록이면 필터링 없이 전체 리뷰합니다.
    """
    skip_total_files = int(review_cfg.get("skip_if_total_files_over", 0))
    skip_total_lines = int(review_cfg.get("skip_if_total_lines_over", 0))
    skip_exts        = [e.lower() for e in review_cfg.get("skip_extensions", [])]
    skip_paths       = review_cfg.get("skip_path_patterns", [])
    max_lines_file   = int(review_cfg.get("max_lines_per_file", 0))

    total_lines = sum(d.lines_inserted + d.lines_deleted for d in diffs)

    # ── 전체 커밋 건너뜀 검사 ─────────────────────────────────────────────────
    if skip_total_files > 0 and total_file_count > skip_total_files:
        reason = (
            f"총 파일 수 {total_file_count}개가 "
            f"skip_if_total_files_over({skip_total_files})를 초과합니다."
        )
        return FilterResult(
            review_files=[], skipped_files=[d.filename for d in diffs],
            skip_reasons={d.filename: reason for d in diffs},
            total_skipped=len(diffs),
            skip_whole_change=True, whole_skip_reason=reason,
        )

    if skip_total_lines > 0 and total_lines > skip_total_lines:
        reason = (
            f"총 변경 줄 수 {total_lines}줄이 "
            f"skip_if_total_lines_over({skip_total_lines})를 초과합니다."
        )
        return FilterResult(
            review_files=[], skipped_files=[d.filename for d in diffs],
            skip_reasons={d.filename: reason for d in diffs},
            total_skipped=len(diffs),
            skip_whole_change=True, whole_skip_reason=reason,
        )

    # ── 파일별 건너뜀 검사 ───────────────────────────────────────────────────
    review_files  = []
    skipped_files = []
    skip_reasons  = {}

    for diff in diffs:
        fname     = diff.filename
        fname_low = fname.lower()
        file_lines= diff.lines_inserted + diff.lines_deleted
        reason    = None

        # 확장자 검사
        if skip_exts:
            for ext in skip_exts:
                if fname_low.endswith(ext):
                    reason = f"건너뜀 확장자 {ext}"
                    break

        # 경로 패턴 검사
        if reason is None and skip_paths:
            for pattern in skip_paths:
                if pattern.lower() in fname_low:
                    reason = f"건너뜀 경로 패턴 '{pattern}'"
                    break

        # 파일당 최대 줄 수 검사
        if reason is None and max_lines_file > 0 and file_lines > max_lines_file:
            reason = (
                f"변경 줄 수 {file_lines}줄이 "
                f"max_lines_per_file({max_lines_file})를 초과합니다."
            )

        if reason:
            skipped_files.append(fname)
            skip_reasons[fname] = reason
        else:
            review_files.append(diff)

    return FilterResult(
        review_files=review_files,
        skipped_files=skipped_files,
        skip_reasons=skip_reasons,
        total_skipped=len(skipped_files),
        skip_whole_change=False,
        whole_skip_reason="",
    )


def format_skip_notice(result: FilterResult) -> str:
    """
    건너뛴 파일 목록을 Gerrit 코멘트용 텍스트로 포맷합니다.
    전체 건너뜀과 파일별 건너뜀 모두 처리합니다.
    """
    if not result.skipped_files:
        return ""

    lines = ["[자동 리뷰 제외 파일 안내]", ""]
    if result.skip_whole_change:
        lines.append(f"이 변경사항은 자동 리뷰에서 제외되었습니다.")
        lines.append(f"사유: {result.whole_skip_reason}")
    else:
        lines.append(f"다음 {result.total_skipped}개 파일은 자동 리뷰에서 제외되었습니다.")
        lines.append("")
        for fname in result.skipped_files:
            reason = result.skip_reasons.get(fname, "")
            lines.append(f"  - {fname}")
            if reason:
                lines.append(f"    ({reason})")

    lines.append("")
    lines.append("전체 리뷰가 필요한 경우 수동으로 검토해 주세요.")
    return "\n".join(lines)



# ──────────────────────────────────────────────────────────────────────────────
# 핵심 리뷰 실행
# ──────────────────────────────────────────────────────────────────────────────

def run_review(
    change_number:   int,
    patchset_number: int,
    cfg:             dict,
    project_dir:     Path,
    dry_run:         bool = False,
    no_post:         bool = False,
    verbose:         bool = False,
    ai_provider_override: str = None,
    ai_model_override:    str = None,
) -> ReviewResult:

    logger = logging.getLogger("gerrit_reviewer")
    start  = time.time()

    gerrit_cfg  = cfg["gerrit"]
    ai_cfg      = cfg["ai"]
    prompt_cfg  = cfg.get("prompt", {})
    output_cfg  = cfg.get("output", {})
    review_cfg  = cfg.get("review", {})

    output_dir  = project_dir / output_cfg.get("dir",  "output")
    log_dir     = project_dir / output_cfg.get("log_dir", "logs")

    provider    = ai_provider_override or ai_cfg.get("provider", "claude")
    model       = ai_model_override    or ai_cfg.get("model")
    api_cfg     = project_dir / "config" / "api_keys.json"

    logger.info("=" * 60)
    logger.info("AI 코드 리뷰 시작")
    logger.info("  Change   : #%d  Patchset: %d", change_number, patchset_number)
    logger.info("  DRY-RUN  : %s  NO-POST: %s", dry_run, no_post)
    logger.info("  Provider : %s  Model: %s", provider, model or "default")
    logger.info("=" * 60)

    # ── 1. Gerrit 클라이언트 초기화 ──────────────────────────────────────────
    gerrit = GerritClient(
        base_url   = gerrit_cfg["url"],
        username   = gerrit_cfg["username"],
        password   = gerrit_cfg["password"],
        auth_type  = gerrit_cfg.get("auth_type", "basic"),
        timeout    = gerrit_cfg.get("timeout", 30),
        verify_ssl = gerrit_cfg.get("verify_ssl", True),
        dry_run    = dry_run,
        version    = gerrit_cfg.get("version", ""),   # 수동 버전 지정 (비어 있으면 자동 감지)
    )
    logger.info("  Gerrit 버전: %s", gerrit.caps.summary())

    # ── 2. 변경사항 정보 조회 ────────────────────────────────────────────────
    logger.info("[1/5] 변경사항 정보 조회 중...")
    change = gerrit.get_change(change_number, patchset_number)
    logger.info(
        "  변경사항: #%d [%s] %s (author: %s)",
        change.change_number, change.project, change.subject, change.owner
    )

    # ── 3. diff 조회 + 파일 필터링 ─────────────────────────────────────────────
    logger.info("[2/5] 파일 diff 조회 및 필터링 중...")
    max_files    = review_cfg.get("max_files",    50)
    context_lines= review_cfg.get("context_lines", 10)

    # 전체 파일 수 먼저 확인 (diff 내용 없이 목록만)
    all_file_list = gerrit.get_changed_files(change_number, patchset_number)
    total_file_count = len(all_file_list)
    logger.info("  변경 파일 수: %d개 (max_files=%d)", total_file_count, max_files)

    # max_files 제한 적용 후 diff 획득
    diffs = gerrit.get_all_diffs(change_number, patchset_number, max_files, context_lines)
    logger.info("  diff 획득: %d개 파일", len(diffs))

    if not diffs:
        logger.warning("변경된 파일이 없습니다. 리뷰를 건너뜁니다.")
        return ReviewResult(
            change_number=change_number,
            patchset_number=patchset_number,
            project=change.project,
            branch=change.branch,
            subject=change.subject,
            owner=change.owner,
            ai_provider=provider,
            ai_model=model or "unknown",
            review_summary="변경된 파일이 없어 리뷰를 건너뜁니다.",
            file_reviews=[],
            is_dry_run=dry_run,
            elapsed_seconds=time.time() - start,
        )

    # ── 파일 필터링 ────────────────────────────────────────────────────────
    filter_result = filter_files(diffs, review_cfg, total_file_count)

    if filter_result.total_skipped > 0:
        logger.info(
            "  필터링 결과: 리뷰 %d개 / 제외 %d개",
            len(filter_result.review_files), filter_result.total_skipped,
        )
        for fname, reason in filter_result.skip_reasons.items():
            logger.info("    [제외] %s — %s", fname, reason)

    # 전체 커밋이 제외 조건에 해당하는 경우
    if filter_result.skip_whole_change:
        skip_notice  = format_skip_notice(filter_result)
        skip_summary = (
            f"[자동 리뷰 제외]\n\n"
            f"{filter_result.whole_skip_reason}\n\n"
            f"변경 파일 수: {total_file_count}개\n"
            f"변경 줄 수: {sum(d.lines_inserted + d.lines_deleted for d in diffs)}줄\n\n"
            f"대량 변경사항은 자동 리뷰 범위를 초과하므로 수동 검토가 필요합니다."
        )
        logger.warning(
            "전체 커밋 리뷰 제외: %s", filter_result.whole_skip_reason
        )
        result = ReviewResult(
            change_number=change_number,
            patchset_number=patchset_number,
            project=change.project,
            branch=change.branch,
            subject=change.subject,
            owner=change.owner,
            ai_provider=provider,
            ai_model=model or "skip",
            review_summary=skip_summary,
            file_reviews=[],
            overall_score=0,
            is_dry_run=dry_run,
            elapsed_seconds=time.time() - start,
        )
        # 결과 파일 저장
        formatter = ReviewFormatter(output_dir)
        formatter.save_all(result)
        # Gerrit 코멘트 등록 (제외 안내 메시지)
        if not no_post:
            review_input = ReviewInput(
                message=skip_notice,
                labels={},
                tag="autogenerated:ai-reviewer",
                notify=review_cfg.get("notify", "NONE"),
            )
            posted = gerrit.post_review(change_number, patchset_number, review_input)
            result.gerrit_posted = posted
        return result

    # 리뷰 대상 파일이 없는 경우 (전부 제외됨)
    diffs = filter_result.review_files
    if not diffs:
        skip_notice  = format_skip_notice(filter_result)
        skip_summary = (
            f"[자동 리뷰 제외]\n\n"
            f"모든 파일({filter_result.total_skipped}개)이 자동 리뷰 제외 조건에 해당합니다.\n\n"
            f"{skip_notice}"
        )
        logger.warning("리뷰 대상 파일 없음: 모든 파일이 필터링됨")
        result = ReviewResult(
            change_number=change_number,
            patchset_number=patchset_number,
            project=change.project,
            branch=change.branch,
            subject=change.subject,
            owner=change.owner,
            ai_provider=provider,
            ai_model=model or "skip",
            review_summary=skip_summary,
            file_reviews=[],
            overall_score=0,
            is_dry_run=dry_run,
            elapsed_seconds=time.time() - start,
        )
        formatter = ReviewFormatter(output_dir)
        formatter.save_all(result)
        if not no_post:
            review_input = ReviewInput(
                message=skip_notice,
                labels={},
                tag="autogenerated:ai-reviewer",
                notify=review_cfg.get("notify", "NONE"),
            )
            gerrit.post_review(change_number, patchset_number, review_input)
        return result

    # ── 4. AI 초기화 ─────────────────────────────────────────────────────────
    logger.info("[3/5] AI(%s) 초기화 중...", provider)
    ai = create_ai(
        provider=provider,
        config_path=str(api_cfg),
        model=model,
        dry_run=dry_run,
        retry_count=ai_cfg.get("retry_count", 3),
        retry_delay=ai_cfg.get("retry_delay", 5.0),
    )
    logger.info("  AI 모델: %s", ai.model)

    # ── 5. 파일별 AI 리뷰 (JSON 구조화 응답 + 인라인 코멘트 파싱) ─────────────
    logger.info("[4/5] 파일별 AI 코드 리뷰 실행 중...")
    file_reviews = []
    for i, diff in enumerate(diffs, 1):
        logger.info("  [%d/%d] 리뷰 중: %s (+%d/-%d)",
                    i, len(diffs), diff.filename, diff.lines_inserted, diff.lines_deleted)

        prompt = build_inline_review_prompt(
            subject   = change.subject,
            project   = change.project,
            branch    = change.branch,
            diff      = diff,
            prompt_cfg= prompt_cfg,
        )
        logger.debug("  프롬프트 길이: %d chars", len(prompt))

        response = ai.chat(prompt)
        if not response.success:
            logger.warning("  AI 응답 실패 (%s): %s", diff.filename, response.error)
            file_reviews.append({
                "filename":        diff.filename,
                "change_type":     diff.change_type,
                "lines_ins":       diff.lines_inserted,
                "lines_del":       diff.lines_deleted,
                "file_summary":    f"[오류] AI 리뷰 실패: {response.error}",
                "review_text":     f"[오류] AI 리뷰 실패: {response.error}",
                "inline_comments": [],
            })
            continue

        logger.debug("  AI 응답: %d chars (%.1fs)",
                     len(response.answer), response.elapsed_seconds or 0)

        # JSON 파싱 → (file_summary, inline_comments)
        file_summary, inline_comments = parse_inline_comments(response.answer, diff.filename)

        logger.info("  → 인라인 코멘트 %d개 (CRITICAL:%d MAJOR:%d MINOR:%d INFO:%d)",
                    len(inline_comments),
                    sum(1 for c in inline_comments if c["severity"] == "CRITICAL"),
                    sum(1 for c in inline_comments if c["severity"] == "MAJOR"),
                    sum(1 for c in inline_comments if c["severity"] == "MINOR"),
                    sum(1 for c in inline_comments if c["severity"] == "INFO"),
                    )

        file_reviews.append({
            "filename":        diff.filename,
            "change_type":     diff.change_type,
            "lines_ins":       diff.lines_inserted,
            "lines_del":       diff.lines_deleted,
            "file_summary":    file_summary,
            "review_text":     file_summary,   # formatter 호환용
            "inline_comments": inline_comments,
        })

    # ── 5b. 제외된 파일이 있으면 file_reviews 에 추가 (출력 파일용) ──────────
    if filter_result.total_skipped > 0:
        for fname in filter_result.skipped_files:
            reason = filter_result.skip_reasons.get(fname, "")
            file_reviews.append({
                "filename":        fname,
                "change_type":     "SKIPPED",
                "lines_ins":       0,
                "lines_del":       0,
                "file_summary":    f"[자동 리뷰 제외] {reason}",
                "review_text":     f"[자동 리뷰 제외] {reason}",
                "inline_comments": [],
                "skipped":         True,
            })
        logger.info(
            "  제외 파일 %d개를 결과에 포함 (출력용)",
            filter_result.total_skipped
        )

    # ── 5c. 전체 요약 생성 ───────────────────────────────────────────────────
    logger.info("  전체 요약 생성 중...")
    # 제외 파일이 있으면 요약 프롬프트에 안내 추가
    skip_note = ""
    if filter_result.total_skipped > 0:
        skip_note = (
            f"\n\n[참고] 다음 {filter_result.total_skipped}개 파일은 "
            f"자동 리뷰에서 제외되었습니다:\n"
            + "\n".join(
                f"  - {f} ({filter_result.skip_reasons.get(f, '')})"
                for f in filter_result.skipped_files
            )
        )
    summary_prompt = build_summary_prompt(
        [fr for fr in file_reviews if not fr.get("skipped")],
        change.subject, prompt_cfg
    )
    summary_resp   = ai.chat(summary_prompt)

    if not summary_resp.success:
        review_summary = "전체 요약 생성 실패: " + (summary_resp.error or "")
        overall_score  = 0
    else:
        review_summary = summary_resp.answer + (skip_note if skip_note else "")
        overall_score  = extract_score(summary_resp.answer)

    logger.info("  Code-Review 점수: %+d", overall_score)

    # ── 5c. Gerrit 인라인 코멘트 dict 구성 ──────────────────────────────────
    gerrit_comments = build_gerrit_comments(file_reviews)
    total_inline    = sum(len(v) for v in gerrit_comments.values())
    logger.info("  Gerrit 인라인 코멘트: 총 %d개 (파일 %d개)", total_inline, len(gerrit_comments))

    # ── 6. 결과 파일 저장 ────────────────────────────────────────────────────
    result = ReviewResult(
        change_number=change_number,
        patchset_number=patchset_number,
        project=change.project,
        branch=change.branch,
        subject=change.subject,
        owner=change.owner,
        ai_provider=provider,
        ai_model=ai.model,
        review_summary=review_summary,
        file_reviews=file_reviews,
        overall_score=overall_score,
        is_dry_run=dry_run,
        elapsed_seconds=time.time() - start,
    )

    formatter = ReviewFormatter(output_dir)
    saved     = formatter.save_all(result)
    for fmt, path in saved.items():
        logger.info("  저장: [%s] %s", fmt.upper(), path)

    # ── 7. Gerrit 리뷰 등록 ──────────────────────────────────────────────────
    if no_post:
        logger.info("[5/5] --no-post 옵션으로 Gerrit 등록을 건너뜁니다.")
    else:
        logger.info("[5/5] Gerrit 코드 리뷰 등록 중...")
        logger.info("  - 전체 요약 코멘트 + 인라인 코멘트 %d개", total_inline)

        label_cfg     = review_cfg.get("post_label", True)
        notify_policy = review_cfg.get("notify", "NONE")

        # 인라인 코멘트 미지원 버전: 파일별 리뷰 전체를 Change 코멘트에 포함
        if gerrit.caps.inline_comments:
            post_message = review_summary
            logger.info("  → 인라인 코멘트 모드 (Gerrit %s)", gerrit.caps.version_str)
        else:
            post_message = build_fallback_message(review_summary, file_reviews)
            logger.info(
                "  → 인라인 미지원(Gerrit %s): 파일별 리뷰를 Change 코멘트에 포함",
                gerrit.caps.version_str
            )

        review_input = ReviewInput(
            message  = post_message,
            labels   = {"Code-Review": overall_score} if label_cfg else {},
            comments = gerrit_comments,          # ← caps.inline_comments=False이면 _build_review_body가 제외
            tag      = "autogenerated:ai-reviewer",
            notify   = notify_policy,
        )
        posted = gerrit.post_review(change_number, patchset_number, review_input)
        result.gerrit_posted = posted
        if posted:
            logger.info("  ✅ Gerrit 리뷰 등록 완료")
        else:
            logger.error("  ❌ Gerrit 리뷰 등록 실패")

    elapsed = time.time() - start
    result.elapsed_seconds = elapsed
    logger.info("=" * 60)
    logger.info("AI 코드 리뷰 완료  (총 %.1f초)", elapsed)
    logger.info("=" * 60)
    return result


# ──────────────────────────────────────────────────────────────────────────────
# CLI 진입점
# ──────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Gerrit AI 자동 코드 리뷰 도구",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  # 기본 실행 (Gerrit에 실제 등록)
  python gerrit_reviewer.py --change 12345 --patchset 1

  # DRY-RUN: AI 호출 없이 테스트 응답으로 실행
  python gerrit_reviewer.py --change 12345 --patchset 1 --dry-run

  # NO-POST: AI 리뷰는 수행하지만 Gerrit에 등록 안함 (디버깅)
  python gerrit_reviewer.py --change 12345 --patchset 1 --no-post

  # 특정 AI 제공자/모델 지정
  python gerrit_reviewer.py --change 12345 --patchset 1 --provider gemini --model gemini-2.0-flash

  # verbose 로그 출력
  python gerrit_reviewer.py --change 12345 --patchset 1 --verbose
""",
    )
    p.add_argument("--change",    type=int, required=True,  help="Gerrit Change 번호")
    p.add_argument("--patchset",  type=int, required=True,  help="Patchset 번호")
    p.add_argument("--config",    type=str, default=None,   help="설정 디렉토리 경로 (기본: <project>/config)")
    p.add_argument("--output",    type=str, default=None,   help="결과 출력 디렉토리 (기본: <project>/output)")
    p.add_argument("--provider",  type=str, default=None,   help="AI 제공자 오버라이드 (claude/gemini/openai)")
    p.add_argument("--model",     type=str, default=None,   help="AI 모델 오버라이드")
    p.add_argument("--dry-run",   action="store_true",      help="AI/Gerrit 실제 호출 없이 테스트 모드로 실행")
    p.add_argument("--no-post",   action="store_true",      help="AI 리뷰 수행 후 Gerrit 등록은 건너뜀")
    p.add_argument("--verbose",   action="store_true",      help="DEBUG 레벨 로그 출력")
    return p


def main():
    args    = build_parser().parse_args()

    # 경로 결정
    project_dir = PROJECT_DIR
    config_dir  = Path(args.config)  if args.config  else project_dir / "config"
    output_dir  = Path(args.output)  if args.output  else project_dir / "output"
    log_dir     = project_dir / "logs"

    # 로깅 초기화
    logger = setup_logging(log_dir, verbose=args.verbose)

    try:
        cfg = load_config(config_dir)
    except FileNotFoundError as exc:
        logger.error("설정 파일 로드 실패: %s", exc)
        sys.exit(1)

    # output dir 오버라이드
    if args.output:
        cfg.setdefault("output", {})["dir"] = args.output

    try:
        result = run_review(
            change_number        = args.change,
            patchset_number      = args.patchset,
            cfg                  = cfg,
            project_dir          = project_dir,
            dry_run              = args.dry_run,
            no_post              = args.no_post,
            verbose              = args.verbose,
            ai_provider_override = args.provider,
            ai_model_override    = args.model,
        )
        if not result.success:
            logger.error("리뷰 실패: %s", result.error)
            sys.exit(2)

    except KeyboardInterrupt:
        logger.warning("사용자 중단 (Ctrl+C)")
        sys.exit(130)
    except Exception as exc:
        logger.exception("예상치 못한 오류: %s", exc)
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
