"""LLM 어댑터 — Azure OpenAI / 로컬 OpenAI 호환 서버 (common/llm_client.py).

운영 경로는 chat_stream_async: FastAPI 이벤트루프를 막지 않는 async 스트리밍.
요청 옵션(llm.max_completion_tokens 등)은 여기서 LLMClient kwargs 로 변환한다.
"""
from typing import AsyncIterator, Iterator

from common.llm_client import LLMClient


class LlmAdapter:
    def __init__(self):
        self._client = LLMClient()

    async def chat_stream_async(self, messages: list[dict], options: dict | None = None) -> AsyncIterator[str]:
        async for token in self._client.chat_stream_async(messages, **llm_options(options)):
            yield token

    def chat_stream(self, messages: list[dict], options: dict | None = None) -> Iterator[str]:
        """동기 스트리밍 (스크립트/legacy 용 — 운영 경로는 chat_stream_async)."""
        return self._client.chat_stream(messages, **llm_options(options))


def llm_options(options: dict | None) -> dict:
    """요청 레벨 LLM 옵션을 LLMClient kwargs 로 변환한다."""
    kwargs: dict = {}
    if not options:
        return kwargs
    max_tokens = options.get("max_completion_tokens") or options.get("max_tokens")
    if max_tokens is not None:
        try:
            kwargs["max_tokens"] = int(max_tokens)
        except (TypeError, ValueError):
            pass
    temperature = options.get("temperature")
    if temperature is not None:
        try:
            kwargs["temperature"] = float(temperature)
        except (TypeError, ValueError):
            pass
    return kwargs
