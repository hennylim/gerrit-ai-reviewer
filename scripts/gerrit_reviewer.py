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

class _SensitiveDataFilter(logging.Filter):
    """
    로그 메시지에서 민감 정보를 마스킹합니다.
    현재 마스킹 대상:
      - Google API 키  : AIza[0-9A-Za-z_-]{35}
      - 기타 api_key 파라미터 : api_key=xxxxx 형태
    """
    import re as _re
    _PATTERNS = [
        (_re.compile(r"AIza[0-9A-Za-z_\-]{35}"),      "AIza***MASKED***"),
        (_re.compile(r"(api[_-]?key['\"]?\s*[:=]\s*['\"]?)[0-9A-Za-z_\-]{20,}",
                     _re.IGNORECASE),                  r"\1***MASKED***"),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for pattern, replacement in self._PATTERNS:
            msg = pattern.sub(replacement, msg)
        # LogRecord 를 직접 수정하여 핸들러가 마스킹된 메시지를 출력하게 함
        record.msg  = msg
        record.args = ()
        return True


def setup_logging(log_dir: Path, verbose: bool = False) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file  = log_dir / f"reviewer_{ts}.log"

    level     = logging.DEBUG if verbose else logging.INFO
    fmt       = "[%(asctime)s] %(levelname)-8s %(name)s — %(message)s"
    datefmt   = "%Y-%m-%d %H:%M:%S"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)   # 핸들러가 필터링

    sensitive_filter = _SensitiveDataFilter()

    # 파일 핸들러: DEBUG 이상 모두 기록
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(fmt, datefmt))
    fh.addFilter(sensitive_filter)
    root.addHandler(fh)

    # 콘솔 핸들러: verbose 여부에 따라 레벨 조정
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(level)
    ch.setFormatter(logging.Formatter(fmt, datefmt))
    ch.addFilter(sensitive_filter)
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


def _extract_json_text(raw: str) -> str:
    """
    AI 응답에서 JSON 텍스트를 추출합니다.
    마크다운 코드블록(```json...```) 제거, { ~ } 범위 추출을 수행합니다.
    """
    import re
    text = raw.strip()

    # 마크다운 코드블록 제거 (닫힌 경우)
    md_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if md_match:
        return md_match.group(1)

    # 마크다운 코드블록 열렸지만 닫히지 않은 경우
    md_open = re.search(r"```(?:json)?\s*(\{.*)", text, re.DOTALL)
    if md_open:
        text = md_open.group(1)

    # 첫 { ~ 마지막 } 추출
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        return brace_match.group(0)

    return text


def _repair_json(text: str) -> str:
    """
    잘린 JSON을 복구합니다.
    닫히지 않은 문자열, 배열, 객체를 닫아 파싱 가능하게 만듭니다.
    """
    # 열린 따옴표 수 확인: 홀수면 닫는 따옴표 추가
    in_string = False
    escaped   = False
    result    = []
    open_braces   = 0
    open_brackets = 0

    for ch in text:
        if escaped:
            escaped = False
            result.append(ch)
            continue
        if ch == "\\" and in_string:
            escaped = True
            result.append(ch)
            continue
        if ch == '"':
            in_string = not in_string
        elif not in_string:
            if ch == "{":
                open_braces += 1
            elif ch == "}":
                open_braces -= 1
            elif ch == "[":
                open_brackets += 1
            elif ch == "]":
                open_brackets -= 1
        result.append(ch)

    repaired = "".join(result)

    # 열린 문자열 닫기
    if in_string:
        repaired += '"'

    # 열린 배열/객체 닫기 (역순)
    repaired += "]" * max(0, open_brackets)
    repaired += "}" * max(0, open_braces)

    return repaired


def _extract_summary_from_partial(text: str) -> str:
    """
    JSON 파싱이 완전히 실패한 경우, 정규식으로 file_summary 값만 추출합니다.
    """
    import re
    # "file_summary": "..." 패턴
    match = re.search(
        r'"file_summary"\s*:\s*"((?:[^"\\]|\\.)*)"',
        text, re.DOTALL
    )
    if match:
        # JSON 이스케이프 디코딩
        try:
            import json as _j
            return _j.loads(f'"{match.group(1)}"')
        except Exception:
            return match.group(1)
    return ""


def _build_result(data: dict, filename: str) -> tuple[str, list[dict]]:
    """파싱된 JSON dict에서 (file_summary, inline_comments)를 구성합니다."""
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

            #icon    = {"CRITICAL": "🔴", "MAJOR": "🟠", "MINOR": "🟡", "INFO": "🔵"}.get(severity, "⚪")
            #cat_str = f"[{category}] " if category else ""
            #formatted_msg = f"{icon} [{severity}] {cat_str}{message}" 

            inline_comments.append({
                "line":     line,
                "side":     side,
                "severity": severity,
                "message":  message,
            })
        except (TypeError, ValueError) as exc:
            logger.debug("인라인 코멘트 항목 파싱 오류: %s — %s", c, exc)

    logger.debug("  파일 '%s': 인라인 코멘트 %d개 파싱 완료", filename, len(inline_comments))
    return file_summary, inline_comments




def parse_inline_comments(ai_response: str, filename: str) -> tuple[str, list[dict]]:
    """
    AI JSON 응답을 파싱해 (file_summary, inline_comments) 를 반환합니다.

    파싱 전략 (순서대로 시도):
      1. 정상 JSON 파싱
      2. 마크다운 코드블록 / 앞뒤 잡음 제거 후 재파싱
      3. 잘린 JSON 복구(repair) 후 재파싱
      4. 정규식으로 file_summary 값만 추출
      5. 위 모두 실패 시 → 원본 텍스트 대신 "[파싱 실패]" 오류 메시지 반환

    Returns:
        file_summary   : 파일 전체 요약 텍스트 (절대 JSON 코드블록이 포함되지 않음)
        inline_comments: [{"line": N, "side": "RIGHT", "severity": "...", "message": "..."}]
    """
    import re
    import json as _json

    def _try_parse(s: str):
        return _json.loads(s)

    # ── 1단계: 원본 그대로 파싱 ─────────────────────────────────────────────
    try:
        data = _try_parse(ai_response.strip())
        return _build_result(data, filename)
    except (_json.JSONDecodeError, ValueError):
        pass

    # ── 2단계: JSON 텍스트 추출 후 파싱 ─────────────────────────────────────
    json_text = _extract_json_text(ai_response)
    try:
        data = _try_parse(json_text)
        return _build_result(data, filename)
    except (_json.JSONDecodeError, ValueError) as exc:
        logger.debug("2단계 파싱 실패 (%s): %s", filename, exc)

    # ── 3단계: JSON 복구 후 파싱 ─────────────────────────────────────────────
    repaired = _repair_json(json_text)
    try:
        data = _try_parse(repaired)
        logger.info("  JSON 복구 성공: %s", filename)
        return _build_result(data, filename)
    except (_json.JSONDecodeError, ValueError) as exc:
        logger.debug("3단계 복구 파싱 실패 (%s): %s", filename, exc)

    # ── 4단계: 정규식으로 file_summary 만 추출 ──────────────────────────────
    summary = _extract_summary_from_partial(json_text) or _extract_summary_from_partial(ai_response)
    if summary:
        logger.warning(
            "파일 '%s' JSON 파싱 실패 — file_summary 부분 추출 성공", filename
        )
        logger.debug("원본 응답(앞 300자):\n%s", ai_response[:300])
        return summary, []

    # ── 5단계: 완전 실패 ─────────────────────────────────────────────────────
    logger.warning(
        "파일 '%s' AI 응답 파싱 완전 실패 — 오류 메시지로 대체합니다.", filename
    )
    logger.debug("원본 응답(앞 500자):\n%s", ai_response[:500])
    fallback_summary = (
        f"[AI 응답 파싱 실패]\n"
        f"AI가 유효한 JSON 형식으로 응답하지 않았습니다.\n"
        f"파일: {filename}\n"
        f"원본 응답 일부: {ai_response[:200].strip()}"
    )
    # 원본 응답에 JSON이 없는 경우 일반 텍스트를 그대로 요약으로 활용
    cleaned = ai_response.strip()
    if not cleaned.startswith("{") and "```" not in cleaned and len(cleaned) < 1000:
        return cleaned, []
    return fallback_summary, []


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
# 배치 처리 (여러 파일을 하나의 AI 요청으로 통합)
# ──────────────────────────────────────────────────────────────────────────────

# AI 모델별 안전 토큰 한도 (문자 수 기준 근사값, 실제 토큰 < 문자 수)
# 1 토큰 ≈ 3~4 문자 (한글 포함 평균)로 보수적으로 설정
_MODEL_CHAR_LIMITS: dict[str, int] = {
    # Gemini
    "gemini-2.5-pro":              600_000,
    "gemini-2.5-flash":            600_000,
    "gemini-3.1-pro-preview":      600_000,
    "gemini-2.0-flash":            400_000,
    "gemini-1.5-pro":              400_000,
    "gemini-1.5-flash":            400_000,
    # Claude
    "claude-opus-4-6":             600_000,
    "claude-sonnet-4-6":           600_000,
    "claude-opus-4-5":             600_000,
    "claude-haiku-4-5-20251001":   300_000,
    "claude-sonnet-3-7-20250219":  600_000,
    "claude-sonnet-3-5-20241022":  600_000,
    # OpenAI
    "gpt-4o":                      400_000,
    "gpt-4o-mini":                 400_000,
    "gpt-4-turbo":                 400_000,
}
_DEFAULT_CHAR_LIMIT = 200_000   # 모델 미등록 시 보수적 기본값
_BATCH_SIZE         = 10        # 한 배치당 최대 파일 수


def _get_char_limit(model_name: str) -> int:
    """모델명으로 문자 수 한도를 조회합니다. 부분 매칭 지원."""
    if not model_name:
        return _DEFAULT_CHAR_LIMIT
    name_lower = model_name.lower()
    # 정확히 일치
    if name_lower in _MODEL_CHAR_LIMITS:
        return _MODEL_CHAR_LIMITS[name_lower]
    # 부분 일치 (예: "gemini-3.1-pro-preview-0325" → "gemini-3.1-pro-preview")
    for key, limit in _MODEL_CHAR_LIMITS.items():
        if key in name_lower or name_lower.startswith(key.split("-")[0]):
            return limit
    return _DEFAULT_CHAR_LIMIT


def split_into_batches(
    diffs:      list,
    model_name: str,
    batch_size: int = _BATCH_SIZE,
) -> list[list]:
    """
    diff 목록을 배치로 분할합니다.

    분할 기준 (둘 중 하나라도 초과 시 새 배치 시작):
      1. 배치당 파일 수 > batch_size (기본 10)
      2. 배치 누적 문자 수 > 모델 한도의 70% (안전 마진 30%)

    Returns:
        [[diff1, diff2, ...], [diff11, ...], ...]
    """
    char_limit    = int(_get_char_limit(model_name) * 0.70)
    batches:  list[list]  = []
    current:  list        = []
    cur_chars: int        = 0

    for diff in diffs:
        diff_chars = len(diff.diff_content) + len(diff.filename) + 200  # 헤더 오버헤드 포함

        # 현재 배치가 있고 한도 초과 예상 → 새 배치
        if current and (
            len(current) >= batch_size
            or cur_chars + diff_chars > char_limit
        ):
            batches.append(current)
            current   = []
            cur_chars = 0

        current.append(diff)
        cur_chars += diff_chars

    if current:
        batches.append(current)

    return batches


def build_batch_review_prompt(
    subject:    str,
    project:    str,
    branch:     str,
    diffs:      list,
    prompt_cfg: dict,
) -> str:
    """
    여러 파일의 diff를 하나의 프롬프트로 통합하여 AI에게 배치 리뷰를 요청합니다.

    응답 형식: 파일 수와 동일한 길이의 JSON 배열
    [
      {"filename": "...", "file_summary": "...", "inline_comments": [...]},
      {"filename": "...", "file_summary": "...", "inline_comments": [...]},
      ...
    ]
    """
    focus_areas = prompt_cfg.get("focus_areas", [])
    language    = prompt_cfg.get("language", "Korean")

    focus_str = ""
    if focus_areas:
        focus_str = "중점 리뷰 영역:\n" + "\n".join(f"  - {a}" for a in focus_areas)

    # 파일별 diff 블록 조합
    file_blocks = []
    for i, diff in enumerate(diffs, 1):
        added_lines = _extract_added_lines(diff.diff_content)
        line_hint   = ""
        if added_lines:
            line_hint = f"  추가된 라인 번호 (RIGHT): {added_lines[:30]}"

        block = (
            f"=== FILE {i}/{len(diffs)}: {diff.filename} ==="
            f"  (변경 유형: {diff.change_type}, +{diff.lines_inserted}/-{diff.lines_deleted})\n"
            f"{line_hint}\n"
            f"--- DIFF START ---\n"
            f"{diff.diff_content}\n"
            f"--- DIFF END ---"
        )
        file_blocks.append(block)

    files_section = "\n\n".join(file_blocks)

    # 파일별 응답 스키마 예시
    schema_example = "[\n" + ",\n".join(
        f'  {{"filename": "{d.filename}", "file_summary": "<요약>", "inline_comments": [...]}}'
        for d in diffs
    ) + "\n]"

    return f"""당신은 10년 이상 경력의 시니어 소프트웨어 엔지니어입니다.
아래 {len(diffs)}개 파일의 코드 diff를 한 번에 리뷰하고, 반드시 JSON 배열 형식으로만 응답하세요.
JSON 배열 외 다른 텍스트(설명, 마크다운 코드블록 등)는 절대 포함하지 마세요.

프로젝트: {project}  브랜치: {branch}
변경 제목: {subject}
{focus_str}

응답 JSON 배열 스키마 (파일 순서 유지, 총 {len(diffs)}개 원소):
{schema_example}

각 원소의 inline_comments 구조:
{{
  "line": <라인 번호(정수)>,
  "side": "RIGHT",
  "severity": "<CRITICAL|MAJOR|MINOR|INFO>",
  "category": "<Security|Bug|Performance|Style|Test|Design>",
  "message": "<{language}로 작성. 문제 설명 + 수정 방법. 300자 이내>"
}}

규칙:
- 응답은 반드시 [{len(diffs)}개 원소 JSON 배열]로만, 다른 텍스트 없이
- filename 은 위 FILE 헤더의 파일명 그대로 사용
- inline_comments 는 실제 문제 있는 라인에만 작성 (없으면 빈 배열 [])
- line 은 diff 에서 + 로 시작하는 줄의 실제 파일 라인 번호
- 심각도: CRITICAL=보안/데이터손실, MAJOR=버그/성능, MINOR=코드품질, INFO=개선제안
- 언어: {language}

{files_section}"""


# 배치 응답이 잘렸다고 판단하는 최소 문자 수 기준 (파일당 50자 미만이면 의심)
_MIN_CHARS_PER_FILE = 50


def _parse_json_lines(text: str) -> list | None:
    """
    JSON Lines 형식(한 줄에 객체 하나) 파싱을 시도합니다.
    Gemini가 배열 대신 여러 JSON 객체를 연속으로 반환할 때 대응합니다.
    """
    import json as _j
    objects = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line in ("{", "}", "[", "]"):
            continue
        line = line.rstrip(",")
        try:
            obj = _j.loads(line)
            if isinstance(obj, dict):
                objects.append(obj)
        except _j.JSONDecodeError:
            pass
    return objects if objects else None


def is_truncated_response(ai_response: str, n_files: int) -> bool:
    """
    AI 응답이 잘렸는지 휴리스틱으로 판단합니다.

    잘린 응답 판단 기준:
    - 파일당 평균 문자 수 < _MIN_CHARS_PER_FILE (너무 짧음)
    - 응답이 JSON 시작 문자([ { `)로 시작하지 않음
      (한글/영문 텍스트로 시작 = 응답 도중에 잘린 조각)

    정상 응답 형식:
    - [ ... ]  JSON 배열
    - { ... }  단일 JSON 객체 (배열로 감싸 처리)
    - [{...}\n{...}]  JSON Lines (각 줄이 { 로 시작)
    - ```json [...]  마크다운 코드블록
    """
    avg = len(ai_response) / max(n_files, 1)
    if avg < _MIN_CHARS_PER_FILE:
        return True
    text = ai_response.strip()
    # JSON 시작 문자 확인: [, {, ` (마크다운)
    first_char = text[0] if text else ""
    if first_char not in ("[", "{", "`"):
        return True
    return False


def parse_batch_response(
    ai_response: str,
    diffs:       list,
) -> list[tuple[str, list]] | None:
    """
    배치 AI 응답(JSON 배열)을 파싱하여 파일별 (file_summary, inline_comments) 반환.

    파싱 전략 (순서대로 시도):
      1. JSON 배열 직접 파싱
      2. 마크다운 코드블록 제거 후 파싱
      3. 닫힌 괄호 보정(_repair_json) 후 파싱
      4. JSON Lines 형식 파싱 (객체 여러 개를 개별 파싱)
      5. 완전 실패 → None 반환 (호출자가 개별 API 재호출)

    Returns:
        [(file_summary, inline_comments), ...] 성공
        None                                   파싱 실패 (개별 재호출 필요)
    """
    import json as _json
    import re

    n = len(diffs)

    # ── 잘린 응답 조기 감지 ────────────────────────────────────────────────
    if is_truncated_response(ai_response, n):
        logger.warning(
            "배치 응답이 잘렸거나 형식 불량 (%d자, 파일 %d개) → 개별 재호출 필요",
            len(ai_response), n,
        )
        return None

    def _build_results_from_list(data: list) -> list[tuple[str, list]]:
        """파싱된 JSON 배열에서 파일별 결과 추출"""
        results  = []
        by_order = len(data) == n
        if not by_order:
            logger.warning(
                "배치 응답 원소 수(%d) ≠ 요청 파일 수(%d) — filename 기반 매핑",
                len(data), n,
            )
        for idx, diff in enumerate(diffs):
            item = None
            if by_order and idx < len(data):
                item = data[idx]
            else:
                for d in data:
                    if isinstance(d, dict) and d.get("filename", "") == diff.filename:
                        item = d
                        break
            if item and isinstance(item, dict):
                try:
                    results.append(_build_result(item, diff.filename))
                    continue
                except Exception as exc:
                    logger.debug("배치 항목 파싱 오류 (%s): %s", diff.filename, exc)
            logger.debug("배치에서 '%s' 미발견 → 빈 결과", diff.filename)
            results.append((f"[배치 파싱 실패] {diff.filename}", []))
        return results

    # ── 텍스트 전처리 ─────────────────────────────────────────────────────
    text = ai_response.strip()

    # 마크다운 코드블록 배열
    md_match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if md_match:
        text = md_match.group(1)
    else:
        arr_match = re.search(r"\[.*\]", text, re.DOTALL)
        if arr_match:
            text = arr_match.group(0)

    # ── 1단계: 직접 파싱 ────────────────────────────────────────────────
    try:
        data = _json.loads(text)
        if isinstance(data, list):
            return _build_results_from_list(data)
        if isinstance(data, dict):
            return _build_results_from_list([data])
    except (_json.JSONDecodeError, ValueError):
        pass

    # ── 2단계: 닫힌 괄호 보정 후 재파싱 ────────────────────────────────
    try:
        data = _json.loads(_repair_json(text))
        if isinstance(data, list):
            logger.info("배치 응답 JSON 복구 성공 (%d개 원소)", len(data))
            return _build_results_from_list(data)
        if isinstance(data, dict):
            return _build_results_from_list([data])
    except (_json.JSONDecodeError, ValueError) as exc:
        logger.debug("배치 JSON 복구 파싱 실패: %s", exc)

    # ── 3단계: JSON Lines 파싱 ─────────────────────────────────────────
    objects = _parse_json_lines(ai_response)
    if objects:
        logger.info("배치 응답 JSON Lines 형식으로 파싱 성공 (%d개)", len(objects))
        return _build_results_from_list(objects)

    # ── 완전 실패 → None (호출자가 개별 재호출) ──────────────────────────
    logger.warning(
        "배치 응답 파싱 완전 실패 (%d자) → 개별 파일 API 재호출 필요",
        len(ai_response),
    )
    logger.debug("배치 응답 앞 300자:\n%s", ai_response[:300])
    return None



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
    force:           bool = False,   # 중복 리뷰 방지 체크를 무시하고 강제 실행
    ai_provider_override: str = None,
    ai_model_override:    str = None,
    gerrit_client=None,   # 외부에서 주입된 공유 GerritClient (None이면 내부 생성)
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
    logger.info("  DRY-RUN  : %s  NO-POST: %s  FORCE: %s", dry_run, no_post, force)
    logger.info("  Provider : %s  Model: %s", provider, model or "default")
    logger.info("=" * 60)

    # ── 1. Gerrit 클라이언트 초기화 ──────────────────────────────────────────
    if gerrit_client is not None:
        gerrit = gerrit_client
        # 외부 주입 클라이언트는 dry_run 상태가 다를 수 있으므로 확인
        if gerrit.dry_run != dry_run:
            logger.debug("주입된 GerritClient dry_run=%s, 요청 dry_run=%s (주입값 사용)", gerrit.dry_run, dry_run)
    else:
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

    # ── 1b. 중복 리뷰 방지 ──────────────────────────────────────────────────
    # dry_run 모드에서만 중복 체크 생략 (Gerrit 호출 자체를 하지 않으므로)
    # no_post 모드에서는 Gerrit 등록을 건너뛸 뿐이므로, 중복 체크는 정상 수행
    # force=True 이면 중복 체크를 건너뛰고 강제로 리뷰 실행
    if not dry_run and not force and review_cfg.get("skip_if_already_reviewed", True):
        if gerrit.has_ai_review(change_number, patchset_number):
            logger.info(
                "중복 리뷰 건너뜀: change=#%d ps=%d — 이미 AI 리뷰 등록됨",
                change_number, patchset_number,
            )
            return ReviewResult(
                change_number   = change_number,
                patchset_number = patchset_number,
                project="", branch="", subject="", owner="",
                ai_provider     = provider,
                ai_model        = model or "skip",
                review_summary  = "[중복 리뷰 건너뜀] 이미 이 Patchset에 AI 리뷰가 등록되어 있습니다.",
                file_reviews    = [],
                overall_score   = 0,
                is_dry_run      = False,
                elapsed_seconds = time.time() - start,
            )
    elif force and not dry_run:
        logger.info(
            "--force 옵션: 중복 리뷰 체크 생략, 강제 실행 (change=#%d ps=%d)",
            change_number, patchset_number,
        )

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

    # ── 5. 배치 AI 코드 리뷰 ──────────────────────────────────────────────────
    # 최대 10개 파일씩 하나의 AI 요청으로 통합하여 속도 최적화
    # 모델 컨텍스트 한도 초과 시 자동 분할
    batch_size = int(review_cfg.get("batch_size", _BATCH_SIZE))
    batches = split_into_batches(diffs, ai.model, batch_size=batch_size)
    logger.info(
        "[4/5] 배치 AI 코드 리뷰 실행 중... (파일 %d개 → 배치 %d개, 모델 한도 %s chars)",
        len(diffs), len(batches),
        f"{_get_char_limit(ai.model):,}",
    )
    for b_idx, b_info in enumerate(
        (f"배치 {i+1}/{len(batches)}: {len(b)}개 파일 [{', '.join(d.filename.split('/')[-1] for d in b[:3])}{'...' if len(b)>3 else ''}]"
         for i, b in enumerate(batches)),
        1
    ):
        pass  # 미리 생성 (로그용)

    file_reviews = []
    processed = 0
    ai_all_failed = True   # AI 호출이 단 하나라도 성공하면 False 로 전환

    def _review_single(diff):
        single_prompt = build_inline_review_prompt(
            subject    = change.subject,
            project    = change.project,
            branch     = change.branch,
            diff       = diff,
            prompt_cfg = prompt_cfg,
        )
        single_resp = ai.chat(single_prompt)

        if not single_resp.success:
            logger.warning("    개별 호출 실패 (%s): %s", diff.filename, single_resp.error)
            return diff, f"[오류] AI 개별 리뷰 실패: {single_resp.error}", [], True  # failed=True

        file_summary, inline_comments = parse_inline_comments(single_resp.answer, diff.filename)
        logger.debug(
            "    개별 응답: %d chars (%.1fs) → 코멘트 %d개",
            len(single_resp.answer), single_resp.elapsed_seconds or 0, len(inline_comments),
        )
        return diff, file_summary, inline_comments, False  # failed=False

    def _review_batch(batch, level=0):
        if not batch:
            return []

        indent = "  " * level
        batch_files = ", ".join(d.filename.split("/")[-1] for d in batch[:3])
        if len(batch) > 3:
            batch_files += f" 외 {len(batch)-3}개"
        logger.info(
            "%s[배치 재시도 레벨 %d] %d개 파일 리뷰: %s",
            indent, level, len(batch), batch_files,
        )

        prompt = build_batch_review_prompt(
            subject    = change.subject,
            project    = change.project,
            branch     = change.branch,
            diffs      = batch,
            prompt_cfg = prompt_cfg,
        )
        response = ai.chat(prompt)

        if not response.success:
            logger.warning("%s배치 %d개 AI 응답 실패: %s", indent, len(batch), response.error)
            if len(batch) == 1:
                diff = batch[0]
                return [ _review_single(diff) ]

            # 실패 시 개별 폴백
            results = []
            for diff in batch:
                results.append(_review_single(diff))
            return results

        logger.debug(
            "%s배치 응답: %d chars (%.1fs)",
            indent, len(response.answer), response.elapsed_seconds or 0,
        )

        batch_results = parse_batch_response(response.answer, batch)

        if batch_results is None:
            if len(batch) > 1:
                logger.warning(
                    "%s배치 응답 파싱 실패 또는 잘림 감지 (%d개) — 반으로 분할 재시도",
                    indent, len(batch),
                )
                mid = len(batch) // 2
                left  = _review_batch(batch[:mid], level=level+1)
                right = _review_batch(batch[mid:], level=level+1)
                return left + right

            diff = batch[0]
            logger.warning("%s단일 파일 재시도도 실패 — 개별 호출로 폴백: %s", indent, diff.filename)
            return [ _review_single(diff) ]

        # 배치 성공: failed=False 로 튜플 구성
        return [ (diff, file_summary, inline_comments, False)
                 for diff, (file_summary, inline_comments) in zip(batch, batch_results) ]

    for b_idx, batch in enumerate(batches, 1):
        ### 배치 처리, 재시도 로직 _review_batch로 통합 ###
        batch_items = _review_batch(batch)

        for diff, file_summary, inline_comments, ai_failed in batch_items:
            processed += 1
            if not ai_failed:
                ai_all_failed = False   # 하나라도 성공
            if not inline_comments:
                logger.info(
                    "    [%d/%d] %s → 인라인 코멘트 없음, 결과에서 제외",
                    processed, len(diffs), diff.filename,
                )
                continue

            sev_counts = {s: sum(1 for c in inline_comments if c["severity"] == s)
                          for s in ("CRITICAL", "MAJOR", "MINOR", "INFO")}
            logger.info(
                "    [%d/%d] %s  → 코멘트 %d개 (C:%d M:%d m:%d I:%d)",
                processed, len(diffs), diff.filename,
                len(inline_comments),
                sev_counts["CRITICAL"], sev_counts["MAJOR"],
                sev_counts["MINOR"], sev_counts["INFO"],
            )

            file_reviews.append({
                "filename":        diff.filename,
                "change_type":     diff.change_type,
                "lines_ins":       diff.lines_inserted,
                "lines_del":       diff.lines_deleted,
                "file_summary":    file_summary,
                "review_text":     file_summary,
                "inline_comments": inline_comments,
            })

    total_comments = sum(len(fr["inline_comments"]) for fr in file_reviews)
    logger.info(
        "  배치 리뷰 완료: 파일 %d개, 인라인 코멘트 총 %d개 (%d회 API 호출)",
        len(file_reviews), total_comments, len(batches),
    )

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

    reviewed_files = [fr for fr in file_reviews if not fr.get("skipped")]

    if not reviewed_files:
        logger.info("  인라인 코멘트가 있는 파일이 없어 전체 요약을 생성하지 않습니다.")
        review_summary = "[AI 리뷰] 리뷰 결과 특이사항이 없어 전체 요약을 생략합니다."
        if skip_note:
            review_summary += skip_note
        overall_score = 0
    else:
        summary_prompt = build_summary_prompt(reviewed_files, change.subject, prompt_cfg)
        summary_resp = ai.chat(summary_prompt)

        if not summary_resp.success:
            review_summary = "전체 요약 생성 실패: " + (summary_resp.error or "")
            overall_score = 0
        else:
            review_summary = summary_resp.answer + (skip_note if skip_note else "")
            overall_score = extract_score(summary_resp.answer)

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
    elif ai_all_failed:
        # AI 호출이 전부 실패한 경우 Gerrit에 등록하지 않음
        # → 빈 리뷰가 기록되면 중복 방지 로직이 오판하여 다음 실행에서도 스킵됨
        logger.warning(
            "[5/5] AI 호출 전체 실패 — Gerrit 등록 생략 (다음 실행에서 재시도됩니다.)"
        )
        result.error = "AI 호출 전체 실패로 Gerrit 등록 생략"
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

  # FORCE: 이미 리뷰된 Patchset도 강제로 다시 리뷰
  python gerrit_reviewer.py --change 12345 --patchset 1 --force

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
    p.add_argument("--force",     action="store_true",      help="중복 리뷰 방지 체크를 무시하고 강제로 리뷰 실행")
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
            force                = args.force,
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
