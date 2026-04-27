"""
gerrit_client.py
----------------
Gerrit REST API 클라이언트.

버전 자동 감지 → 기능(Capability) 레지스트리 조회 →
해당 버전에 맞는 최적 페이로드를 처음부터 올바르게 구성합니다.

새 Gerrit 버전 지원 추가 방법:
    GERRIT_VERSION_REGISTRY 에 한 줄만 추가하면 됩니다.

Gerrit REST API 문서:
    https://gerrit-review.googlesource.com/Documentation/rest-api-changes.html
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote

import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

logger = logging.getLogger(__name__)


# ==============================================================================
# Gerrit 버전별 기능(Capability) 레지스트리
# ==============================================================================
#
# ★ 새 Gerrit 버전 추가 시 이 리스트에 한 줄만 추가하세요.
#
# 형식: (최소 버전 튜플, 기능 딕셔너리)
# - 내림차순 정렬 필수 (높은 버전이 앞에)
# - 감지된 버전 >= 최소 버전인 첫 번째 항목 선택
#
# 기능 키:
#   tag             : SetReviewInput.tag 필드 (2.15+)
#   unresolved      : CommentInput.unresolved 필드 (3.0+)
#   inline_comments : SetReviewInput.comments 필드 — Gerrit 2.1+ 부터 지원
#                     (2.14+ 에서 추가된 것은 robot_comments 이며 inline_comments 가 아님)
#   comment_side    : CommentInput.side 필드 "REVISION"/"PARENT" (2.15+)
#                     2.13/2.14 에 포함하면 400 반환 → 반드시 제거
#   notify_none     : notify=NONE 값 (2.15+). False이면 notify 필드 생략
#   robot_comments  : robot_comments 필드 (2.14+)
#
GERRIT_VERSION_REGISTRY: list[tuple[tuple, dict]] = [
    # ★ 새 버전 추가 시 내림차순으로 한 줄 삽입
    #
    # 기능 키:
    #   labels          : SetReviewInput.labels 필드 (Code-Review 점수) 안정 지원
    #   tag             : SetReviewInput.tag 필드 (2.15+)
    #   unresolved      : CommentInput.unresolved 필드 (3.0+)
    #   inline_comments : SetReviewInput.comments 필드 (2.1+ 부터 지원)
    #   comment_side    : CommentInput.side 필드 (2.15+) — 2.13/2.14 미지원
    #   notify_none     : notify=NONE 값 (2.15+)
    #   robot_comments  : robot_comments 필드 (2.14+)
    #
    # ── Gerrit 3.x ──────────────────────────────────────────────────────────
    ((3, 9), dict(labels=True, tag=True,  unresolved=True,  inline_comments=True,  comment_side=True,  notify_none=True,  robot_comments=True)),
    ((3, 5), dict(labels=True, tag=True,  unresolved=True,  inline_comments=True,  comment_side=True,  notify_none=True,  robot_comments=True)),
    ((3, 3), dict(labels=True, tag=True,  unresolved=True,  inline_comments=True,  comment_side=True,  notify_none=True,  robot_comments=True)),
    ((3, 0), dict(labels=True, tag=True,  unresolved=True,  inline_comments=True,  comment_side=True,  notify_none=True,  robot_comments=True)),
    # ── Gerrit 2.x ──────────────────────────────────────────────────────────
    ((2, 16), dict(labels=True, tag=True,  unresolved=False, inline_comments=True,  comment_side=True,  notify_none=True,  robot_comments=True)),
    ((2, 15), dict(labels=True, tag=True,  unresolved=False, inline_comments=True,  comment_side=True,  notify_none=True,  robot_comments=False)),
    ((2, 14), dict(labels=True, tag=False, unresolved=False, inline_comments=True,  comment_side=False, notify_none=False, robot_comments=False)),
    ((2, 13), dict(labels=True, tag=False, unresolved=False, inline_comments=True,  comment_side=False, notify_none=False, robot_comments=False)),
    # ── 2.12 이하 / 버전 감지 실패 → 최소 기능 (message 만) ─────────────────
    ((0,  0), dict(labels=False, tag=False, unresolved=False, inline_comments=False, comment_side=False, notify_none=False, robot_comments=False)),
]


# ==============================================================================
# 데이터 클래스
# ==============================================================================

@dataclass
class GerritCapabilities:
    """감지된 Gerrit 서버의 기능 집합"""
    version_str:     str   = "unknown"
    version_tuple:   tuple = (0, 0)
    labels:          bool  = False   # Code-Review 점수 레이블 안정 지원
    tag:             bool  = False
    unresolved:      bool  = False
    inline_comments: bool  = False
    comment_side:    bool  = False   # CommentInput.side 필드 (2.15+) — 2.13/2.14 미지원
    notify_none:     bool  = False
    robot_comments:  bool  = False

    def summary(self) -> str:
        feats = [k for k in ("labels", "tag", "unresolved", "inline_comments",
                              "comment_side", "notify_none", "robot_comments") if getattr(self, k)]
        return (
            f"Gerrit {self.version_str} "
            f"(지원 기능: {', '.join(feats) if feats else '최소'})"
        )


@dataclass
class GerritChange:
    """Gerrit 변경사항 정보"""
    change_id:       str
    project:         str
    branch:          str
    subject:         str
    status:          str
    change_number:   int
    patchset_number: int
    owner:           str = ""
    created:         str = ""
    updated:         str = ""


@dataclass
class FileDiff:
    """파일별 diff 정보"""
    filename:       str
    old_path:       str = ""
    change_type:    str = "MODIFIED"
    lines_inserted: int = 0
    lines_deleted:  int = 0
    diff_content:    str  = ""
    # ── 라인 번호 정확성 보조 필드 ─────────────────────────────────────────
    # valid_new_lines : RIGHT(REVISION) side 코멘트 가능한 실제 파일 라인 번호 목록
    # annotated_diff  : 실제 파일 라인 번호가 주석으로 달린 diff (AI 프롬프트용)
    valid_new_lines: list = field(default_factory=list)
    annotated_diff:  str  = ""


@dataclass
class ReviewInput:
    """Gerrit 코드 리뷰 등록 입력"""
    message:  str  = ""
    labels:   dict = field(default_factory=dict)
    comments: dict = field(default_factory=dict)
    tag:      str  = "autogenerated:ai-reviewer"
    notify:   str  = "NONE"


# ==============================================================================
# 버전 유틸리티
# ==============================================================================

def parse_version(version_str: str) -> tuple:
    """
    버전 문자열 → 정수 튜플 변환

    "3.5.0.1"  → (3, 5, 0, 1)
    "2.16.28"  → (2, 16, 28)
    "3.9-rc1"  → (3, 9)
    ""         → (0, 0)
    """
    parts = re.findall(r"\d+", str(version_str))
    return tuple(int(p) for p in parts[:4]) if parts else (0, 0)


def detect_capabilities(version_tuple: tuple, version_str: str) -> GerritCapabilities:
    """
    버전 튜플을 GERRIT_VERSION_REGISTRY 와 대조해 GerritCapabilities 반환.
    레지스트리는 내림차순이므로 첫 번째 매칭이 가장 근접한 상위 버전.
    """
    for min_ver, feats in GERRIT_VERSION_REGISTRY:
        if version_tuple >= min_ver:
            caps = GerritCapabilities(
                version_str=version_str,
                version_tuple=version_tuple,
                **feats,
            )
            logger.info("Gerrit 버전 감지: %s", caps.summary())
            return caps
    # (0,0) 항목이 항상 매칭되므로 여기는 도달 불가
    return GerritCapabilities(version_str=version_str, version_tuple=version_tuple)


# ==============================================================================
# Gerrit REST API 클라이언트
# ==============================================================================

class GerritClient:
    """
    Gerrit REST API 클라이언트.

    초기화 시 버전을 자동 감지하여 GerritCapabilities 에 저장.
    이후 모든 API 호출은 capabilities 기반으로 올바른 페이로드를 구성.
    폴백(fallback) 없이 처음부터 정확한 페이로드를 전송합니다.

    새 버전 대응:
        GERRIT_VERSION_REGISTRY 에 항목 추가만으로 완료됩니다.
    수동 버전 지정:
        reviewer_config.json > gerrit.version 으로 오버라이드 가능.
    """

    MAGIC_PREFIX = ")]}'\n"

    def __init__(
        self,
        base_url:    str,
        username:    str,
        password:    str,
        auth_type:   str  = "basic",
        timeout:     int  = 30,
        verify_ssl:  bool = True,
        dry_run:     bool = False,
        version:     str  = "",     # 수동 버전 지정 (비어 있으면 자동 감지)
    ):
        self.base_url   = base_url.rstrip("/")
        self.username   = username
        self.timeout    = timeout
        self.dry_run    = dry_run
        self.verify_ssl = verify_ssl

        self.auth = (
            HTTPDigestAuth(username, password)
            if auth_type == "digest"
            else HTTPBasicAuth(username, password)
        )

        self.session = requests.Session()
        self.session.auth   = self.auth
        self.session.verify = self.verify_ssl
        self.session.headers.update({
            "Accept":       "application/json",
            "Content-Type": "application/json;charset=UTF-8",
        })

        # 버전 결정: 수동 지정 우선, 없으면 자동 감지
        if version:
            ver_tuple  = parse_version(version)
            self.caps  = detect_capabilities(ver_tuple, version)
            logger.info("버전 수동 지정 적용: %s", self.caps.summary())
        else:
            self.caps = self._detect_version()

        # 중복 리뷰 감지용 계정 ID 캐시 (첫 번째 has_ai_review 호출 시 채워짐)
        self._self_account_id: int | None = None

        logger.debug(
            "GerritClient 초기화 완료: url=%s user=%s dry_run=%s caps=[%s]",
            self.base_url, self.username, self.dry_run, self.caps.summary()
        )

    # ── 버전 자동 감지 ────────────────────────────────────────────────────────

    def _detect_version(self) -> GerritCapabilities:
        """
        Gerrit 서버 버전을 조회하여 GerritCapabilities 반환.
        감지 실패 시 최소 기능 (0,0) 으로 안전하게 폴백.

        시도 순서:
          1. GET /a/config/server/version  (2.15+, 간결)
          2. GET /a/config/server/info     (구버전 호환)
        """
        raw_ver = "unknown"

        # 시도 1: /config/server/version
        try:
            resp = self.session.get(
                self._url("config/server/version"),
                timeout=self.timeout,
            )
            if resp.status_code == 200:
                text = resp.text
                if text.startswith(self.MAGIC_PREFIX):
                    text = text[len(self.MAGIC_PREFIX):]
                raw_ver = json.loads(text).strip('"')
                logger.debug("version 엔드포인트: %s", raw_ver)
        except Exception as exc:
            logger.debug("version 엔드포인트 실패(정상): %s", exc)

        # 시도 2: /config/server/info
        if raw_ver == "unknown":
            try:
                resp = self.session.get(
                    self._url("config/server/info"),
                    timeout=self.timeout,
                )
                if resp.status_code == 200:
                    text = resp.text
                    if text.startswith(self.MAGIC_PREFIX):
                        text = text[len(self.MAGIC_PREFIX):]
                    info    = json.loads(text)
                    raw_ver = (
                        info.get("gerrit", {}).get("version", "")
                        or info.get("version", "unknown")
                    )
                    logger.debug("server/info 버전: %s", raw_ver)
            except Exception as exc:
                logger.debug("server/info 실패: %s", exc)

        ver_tuple = parse_version(raw_ver)
        caps      = detect_capabilities(ver_tuple, raw_ver)

        if ver_tuple == (0, 0):
            logger.warning(
                "Gerrit 버전 자동 감지 실패 → 최소 기능 모드. "
                "reviewer_config.json 의 gerrit.version 으로 수동 지정 가능."
            )
        return caps

    def override_version(self, version_str: str) -> None:
        """버전을 수동으로 재지정합니다 (런타임 오버라이드)."""
        ver_tuple = parse_version(version_str)
        self.caps = detect_capabilities(ver_tuple, version_str)
        logger.info("버전 재지정: %s", self.caps.summary())

    # ── 내부 헬퍼 ─────────────────────────────────────────────────────────────

    def _url(self, path: str) -> str:
        return f"{self.base_url}/a/{path.lstrip('/')}"

    def _parse(self, text: str) -> dict | list:
        if text.startswith(self.MAGIC_PREFIX):
            text = text[len(self.MAGIC_PREFIX):]
        return json.loads(text)

    def _get(self, path: str, params: dict = None) -> dict | list:
        url = self._url(path)
        logger.debug("GET %s params=%s", url, params)
        resp = self.session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return self._parse(resp.text)

    def _post(self, path: str, body: dict) -> dict | list | None:
        url = self._url(path)
        if self.dry_run:
            logger.info(
                "[DRY-RUN] POST %s\n%s",
                url, json.dumps(body, indent=2, ensure_ascii=False)
            )
            return None
        logger.debug("POST %s body_keys=%s", url, list(body.keys()))
        resp = self.session.post(url, json=body, timeout=self.timeout)
        resp.raise_for_status()
        return self._parse(resp.text) if resp.text.strip() else None

    # ── 메시지 정제 유틸리티 ────────────────────────────────────────────────────

    _EMOJI_REPLACEMENTS = {
        # 심각도 아이콘 → 텍스트
        "🔴": "[CRITICAL]", "🟠": "[MAJOR]", "🟡": "[MINOR]", "🔵": "[INFO]",
        "⚪": "[NOTE]", "✅": "[OK]", "❌": "[FAIL]", "⚠️": "[WARN]", "⚠": "[WARN]",
        # 박스/헤더 장식
        "📂": "[FILES]", "📄": "[FILE]", "🤖": "[AI]",
        "═": "=", "─": "-",
    }

    # Gerrit 구버전 안전 메시지 최대 길이 (8000자 초과 시 잘라냄)
    _MSG_MAX_LEN = 8000

    def _sanitize_message(self, text: str, max_len: int = None) -> str:
        """
        구버전 Gerrit 과 호환되도록 메시지를 정제합니다.

        1. 등록된 이모지를 ASCII 텍스트로 교체
        2. 나머지 비 BMP 문자(이모지 등) 제거
        3. 지정 길이로 잘라냄
        """
        if max_len is None:
            max_len = self._MSG_MAX_LEN

        # 1단계: 알려진 이모지 치환
        for emoji, replacement in self._EMOJI_REPLACEMENTS.items():
            text = text.replace(emoji, replacement)

        # 2단계: BMP 밖 문자(U+10000 이상, surrogate pair) 제거
        #        Gerrit 2.13 Java 1.7 기반 JSON 파서가 surrogate pair 를 거부함
        text = "".join(ch for ch in text if ord(ch) < 0x10000)

        # 3단계: 길이 제한
        if len(text) > max_len:
            cutoff = text[:max_len]
            # 단어 경계에서 자름
            last_nl = cutoff.rfind("\n")
            if last_nl > max_len * 0.8:
                cutoff = cutoff[:last_nl]
            text = cutoff + "\n\n[... 리뷰 전문은 output/ 디렉토리 파일을 확인하세요]"

        return text

    # ── 페이로드 빌더 ─────────────────────────────────────────────────────────

    def _build_review_body(self, review: ReviewInput) -> dict:
        """
        GerritCapabilities 를 기반으로 SetReviewInput 페이로드 구성.
        미지원 필드는 처음부터 제외하므로 서버 오류 없이 한 번에 성공합니다.
        """
        caps = self.caps
        body: dict = {}

        # tag (2.15+)
        if caps.tag and review.tag:
            body["tag"] = review.tag

        # message (항상)
        if review.message:
            body["message"] = review.message

        # labels (기능 지원 버전만 포함. 미지원 시 점수 없이 코멘트만 등록)
        if caps.labels and review.labels:
            body["labels"] = review.labels

        # notify (2.15+ NONE 지원. 미지원 시 OWNER/ALL만 포함)
        if caps.notify_none:
            body["notify"] = review.notify
        elif review.notify not in ("NONE",):
            body["notify"] = review.notify

        # inline comments (2.1+)
        if caps.inline_comments and review.comments:
            if caps.comment_side and caps.unresolved:
                # 3.0+: 모든 필드 그대로
                body["comments"] = review.comments
            else:
                # 2.13/2.14: side, unresolved 제거
                # - side      : 2.15+ 전용, 2.13/2.14에 포함 시 400 반환
                # - unresolved: 3.0+ 전용
                strip_keys = set()
                if not caps.comment_side:
                    strip_keys.add("side")
                if not caps.unresolved:
                    strip_keys.add("unresolved")
                body["comments"] = {
                    fname: [
                        {k: v for k, v in c.items() if k not in strip_keys}
                        for c in cmts
                    ]
                    for fname, cmts in review.comments.items()
                }

        logger.debug(
            "페이로드 구성 [Gerrit %s]: 필드=%s  인라인=%d개",
            caps.version_str, list(body.keys()),
            sum(len(v) for v in body.get("comments", {}).values()),
        )
        return body

    # ── 변경사항 조회 ─────────────────────────────────────────────────────────

    def get_change(self, change_number: int, patchset_number: int) -> GerritChange:
        data = self._get(
            f"changes/{change_number}",
            params={"o": ["DETAILED_ACCOUNTS", "CURRENT_REVISION"]}
        )
        owner_name = ""
        if "owner" in data:
            owner_name = data["owner"].get("name") or data["owner"].get("email", "")

        return GerritChange(
            change_id       = data.get("change_id", ""),
            project         = data.get("project", ""),
            branch          = data.get("branch", ""),
            subject         = data.get("subject", ""),
            status          = data.get("status", ""),
            change_number   = change_number,
            patchset_number = patchset_number,
            owner           = owner_name,
            created         = data.get("created", ""),
            updated         = data.get("updated", ""),
        )

    # ── Diff 조회 ─────────────────────────────────────────────────────────────

    def get_changed_files(self, change_number: int, patchset_number: int) -> list[str]:
        data = self._get(
            f"changes/{change_number}/revisions/{patchset_number}/files"
        )
        return [f for f in data.keys() if not f.startswith("/")]

    def get_file_diff(
        self,
        change_number:   int,
        patchset_number: int,
        filename:        str,
        context_lines:   int = 10,
    ) -> FileDiff:
        encoded = quote(filename, safe="")
        # intraline 은 소문자 "true" 문자열로 전달 (Python bool True → "True" 버그 방지)
        data = self._get(
            f"changes/{change_number}/revisions/{patchset_number}/files/{encoded}/diff",
            params={"context": context_lines, "intraline": "true"}
        )

        change_type = data.get("change_type", "MODIFIED")
        old_path    = data.get("meta_a", {}).get("name", filename)

        # ── 실제 파일 라인 번호를 추적하며 표준 unified diff 생성 ──────────────
        # Gerrit diff API content 구조:
        #   {"ab": [...]}        → 양쪽 동일 컨텍스트 줄
        #   {"a": [...], "b": [...]} → 변경 줄 (a=삭제, b=추가, 한쪽만 있을 수도 있음)
        #   {"skip": N}          → 생략된 줄 수 (context 옵션에 의해 잘린 부분)
        #
        # @@ 헝크 헤더를 올바르게 생성해야 _extract_added_lines()가
        # 정확한 NEW 파일 라인 번호를 추출할 수 있음.
        diff_lines      = []
        ann_lines       = []   # annotated diff (라인 번호 명시, AI 프롬프트용)
        valid_new_lines = []   # 추가된 줄의 실제 파일 라인 번호 (RIGHT side)
        lines_inserted  = 0
        lines_deleted   = 0
        old_line        = 1   # 이전 파일(a) 기준 현재 라인 번호
        new_line        = 1   # 새 파일(b) 기준 현재 라인 번호

        diff_lines.append(f"--- a/{old_path}")
        diff_lines.append(f"+++ b/{filename}")
        ann_lines.append(f"--- a/{old_path}")
        ann_lines.append(f"+++ b/{filename}")
        ann_lines.append("형식: [A 라인번호] = 추가줄(RIGHT), [D 라인번호] = 삭제줄(LEFT), [  라인번호] = 컨텍스트")

        for section in data.get("content", []):
            skip = section.get("skip", 0)
            if skip:
                # 생략 구간: 라인 번호만 전진
                old_line += skip
                new_line += skip
                ann_lines.append(f"... ({skip}줄 생략) ...")
                continue

            ab_lines = section.get("ab", [])
            a_lines  = section.get("a",  [])
            b_lines  = section.get("b",  [])

            if a_lines or b_lines:
                # 변경이 있는 섹션 → @@ 헝크 헤더 삽입
                old_count = len(a_lines) + len(ab_lines)
                new_count = len(b_lines) + len(ab_lines)
                diff_lines.append(
                    f"@@ -{old_line},{old_count} +{new_line},{new_count} @@"
                )

            for line in ab_lines:
                diff_lines.append(f" {line}")
                # annotated: 컨텍스트 줄 — new_line 기준으로 표시
                ann_lines.append(f"[  {new_line:4d}]  {line}")
                old_line += 1
                new_line += 1
            for line in a_lines:
                diff_lines.append(f"-{line}")
                # annotated: 삭제 줄 — LEFT(PARENT) side 코멘트시 이 번호 사용
                ann_lines.append(f"[D {old_line:4d}] -{line}")
                old_line += 1
                lines_deleted += 1
            for line in b_lines:
                diff_lines.append(f"+{line}")
                # annotated: 추가 줄 — RIGHT(REVISION) side 코멘트시 이 번호 사용
                ann_lines.append(f"[A {new_line:4d}] +{line}")
                valid_new_lines.append(new_line)
                new_line += 1
                lines_inserted += 1

        return FileDiff(
            filename=filename, old_path=old_path, change_type=change_type,
            lines_inserted=lines_inserted, lines_deleted=lines_deleted,
            diff_content="\n".join(diff_lines),
            valid_new_lines=valid_new_lines,
            annotated_diff="\n".join(ann_lines),
        )

    def get_all_diffs(
        self,
        change_number:   int,
        patchset_number: int,
        max_files:       int = 50,
        context_lines:   int = 10,
    ) -> list[FileDiff]:
        files = self.get_changed_files(change_number, patchset_number)
        logger.info("변경 파일 수: %d (max_files=%d)", len(files), max_files)

        if len(files) > max_files:
            logger.warning(
                "변경 파일(%d) > max_files(%d). 처음 %d개만 처리합니다.",
                len(files), max_files, max_files
            )
            files = files[:max_files]

        diffs = []
        for fname in files:
            try:
                diff = self.get_file_diff(
                    change_number, patchset_number, fname, context_lines
                )
                diffs.append(diff)
                logger.debug("diff 획득: %s (+%d/-%d)",
                             fname, diff.lines_inserted, diff.lines_deleted)
            except Exception as exc:
                logger.warning("diff 획득 실패: %s — %s", fname, exc)
        return diffs

    # ── 리뷰 등록 ─────────────────────────────────────────────────────────────

    def post_review(
        self,
        change_number:   int,
        patchset_number: int,
        review:          ReviewInput,
    ) -> bool:
        """
        코드 리뷰 코멘트를 Gerrit에 등록합니다.

        4단계 순차 시도:
          1. FULL      : 버전 기반 최적 페이로드 (이모지 정제 + 길이 제한 적용)
          2. NO-LABELS : labels 제거 (구버전 Gerrit 권한 문제 대응)
          3. SAFE-MSG  : labels 제거 + 메시지 추가 정제 (더 엄격한 ASCII 안전 처리)
          4. MINIMAL   : 최소 코멘트만 (연결 자체는 되는데 내용이 문제인 경우)

        각 단계의 실패 원인을 상세 로그로 기록합니다.

        Returns:
            True: 성공 (dry_run 포함), False: 전체 단계 실패
        """
        inline_total = sum(len(v) for v in review.comments.values()) if review.comments else 0

        if self.dry_run:
            body = self._build_review_body(review)
            # dry-run 에서도 정제 시뮬레이션
            if "message" in body:
                sanitized = self._sanitize_message(body["message"])
                body["message"] = sanitized
            logger.info(
                "[DRY-RUN] 리뷰 시뮬레이션 [%s]\n"
                "  인라인=%d개  label=%s  필드=%s\n"
                "  메시지 길이: %d자",
                self.caps.version_str, inline_total, review.labels,
                list(body.keys()), len(body.get("message", "")),
            )
            return True

        if not self.caps.inline_comments and inline_total > 0:
            logger.info(
                "Gerrit %s 인라인 코멘트 미지원 → 파일별 리뷰가 Change 코멘트에 포함됩니다.",
                self.caps.version_str
            )

        endpoint = f"changes/{change_number}/revisions/{patchset_number}/review"

        def _post_attempt(b: dict, stage: str) -> bool:
            """단일 POST 시도. 전송 전 페이로드 요약 로그."""
            msg_len = len(b.get("message", ""))
            logger.info(
                "  [%s] 시도: 필드=%s  메시지=%d자  인라인=%d개",
                stage, list(b.keys()), msg_len,
                sum(len(v) for v in b.get("comments", {}).values()),
            )
            logger.debug("  [%s] 페이로드(앞 300자): %s",
                         stage, json.dumps(b, ensure_ascii=False)[:300])
            try:
                self._post(endpoint, b)
                logger.info(
                    "  [%s] 등록 완료: change=%d ps=%d  label=%s",
                    stage, change_number, patchset_number, b.get("labels", "(없음)"),
                )
                return True
            except requests.HTTPError as exc:
                status    = exc.response.status_code if exc.response is not None else "?"
                resp_body = exc.response.text[:200]  if exc.response is not None else ""
                logger.warning(
                    "  [%s] 실패 HTTP %s — 응답: %s",
                    stage, status, resp_body.strip(),
                )
                return False

        # ── 기본 페이로드 구성 (버전 기반) + 메시지 정제 적용 ────────────────
        base_body = self._build_review_body(review)
        if "message" in base_body:
            base_body["message"] = self._sanitize_message(base_body["message"])

        # ── 1단계 FULL: 버전 최적 페이로드 ──────────────────────────────────
        logger.info("Gerrit 리뷰 등록 시작 [%s]", self.caps.summary())
        if _post_attempt(base_body, "FULL"):
            return True

        # ── 2단계 NO-LABELS: labels 제거 ─────────────────────────────────────
        #    원인: Gerrit 2.13에서 Labels 권한 없을 때 403 대신 500 반환
        logger.warning("2단계: labels 제거 후 재시도...")
        body2 = {k: v for k, v in base_body.items() if k != "labels"}
        if _post_attempt(body2, "NO-LABELS"):
            logger.warning(
                "  => Code-Review 점수 미등록. "
                "Gerrit 관리자에게 ai-reviewer 의 Label 투표 권한을 요청하세요."
            )
            return True

        # ── 3단계 SAFE-MSG: 메시지 추가 정제 (더 엄격) ───────────────────────
        #    원인: 이모지/특수문자/긴 메시지가 구버전 Jackson/Jersey 파싱 실패 유발
        logger.warning("3단계: 메시지 추가 정제 후 재시도...")
        safe_msg = self._sanitize_message(
            base_body.get("message", ""),
            max_len=3000,   # 더 엄격한 길이 제한
        )
        # ASCII + 한글 + 기본 구두점만 남기기 (BMP 전체 → Latin+한글로 축소)
        safe_msg = "".join(
            ch for ch in safe_msg
            if (ord(ch) < 0x0100            # ASCII + Latin-1
                or 0xAC00 <= ord(ch) <= 0xD7A3  # 한글 완성형
                or 0x3130 <= ord(ch) <= 0x318F  # 한글 자모
                or ch in (" \t\n\r.,;:()[]{}+-=/<>!?@#%&*_"))
        )
        body3 = {k: v for k, v in base_body.items() if k not in ("labels",)}
        body3["message"] = safe_msg
        if _post_attempt(body3, "SAFE-MSG"):
            logger.warning(
                "  => 메시지 정제 후 등록 성공. "
                "원본 리뷰는 output/ 디렉토리를 확인하세요."
            )
            return True

        # ── 4단계 MINIMAL: 최소 메시지만 ─────────────────────────────────────
        #    원인: 메시지 내용 자체에 문제가 있을 때 최후 수단
        logger.warning("4단계: 최소 코멘트만 등록 시도...")
        minimal_msg = (
            f"[AI Code Review] Change #{change_number} PS{patchset_number} "
            f"review completed. Please check output directory for full report."
        )
        body4 = {"message": minimal_msg}
        if _post_attempt(body4, "MINIMAL"):
            logger.warning(
                "  => 최소 코멘트만 등록됨. "
                "전체 리뷰는 output/ 디렉토리 파일을 확인하세요."
            )
            return True

        # ── 최종 실패 ────────────────────────────────────────────────────────
        logger.error(
            "리뷰 등록 최종 실패 (change=%d ps=%d) — 4단계 모두 실패.",
            change_number, patchset_number
        )
        logger.error(
            "  Gerrit 버전: %s", self.caps.summary()
        )
        logger.error(
            "  확인 사항:\n"
            "    1. ai-reviewer 계정이 해당 Change 에 코멘트 권한이 있는지 확인\n"
            "    2. Change 상태가 NEW 인지 확인 (MERGED/ABANDONED 는 코멘트 불가)\n"
            "    3. Gerrit 서버 로그에서 실제 오류 원인 확인\n"
            "    4. reviewer_config.json 의 gerrit.version 을 수동 지정 시도"
        )
        return False


    # ── 중복 리뷰 방지 ────────────────────────────────────────────────────────

    def _get_self_account_id(self) -> int | None:
        """
        인증된 계정(self.username)의 Gerrit _account_id를 반환합니다 (캐시됨).

        _account_id 는 DETAILED_ACCOUNTS 없이도 author 객체에 항상 포함되므로
        Gerrit 2.13 등 구버전에서 username 필드가 없는 경우에도 중복 감지가 가능합니다.
        """
        if self._self_account_id is not None:
            return self._self_account_id
        try:
            data = self._get("accounts/self")
            aid  = data.get("_account_id") if isinstance(data, dict) else None
            if aid:
                self._self_account_id = int(aid)
                logger.debug(
                    "인증 계정 ID 확인: _account_id=%d (username=%s)",
                    self._self_account_id, self.username,
                )
            return self._self_account_id
        except Exception as exc:
            logger.debug("accounts/self 조회 실패 (계정 ID 없이 진행): %s", exc)
            return None

    def _get_change_messages(self, change_number: int) -> list:
        """
        Change 메시지 목록을 조회합니다. Gerrit 버전에 따라 엔드포인트를 선택합니다.

          - 2.14+ : GET /a/changes/{id}/messages
          - 2.13  : GET /a/changes/{id}?o=MESSAGES  (버전을 알면 바로 사용, 불필요한 404 왕복 없음)
        """
        # 버전을 이미 알고 있으면 바로 올바른 엔드포인트 선택 (불필요한 404 왕복 방지)
        use_messages_endpoint = self.caps.version_tuple >= (2, 14)

        if use_messages_endpoint:
            # 2.14+: /messages 전용 엔드포인트
            try:
                data = self._get(f"changes/{change_number}/messages")
                if isinstance(data, list):
                    logger.debug("메시지 조회 성공 (/messages): %d개", len(data))
                    return data
            except Exception as exc:
                logger.debug("GET /messages 실패: %s", exc)
        else:
            # 2.13 이하: ?o=MESSAGES 파라미터 직접 사용
            # DETAILED_ACCOUNTS 를 함께 요청해야 author.username 필드가 포함됨
            try:
                data = self._get(
                    f"changes/{change_number}",
                    params={"o": ["MESSAGES", "DETAILED_ACCOUNTS"]},
                )
                messages = data.get("messages", []) if isinstance(data, dict) else []
                logger.debug("메시지 조회 성공 (?o=MESSAGES): %d개", len(messages))
                return messages
            except Exception as exc:
                logger.warning("메시지 조회 실패 (?o=MESSAGES): %s", exc)

        return []

    def has_ai_review(self, change_number: int, patchset_number: int) -> bool:
        """
        해당 Change 의 지정 Patchset 에 AI 리뷰 코멘트가 이미 있는지 확인합니다.

        Gerrit 버전 호환:
          - 2.14+ : GET /changes/{id}/messages
          - 2.13  : GET /changes/{id}?o=MESSAGES (자동 폴백)

        판단 기준 (둘 중 하나라도 해당하면 True):
          1. Change 메시지 중 tag='autogenerated:ai-reviewer' 인 항목이 존재
          2. 현재 AI 계정(self.username)이 작성한 메시지가 존재

        _revision_number == patchset_number 이거나 0(Change 전체 코멘트)인 경우만 해당.

        Returns:
            True : 이미 AI 리뷰 완료 → 건너뜀
            False: 리뷰 없음 또는 조회 실패 → 진행
        """
        messages = self._get_change_messages(change_number)

        if not messages:
            logger.debug(
                "중복 없음: change=#%d ps=%d (메시지 없음)",
                change_number, patchset_number,
            )
            return False

        # username 비교의 보완책으로 _account_id 를 미리 확보 (Gerrit 2.13 호환)
        # Gerrit 2.13의 ?o=MESSAGES 응답은 DETAILED_ACCOUNTS 없이 username 을 포함하지
        # 않지만, _account_id 는 항상 포함되므로 이를 기준으로 비교한다.
        self_aid = self._get_self_account_id()

        for msg in messages:
            tag    = msg.get("tag", "") or ""
            msg_ps = msg.get("_revision_number", 0)
            author = msg.get("author", {}) or {}

            # patchset 번호 확인 (0 = Change 전체 코멘트, 항상 해당)
            if msg_ps != patchset_number and msg_ps != 0:
                continue

            if "autogenerated:ai-reviewer" in tag:
                logger.info(
                    "중복 리뷰 감지: change=#%d ps=%d — tag=%s",
                    change_number, patchset_number, tag,
                )
                return True

            # username 비교 (DETAILED_ACCOUNTS 가 있을 때 동작)
            if author.get("username", "") == self.username:
                logger.info(
                    "중복 리뷰 감지: change=#%d ps=%d — username=%s 기존 코멘트",
                    change_number, patchset_number, self.username,
                )
                return True

            # _account_id 비교 — DETAILED_ACCOUNTS 없이도 항상 동작 (Gerrit 2.13 호환)
            if self_aid and author.get("_account_id") == self_aid:
                logger.info(
                    "중복 리뷰 감지: change=#%d ps=%d — _account_id=%d 기존 코멘트",
                    change_number, patchset_number, self_aid,
                )
                return True

        logger.debug(
            "중복 없음: change=#%d ps=%d (메시지 %d개 확인)",
            change_number, patchset_number, len(messages),
        )
        return False

    # ── 연결 테스트 ───────────────────────────────────────────────────────────

    def test_connection(self) -> bool:
        try:
            data    = self._get("config/server/info")
            project = data.get("gerrit", {}).get("all_projects_name", "All-Projects")
            logger.info(
                "Gerrit 연결 성공: %s  all_projects=%s  %s",
                self.base_url, project, self.caps.summary()
            )
            return True
        except Exception as exc:
            logger.error("Gerrit 연결 실패: %s — %s", self.base_url, exc)
            return False
