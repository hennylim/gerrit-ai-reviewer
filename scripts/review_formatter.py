"""
review_formatter.py
-------------------
AI 코드 리뷰 결과를 다양한 포맷으로 저장하는 모듈.

지원 포맷:
  - text  : 콘솔 친화적 텍스트
  - markdown : 마크다운
  - json  : 구조화된 JSON
  - html  : 브라우저에서 바로 열 수 있는 HTML 리포트
"""

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# 리뷰 결과 데이터 구조
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ReviewResult:
    """AI 코드 리뷰 최종 결과"""
    change_number:   int
    patchset_number: int
    project:         str
    branch:          str
    subject:         str
    owner:           str
    ai_provider:     str
    ai_model:        str
    review_summary:  str                    # 전체 요약 코멘트 (Gerrit에 등록)
    file_reviews:    list[dict]             # [{filename, review_text, score}]
    overall_score:   int  = 0              # -2~+2 (Gerrit Code-Review 레이블)
    is_dry_run:      bool = False
    gerrit_posted:   bool = False
    error:           Optional[str] = None
    elapsed_seconds: float = 0.0
    timestamp:       str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    @property
    def success(self) -> bool:
        return self.error is None


# ──────────────────────────────────────────────────────────────────────────────
# 포맷터
# ──────────────────────────────────────────────────────────────────────────────

class ReviewFormatter:
    """리뷰 결과 다중 포맷 저장기"""

    SEP  = "=" * 72
    THIN = "-" * 72

    def __init__(self, output_dir: str | Path):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ── 파일명 생성 ────────────────────────────────────────────────────────────

    def _stem(self, result: ReviewResult) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"review_c{result.change_number}_p{result.patchset_number}_{ts}"

    # ── TEXT ──────────────────────────────────────────────────────────────────

    def to_text(self, result: ReviewResult) -> str:
        dry = " [DRY-RUN]" if result.is_dry_run else ""
        posted = " ✅ Gerrit 등록 완료" if result.gerrit_posted else " ⏸ Gerrit 미등록"
        score_str = f"{result.overall_score:+d}" if result.overall_score else "N/A"

        lines = [
            self.SEP,
            f"  Gerrit AI 코드 리뷰 결과{dry}",
            self.THIN,
            f"  Change   : #{result.change_number} (Patchset {result.patchset_number})",
            f"  Project  : {result.project} [{result.branch}]",
            f"  Subject  : {result.subject}",
            f"  Author   : {result.owner}",
            f"  AI       : {result.ai_provider} / {result.ai_model}",
            f"  Score    : Code-Review {score_str}",
            f"  Status   : {posted}",
            f"  Time     : {result.timestamp} ({result.elapsed_seconds:.1f}s)",
            self.SEP,
            "",
            "[ 전체 리뷰 요약 ]",
            result.review_summary,
            "",
        ]

        sev_icon = {"CRITICAL": "[CRITICAL]", "MAJOR": "[MAJOR]", "MINOR": "[MINOR]", "INFO": "[INFO]"}
        for fr in result.file_reviews:
            inlines = fr.get("inline_comments", [])
            lines += [
                self.THIN,
                f"📄 {fr.get('filename', '?')}  (+{fr.get('lines_ins',0)}/-{fr.get('lines_del',0)})  코멘트 {len(inlines)}개",
                self.THIN,
                fr.get("file_summary") or fr.get("review_text", ""),
                "",
            ]
            if inlines:
                lines.append("  [인라인 코멘트]")
                for c in inlines:
                    sev   = c.get("severity", "INFO")
                    label = sev_icon.get(sev, f"[{sev}]")
                    lines.append(f"  Line {c['line']:4d}  {label}  {c['message']}")
                lines.append("")

        if result.error:
            lines += [self.THIN, f"⚠️  오류: {result.error}", ""]

        lines.append(self.SEP)
        return "\n".join(lines)

    def save_text(self, result: ReviewResult) -> Path:
        path = self.output_dir / f"{self._stem(result)}.txt"
        path.write_text(self.to_text(result), encoding="utf-8")
        logger.info("TEXT 저장: %s", path)
        return path

    # ── MARKDOWN ──────────────────────────────────────────────────────────────

    def to_markdown(self, result: ReviewResult) -> str:
        dry          = " *(Dry Run)*" if result.is_dry_run else ""
        posted_badge = "✅ 등록됨" if result.gerrit_posted else "⏸ 미등록"
        score_str    = f"`{result.overall_score:+d}`" if result.overall_score else "`N/A`"
        total_inline = sum(len(fr.get("inline_comments", [])) for fr in result.file_reviews)

        file_sections = ""
        for fr in result.file_reviews:
            fname   = fr.get("filename", "unknown")
            summary = fr.get("file_summary") or fr.get("review_text", "")
            inlines = fr.get("inline_comments", [])
            ins     = fr.get("lines_ins", 0)
            dels    = fr.get("lines_del", 0)

            inline_table = ""
            if inlines:
                rows = "\n".join(
                    f"| `{c['line']}` | **{c['severity']}** | {c['message'].replace(chr(10), ' ')} |"
                    for c in inlines
                )
                inline_table = (
                    "\n\n**인라인 코멘트**\n\n"
                    "| Line | 심각도 | 메시지 |\n"
                    "|------|--------|--------|\n"
                    f"{rows}\n"
                )

            file_sections += (
                f"\n### 📄 `{fname}`"
                f"  <sub>+{ins} / -{dels} | 코멘트 {len(inlines)}개</sub>\n\n"
                f"{summary}"
                f"{inline_table}\n"
            )

        error_section = f"\n> ⚠️ **오류**: {result.error}\n" if result.error else ""

        return (
            f"# 🤖 Gerrit AI 코드 리뷰{dry}\n\n"
            f"## 📋 변경사항 정보\n\n"
            f"| 항목 | 값 |\n"
            f"|------|-----|\n"
            f"| Change | `#{result.change_number}` (Patchset `{result.patchset_number}`) |\n"
            f"| Project | `{result.project}` / `{result.branch}` |\n"
            f"| Subject | {result.subject} |\n"
            f"| Author | {result.owner} |\n"
            f"| AI | `{result.ai_provider}` / `{result.ai_model}` |\n"
            f"| Code-Review | {score_str} |\n"
            f"| 인라인 코멘트 | `{total_inline}개` |\n"
            f"| Gerrit | {posted_badge} |\n"
            f"| 시각 | `{result.timestamp}` ({result.elapsed_seconds:.1f}s) |\n\n"
            f"---\n\n"
            f"## 💬 전체 리뷰 요약\n\n"
            f"{result.review_summary}\n\n"
            f"---\n\n"
            f"## 📂 파일별 리뷰\n"
            f"{file_sections}\n"
            f"{error_section}"
            f"---\n"
            f"*Generated by Gerrit AI Reviewer — {result.timestamp}*\n"
        )

    def save_markdown(self, result: ReviewResult) -> Path:
        path = self.output_dir / f"{self._stem(result)}.md"
        path.write_text(self.to_markdown(result), encoding="utf-8")
        logger.info("MARKDOWN 저장: %s", path)
        return path

    # ── JSON ──────────────────────────────────────────────────────────────────

    def to_json(self, result: ReviewResult) -> str:
        d = asdict(result)
        return json.dumps(d, ensure_ascii=False, indent=2)

    def save_json(self, result: ReviewResult) -> Path:
        path = self.output_dir / f"{self._stem(result)}.json"
        path.write_text(self.to_json(result), encoding="utf-8")
        logger.info("JSON 저장: %s", path)
        return path

    # ── HTML ──────────────────────────────────────────────────────────────────

    def to_html(self, result: ReviewResult) -> str:
        score_color = {
            2: "#2d9c4e", 1: "#5cb85c", 0: "#888",
            -1: "#e07b39", -2: "#c9302c"
        }.get(result.overall_score, "#888")

        # 파일별 HTML 생성
        file_html = ""
        for fr in result.file_reviews:
            fname    = fr.get("filename", "unknown")
            summary  = (fr.get("file_summary") or fr.get("review_text", "")).replace("\n", "<br>")
            inlines  = fr.get("inline_comments", [])
            ins      = fr.get("lines_ins", 0)
            dels     = fr.get("lines_del", 0)

            sev_icon = {"CRITICAL": "🔴", "MAJOR": "🟠", "MINOR": "🟡", "INFO": "🔵"}
            sev_color = {
                "CRITICAL": "#ffeef0", "MAJOR": "#fff8e6",
                "MINOR": "#f0fff4",    "INFO":  "#e8f4fd"
            }
            sev_border = {
                "CRITICAL": "#f97583", "MAJOR": "#ffc107",
                "MINOR":    "#28a745", "INFO":  "#17a2b8"
            }

            inline_html = ""
            if inlines:
                inline_html = "<div style='margin-top:10px'>"
                for c in inlines:
                    sev  = c.get("severity", "INFO")
                    line = c.get("line", "?")
                    msg  = c.get("message", "").replace("\n", "<br>")
                    bg   = sev_color.get(sev, "#f8f9fa")
                    bd   = sev_border.get(sev, "#dee2e6")
                    inline_html += (
                        f"<div style='background:{bg};border-left:3px solid {bd};"
                        f"border-radius:0 4px 4px 0;padding:8px 12px;margin:5px 0;font-size:12px'>"
                        f"<span style='font-weight:500;font-family:monospace'>Line {line}</span>"
                        f"&nbsp;&nbsp;{msg}</div>"
                    )
                inline_html += "</div>"

            file_html += f"""
            <div class="file-review">
              <div class="file-name">📄 {fname}
                <span style="font-size:11px;font-weight:400;color:#888;margin-left:8px">
                  +{ins} / -{dels} &nbsp;|&nbsp; 코멘트 {len(inlines)}개
                </span>
              </div>
              <div class="review-body">
                {summary}
                {inline_html}
              </div>
            </div>"""

        dry_banner = '<div class="dry-run-banner">🔶 DRY-RUN 모드 — Gerrit 실제 등록 없음</div>' if result.is_dry_run else ""
        error_html = f'<div class="error-box">⚠️ {result.error}</div>' if result.error else ""
        posted_label = "✅ Gerrit 등록 완료" if result.gerrit_posted else "⏸ Gerrit 미등록"

        total_inline = sum(len(fr.get("inline_comments", [])) for fr in result.file_reviews)

        return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>AI 코드 리뷰 — #{result.change_number}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         margin: 0; background: #f5f7fa; color: #24292e; }}
  .container {{ max-width: 960px; margin: 32px auto; padding: 0 16px; }}
  h1 {{ font-size: 1.6rem; color: #1a1e23; }}
  .dry-run-banner {{ background:#fff3cd; border:1px solid #ffc107;
                     padding:10px 16px; border-radius:6px; margin-bottom:16px; }}
  .meta-table {{ width:100%; border-collapse:collapse; margin:16px 0; }}
  .meta-table td {{ padding:7px 12px; border:1px solid #d1d5da; }}
  .meta-table td:first-child {{ font-weight:600; background:#f6f8fa; width:130px; }}
  .score {{ font-size:1.2rem; font-weight:700; color:{score_color}; }}
  .summary-box {{ background:#fff; border:1px solid #d1d5da; border-radius:8px;
                  padding:20px; margin:16px 0; white-space:pre-wrap; line-height:1.7; }}
  .file-review {{ background:#fff; border:1px solid #d1d5da; border-radius:8px;
                  margin:12px 0; overflow:hidden; }}
  .file-name {{ background:#f6f8fa; padding:10px 16px; font-weight:600;
                border-bottom:1px solid #d1d5da; font-family:monospace; }}
  .review-body {{ padding:16px; white-space:pre-wrap; line-height:1.7; }}
  .error-box {{ background:#ffeef0; border:1px solid #f97583;
                border-radius:6px; padding:12px 16px; margin:12px 0; }}
  footer {{ text-align:center; color:#888; font-size:.8rem; margin-top:32px; padding:16px; }}
</style>
</head>
<body>
<div class="container">
  <h1>🤖 Gerrit AI 코드 리뷰
    <span style="background:#0366d6;color:#fff;padding:3px 10px;border-radius:12px;font-size:.8rem;font-weight:600;margin-left:8px">#{result.change_number}</span>
  </h1>
  {dry_banner}
  <table class="meta-table">
    <tr><td>Project</td><td>{result.project} / {result.branch}</td></tr>
    <tr><td>Subject</td><td>{result.subject}</td></tr>
    <tr><td>Author</td><td>{result.owner}</td></tr>
    <tr><td>Patchset</td><td>{result.patchset_number}</td></tr>
    <tr><td>AI</td><td>{result.ai_provider} / {result.ai_model}</td></tr>
    <tr><td>Code-Review</td><td class="score">{result.overall_score:+d}</td></tr>
    <tr><td>인라인 코멘트</td><td>{total_inline}개</td></tr>
    <tr><td>Gerrit</td><td>{posted_label}</td></tr>
    <tr><td>시각</td><td>{result.timestamp} ({result.elapsed_seconds:.1f}s)</td></tr>
  </table>

  <h2>💬 전체 리뷰 요약</h2>
  <div class="summary-box">{result.review_summary}</div>

  <h2>📂 파일별 리뷰</h2>
  {file_html}
  {error_html}
</div>
<footer>Generated by Gerrit AI Reviewer — {result.timestamp}</footer>
</body>
</html>"""

    def save_html(self, result: ReviewResult) -> Path:
        path = self.output_dir / f"{self._stem(result)}.html"
        path.write_text(self.to_html(result), encoding="utf-8")
        logger.info("HTML 저장: %s", path)
        return path

    # ── 일괄 저장 ─────────────────────────────────────────────────────────────

    def save_all(self, result: ReviewResult) -> dict[str, Path]:
        """TEXT / MARKDOWN / JSON / HTML 모두 저장합니다."""
        paths = {
            "text":     self.save_text(result),
            "markdown": self.save_markdown(result),
            "json":     self.save_json(result),
            "html":     self.save_html(result),
        }
        logger.info("전체 리뷰 결과 저장 완료: %s", self.output_dir)
        return paths
