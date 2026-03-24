"""
ai_chat 패키지
--------------
재사용 가능한 AI 채팅 라이브러리.

지원 AI 제공자:
    - gemini  : Google Gemini (google-genai)
    - openai  : OpenAI GPT (openai)
    - claude  : Anthropic Claude (anthropic)

빠른 사용 예시:
    from ai_chat import create_ai

    ai = create_ai("gemini", config_path="config/api_keys.json")
    response = ai.chat("파이썬의 장점은 무엇인가요?")
    print(response.answer)
    print(response.to_markdown())
"""

from .base_ai import BaseAI, ChatResponse
from .gemini_ai import GeminiAI
from .openai_ai import OpenAIChat
from .claude_ai import ClaudeAI
from .ai_factory import create_ai, load_api_keys, list_providers

__all__ = [
    "BaseAI",
    "ChatResponse",
    "GeminiAI",
    "OpenAIChat",
    "ClaudeAI",
    "create_ai",
    "load_api_keys",
    "list_providers",
]
