"""RAG 검색 어댑터 — RETRIEVE_PROVIDER=local(stub) / azure(Azure AI Search).

실제 백엔드 구현은 retrieve/client.py. 검색 API 는 동기(블로킹)라서
스레드로 오프로딩해 safety/classify 와의 gather 병렬성을 유지한다.
"""
import asyncio

from retrieve.client import get_retriever


class RetrieverAdapter:
    def __init__(self):
        self._retriever = get_retriever()

    async def retrieve(self, text: str) -> list[dict]:
        return await asyncio.to_thread(self._retriever.retrieve, text)
