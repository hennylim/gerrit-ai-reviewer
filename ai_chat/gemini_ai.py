"""
gemini_ai.py
------------
Google Gemini AI 구현체.
웹 검색: google_search grounding tool (types.GoogleSearch)

설치: pip install google-genai
공식 문서: https://ai.google.dev/gemini-api/docs/google-search
"""

import logging
import time
from typing import Optional

try:
    from google import genai
    from google.genai import types as genai_types
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

from .base_ai import BaseAI, ChatResponse, SearchSource

logger = logging.getLogger(__name__)


class GeminiAI(BaseAI):
    """
    Google Gemini AI 제공자. (google-genai SDK)

    ┌──────────────────────────────────────────────────────────────┐
    │              Gemini 모델 목록 (2026-03)                       │
    ├───────────────────────────────┬──────┬───────┬───────────────┤
    │ 모델명                         │ 무료 │ 유료  │ 비고          │
    ├───────────────────────────────┼──────┼───────┼───────────────┤
    │ gemini-2.5-flash ★기본값      │  ✅  │  ✅  │ 범용, 검색지원 │
    │ gemini-2.5-flash-lite         │  ✅  │  ✅  │ 초경량        │
    │ gemini-3-flash-preview        │  ✅  │  ✅  │ 빠른 프론티어  │
    │ gemini-3.1-flash-lite         │  ✅  │  ✅  │ 최신 초경량    │
    │ gemini-2.0-flash              │  ✅  │  ✅  │ ⚠️ 종료예정  │
    │ gemini-2.0-flash-lite         │  ✅  │  ✅  │ ⚠️ 종료예정  │
    │ gemini-3.1-pro-preview        │  -   │  ✅  │ 최신 Pro      │
    │ gemini-3.1-flash-image        │  -   │  ✅  │ 이미지 생성   │
    │ gemini-3-pro-image-preview    │  -   │  ✅  │ 고품질 이미지 │
    └───────────────────────────────┴──────┴───────┴───────────────┘

    🔍 웹 검색 (--web-search):
        Google Search grounding 사용.
        모든 무료/유료 텍스트 모델에서 동작.
        응답에 grounding_metadata(검색 쿼리, 참고 출처) 포함.
    """

    FREE_TIER_MODELS = [
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-3-flash-preview",
        "gemini-3.1-flash-lite",
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
    ]
    PAID_TIER_MODELS = [
        "gemini-3.1-pro-preview",
        "gemini-3.1-flash-image",
        "gemini-3-pro-image-preview",
    ]
    SUPPORTED_MODELS  = FREE_TIER_MODELS + PAID_TIER_MODELS
    DEPRECATED_MODELS = {
        "gemini-3-pro-preview":          "2026-03-09 종료 → gemini-3.1-pro-preview 사용",
        "gemini-2.0-flash":              "2026-06-01 종료 예정 → gemini-2.5-flash 권장",
        "gemini-2.0-flash-lite":         "2026-06-01 종료 예정 → gemini-2.5-flash-lite 권장",
        "gemini-2.5-flash-image-preview":"2026-01-15 종료",
    }

    def __init__(
        self,
        api_key:           str,
        model:             Optional[str] = None,
        dry_run:           bool  = False,
        web_search:        bool  = False,
        retry_count:       int   = 3,
        retry_delay:       float = 5.0,
        temperature:       float = 0.7,
        max_output_tokens: int   = 8192,
    ):
        super().__init__(api_key=api_key, model=model, dry_run=dry_run, web_search=web_search, retry_count=retry_count, retry_delay=retry_delay)
        self.temperature       = temperature
        self.max_output_tokens = max_output_tokens
        self._client           = None

        if self.model in self.DEPRECATED_MODELS:
            logger.warning("⚠️  '%s' 모델은 종료되었거나 종료 예정입니다. %s",
                           self.model, self.DEPRECATED_MODELS[self.model])

    @property
    def provider_name(self) -> str:
        return "Google Gemini"

    @property
    def default_model(self) -> str:
        return "gemini-2.5-flash"

    def _get_client(self):
        if self._client is None:
            if not GEMINI_AVAILABLE:
                raise ImportError(
                    "google-genai 패키지가 설치되지 않았습니다.\n"
                    "설치 명령어: pip install google-genai"
                )
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def _call_api(self, prompt: str) -> ChatResponse:
        start_time = time.time()
        try:
            client = self._get_client()

            # ── 웹 검색 도구 설정 ───────────────────────────
            tools = None
            if self.web_search:
                tools = [genai_types.Tool(google_search=genai_types.GoogleSearch())]
                logger.debug("Gemini: Google Search grounding 활성화")

            config = genai_types.GenerateContentConfig(
                temperature=self.temperature,
                max_output_tokens=self.max_output_tokens,
                tools=tools,
            )

            response = client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=config,
            )
            elapsed = time.time() - start_time

            # ── 토큰 사용량 ──────────────────────────────────
            tokens = None
            if hasattr(response, "usage_metadata") and response.usage_metadata:
                tokens = getattr(response.usage_metadata, "total_token_count", None)

            # ── 웹 검색 출처 추출 (grounding_metadata) ────────
            sources: list[SearchSource] = []
            search_used = False

            if self.web_search:
                try:
                    candidate = response.candidates[0] if response.candidates else None
                    if candidate and hasattr(candidate, "grounding_metadata"):
                        gm = candidate.grounding_metadata
                        if gm:
                            search_used = True
                            # grounding_chunks: 검색에 사용된 웹 페이지 목록
                            chunks = getattr(gm, "grounding_chunks", []) or []
                            for chunk in chunks:
                                web = getattr(chunk, "web", None)
                                if web:
                                    sources.append(SearchSource(
                                        title=getattr(web, "title", "") or "",
                                        url=getattr(web, "uri",   "") or "",
                                    ))
                            # 검색 쿼리 로그
                            queries = getattr(gm, "web_search_queries", []) or []
                            if queries:
                                logger.info("Gemini 검색 쿼리: %s", queries)
                except Exception as e:
                    logger.debug("grounding_metadata 추출 실패 (무시): %s", e)

            return ChatResponse(
                prompt=prompt,
                answer=response.text,
                model=self.model,
                provider=self.provider_name,
                tokens_used=tokens,
                elapsed_seconds=elapsed,
                web_search_used=search_used,
                search_sources=sources,
            )

        except Exception as e:
            elapsed    = time.time() - start_time
            error_msg  = str(e)
            hint       = self._error_hint(error_msg)
            return ChatResponse(
                prompt=prompt, answer="", model=self.model,
                provider=self.provider_name, elapsed_seconds=elapsed,
                error=error_msg + hint,
            )

    def _error_hint(self, error_msg: str) -> str:
        if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
            free_list = "\n".join(f"    • {m}" for m in self.FREE_TIER_MODELS)
            return (
                f"\n\n💡 할당량 초과:\n"
                f"  무료 티어 모델로 변경하세요:\n{free_list}\n"
                f"  또는 유료 플랜: https://ai.google.dev/pricing"
            )
        if "404" in error_msg or "NOT_FOUND" in error_msg:
            return (
                f"\n\n💡 모델을 찾을 수 없음:\n"
                f"  올바른 모델명 사용: --model gemini-2.5-flash"
            )
        return ""
