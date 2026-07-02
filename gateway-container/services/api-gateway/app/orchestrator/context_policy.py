"""Context Policy Layer — 분류 결과를 응답 전략으로 바꾸는 사람 편집용 정책 테이블.

┌─────────────────────────────────────────────────────────────────────┐
│ "이 라벨일 때 어떻게 응답할까"를 바꾸려면 이 파일의 POLICIES 만 수정.  │
│                                                                     │
│ 라우팅 규칙 (아키텍처 문서 'Context Policy 라우팅'과 동일):           │
│   ① safety unsafe          → CRISIS_POLICY (최우선, LLM 우회)        │
│   ② primary ∈ POLICIES     → 해당 정책                               │
│   ③ 그 외 인지왜곡 라벨     → DEFAULT_POLICY (라벨 지침 CBT + RAG)    │
│                                                                     │
│ prompt_strategy 값은 llm/prompts.py 의 PROMPT_STRATEGIES 키와 맞춘다. │
└─────────────────────────────────────────────────────────────────────┘
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ContextPolicy:
    name: str                   # 정책 이름 — 세션 턴의 policy 메타데이터로 저장됨
    prompt_strategy: str        # llm/prompts.py 전략 키
    use_rag: bool = True        # RAG 검색 결과를 프롬프트에 넣을지
    rag_top_n: int | None = None  # RAG 문서 수 (None = settings.RERANK_TOP_N)
    is_crisis: bool = False     # True 면 LLM 을 호출하지 않고 고정 위기 메시지 출력

    def as_metadata(self) -> dict:
        """세션 턴에 남기는 관측용 메타데이터."""
        return {"name": self.name, "prompt_strategy": self.prompt_strategy, "use_rag": self.use_rag}


# ① 위기: safety 가 unsafe 로 판정하면 라벨과 무관하게 항상 이 정책 (crisis.py 로 연결)
CRISIS_POLICY = ContextPolicy(
    name="crisis_override", prompt_strategy="cbt_label_guided", use_rag=False, is_crisis=True,
)

# ③ 인지왜곡 라벨 기본값: 라벨별 지침 CBT 프롬프트 + RAG
DEFAULT_POLICY = ContextPolicy(name="cbt_label_guided", prompt_strategy="cbt_label_guided")

# ② 라벨별 예외 정책 — 여기를 편집해서 라벨별 응답 방식을 조정한다.
POLICIES: dict[str, ContextPolicy] = {
    # 일반 발화: 교정하지 않고 지지/공감. RAG 는 약하게(2건)만 참고.
    "정상": ContextPolicy(name="normal_supportive", prompt_strategy="supportive", rag_top_n=2),
    # 정보 부족: 명확화 질문 중심. RAG 생략.
    "불충분": ContextPolicy(name="insufficient_clarify", prompt_strategy="clarify", use_rag=False),
    # 예) 특정 왜곡 라벨만 RAG 를 늘리고 싶다면:
    # "파국화": ContextPolicy(name="catastrophizing_deep", prompt_strategy="cbt_label_guided", rag_top_n=6),
}

# [사람 작업 가이드] '불충분' 최근 N턴 재분류 (아키텍처 문서의 도입 예정 기능)
#   현재는 이번 발화 하나만 분류한다. 아키텍처 설계상 '불충분'일 때는 최근 N턴(예: 3턴)을
#   이어붙여 재분류하고, 왜곡 라벨이 나오면 그 라벨의 정책을 적용하는 단계가 예정되어 있다.
#   구현 위치: respond_flow.py 의 policy 결정 직후 —
#     1) await session_repository.recent_llm_messages(session_id, N) 로 최근 발화 수집
#     2) "\n".join(user 발화들 + 이번 발화) 를 services.classifier.classify_one 으로 재분류
#     3) 결과가 '불충분'/'정상' 이 아니면 resolve() 를 그 라벨로 다시 호출
#   재분류는 분류기 호출이 1회 늘어나므로 지연시간을 확인하고 켠다.


def resolve(safety: dict, classification: dict) -> ContextPolicy:
    """safety + primary 라벨로 이번 턴의 컨텍스트 정책을 결정한다."""
    if not safety.get("safe", True):
        return CRISIS_POLICY
    return POLICIES.get(classification.get("primary", ""), DEFAULT_POLICY)
