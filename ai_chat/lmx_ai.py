"""
lmx_ai.py
---------
LMX AI 구현체.

OpenAI 호환 API를 제공하는 LMX 엔드포인트와 연동합니다.
표준 라이브러리(urllib)를 사용하여 의존성 없이 동작합니다.

api_keys.json 설정 예시:
    "lmx": {
        "api_key": "your_lmx_api_key",
        "host": "https://api.lmx.example.com/v1",
        "model": "your_lmx_model"
    }
"""

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Optional

from .base_ai import BaseAI, ChatResponse

logger = logging.getLogger(__name__)

# LMX API 기본값 (OpenAI 호환 형태라고 가정)
_DEFAULT_HOST      = "https://api.lmx.example.com/v1"
_CHAT_PATH         = "/chat/completions"
_DEFAULT_MODEL     = "lmx-default-model"


class LmxAI(BaseAI):
    """
    LMX AI 제공자.

    OpenAI 호환 API 엔드포인트로 통신합니다.
    """

    def __init__(
        self,
        api_key:        str,
        model:          Optional[str] = None,
        dry_run:        bool  = False,
        web_search:     bool  = False,
        retry_count:    int   = 3,
        retry_delay:    float = 5.0,
        temperature:    float = 0.2,
        max_tokens:     int   = 4096,
        timeout:        int   = 60,
        host:           Optional[str] = None,
    ):
        """
        Args:
            api_key:       LMX API Key
            model:         사용할 모델명
            dry_run:       실제 호출 여부
            web_search:    웹 검색 사용 여부
            retry_count:   재시도 횟수
            retry_delay:   재시도 대기 시간
            temperature:   생성 온도
            max_tokens:    최대 출력 토큰
            timeout:       요청 타임아웃
            host:          LMX API Host URL
        """
        super().__init__(
            api_key=api_key,
            model=model or _DEFAULT_MODEL,
            dry_run=dry_run,
            web_search=web_search,
            retry_count=retry_count,
            retry_delay=retry_delay,
            temperature=temperature,
        )
        self.max_tokens = max_tokens
        self.timeout    = timeout
        # 호스트 주소가 주어지지 않으면 기본값 사용, 끝의 '/' 제거
        self.host       = (host or _DEFAULT_HOST).rstrip("/")

        if web_search:
            logger.warning("LMX는 현재 web_search를 명시적으로 지원하지 않을 수 있습니다.")

    @property
    def provider_name(self) -> str:
        return "LMX"

    @property
    def default_model(self) -> str:
        return _DEFAULT_MODEL

    def _call_api(self, prompt: str) -> ChatResponse:
        start_time = time.time()

        messages = [
            {"role": "user", "content": prompt}
        ]

        payload = {
            "model":       self.model,
            "messages":    messages,
            "temperature": self.temperature,
            "max_tokens":  self.max_tokens,
            "stream":      False,
        }

        url = f"{self.host}{_CHAT_PATH}"

        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req  = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept":       "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                method="POST",
            )

            logger.debug(
                "LMX 요청: url=%s, model=%s, temperature=%.2f",
                url, self.model, self.temperature
            )

            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw  = resp.read().decode("utf-8")
                data = json.loads(raw)

            elapsed = time.time() - start_time

            choices = data.get("choices", [])
            if not choices:
                return ChatResponse(
                    prompt=prompt, answer="", model=self.model,
                    provider=self.provider_name, elapsed_seconds=elapsed,
                    error="LMX 응답에 choices가 없습니다.",
                )

            content = choices[0].get("message", {}).get("content", "").strip()
            
            usage        = data.get("usage", {})
            total_tokens = usage.get("total_tokens") or None

            return ChatResponse(
                prompt=prompt,
                answer=content,
                model=self.model,
                provider=self.provider_name,
                elapsed_seconds=elapsed,
                tokens_used=total_tokens,
            )

        except urllib.error.HTTPError as e:
            elapsed   = time.time() - start_time
            body_text = ""
            try:
                body_text = e.read().decode("utf-8")[:200]
            except Exception:
                pass
            err_msg = f"LMX API HTTP 오류 {e.code}: {e.reason}\n응답: {body_text}"
            logger.error("LMX HTTPError %d: %s", e.code, body_text)
            return ChatResponse(
                prompt=prompt, answer="", model=self.model,
                provider=self.provider_name, elapsed_seconds=elapsed,
                error=err_msg,
            )

        except urllib.error.URLError as e:
            elapsed = time.time() - start_time
            reason  = getattr(e, "reason", str(e))
            err_msg = f"LMX 서버 연결 실패: {reason}\n서버 주소: {self.host}"
            logger.error("LMX URLError: %s", reason)
            return ChatResponse(
                prompt=prompt, answer="", model=self.model,
                provider=self.provider_name, elapsed_seconds=elapsed,
                error=err_msg,
            )
            
        except TimeoutError:
            elapsed = time.time() - start_time
            err_msg = f"LMX 응답 시간 초과 ({self.timeout}초)."
            logger.error("LMX Timeout: %d초 초과", self.timeout)
            return ChatResponse(
                prompt=prompt, answer="", model=self.model,
                provider=self.provider_name, elapsed_seconds=elapsed,
                error=err_msg,
            )

        except Exception as e:
            elapsed = time.time() - start_time
            err_msg = f"LMX API 호출 오류: {type(e).__name__}: {e}"
            logger.error("LMX 예외: %s", e, exc_info=True)
            return ChatResponse(
                prompt=prompt, answer="", model=self.model,
                provider=self.provider_name, elapsed_seconds=elapsed,
                error=err_msg,
            )
