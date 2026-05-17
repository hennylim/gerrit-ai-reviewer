"""
ollama_ai.py
------------
Ollama 로컬 LLM AI 구현체.

Ollama를 사용하면 gemma4:e4b, llama3, mistral 등 다양한 오픈소스 모델을
로컬 환경에서 API 비용 없이 실행할 수 있습니다.

설치:
    # Ollama 설치 (macOS/Linux)
    curl -fsSL https://ollama.com/install.sh | sh

    # 모델 다운로드
    ollama pull gemma4:e4b        # Google Gemma 4 27B E4B (기본값)
    ollama pull gemma3:27b        # Gemma 3 27B
    ollama pull llama3.1:70b      # Meta LLaMA 3.1 70B
    ollama pull qwen2.5-coder:32b # Alibaba Qwen 2.5 Coder 32B (코드 특화)

공식 문서:
    https://github.com/ollama/ollama
    https://github.com/ollama/ollama/blob/main/docs/api.md

api_keys.json 설정:
    "ollama": {
        "api_key": "http://localhost:11434"   ← Ollama 서버 주소 (기본값)
    }

주의:
    - web_search 옵션은 Ollama에서 지원하지 않습니다 (경고 후 무시).
    - 모델 컨텍스트 크기(num_ctx)는 모델에 따라 다릅니다.
      gemma4:e4b 기준 128K 토큰을 지원합니다.
"""

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Optional

try:
    from duckduckgo_search import DDGS
    DDGS_AVAILABLE = True
except ImportError:
    DDGS_AVAILABLE = False

from .base_ai import BaseAI, ChatResponse, SearchSource

logger = logging.getLogger(__name__)

# Ollama API 기본 엔드포인트
_DEFAULT_HOST = "http://localhost:11434"
_CHAT_PATH    = "/api/chat"


class OllamaAI(BaseAI):
    """
    Ollama 로컬 LLM AI 제공자.

    ┌──────────────────────────────────────────────────────────────────┐
    │            추천 Ollama 모델 (코드 리뷰 목적)                      │
    ├────────────────────────────┬─────────────┬───────────────────────┤
    │ 모델명                      │ VRAM        │ 특징                  │
    ├────────────────────────────┼─────────────┼───────────────────────┤
    │ gemma4:e4b  ★기본값        │ ~8GB        │ Google Gemma 4 E4B    │
    │                            │             │ 128K ctx, 코드 이해력  │
    ├────────────────────────────┼─────────────┼───────────────────────┤
    │ gemma3:27b                 │ ~16GB       │ Gemma 3 27B           │
    │ qwen2.5-coder:32b          │ ~20GB       │ 코드 특화, 매우 강력   │
    │ llama3.1:70b               │ ~40GB       │ Meta LLaMA 3.1 70B   │
    │ deepseek-coder-v2:16b      │ ~10GB       │ 코드 특화 경량         │
    └────────────────────────────┴─────────────┴───────────────────────┘

    API 키 설정:
        api_keys.json 의 "api_key" 필드에 Ollama 서버 주소를 입력합니다.
        기본값: "http://localhost:11434"
        원격 서버: "http://192.168.1.100:11434"
    """

    _WEB_SEARCH_TOOL = {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "웹에서 최신 정보를 검색합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "검색어 (예: '오늘 서울 날씨')"
                    }
                },
                "required": ["query"]
            }
        }
    }

    def __init__(
        self,
        api_key:     str,                  # Ollama 서버 주소 (예: http://localhost:11434)
        model:       Optional[str] = None,
        dry_run:     bool  = False,
        web_search:  bool  = False,
        retry_count: int   = 3,
        retry_delay: float = 5.0,
        temperature: float | None = None,
        max_tokens:  int   = 8192,         # Ollama num_predict (출력 토큰 한도)
        num_ctx:     int   = 32768,        # Ollama 컨텍스트 윈도우 크기 (기본 32K)
        timeout:     int   = 300,          # 요청 타임아웃 (초) — 로컬 모델은 느릴 수 있음
    ):
        """
        Args:
            api_key:     Ollama 서버 주소. 비어있으면 http://localhost:11434 사용.
            model:       Ollama 모델명 (예: gemma4:e4b). None이면 기본값 사용.
            dry_run:     True이면 실제 Ollama 호출 없이 테스트 응답 반환.
            web_search:  Ollama는 웹 검색을 지원하지 않습니다 (설정 시 경고 출력).
            retry_count: 일시적 오류 발생 시 최대 재시도 횟수.
            retry_delay: 첫 재시도 대기 시간(초).
            temperature: 생성 무작위성 (0.0~1.0). None이면 DEFAULT_TEMPERATURE(0.2) 사용.
            max_tokens:  최대 출력 토큰 수 (Ollama의 num_predict).
            num_ctx:     컨텍스트 윈도우 크기. 모델이 지원하는 최대값 이하로 설정.
                         gemma4:e4b 기준 최대 128K. 크게 설정할수록 메모리 사용량 증가.
            timeout:     HTTP 요청 타임아웃(초). 로컬 모델은 응답이 느릴 수 있으므로
                         넉넉하게 설정하세요 (기본 300초 = 5분).
        """
        # api_key 필드를 host URL로 사용 (비어있으면 기본 localhost)
        host = (api_key or "").strip().rstrip("/") or _DEFAULT_HOST
        super().__init__(
            api_key=host,
            model=model,
            dry_run=dry_run,
            web_search=web_search,
            retry_count=retry_count,
            retry_delay=retry_delay,
            temperature=temperature,
        )
        self.host       = host
        self.max_tokens = max_tokens
        self.num_ctx    = num_ctx
        self.timeout    = timeout

        if web_search and not DDGS_AVAILABLE:
            logger.warning(
                "⚠️  duckduckgo-search 패키지가 설치되지 않아 웹 검색이 비활성화됩니다. "
                "pip install duckduckgo-search"
            )
            self.web_search = False

    # ── BaseAI 추상 메서드 구현 ───────────────────────────────────────────

    @property
    def provider_name(self) -> str:
        return "Ollama"

    @property
    def default_model(self) -> str:
        return "gemma4:e4b"

    # ── Ollama 연결 상태 확인 ─────────────────────────────────────────────

    def check_connection(self) -> tuple[bool, str]:
        """
        Ollama 서버 연결 상태를 확인합니다.

        Returns:
            (success: bool, message: str)
        """
        try:
            req = urllib.request.Request(
                f"{self.host}/api/tags",
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                models = [m["name"] for m in data.get("models", [])]
                return True, f"연결 성공. 설치된 모델: {models}"
        except urllib.error.URLError as e:
            return False, f"Ollama 서버에 연결할 수 없습니다: {e.reason}\n서버 주소: {self.host}"
        except Exception as e:
            return False, f"연결 확인 오류: {e}"

    def check_model(self) -> tuple[bool, str]:
        """
        설정된 모델이 Ollama에 설치되어 있는지 확인합니다.

        Returns:
            (available: bool, message: str)
        """
        try:
            req = urllib.request.Request(
                f"{self.host}/api/tags",
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data   = json.loads(resp.read().decode())
                models = [m["name"] for m in data.get("models", [])]
                # "gemma4:e4b" → exact match or prefix match ("gemma4")
                model_base = self.model.split(":")[0]
                found = any(
                    m == self.model or m.startswith(model_base + ":")
                    for m in models
                )
                if found:
                    return True, f"모델 '{self.model}' 사용 가능"
                return False, (
                    f"모델 '{self.model}'이 설치되지 않았습니다.\n"
                    f"설치 명령: ollama pull {self.model}\n"
                    f"설치된 모델: {models}"
                )
        except Exception as e:
            return False, f"모델 확인 오류: {e}"

    # ── 핵심 API 호출 ─────────────────────────────────────────────────────

    def _call_api(self, prompt: str) -> ChatResponse:
        """
        Ollama /api/chat 엔드포인트를 호출합니다.

        Ollama API 문서:
            POST /api/chat
            Body: {
                "model": "gemma4:e4b",
                "messages": [{"role": "user", "content": "..."}],
                "stream": false,
                "options": {
                    "temperature": 0.2,
                    "num_predict": 8192,
                    "num_ctx": 32768
                }
            }
        """
        start_time = time.time()

        url      = f"{self.host}{_CHAT_PATH}"
        messages = [{"role": "user", "content": prompt}]
        
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,   # 스트리밍 비활성화 (단일 응답 수신)
            "options": {
                "temperature": self.temperature,
                "num_predict": self.max_tokens,
                "num_ctx":     self.num_ctx,
            },
        }

        if self.web_search:
            payload["tools"] = [self._WEB_SEARCH_TOOL]

        search_sources: list[SearchSource] = []
        search_used = False

        def _do_request(curr_payload: dict) -> dict:
            body = json.dumps(curr_payload, ensure_ascii=False).encode("utf-8")
            req  = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            logger.debug(
                "Ollama 요청: model=%s, temperature=%.2f, num_ctx=%d, url=%s",
                self.model, self.temperature, self.num_ctx, url,
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)

        try:
            data = _do_request(payload)
            elapsed = time.time() - start_time

            # ── 응답 파싱 ──────────────────────────────────────────────────
            message = data.get("message", {})
            tool_calls = message.get("tool_calls", [])

            # ── 툴 호출(웹 검색) 처리 ──────────────────────────────────────
            if tool_calls and self.web_search:
                search_used = True
                messages.append(message)  # assistant의 tool_call 메시지 추가
                
                for tool_call in tool_calls:
                    function_name = tool_call.get("function", {}).get("name")
                    arguments = tool_call.get("function", {}).get("arguments", {})
                    
                    if function_name == "web_search":
                        query = arguments.get("query", "")
                        logger.info("Ollama 웹 검색 실행: %s", query)
                        
                        try:
                            results = DDGS().text(query, max_results=3)
                            search_text = ""
                            for r in results:
                                search_text += f"제목: {r.get('title')}\n내용: {r.get('body')}\n\n"
                                if r.get("href"):
                                    search_sources.append(SearchSource(title=r.get("title", ""), url=r.get("href", "")))
                            if not search_text:
                                search_text = "검색 결과가 없습니다."
                        except Exception as e:
                            logger.error("웹 검색 실패: %s", e)
                            search_text = f"웹 검색에 실패했습니다: {e}"
                        
                        messages.append({
                            "role": "tool",
                            "content": search_text,
                        })
                
                # 툴 결과를 포함하여 2차 요청
                payload["messages"] = messages
                payload.pop("tools", None)  # 2차 요청 시에는 도구 사용 비활성화
                
                data = _do_request(payload)
                elapsed = time.time() - start_time
                message = data.get("message", {})

            content = message.get("content", "").strip()

            if not content and not tool_calls:
                return ChatResponse(
                    prompt=prompt,
                    answer="",
                    model=self.model,
                    provider=self.provider_name,
                    error="Ollama 응답에 content가 없습니다.",
                    elapsed_seconds=elapsed,
                )

            # ── 토큰 사용량 ────────────────────────────────────────────────
            prompt_tokens = data.get("prompt_eval_count", 0)
            output_tokens = data.get("eval_count", 0)
            total_tokens  = prompt_tokens + output_tokens if prompt_tokens else output_tokens or None

            logger.debug(
                "Ollama 응답 완료: elapsed=%.1fs, 입력=%s tok, 출력=%s tok",
                elapsed,
                prompt_tokens or "?",
                output_tokens or "?",
            )

            return ChatResponse(
                prompt=prompt,
                answer=content,
                model=self.model,
                provider=self.provider_name,
                elapsed_seconds=elapsed,
                tokens_used=total_tokens,
                web_search_used=search_used,
                search_sources=search_sources,
            )

        except urllib.error.URLError as e:
            elapsed = time.time() - start_time
            reason  = getattr(e, "reason", str(e))
            err_msg = f"Ollama 서버 연결 실패: {reason}\n서버 주소: {self.host}"
            logger.error("Ollama URLError: %s", reason)
            return ChatResponse(
                prompt=prompt, answer="",
                model=self.model, provider=self.provider_name,
                error=err_msg, elapsed_seconds=elapsed,
            )

        except TimeoutError:
            elapsed = time.time() - start_time
            err_msg = (
                f"Ollama 응답 시간 초과 ({self.timeout}초). "
                f"timeout 설정을 늘리거나 num_ctx를 줄여보세요."
            )
            logger.error("Ollama Timeout: %s초 초과", self.timeout)
            return ChatResponse(
                prompt=prompt, answer="",
                model=self.model, provider=self.provider_name,
                error=err_msg, elapsed_seconds=elapsed,
            )

        except json.JSONDecodeError as e:
            elapsed = time.time() - start_time
            err_msg = f"Ollama 응답 JSON 파싱 실패: {e}"
            logger.error("Ollama JSON decode error: %s", e)
            return ChatResponse(
                prompt=prompt, answer="",
                model=self.model, provider=self.provider_name,
                error=err_msg, elapsed_seconds=elapsed,
            )

        except Exception as e:
            elapsed = time.time() - start_time
            err_msg = f"Ollama API 호출 오류: {type(e).__name__}: {e}"
            logger.error("Ollama unexpected error: %s", e, exc_info=True)
            return ChatResponse(
                prompt=prompt, answer="",
                model=self.model, provider=self.provider_name,
                error=err_msg, elapsed_seconds=elapsed,
            )

    # ── 재시도 판단 (Ollama 특화) ─────────────────────────────────────────

    def _is_retryable_error(self, error_msg: str) -> bool:
        """Ollama는 로컬 서버이므로 연결 실패도 재시도 대상입니다."""
        base_retryable = super()._is_retryable_error(error_msg)
        ollama_patterns = [
            "Connection refused",   # Ollama 서버 미실행
            "연결 실패",
            "timed out",
            "time out",
            "시간 초과",
            "model is being loaded", # 모델 로딩 중
        ]
        return base_retryable or any(p in error_msg for p in ollama_patterns)

    # ── 모델 나열 메서드 ──────────────────────────────────────────────────
    @classmethod
    def list_models(cls, host: str = DEFAULT_HOST) -> list[str]:
        """
        Ollama 서버에서 사용 가능한 모델 목록을 반환합니다.

        Args:
            host: Ollama 서버 주소

        Returns:
            모델명 리스트
        """
        try:
            resp = requests.get(f"{host}/api/tags", timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return [m["name"] for m in data.get("models", [])]
        except Exception:
            return []

    # 