"""Retrieval seam for the gateway.

Supported providers:
- local/stub: offline canned documents for development and smoke tests
- azure/azure_ai_search/aisearch: Azure AI Search keyword/semantic search

This implementation intentionally starts with lightweight text/semantic search, not vector
search, so the API gateway does not need sentence-transformers or large model downloads.
"""
from __future__ import annotations

import os
from typing import Any

from .types import RetrievedDoc


class BaseRetriever:
    def retrieve(self, text: str, k: int = 8) -> list[RetrievedDoc]:
        raise NotImplementedError


class LocalStubRetriever(BaseRetriever):
    """Offline stub for dev/test when no external RAG service is configured."""

    _CANNED: list[RetrievedDoc] = [
        {
            "id": "t1",
            "content": "생각을 증거 기반으로 다시 확인합니다. 지금 떠오른 결론이 사실인지, 다른 해석은 없는지 살펴봅니다.",
            "score": 0.6,
            "metadata": {
                "technique": "cognitive_restructuring",
                "distortions": ["흑백 사고", "과잉 일반화", "성급한 판단"],
            },
        },
        {
            "id": "t2",
            "content": "탈파국화: 최악의 시나리오가 실제로 일어날 가능성과 대처 자원을 함께 평가합니다.",
            "score": 0.5,
            "metadata": {
                "technique": "decatastrophizing",
                "distortions": ["확대와 축소", "감정적 추론"],
            },
        },
        {
            "id": "t3",
            "content": "행동 실험: 예측을 작은 행동으로 검증하고 결과를 기록합니다.",
            "score": 0.4,
            "metadata": {
                "technique": "behavioral_experiment",
                "distortions": ["성급한 판단", "부정적 편향"],
            },
        },
    ]

    def retrieve(self, text: str, k: int = 8) -> list[RetrievedDoc]:
        return self._CANNED[:k]


class AzureAiSearchRetriever(BaseRetriever):
    """Azure AI Search retriever for Foundry IQ/Search Service indexes.

    Required environment variables:
    - AZURE_SEARCH_ENDPOINT: e.g. https://cbt-rag-search.search.windows.net
      (service name `cbt-rag-search` is also accepted)
    - AZURE_SEARCH_API_KEY: admin/query key; use ACA secretref in production
    - AZURE_SEARCH_INDEX: target index name

    Recommended field environment variables:
    - AZURE_SEARCH_CONTENT_FIELD: text field to feed back to the RAG prompt
      (default: content; fallback probes common names such as chunk/text/content_kr)
    - AZURE_SEARCH_ID_FIELD: stable document/chunk id field (default: id)
    - AZURE_SEARCH_TITLE_FIELD: optional title/source field
    - AZURE_SEARCH_SEMANTIC_CONFIG: optional semantic configuration name

    Optional:
    - AZURE_SEARCH_SELECT_FIELDS: comma-separated fields to request from search.
      If omitted, the SDK returns retrievable fields configured on the index.
    """

    _CONTENT_FALLBACK_FIELDS = (
        "content",
        "chunk",
        "text",
        "pageContent",
        "body",
        "markdown",
        "content_kr",
        "merged_content",
    )
    _ID_FALLBACK_FIELDS = ("id", "chunk_id", "key", "metadata_storage_path", "source")

    def __init__(self) -> None:
        try:
            from azure.core.credentials import AzureKeyCredential
            from azure.search.documents import SearchClient
        except ImportError as exc:
            raise RuntimeError(
                "Azure AI Search provider requires package 'azure-search-documents'. "
                "Add it to gateway-container/services/api-gateway/requirements.txt and rebuild the gateway image."
            ) from exc

        self.endpoint = self._normalize_endpoint(os.getenv("AZURE_SEARCH_ENDPOINT", ""))
        self.api_key = os.getenv("AZURE_SEARCH_API_KEY", "")
        self.index = os.getenv("AZURE_SEARCH_INDEX", "")
        self.content_field = os.getenv("AZURE_SEARCH_CONTENT_FIELD", "content")
        self.id_field = os.getenv("AZURE_SEARCH_ID_FIELD", "id")
        self.title_field = os.getenv("AZURE_SEARCH_TITLE_FIELD", "")
        self.semantic_config = os.getenv("AZURE_SEARCH_SEMANTIC_CONFIG", "")
        self.select_fields = self._split_csv(os.getenv("AZURE_SEARCH_SELECT_FIELDS", ""))

        missing = [
            name
            for name, value in {
                "AZURE_SEARCH_ENDPOINT": self.endpoint,
                "AZURE_SEARCH_API_KEY": self.api_key,
                "AZURE_SEARCH_INDEX": self.index,
            }.items()
            if not value
        ]
        if missing:
            raise ValueError("Missing Azure AI Search env vars: " + ", ".join(missing))

        self.client = SearchClient(
            endpoint=self.endpoint,
            index_name=self.index,
            credential=AzureKeyCredential(self.api_key),
        )

    @staticmethod
    def _normalize_endpoint(value: str) -> str:
        value = (value or "").strip().rstrip("/")
        if not value:
            return ""
        if value.startswith("http://") or value.startswith("https://"):
            return value
        if ".search.windows.net" in value:
            return "https://" + value
        return f"https://{value}.search.windows.net"

    @staticmethod
    def _split_csv(value: str) -> list[str] | None:
        fields = [part.strip() for part in (value or "").split(",") if part.strip()]
        return fields or None

    @staticmethod
    def _as_score(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _compact_metadata(doc: dict[str, Any], content_field: str, id_field: str) -> dict[str, Any]:
        metadata: dict[str, Any] = {}
        for key, value in doc.items():
            if key in {content_field, id_field} or key.startswith("@search."):
                continue
            # Avoid returning huge vector/list payloads to the prompt/dashboard.
            if isinstance(value, list) and len(value) > 20:
                metadata[key] = f"<list:{len(value)}>"
            else:
                metadata[key] = value
        metadata["search_score"] = doc.get("@search.score")
        if "@search.reranker_score" in doc:
            metadata["reranker_score"] = doc.get("@search.reranker_score")
        return metadata

    def _pick_content(self, doc: dict[str, Any]) -> str:
        candidates = (self.content_field, *self._CONTENT_FALLBACK_FIELDS)
        for field in candidates:
            value = doc.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
        # Last-resort fallback: concatenate small string fields.
        pieces: list[str] = []
        for key, value in doc.items():
            if key.startswith("@search."):
                continue
            if isinstance(value, str) and value.strip():
                pieces.append(value.strip())
            if len("\n".join(pieces)) > 1200:
                break
        return "\n".join(pieces).strip()

    def _pick_id(self, doc: dict[str, Any], fallback: int) -> str:
        candidates = (self.id_field, *self._ID_FALLBACK_FIELDS)
        for field in candidates:
            value = doc.get(field)
            if value is not None and str(value).strip():
                return str(value)
        return f"azure-search-{fallback}"

    def _search(self, text: str, k: int, *, semantic: bool):
        kwargs: dict[str, Any] = {
            "search_text": text,
            "top": k,
            "include_total_count": False,
        }
        if self.select_fields:
            kwargs["select"] = self.select_fields
        if semantic and self.semantic_config:
            kwargs["query_type"] = "semantic"
            kwargs["semantic_configuration_name"] = self.semantic_config
        return self.client.search(**kwargs)

    def retrieve(self, text: str, k: int = 8) -> list[RetrievedDoc]:
        query = (text or "").strip()
        if not query:
            return []

        try:
            results = self._search(query, k, semantic=bool(self.semantic_config))
        except Exception:
            # Semantic config names are easy to mismatch in the Portal. Fall back to
            # standard keyword/BM25 search rather than failing the whole response path.
            if not self.semantic_config:
                raise
            results = self._search(query, k, semantic=False)

        docs: list[RetrievedDoc] = []
        for idx, row in enumerate(results, start=1):
            doc = dict(row)
            content = self._pick_content(doc)
            if not content:
                continue
            score = self._as_score(
                doc.get("@search.reranker_score"),
                self._as_score(doc.get("@search.score"), 0.0),
            )
            docs.append(
                {
                    "id": self._pick_id(doc, idx),
                    "content": content,
                    "score": score,
                    "metadata": self._compact_metadata(doc, self.content_field, self.id_field),
                }
            )
        return docs[:k]


def get_retriever() -> BaseRetriever:
    provider = os.getenv("RETRIEVE_PROVIDER", "local").strip().lower()
    if provider in ("local", "stub"):
        return LocalStubRetriever()
    if provider in ("azure", "azure_ai_search", "aisearch"):
        return AzureAiSearchRetriever()
    raise ValueError(f"Unsupported RETRIEVE_PROVIDER '{provider}' (use: local, azure)")
