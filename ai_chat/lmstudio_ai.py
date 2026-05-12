"""
lmstudio_ai.py
--------------
LM Studio 로컬 LLM AI 구현체.

LM Studio는 OpenAI 호환 REST API를 제공합니다.
로컬 또는 네트워크 상의 LM Studio 서버에 연결하여
다양한 GGUF 모델을 API 비용 없이 실행할 수 있습니다.

설치:
    1. https://lmstudio.ai 에서 LM Studio 다운로드 및 설치
    2. 원하는 모델 다운로드 (Discover 탭)
    3. Local Server 탭 → "Start Server" 클릭
    4. 기본 서버 주소: http://localhost:1234

api_keys.json 설정:
    "lmstudio": {
        "api_key": "http://localhost:1234",   ← LM Studio 서버 주소
        "model":   ""                         ← 비어있으면 서버의 로드된 모델 자동 사용
    }

    원격 서버 사용:
    "lmstudio": {
        "api_key": "http://192.168.1.100:1234"
    }

추천 모델 (코드 리뷰 목적):
    ┌──────────────────────────────────────┬──────────┬──────────────────────────┐
    │ 모델명                                │ VRAM     │ 특징                     │
    ├──────────────────────────────────────┼──────────┼──────────────────────────┤
    │ Qwen2.5-Coder-7B-Instruct-GGUF       │ ~5GB     │ 코드 특화, 경량           │
    │ Qwen2.5-Coder-32B-Instruct-GGUF      │ ~20GB    │ 코드 특화, 강력           │
    │ Meta-Llama-3.1-8B-Instruct-GGUF      │ ~5GB     │ 범용, 균형                │
    │ Meta-Llama-3.1-70B-Instruct-GGUF     │ ~40GB    │ 범용, 고성능              │
    │ DeepSeek-Coder-V2-Lite-Instruct-GGUF │ ~10GB    │ 코드 특화                 │
    │ gemma-3-12b-it-GGUF                  │ ~8GB     │ Google Gemma 3 12B       │
    └──────────────────────────────────────┴──────────┴──────────────────────────┘

주의:
    - web_search 옵션은 LM Studio에서 지원하지 않습니다 (경고 후 무시).
    - 서버가 실행 중이어야 합니다 (Local Server 탭 → Start Server).
    - 모델이 로드되어 있어야 합니다 (모델 선택 후 Load 클릭).
    - model 을 비워두면 현재 로드된 모델을 자동 감지합니다.
"""

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Optional

from .base_ai import BaseAI, ChatResponse

logger = logging.getLogger(__name__)

# LM Studio 기본값
_DEFAULT_HOST      = "http://localhost:1234"
_CHAT_PATH         = "/v1/chat/completions"
_MODELS_PATH       = "/v1/models"
_AUTO_MODEL        = "auto"   # 서버 로드 모델 자동 감지 플래그


class LMStudioAI(BaseAI):
    """
    LM Studio 로컬 LLM AI 제공자.

    LM Studio의 OpenAI 호환 API(/v1/chat/completions)를 사용합니다.
    외부 SDK 없이 표준 라이브러리(urllib)만으로 동작합니다.

    API 키 설정:
        api_keys.json 의 "api_key" 필드에 LM Studio 서버 주소를 입력합니다.
        기본값: "http://localhost:1234"
        원격:   "http://192.168.1.100:1234"

    모델 설정:
        - 비어있거나 "auto": 서버에서 현재 로드된 모델을 자동 감지
        - 명시 지정: 해당 모델 identifier를 그대로 사용
          (예: "lmstudio-community/Meta-Llama-3.1-8B-Instruct-GGUF/...")
    """

    def __init__(
        self,
        api_key:        str,              # LM Studio 서버 주소 (예: http://localhost:1234)
        model:          Optional[str] = None,
        dry_run:        bool  = False,
        web_search:     bool  = False,
        retry_count:    int   = 3,
        retry_delay:    float = 5.0,
        temperature:    float | None = None,
        max_tokens:     int   = 8192,     # 최대 출력 토큰
        timeout:        int   = 300,      # HTTP 타임아웃 (초) — 로컬 모델은 느릴 수 있음
        system_prompt:  str   = "",       # 시스템 프롬프트 (빈 문자열이면 생략)
    ):
        """
        Args:
            api_key:       LM Studio 서버 주소. 비어있으면 http://localhost:1234 사용.
            model:         모델 identifier. None/"auto"이면 로드된 모델 자동 감지.
            dry_run:       True이면 실제 API 호출 없이 테스트 응답 반환.
            web_search:    LM Studio는 웹 검색 미지원 (설정 시 경고 출력).
            retry_count:   일시적 오류 발생 시 최대 재시도 횟수.
            retry_delay:   첫 재시도 대기 시간(초).
            temperature:   생성 무작위성 (0.0~1.0). None이면 DEFAULT_TEMPERATURE(0.2) 사용.
            max_tokens:    최대 출력 토큰 수.
            timeout:       HTTP 요청 타임아웃(초).
            system_prompt: 모든 요청에 앞서 삽입할 시스템 프롬프트.
                           코드 리뷰 컨텍스트 설정에 유용합니다.
        """
        host = (api_key or "").strip().rstrip("/") or _DEFAULT_HOST
        # model이 비어있거나 None이면 자동 감지(_AUTO_MODEL)로 설정
        resolved_model = (model or "").strip() or _AUTO_MODEL

        super().__init__(
            api_key=host,
            model=resolved_model,
            dry_run=dry_run,
            web_search=web_search,
            retry_count=retry_count,
            retry_delay=retry_delay,
            temperature=temperature,
        )
        self.host          = host
        self.max_tokens    = max_tokens
        self.timeout       = timeout
        self.system_prompt = system_prompt

        # 자동 감지된 실제 모델명 캐시 (최초 호출 시 설정)
        self._resolved_model: Optional[str] = None

        if web_search:
            logger.warning(
                "⚠️  LM Studio는 웹 검색(web_search)을 지원하지 않습니다. "
                "해당 옵션은 무시됩니다."
            )

    # ── BaseAI 추상 메서드 ────────────────────────────────────────────────────

    @property
    def provider_name(self) -> str:
        return "LM Studio"

    @property
    def default_model(self) -> str:
        return _AUTO_MODEL

    # ── 서버 연결 및 모델 확인 ────────────────────────────────────────────────

    def _http_get(self, path: str, timeout: int = 10) -> dict:
        """LM Studio 서버에 GET 요청을 보냅니다."""
        req = urllib.request.Request(
            f"{self.host}{path}",
            method="GET",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_loaded_models(self) -> list[str]:
        """
        LM Studio 서버에 현재 로드된 모델 목록을 반환합니다.

        Returns:
            모델 identifier 문자열 목록. 예:
            ["lmstudio-community/Meta-Llama-3.1-8B-Instruct-GGUF/..."]
        """
        try:
            data = self._http_get(_MODELS_PATH)
            return [m.get("id", "") for m in data.get("data", []) if m.get("id")]
        except Exception as exc:
            logger.debug("모델 목록 조회 실패: %s", exc)
            return []

    def _get_active_model(self) -> str:
        """
        사용할 실제 모델명을 반환합니다.

        - model이 "auto"이면 서버의 첫 번째 로드된 모델을 반환
        - 명시된 경우 그대로 반환
        """
        if self._resolved_model:
            return self._resolved_model

        if self.model != _AUTO_MODEL:
            self._resolved_model = self.model
            return self._resolved_model

        # 자동 감지
        models = self.get_loaded_models()
        if not models:
            logger.warning(
                "LM Studio 서버에 로드된 모델이 없습니다. "
                "LM Studio에서 모델을 선택하고 Load 하세요. "
                "서버 주소: %s", self.host
            )
            return "unknown"

        detected = models[0]
        logger.info("LM Studio 로드된 모델 자동 감지: '%s'", detected)
        if len(models) > 1:
            logger.debug("로드된 모델 전체: %s", models)
        self._resolved_model = detected
        return detected

    def check_connection(self) -> tuple[bool, str]:
        """
        LM Studio 서버 연결 상태를 확인합니다.

        Returns:
            (success: bool, message: str)
        """
        try:
            data   = self._http_get(_MODELS_PATH)
            models = [m.get("id", "") for m in data.get("data", [])]
            if models:
                return True, f"연결 성공. 로드된 모델: {models}"
            return True, "연결 성공. 로드된 모델 없음 (LM Studio에서 모델을 로드하세요)"
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", str(e))
            return False, (
                f"LM Studio 서버에 연결할 수 없습니다: {reason}\n"
                f"서버 주소: {self.host}\n"
                f"LM Studio → Local Server → Start Server 버튼을 눌러주세요."
            )
        except Exception as e:
            return False, f"연결 확인 오류: {e}"

    def check_model(self) -> tuple[bool, str]:
        """
        설정된 모델이 LM Studio에 로드되어 있는지 확인합니다.

        Returns:
            (available: bool, message: str)
        """
        models = self.get_loaded_models()
        if not models:
            return False, (
                f"LM Studio에 로드된 모델이 없습니다.\n"
                f"LM Studio → 모델 선택 → Load Model 을 클릭하세요."
            )

        if self.model == _AUTO_MODEL:
            return True, f"자동 감지 모드. 로드된 모델: {models[0]}"

        # 명시된 모델이 로드됐는지 확인 (부분 매칭 포함)
        found = any(self.model in m or m.endswith(self.model) for m in models)
        if found:
            return True, f"모델 '{self.model}' 사용 가능"
        return False, (
            f"모델 '{self.model}'이 로드되지 않았습니다.\n"
            f"현재 로드된 모델: {models}\n"
            f"model을 비워두면 자동으로 로드된 모델을 사용합니다."
        )

    # ── 핵심 API 호출 ─────────────────────────────────────────────────────────

    def _call_api(self, prompt: str) -> ChatResponse:
        """
        LM Studio /v1/chat/completions 엔드포인트를 호출합니다.

        OpenAI 호환 Chat Completions API:
            POST /v1/chat/completions
            {
                "model": "lmstudio-community/...",
                "messages": [
                    {"role": "system", "content": "..."},  // 선택
                    {"role": "user",   "content": "..."}
                ],
                "temperature": 0.2,
                "max_tokens": 8192,
                "stream": false
            }
        """
        start_time  = time.time()
        active_model = self._get_active_model()

        # ── 메시지 구성 ───────────────────────────────────────────────────────
        messages: list[dict] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model":       active_model,
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
                    # LM Studio는 인증 불필요이지만 일부 설정에서 필요할 수 있음
                    "Authorization": "Bearer lm-studio",
                },
                method="POST",
            )

            logger.debug(
                "LM Studio 요청: model=%s, temperature=%.2f, max_tokens=%d",
                active_model, self.temperature, self.max_tokens,
            )

            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw  = resp.read().decode("utf-8")
                data = json.loads(raw)

            elapsed = time.time() - start_time

            # ── 응답 파싱 (OpenAI 호환 형식) ─────────────────────────────────
            # {
            #   "id": "chatcmpl-...",
            #   "choices": [{"message": {"role": "assistant", "content": "..."}}],
            #   "usage": {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N}
            # }
            choices = data.get("choices", [])
            if not choices:
                return ChatResponse(
                    prompt=prompt, answer="", model=active_model,
                    provider=self.provider_name, elapsed_seconds=elapsed,
                    error="LM Studio 응답에 choices가 없습니다.",
                )

            content = choices[0].get("message", {}).get("content", "").strip()
            if not content:
                return ChatResponse(
                    prompt=prompt, answer="", model=active_model,
                    provider=self.provider_name, elapsed_seconds=elapsed,
                    error="LM Studio 응답 content가 비어있습니다.",
                )

            # ── 토큰 사용량 ───────────────────────────────────────────────────
            usage        = data.get("usage", {})
            total_tokens = usage.get("total_tokens") or None

            # finish_reason 확인 (length = max_tokens 초과 잘림)
            finish_reason = choices[0].get("finish_reason", "")
            if finish_reason == "length":
                logger.warning(
                    "LM Studio 응답이 max_tokens(%d)에 의해 잘렸습니다. "
                    "max_tokens 값을 늘리거나 프롬프트를 줄이세요.",
                    self.max_tokens,
                )

            logger.debug(
                "LM Studio 응답: elapsed=%.1fs, tokens=%s, finish=%s",
                elapsed, total_tokens or "?", finish_reason,
            )

            return ChatResponse(
                prompt=prompt,
                answer=content,
                model=active_model,
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
            err_msg = (
                f"LM Studio API HTTP 오류 {e.code}: {e.reason}\n"
                f"응답: {body_text}"
            )
            logger.error("LM Studio HTTPError %d: %s", e.code, body_text)
            return ChatResponse(
                prompt=prompt, answer="", model=active_model,
                provider=self.provider_name, elapsed_seconds=elapsed,
                error=err_msg,
            )

        except urllib.error.URLError as e:
            elapsed = time.time() - start_time
            reason  = getattr(e, "reason", str(e))
            err_msg = (
                f"LM Studio 서버 연결 실패: {reason}\n"
                f"서버 주소: {self.host}\n"
                f"LM Studio → Local Server → Start Server 를 확인하세요."
            )
            logger.error("LM Studio URLError: %s", reason)
            return ChatResponse(
                prompt=prompt, answer="", model=active_model,
                provider=self.provider_name, elapsed_seconds=elapsed,
                error=err_msg,
            )

        except TimeoutError:
            elapsed = time.time() - start_time
            err_msg = (
                f"LM Studio 응답 시간 초과 ({self.timeout}초).\n"
                f"timeout 설정을 늘리거나 더 가벼운 모델을 사용하세요."
            )
            logger.error("LM Studio Timeout: %d초 초과", self.timeout)
            return ChatResponse(
                prompt=prompt, answer="", model=active_model,
                provider=self.provider_name, elapsed_seconds=elapsed,
                error=err_msg,
            )

        except json.JSONDecodeError as e:
            elapsed = time.time() - start_time
            err_msg = f"LM Studio 응답 JSON 파싱 실패: {e}"
            logger.error("LM Studio JSON 파싱 오류: %s", e)
            return ChatResponse(
                prompt=prompt, answer="", model=active_model,
                provider=self.provider_name, elapsed_seconds=elapsed,
                error=err_msg,
            )

        except Exception as e:
            elapsed = time.time() - start_time
            err_msg = f"LM Studio API 호출 오류: {type(e).__name__}: {e}"
            logger.error("LM Studio 예외: %s", e, exc_info=True)
            return ChatResponse(
                prompt=prompt, answer="", model=active_model,
                provider=self.provider_name, elapsed_seconds=elapsed,
                error=err_msg,
            )

    # ── 재시도 판단 (LM Studio 특화) ─────────────────────────────────────────

    def _is_retryable_error(self, error_msg: str) -> bool:
        """LM Studio 특화 재시도 판단. 서버 연결 실패도 재시도 대상."""
        base_retryable = super()._is_retryable_error(error_msg)
        lmstudio_patterns = [
            "Connection refused",    # 서버 미실행
            "연결 실패",
            "timed out",
            "time out",
            "시간 초과",
            "model is loading",      # 모델 로딩 중
            "No model loaded",       # 모델 미로드
        ]
        return base_retryable or any(p in error_msg for p in lmstudio_patterns)
