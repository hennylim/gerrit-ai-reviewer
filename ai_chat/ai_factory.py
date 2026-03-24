"""
ai_factory.py
-------------
AI 제공자를 선택하고 생성하는 팩토리 모듈.
외부 JSON 파일에서 API 키를 로드합니다.

새 AI 추가 방법:
    1. 새 AI 클래스 파일 생성 (base_ai.BaseAI 상속)
    2. 이 파일의 AI_REGISTRY 에 등록
    3. api_keys.json 에 키 항목 추가
"""

import json
import logging
from pathlib import Path
from typing import Optional, Type, Dict, Any

from .base_ai import BaseAI
from .gemini_ai import GeminiAI
from .openai_ai import OpenAIChat
from .claude_ai import ClaudeAI

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# AI 레지스트리: 새 AI 추가 시 여기에 등록하세요
# key: api_keys.json 의 provider 키 이름 (소문자)
# ──────────────────────────────────────────────
AI_REGISTRY: Dict[str, Type[BaseAI]] = {
    "gemini":    GeminiAI,
    "openai":    OpenAIChat,
    "claude":    ClaudeAI,
    # "mistral": MistralAI,  ← 새 AI 추가 예시
}


def load_api_keys(config_path: str | Path) -> Dict[str, Any]:
    """
    JSON 파일에서 API 키 설정을 로드합니다.

    Args:
        config_path: api_keys.json 파일 경로

    Returns:
        파싱된 설정 딕셔너리

    Raises:
        FileNotFoundError: 파일이 없을 때
        json.JSONDecodeError: JSON 형식 오류
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"API 키 파일을 찾을 수 없습니다: {path.absolute()}")

    with path.open("r", encoding="utf-8") as f:
        keys = json.load(f)

    logger.debug("API 키 파일 로드 완료: %s", path)
    return keys


def list_providers() -> list[str]:
    """등록된 AI 제공자 목록을 반환합니다."""
    return list(AI_REGISTRY.keys())


def create_ai(
    provider:    str,
    config_path: str | Path = "config/api_keys.json",
    model:       Optional[str] = None,
    dry_run:     bool  = False,
    web_search:  bool  = False,
    retry_count: int   = 3,
    retry_delay: float = 5.0,
    **kwargs,
) -> BaseAI:
    """
    AI 제공자 인스턴스를 생성합니다.

    Args:
        provider:    AI 제공자 이름 (예: 'gemini', 'openai', 'claude')
        config_path: API 키 JSON 파일 경로
        model:       사용할 모델명 (None이면 기본값 사용)
        dry_run:     테스트 모드 여부
        web_search:  실시간 웹 검색 활성화 여부
        retry_count: 일시적 오류 시 최대 재시도 횟수 (기본 3)
        retry_delay: 첫 재시도 대기 시간(초), 이후 2배씩 증가 (기본 5초)
        **kwargs:    각 AI 클래스의 추가 파라미터

    Returns:
        BaseAI 구현체 인스턴스

    Raises:
        ValueError: 등록되지 않은 provider
        KeyError: api_keys.json 에 해당 provider 키 없음
    """
    provider_lower = provider.lower()

    if provider_lower not in AI_REGISTRY:
        available = ", ".join(list_providers())
        raise ValueError(
            f"지원하지 않는 AI 제공자: '{provider}'\n"
            f"사용 가능한 제공자: {available}"
        )

    # API 키 로드
    keys = load_api_keys(config_path)

    if provider_lower not in keys:
        raise KeyError(
            f"api_keys.json 에 '{provider_lower}' 항목이 없습니다."
        )

    api_key = keys[provider_lower].get("api_key", "")
    if not api_key or api_key.startswith("YOUR_"):
        if not dry_run:
            logger.warning(
                "'%s' API 키가 설정되지 않았습니다. --dry-run 모드를 사용하세요.", provider
            )

    ai_class = AI_REGISTRY[provider_lower]
    instance = ai_class(
        api_key=api_key, model=model,
        dry_run=dry_run, web_search=web_search,
        retry_count=retry_count, retry_delay=retry_delay,
        **kwargs,
    )

    logger.debug(
        "AI 인스턴스 생성: provider=%s, model=%s, dry_run=%s, web_search=%s, retry=%d*%.0fs",
        provider_lower, instance.model, dry_run, web_search, retry_count, retry_delay
    )
    return instance
