"""
claude_ai.py
------------
Anthropic Claude AI 구현체.
웹 검색: web_search_20250305 built-in tool (server-side)

설치: pip install anthropic
공식 문서: https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool
"""

import logging
import time
from typing import Optional

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

from .base_ai import BaseAI, ChatResponse, SearchSource

logger = logging.getLogger(__name__)

# 웹 검색 tool 정의
_WEB_SEARCH_TOOL = {
    "type":     "web_search_20250305",
    "name":     "web_search",
    "max_uses": 5,          # 요청 당 최대 검색 횟수
}


class ClaudeAI(BaseAI):
    """
    Anthropic Claude AI 제공자.

    ┌──────────────────────────────────────────────────────────────┐
    │              Claude 모델 목록 (2026-03)                       │
    ├───────────────────────────────┬──────────────────────────────┤
    │ 모델명                         │ 특징                         │
    ├───────────────────────────────┼──────────────────────────────┤
    │ claude-opus-4-6  ★기본값      │ 최고성능, 1M 컨텍스트        │
    │ claude-sonnet-4-6             │ 코딩 평가 Opus급, 성가비 최고 │
    │ claude-opus-4-5               │ 코딩·에이전틱                 │
    │ claude-sonnet-4-5-20251022    │ 에이전트, 오피스 파일         │
    │ claude-haiku-4-5-20251001     │ 최경량, 고속, 저비용          │
    │ claude-opus-4-1               │ 고급 추론·에이전틱            │
    │ claude-sonnet-4-20250514      │ Sonnet 4, 일반 작업           │
    │ claude-sonnet-3-7-20250219    │ 하이브리드 추론               │
    │ claude-sonnet-3-5-20241022    │ 3.5 Sonnet v2                │
    │ claude-haiku-3-5-20241022     │ 3.5 Haiku, 빠르고 저렴       │
    └───────────────────────────────┴──────────────────────────────┘

    🔍 웹 검색 (--web-search):
        web_search_20250305 built-in tool 사용.
        Anthropic이 server-side에서 Brave Search를 통해 검색을 실행.
        모든 Claude 모델에서 동작 (단일 API 호출로 처리).
        응답 텍스트에 citations (출처 인용) 포함될 수 있음.
    """

    RECOMMENDED_MODELS = [
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "claude-opus-4-5",
        "claude-sonnet-4-5-20251022",
        "claude-haiku-4-5-20251001",
        "claude-opus-4-1",
        "claude-sonnet-4-20250514",
    ]
    LEGACY_MODELS = [
        "claude-sonnet-3-7-20250219",
        "claude-sonnet-3-5-20241022",
        "claude-haiku-3-5-20241022",
    ]
    DEPRECATED_MODELS = {
        "claude-opus-4-0":          "2026-05-14 종료 예정 → claude-opus-4-5 권장",
        "claude-3-opus-20240229":   "2026-01 종료 → claude-opus-4-1 권장",
        "claude-3-sonnet-20240229": "2025-07 종료 → claude-sonnet-4 권장",
        "claude-2-1":               "2025 종료 → 최신 모델 사용 권장",
    }
    SUPPORTED_MODELS = RECOMMENDED_MODELS + LEGACY_MODELS

    def __init__(
        self,
        api_key:     str,
        model:       Optional[str] = None,
        dry_run:     bool  = False,
        web_search:  bool  = False,
        retry_count: int   = 3,
        retry_delay: float = 5.0,
        max_tokens:  int   = 8192,
    ):
        super().__init__(api_key=api_key, model=model, dry_run=dry_run, web_search=web_search, retry_count=retry_count, retry_delay=retry_delay)
        self.max_tokens = max_tokens
        self._client    = None

        if self.model in self.DEPRECATED_MODELS:
            logger.warning("⚠️  '%s' 모델은 종료되었거나 종료 예정입니다. %s",
                           self.model, self.DEPRECATED_MODELS[self.model])

    @property
    def provider_name(self) -> str:
        return "Anthropic Claude"

    @property
    def default_model(self) -> str:
        return "claude-opus-4-6"

    def _get_client(self):
        if self._client is None:
            if not ANTHROPIC_AVAILABLE:
                raise ImportError(
                    "anthropic 패키지가 설치되지 않았습니다.\n"
                    "설치 명령어: pip install anthropic"
                )
            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def _extract_answer_and_sources(self, message) -> tuple[str, list[SearchSource]]:
        """
        API 응답에서 텍스트 답변과 웹 검색 출처를 추출합니다.

        Claude의 web_search tool 응답은 여러 content block으로 구성됩니다:
        - text 블록: 최종 답변 텍스트 (citations 포함 가능)
        - tool_use 블록: Claude가 호출한 도구 (web_search)
        - tool_result 블록: 검색 결과 (암호화된 내부 데이터)
        """
        answer_parts: list[str] = []
        sources:      list[SearchSource] = []
        seen_urls:    set[str] = set()

        for block in (message.content or []):
            block_type = getattr(block, "type", "")

            # text 블록: 최종 답변
            if block_type == "text":
                text = getattr(block, "text", "") or ""
                if text:
                    answer_parts.append(text)

                # citations: text 블록 안에 인용 출처가 포함된 경우
                citations = getattr(block, "citations", None) or []
                for cit in citations:
                    url   = getattr(cit, "url",   "") or ""
                    title = getattr(cit, "title", "") or ""
                    if url and url not in seen_urls:
                        sources.append(SearchSource(title=title, url=url))
                        seen_urls.add(url)

        return "\n".join(answer_parts), sources

    def _call_api(self, prompt: str) -> ChatResponse:
        start_time = time.time()
        try:
            client = self._get_client()

            # ── 웹 검색 도구 설정 ───────────────────────────
            tools = [_WEB_SEARCH_TOOL] if self.web_search else None
            if self.web_search:
                logger.debug("Claude: web_search_20250305 tool 활성화 (max_uses=%d)",
                             _WEB_SEARCH_TOOL["max_uses"])

            kwargs = dict(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            if tools:
                kwargs["tools"] = tools

            # ── API 호출 (pause_turn 루프 처리) ────────────
            # web_search tool은 server-side에서 처리되지만,
            # 복잡한 다단계 검색 시 stop_reason="pause_turn" 이 발생할 수 있음
            messages_history = [{"role": "user", "content": prompt}]
            final_message    = None
            max_iterations   = 5   # 무한 루프 방지

            for iteration in range(max_iterations):
                kwargs["messages"] = messages_history
                message = client.messages.create(**kwargs)

                if message.stop_reason != "pause_turn":
                    final_message = message
                    break

                # pause_turn: 검색 중간 결과를 이어서 전송
                logger.debug("Claude pause_turn 감지 (iteration=%d), 계속 진행", iteration + 1)
                messages_history.append({
                    "role":    "assistant",
                    "content": message.content,
                })
            else:
                # max_iterations 초과 시 마지막 응답 사용
                final_message = message

            elapsed = time.time() - start_time

            # ── 토큰 사용량 ──────────────────────────────────
            tokens = None
            if hasattr(final_message, "usage") and final_message.usage:
                tokens = (
                    final_message.usage.input_tokens +
                    final_message.usage.output_tokens
                )

            # ── 답변 및 출처 추출 ────────────────────────────
            answer, sources = self._extract_answer_and_sources(final_message)

            if self.web_search and sources:
                logger.info("Claude 웹 검색 출처 %d개 추출", len(sources))

            return ChatResponse(
                prompt=prompt,
                answer=answer,
                model=self.model,
                provider=self.provider_name,
                tokens_used=tokens,
                elapsed_seconds=elapsed,
                web_search_used=self.web_search,
                search_sources=sources,
            )

        except Exception as e:
            elapsed   = time.time() - start_time
            error_msg = str(e)
            hint      = self._error_hint(error_msg)
            return ChatResponse(
                prompt=prompt, answer="", model=self.model,
                provider=self.provider_name, elapsed_seconds=elapsed,
                error=error_msg + hint,
            )

    def _error_hint(self, error_msg: str) -> str:
        if "401" in error_msg or "authentication" in error_msg.lower():
            return "\n\n💡 API 키 확인: config/api_keys.json → 'claude' 항목"
        if "429" in error_msg or "rate_limit" in error_msg.lower():
            return (
                "\n\n💡 요청 한도 초과:\n"
                "  경량 모델: claude-haiku-4-5-20251001, claude-sonnet-4-6"
            )
        if "404" in error_msg or "not_found_error" in error_msg.lower():
            return "\n\n💡 모델 없음: --model claude-opus-4-6 을 사용해 보세요."
        return ""
