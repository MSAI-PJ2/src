"""Service adapter boundary for external Gateway dependencies.

The Gateway DAG should describe orchestration flow, not the SDK/HTTP details
of each external service.  These adapters keep the current implementation
intact while giving 3-3 (Cosmos DB session persistence) a cleaner boundary to
build on.
"""

from __future__ import annotations

import asyncio
from typing import Iterator

from common.llm_client import LLMClient
from common.speech_client import transcribe_audio_input_detailed
from retrieve.client import get_retriever

from . import clients
from .safety import safety_check
from .tts import synthesize_tts


class ClassifierAdapter:
    """Adapter for the internal cogdist classifier Container App."""

    async def classify_one(self, text: str, threshold: float | None = None) -> dict:
        return await clients.classify_one(text, threshold)

    async def classify_batch(self, texts: list[str], threshold: float | None = None) -> dict:
        return await clients.classify_batch(texts, threshold)


class SafetyAdapter:
    """Adapter for Azure Content Safety plus keyword fallback."""

    async def check(self, text: str) -> dict:
        return await safety_check(text)


class RetrieverAdapter:
    """Adapter for local/Azure AI Search retrieval providers."""

    def __init__(self):
        self._retriever = get_retriever()

    async def retrieve(self, text: str) -> list[dict]:
        # The retriever API is sync; run it in a worker thread so DAG fan-out can
        # still gather with safety/classification work.
        return await asyncio.to_thread(self._retriever.retrieve, text)


class LlmAdapter:
    """Adapter for Azure OpenAI/local LLM client."""

    def __init__(self):
        self._client = LLMClient()

    def chat_stream(self, messages: list[dict], options: dict | None = None) -> Iterator[str]:
        return self._client.chat_stream(messages, **llm_options(options))


class SpeechAdapter:
    """Adapter for Azure Speech STT/TTS helpers."""

    async def transcribe_audio(self, audio: dict | None) -> dict:
        return await asyncio.to_thread(transcribe_audio_input_detailed, audio)

    async def synthesize_tts(self, text: str, tts_options: dict | None) -> dict:
        return await synthesize_tts(text, tts_options)


class GatewayServiceAdapters:
    """Container for external service adapters used by the DAG and routes."""

    def __init__(self):
        self.classifier = ClassifierAdapter()
        self.safety = SafetyAdapter()
        self.retriever = RetrieverAdapter()
        self.llm = LlmAdapter()
        self.speech = SpeechAdapter()


def llm_options(options: dict | None) -> dict:
    """Map request-level LLM options to LLMClient kwargs."""
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


services = GatewayServiceAdapters()
