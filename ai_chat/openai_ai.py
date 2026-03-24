"""
openai_ai.py
------------
OpenAI GPT AI 구현체.
웹 검색: Chat Completions + web_search_options (전용 검색 모델)

설치: pip install openai
공식 문서: https://platform.openai.com/docs/guides/tools-web-search
"""

import logging
import time
from typing import Optional

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

from .base_ai import BaseAI, ChatResponse, SearchSource

logger = logging.getLogger(__name__)


class OpenAIChat(BaseAI):
    """
    OpenAI GPT 제공자.

    ┌──────────────────────────────────────────────────────────────┐
    │              OpenAI 모델 목록 (2026-03)                       │
    ├──────────────────────────┬───────────────────────────────────┤
    │ 모델명                    │ 특징                              │
    ├──────────────────────────┼───────────────────────────────────┤
    │ ── GPT-5 (최신) ──────── │                                   │
    │ gpt-5.4  ★기본값         │ 최신 플래그십, 추론+코딩+비전     │
    │ gpt-5.3 / 5.2 / 5.1     │ 이전 플래그십 (API 유지)          │
    │ gpt-5                    │ GPT-5 초기 릴리즈                 │
    │ gpt-5-mini               │ 빠르고 저렴                       │
    │ ── GPT-4.1 ────────────  │                                   │
    │ gpt-4.1                  │ 코딩·지시 이행, 1M 컨텍스트       │
    │ gpt-4.1-mini             │ 경량, 파인튜닝 지원               │
    │ gpt-4.1-nano             │ 최경량, 고속                      │
    │ ── GPT-4o ─────────────  │                                   │
    │ gpt-4o                   │ 멀티모달 (텍스트+오디오)          │
    │ gpt-4o-mini              │ 경량, 무료 티어                   │
    │ ── 추론(Reasoning) ─────  │                                   │
    │ o3 / o3-pro / o4-mini    │ 수학·과학·코딩 특화               │
    │ o1-pro                   │ o1 Pro 추론                       │
    │ ── 오픈웨이트 ──────────── │                                   │
    │ gpt-oss-120b / gpt-oss-20b│ Apache 2.0, 셀프호스팅           │
    └──────────────────────────┴───────────────────────────────────┘

    🔍 웹 검색 (--web-search):
        Chat Completions API에서 web_search_options={} 를 지원하는
        전용 검색 모델을 자동으로 선택합니다.

        검색 모델 우선순위:
          1. 사용자가 이미 검색 모델 지정 시 → 그대로 사용
          2. gpt-5-search-api  (gpt-5 계열 기본)
          3. gpt-4o-search-preview (gpt-4o 계열 또는 fallback)

        ※ gpt-4.1-nano, 순수 추론 모델(o3 등)은 웹 검색 미지원.
        ※ 검색 결과의 url_citation annotation 에서 출처를 추출합니다.
    """

    GPT5_MODELS        = ["gpt-5.4", "gpt-5.3", "gpt-5.2", "gpt-5.1", "gpt-5", "gpt-5-mini"]
    GPT41_MODELS       = ["gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano"]
    GPT4O_MODELS       = ["gpt-4o", "gpt-4o-mini"]
    REASONING_MODELS   = ["o3", "o3-pro", "o4-mini", "o1-pro"]
    OPEN_WEIGHT_MODELS = ["gpt-oss-120b", "gpt-oss-20b"]
    SUPPORTED_MODELS   = (
        GPT5_MODELS + GPT41_MODELS + GPT4O_MODELS +
        REASONING_MODELS + OPEN_WEIGHT_MODELS
    )

    # Chat Completions API에서 web_search_options 를 지원하는 전용 모델
    SEARCH_MODELS = [
        "gpt-5-search-api",
        "gpt-4o-search-preview",
        "gpt-4o-mini-search-preview",
    ]

    def __init__(
        self,
        api_key:     str,
        model:       Optional[str] = None,
        dry_run:     bool  = False,
        web_search:  bool  = False,
        retry_count: int   = 3,
        retry_delay: float = 5.0,
        temperature: float = 0.7,
        max_tokens:  int   = 4096,
    ):
        super().__init__(api_key=api_key, model=model, dry_run=dry_run, web_search=web_search, retry_count=retry_count, retry_delay=retry_delay)
        self.temperature = temperature
        self.max_tokens  = max_tokens
        self._client     = None

    @property
    def provider_name(self) -> str:
        return "OpenAI"

    @property
    def default_model(self) -> str:
        return "gpt-5.4"

    def _get_client(self):
        if self._client is None:
            if not OPENAI_AVAILABLE:
                raise ImportError(
                    "openai 패키지가 설치되지 않았습니다.\n"
                    "설치 명령어: pip install openai"
                )
            self._client = OpenAI(api_key=self.api_key)
        return self._client

    def _resolve_search_model(self) -> str:
        """
        웹 검색에 사용할 모델명을 결정합니다.

        - 사용자가 이미 검색 모델을 지정했으면 그대로 반환
        - gpt-5 계열 → gpt-5-search-api
        - 그 외 → gpt-4o-search-preview
        """
        if self.model in self.SEARCH_MODELS:
            return self.model
        if self.model in self.GPT5_MODELS:
            logger.info(
                "웹 검색 활성화: '%s' → 'gpt-5-search-api' 로 전환합니다.", self.model
            )
            return "gpt-5-search-api"
        logger.info(
            "웹 검색 활성화: '%s' → 'gpt-4o-search-preview' 로 전환합니다.", self.model
        )
        return "gpt-4o-search-preview"

    def _call_api(self, prompt: str) -> ChatResponse:
        start_time = time.time()
        try:
            client = self._get_client()

            # ── 웹 검색 모드 ──────────────────────────────────
            if self.web_search:
                actual_model = self._resolve_search_model()
                kwargs: dict = {
                    "model":              actual_model,
                    "messages":           [{"role": "user", "content": prompt}],
                    "max_tokens":         self.max_tokens,
                    "web_search_options": {},        # 검색 활성화
                }
            else:
                actual_model = self.model
                kwargs = {
                    "model":    actual_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": self.max_tokens,
                }
                # o-시리즈 추론 모델은 temperature 미지원
                if actual_model not in self.REASONING_MODELS:
                    kwargs["temperature"] = self.temperature

            response = client.chat.completions.create(**kwargs)
            elapsed  = time.time() - start_time

            answer = response.choices[0].message.content or ""
            tokens = response.usage.total_tokens if response.usage else None

            # ── 웹 검색 출처 추출 (url_citation annotations) ──
            sources: list[SearchSource] = []
            if self.web_search:
                annotations = getattr(response.choices[0].message, "annotations", None) or []
                seen_urls: set[str] = set()
                for ann in annotations:
                    if getattr(ann, "type", "") == "url_citation":
                        cit = getattr(ann, "url_citation", None)
                        if cit:
                            url   = getattr(cit, "url",   "") or ""
                            title = getattr(cit, "title", "") or ""
                            if url and url not in seen_urls:
                                sources.append(SearchSource(title=title, url=url))
                                seen_urls.add(url)

                if sources:
                    logger.info("OpenAI 웹 검색 출처 %d개 추출", len(sources))

            return ChatResponse(
                prompt=prompt,
                answer=answer,
                model=actual_model,
                provider=self.provider_name,
                tokens_used=tokens,
                elapsed_seconds=elapsed,
                web_search_used=self.web_search,
                search_sources=sources,
            )

        except Exception as e:
            elapsed   = time.time() - start_time
            error_msg = str(e)
            hint      = _openai_error_hint(error_msg)
            return ChatResponse(
                prompt=prompt, answer="", model=self.model,
                provider=self.provider_name, elapsed_seconds=elapsed,
                error=error_msg + hint,
            )


def _openai_error_hint(error_msg: str) -> str:
    if "insufficient_quota" in error_msg:
        return (
            "\n\n💳 [크레딧 없음] OpenAI 계정에 크레딧이 없습니다.\n"
            "  해결: https://platform.openai.com/settings/billing"
        )
    if "429" in error_msg or "rate_limit" in error_msg:
        return "\n\n⏳ [속도 제한] 잠시 후 재시도하거나 경량 모델로 변경하세요."
    if "401" in error_msg or "invalid_api_key" in error_msg:
        return "\n\n🔑 [인증 오류] config/api_keys.json 의 openai.api_key 를 확인하세요."
    if "404" in error_msg or "model_not_found" in error_msg:
        return "\n\n❓ [모델 없음] --model gpt-4.1 또는 --model gpt-4o 를 사용해 보세요."
    return ""
