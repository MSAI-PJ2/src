"""운영 LLM 클라이언트 — Azure OpenAI Chat Completions / 로컬 OpenAI 호환 서버.

LLM_PROVIDER 로 전환한다:
    azure (azure_openai)   Azure OpenAI 배포 (운영 — 현재 gpt-4.1-mini)
    local (nemotron)       로컬 vLLM 등 OpenAI 호환 서버 (무료 개발/데모)

운영 경로는 chat_stream_async (FastAPI 이벤트루프를 막지 않는 async 스트리밍).
동기 chat/chat_stream 은 스크립트·스모크 테스트용으로 남긴다.

과거 프로토타입 경로(azure_responses/GPT-5, model_router, chat_json 구조화 출력)는
llm_client_legacy.py 로 격리했다 — 운영 코드에서 import 하지 않는다.
"""
from __future__ import annotations

import os

from openai import AsyncAzureOpenAI, AsyncOpenAI, AzureOpenAI, OpenAI

LOCAL_PROVIDER_ALIASES = {"local", "nemotron"}
AZURE_PROVIDER_ALIASES = {"azure", "azure_openai"}


def _int_env(name: str) -> int | None:
    raw = os.getenv(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _positive_int(value: int | None, fallback: int) -> int:
    if value is None:
        return fallback
    try:
        value = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(1, value)


class LLMClient:
    def __init__(self, provider: str | None = None):
        raw = (provider or os.getenv("LLM_PROVIDER", "local")).strip().lower()

        if raw in LOCAL_PROVIDER_ALIASES:
            self.provider = "local"
            self.model = os.getenv("LOCAL_LLM_MODEL", "nemotron-3-super-no-think")
            base_url = os.getenv("LOCAL_LLM_BASE_URL", "http://192.168.1.155:8002/v1")
            api_key = os.getenv("LOCAL_LLM_API_KEY", "dummy")
            self.client = OpenAI(base_url=base_url, api_key=api_key)
            self.async_client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        elif raw in AZURE_PROVIDER_ALIASES:
            self.provider = "azure"
            endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
            api_key = os.getenv("AZURE_OPENAI_API_KEY")
            api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
            deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT")
            missing = [
                name
                for name, value in {
                    "AZURE_OPENAI_ENDPOINT": endpoint,
                    "AZURE_OPENAI_API_KEY": api_key,
                    "AZURE_OPENAI_DEPLOYMENT": deployment,
                }.items()
                if not value
            ]
            if missing:
                raise ValueError("Azure provider missing env vars: " + ", ".join(missing))
            self.model = deployment
            self.client = AzureOpenAI(azure_endpoint=endpoint, api_key=api_key, api_version=api_version)
            self.async_client = AsyncAzureOpenAI(azure_endpoint=endpoint, api_key=api_key, api_version=api_version)
        else:
            raise ValueError(
                f"Unsupported LLM_PROVIDER '{raw}' (use: local, nemotron, azure, azure_openai). "
                "azure_responses/model_router 등 프로토타입 경로는 common/llm_client_legacy.py 참고"
            )

    def _completion_token_limit(self, requested: int | None, fallback: int) -> int:
        """요청 토큰 상한을 서버측 한도 안에서 결정한다.

        AZURE_OPENAI_MAX_COMPLETION_TOKENS 가 기본값이자(별도 *_LIMIT 이 없으면)
        상한이다. 요청 값은 상한까지만 반영된다 — 비용/지연 폭주 방지.
        """
        env_default = _int_env("AZURE_OPENAI_MAX_COMPLETION_TOKENS") or _int_env("LLM_MAX_COMPLETION_TOKENS")
        default = _positive_int(env_default, fallback)
        desired = _positive_int(requested, default) if requested is not None else default
        upper = _int_env("AZURE_OPENAI_MAX_COMPLETION_TOKENS_LIMIT") or env_default or fallback
        return min(desired, _positive_int(upper, default))

    def _chat_kwargs(self, messages, *, temperature: float, max_tokens: int | None, stream: bool) -> dict:
        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": stream,
        }
        token_limit = self._completion_token_limit(max_tokens, 900)
        if self.provider == "azure":
            kwargs.update(
                {
                    "top_p": float(os.getenv("AZURE_OPENAI_TOP_P", "1.0")),
                    "frequency_penalty": float(os.getenv("AZURE_OPENAI_FREQUENCY_PENALTY", "0.0")),
                    "presence_penalty": float(os.getenv("AZURE_OPENAI_PRESENCE_PENALTY", "0.0")),
                    "max_completion_tokens": token_limit,
                }
            )
        else:
            kwargs["max_tokens"] = token_limit
        if not stream:
            kwargs.pop("stream")
        return kwargs

    def chat(self, messages, *, temperature: float = 0.0, max_tokens: int | None = None) -> str:
        """단발 완성 (동기) — 스크립트/평가용."""
        resp = self.client.chat.completions.create(
            **self._chat_kwargs(messages, temperature=temperature, max_tokens=max_tokens, stream=False)
        )
        return resp.choices[0].message.content or ""

    def chat_stream(self, messages, *, temperature: float = 0.0, max_tokens: int | None = None):
        """토큰 스트리밍 (동기 제너레이터) — 스크립트용. 서버 운영 경로는 chat_stream_async."""
        stream = self.client.chat.completions.create(
            **self._chat_kwargs(messages, temperature=temperature, max_tokens=max_tokens, stream=True)
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and getattr(delta, "content", None):
                yield delta.content

    async def chat_stream_async(self, messages, *, temperature: float = 0.0, max_tokens: int | None = None):
        """토큰 스트리밍 (async) — FastAPI 이벤트루프를 막지 않는 운영 경로."""
        stream = await self.async_client.chat.completions.create(
            **self._chat_kwargs(messages, temperature=temperature, max_tokens=max_tokens, stream=True)
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and getattr(delta, "content", None):
                yield delta.content
