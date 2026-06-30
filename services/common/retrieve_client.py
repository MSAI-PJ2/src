"""프로바이더 무관 검색(retrieve) 클라이언트.

llm_client.LLMClient 와 동일한 비용 절약 패턴을 RAG 검색 레인에 적용한다:
같은 코드로 (a) 로컬 인메모리 벡터 store(무료, 개발/테스트)와 (b) Azure AI Search
(서비스, 과금)를 호출한다. `RETRIEVE_PROVIDER` 환경변수로만 전환한다
(LLM_PROVIDER 와 대칭).

공통 계약:
    retrieve(text, k) -> list[{id, content, score, metadata}]

심서리 3-레인 DAG에서 retrieve 레인은 입력 텍스트(text)에만 의존하므로
safety_check / classify 와 함께 asyncio.gather 로 병렬 실행된다. 그래서
동기 인터페이스(retrieve)와 더불어 await 가능한 aretrieve 도 제공한다
(기본 구현은 스레드 오프로딩 — 블로킹 호출이 이벤트루프를 막지 않게).

임베딩 일관성(중요):
    코퍼스와 쿼리는 *반드시 동일한 임베딩 모델*로 인코딩해야 점수가 의미를 가진다.
    로컬/Azure 양쪽에서 같은 sentence-transformers 모델을 사용하고(클라이언트측 벡터를
    Azure에 push), Azure '통합 벡터화'는 채택하지 않는다(다른 모델로 인덱싱되면
    로컬과 벡터가 호환되지 않음 — 일관성 함정).
"""
import os
import asyncio
from abc import ABC, abstractmethod

LOCAL_PROVIDER_ALIASES = {"local", "memory", "inmemory"}
AZURE_PROVIDER_ALIASES = {"azure", "azure_search", "azure_ai_search"}

# 코퍼스+쿼리 양쪽에 동일하게 쓰는 한국어/다국어 임베딩 모델.
# 기본값: 다국어 MiniLM(384d, 가벼움). 한국어 품질을 더 원하면
# RETRIEVE_EMBED_MODEL=jhgan/ko-sroberta-multitask (768d) 로 교체.
DEFAULT_EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def _get_embedder():
    """프로세스 1회 로드되는 sentence-transformers 인코더(싱글톤).

    encode(list[str]) -> np.ndarray(정규화됨). 코사인=내적이 되도록 L2 정규화.
    무거운 import 는 지연(로컬 provider 일 때만 필요)."""
    global _EMBEDDER
    try:
        return _EMBEDDER
    except NameError:
        pass
    from sentence_transformers import SentenceTransformer  # 지연 import
    model_name = os.getenv("RETRIEVE_EMBED_MODEL", DEFAULT_EMBED_MODEL)

    class _Enc:
        def __init__(self):
            self.m = SentenceTransformer(model_name)
            self.dim = self.m.get_sentence_embedding_dimension()

        def encode(self, texts):
            return self.m.encode(
                texts, normalize_embeddings=True, convert_to_numpy=True
            )

    enc = _Enc()
    globals()["_EMBEDDER"] = enc
    return enc


class BaseRetriever(ABC):
    """retrieve 계약을 구현하는 모든 백엔드의 공통 베이스."""

    @abstractmethod
    def retrieve(self, text: str, k: int = 5) -> list:
        """list[{id, content, score, metadata}] 반환. score 는 높을수록 관련 ↑ (코사인/RRF)."""

    async def aretrieve(self, text: str, k: int = 5) -> list:
        """기본 await 구현: 동기 retrieve 를 스레드로 오프로딩(gather 친화)."""
        return await asyncio.to_thread(self.retrieve, text, k)


class LocalRetriever(BaseRetriever):
    """인메모리 코사인 검색. ~43개 한국어 문서 규모에 충분(무의존 서버 인프라).

    코퍼스 JSONL(각 줄 {id, content, metadata?})을 읽어 동일 임베딩 모델로 인코딩,
    행렬곱 1회로 top-k 를 구한다. FAISS/Chroma 불필요(소규모에서 동일 결과·더 단순)."""

    def __init__(self, corpus_path=None):
        import json
        import numpy as np
        self._np = np
        self.enc = _get_embedder()
        corpus_path = corpus_path or os.getenv("RETRIEVE_LOCAL_CORPUS")
        if not corpus_path or not os.path.exists(corpus_path):
            raise ValueError(
                f"RETRIEVE_LOCAL_CORPUS 경로가 없음: {corpus_path!r} "
                "(JSONL: 줄당 {{id, content, metadata?}})"
            )
        self.docs = []
        with open(corpus_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    self.docs.append(json.loads(line))
        if not self.docs:
            raise ValueError(f"코퍼스가 비어있음: {corpus_path}")
        self.matrix = self.enc.encode([d["content"] for d in self.docs])  # (N, dim) 정규화됨

    def retrieve(self, text: str, k: int = 5) -> list:
        q = self.enc.encode([text])[0]            # (dim,) 정규화됨
        scores = self.matrix @ q                   # 정규화 → 내적=코사인
        n = len(self.docs)
        k = max(1, min(k, n))
        top = self._np.argpartition(-scores, k - 1)[:k]
        top = top[self._np.argsort(-scores[top])]
        out = []
        for i in top:
            d = self.docs[int(i)]
            out.append({
                "id": str(d.get("id", int(i))),
                "content": d["content"],
                "score": float(scores[int(i)]),
                "metadata": d.get("metadata", {}),
            })
        return out


class AzureRetriever(BaseRetriever):
    """Azure AI Search 백엔드. 동일 계약 뒤에서 hybrid(keyword+vector) 쿼리.

    인증: API 키(환경변수). Free 티어는 managed identity 미지원이므로 키 인증 고정.
    벡터: 로컬과 *동일한* sentence-transformers 모델로 쿼리를 클라이언트측 벡터화해 push
    (통합 벡터화 미사용 — 코퍼스도 같은 모델로 인덱싱했다는 전제). 임베딩 일관성 보장."""

    def __init__(self):
        from azure.core.credentials import AzureKeyCredential
        from azure.search.documents import SearchClient
        self._SearchClient = SearchClient
        endpoint = os.getenv("AZURE_SEARCH_ENDPOINT")
        api_key = os.getenv("AZURE_SEARCH_API_KEY")
        index = os.getenv("AZURE_SEARCH_INDEX")
        missing = [n for n, v in {
            "AZURE_SEARCH_ENDPOINT": endpoint,
            "AZURE_SEARCH_API_KEY": api_key,
            "AZURE_SEARCH_INDEX": index,
        }.items() if not v]
        if missing:
            raise ValueError("Azure Search 필수 환경변수 누락: " + ", ".join(missing))

        self.vector_field = os.getenv("AZURE_SEARCH_VECTOR_FIELD", "contentVector")
        self.content_field = os.getenv("AZURE_SEARCH_CONTENT_FIELD", "content")
        self.id_field = os.getenv("AZURE_SEARCH_ID_FIELD", "id")
        # 시맨틱 랭킹은 리전 의존(Free 에서 일부 리전만). 설정 시에만 사용.
        self.semantic_config = os.getenv("AZURE_SEARCH_SEMANTIC_CONFIG") or None
        self.enc = _get_embedder()
        self.client = SearchClient(
            endpoint=endpoint, index_name=index,
            credential=AzureKeyCredential(api_key),
        )

    def retrieve(self, text: str, k: int = 5) -> list:
        from azure.search.documents.models import VectorizedQuery
        vec = self.enc.encode([text])[0].tolist()
        vq = VectorizedQuery(
            vector=vec, k_nearest_neighbors=k, fields=self.vector_field, kind="vector",
        )
        kwargs = {
            "search_text": text,        # keyword + vector = hybrid (RRF 융합)
            "vector_queries": [vq],
            "top": k,
        }
        if self.semantic_config:
            kwargs["query_type"] = "semantic"
            kwargs["semantic_configuration_name"] = self.semantic_config
        results = self.client.search(**kwargs)
        out = []
        for r in results:
            score = r.get("@search.reranker_score") or r.get("@search.score") or 0.0
            out.append({
                "id": str(r.get(self.id_field, "")),
                "content": r.get(self.content_field, ""),
                "score": float(score),
                "metadata": {
                    key: v for key, v in r.items()
                    if not key.startswith("@search.")
                    and key not in (self.content_field, self.vector_field)
                },
            })
        return out


def get_retriever(provider=None) -> BaseRetriever:
    """RETRIEVE_PROVIDER 로 백엔드 선택(LLMClient 와 대칭). 기본=local(무료)."""
    raw = (provider or os.getenv("RETRIEVE_PROVIDER", "local")).strip().lower()
    if raw in LOCAL_PROVIDER_ALIASES:
        return LocalRetriever()
    if raw in AZURE_PROVIDER_ALIASES:
        return AzureRetriever()
    raise ValueError(
        f"지원하지 않는 RETRIEVE_PROVIDER '{raw}' "
        "(가능: local, memory, azure, azure_search)"
    )
