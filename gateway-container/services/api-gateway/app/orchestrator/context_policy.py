"""[정책 테이블 — 사람 편집용] 분류 라벨별로 "어떻게 응답할지"를 정하는 곳.

"이 라벨일 때 응답 방식을 바꾸고 싶다"면 코드 로직이 아니라 아래 POLICIES
사전(dict)만 수정하면 된다.

라우팅 규칙 (respond_stream 이 resolve() 를 호출):
    ① 안전검사가 위험 판정 → CRISIS_POLICY (라벨 무관 최우선, LLM 우회)
    ② 라벨이 POLICIES 에 있음 → 그 정책 사용
    ③ 그 외 인지왜곡 라벨 → DEFAULT_POLICY (라벨 지침 CBT + 참고자료)

prompt_strategy 값은 prompts.py 의 PROMPT_STRATEGIES 키와 짝이다 —
새 전략을 만들려면 prompts.py 에 build_xxx 함수를 추가하고 여기서 이름으로 쓴다.
"""
from dataclasses import dataclass

from .. import settings


@dataclass(frozen=True)  # frozen=True: 만든 뒤 값 변경 불가(실수 방지용 읽기 전용 묶음)
class ContextPolicy:
    name: str                     # 정책 이름 — 어떤 정책이 적용됐는지 세션 기록에 남는다
    prompt_strategy: str          # prompts.py 의 어떤 프롬프트 전략을 쓸지
    use_rag: bool = True          # 검색된 참고자료를 프롬프트에 넣을지
    rag_top_n: int | None = None  # 참고자료 개수 (None = settings.RERANK_TOP_N, 기본 4)
    is_crisis: bool = False       # True 면 LLM 을 부르지 않고 고정 위기 메시지 출력

    def as_metadata(self) -> dict:
        """세션 기록에 남길 요약 (나중에 "왜 이런 답이 나왔나" 추적용)."""
        return {"name": self.name, "prompt_strategy": self.prompt_strategy, "use_rag": self.use_rag}


# ① 위기: 안전검사가 위험으로 판정하면 무조건 이 정책 (crisis.py 의 고정 메시지로 연결)
CRISIS_POLICY = ContextPolicy("crisis_override", "cbt_label_guided", use_rag=False, is_crisis=True)

# ③ 기본값: 라벨별 지침이 담긴 CBT 프롬프트 + 참고자료 4건
DEFAULT_POLICY = ContextPolicy("cbt_label_guided", "cbt_label_guided")

# 저확신 강등: 왜곡 라벨인데 확신이 POLICY_MIN_CONFIDENCE 미만이면 이 정책으로.
# 이름을 따로 둔 이유 — 세션 policy 메타데이터에서 "하한 때문에 강등된 턴"을 집계하기 위해.
LOW_CONFIDENCE_POLICY = ContextPolicy("low_confidence_clarify", "clarify", use_rag=False)

# ② 라벨별 예외 — 여기를 편집해서 라벨별 응답 방식을 조정한다
POLICIES: dict[str, ContextPolicy] = {
    # 일반 발화: 왜곡 교정을 시도하지 않고 지지·공감 중심. 참고자료는 2건만 가볍게
    "정상": ContextPolicy("normal_supportive", "supportive", rag_top_n=2),
    # 판단 불가 발화: 참고자료 없이, 상황을 더 물어보는 명확화 질문 중심
    "불충분": ContextPolicy("insufficient_clarify", "clarify", use_rag=False),
    # 예) 특정 라벨만 참고자료를 6건으로 늘리고 싶다면 아래처럼 한 줄 추가:
    # "흑백 사고": ContextPolicy("dichotomous_deep", "cbt_label_guided", rag_top_n=6),
}

# [도입 예정] '불충분' 최근 N턴 재분류: 한 문장으로 판단이 안 되면 최근 사용자 발화
# 여러 개를 이어붙여 다시 분류하고, 왜곡 라벨이 나오면 그 라벨의 정책을 적용하는 기능.
# 구현 위치는 respond_stream 의 resolve() 호출 직후. 분류기 호출이 1회 늘어나므로
# 응답 지연을 확인한 뒤 도입한다.


def resolve(safety: dict, classification: dict) -> ContextPolicy:
    """안전검사 결과 + 대표 라벨(+확신 하한) → 이번 턴에 적용할 정책 하나를 고른다."""
    if not safety.get("safe", True):
        return CRISIS_POLICY
    primary = classification.get("primary", "")
    # 저확신 강등: 왜곡 라벨인데 확신이 하한 미만이면 단정하지 않고 명확화로 (기본 꺼짐)
    if settings.POLICY_MIN_CONFIDENCE > 0 and primary not in ("정상", "불충분", ""):
        confidence = next((l.get("score", 0.0) for l in classification.get("labels", [])
                           if l.get("label") == primary), 0.0)
        if confidence < settings.POLICY_MIN_CONFIDENCE:
            return LOW_CONFIDENCE_POLICY
    return POLICIES.get(primary, DEFAULT_POLICY)
