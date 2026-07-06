"""[정책·프롬프트 — 사람 편집 영역] "무엇을, 어떤 태도로 답할지"가 전부 이 파일에 있다.

코드 로직을 몰라도 이 파일의 표(dict)와 문자열만 고치면 상담 동작이 바뀐다.

구획 목차 (Ctrl+F 로 "[구획" 검색):
    [구획 1] 컨텍스트 정책     라벨 → 응답 방식 매핑 (POLICIES 테이블)
    [구획 2] 시스템 프롬프트   페르소나·말투·라벨별 접근법 (PERSONA/STYLE_RULES/LABEL_GUIDANCE)
    [구획 3] 위기 대응         고정 위기 메시지 + 핫라인 (+ 위치기반 DB 작업 가이드)

흐름과의 연결: respond/flow.py 의 respond_stream 이
    resolve()(구획 1) → build_llm_messages()(구획 2) / crisis_payload()(구획 3) 순으로 쓴다.
"""
import asyncio
import logging
import os
from dataclasses import dataclass

from .. import settings
from ..profile import profile_repository  # region 프로필 조회 루트 (구획 3)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════
# [구획 1] 컨텍스트 정책 — 분류 라벨별로 "어떻게 응답할지"를 정하는 표
#
# 라우팅 규칙 (flow.respond_stream 이 resolve() 를 호출):
#   ① 안전검사가 위험 판정 → CRISIS_POLICY (라벨 무관 최우선, LLM 우회 → 구획 3)
#   ② 왜곡 라벨인데 확신 < POLICY_MIN_CONFIDENCE → LOW_CONFIDENCE_POLICY (기본 꺼짐)
#   ③ 라벨이 POLICIES 에 있음 → 그 정책 사용
#   ④ 그 외 인지왜곡 라벨 → DEFAULT_POLICY (라벨 지침 CBT + 참고자료)
# prompt_strategy 값은 구획 2 의 PROMPT_STRATEGIES 키와 짝이다.
# ══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)  # frozen=True: 만든 뒤 값 변경 불가(실수 방지용 읽기 전용 묶음)
class ContextPolicy:
    name: str                     # 정책 이름 — 어떤 정책이 적용됐는지 세션 기록에 남는다
    prompt_strategy: str          # 구획 2 의 어떤 프롬프트 전략을 쓸지
    use_rag: bool = True          # 검색된 참고자료를 프롬프트에 넣을지
    rag_top_n: int | None = None  # 참고자료 개수 (None = settings.RAG_TOP_N, 기본 4)
    is_crisis: bool = False       # True 면 LLM 을 부르지 않고 고정 위기 메시지 출력

    def as_metadata(self) -> dict:
        """세션 기록에 남길 요약 (나중에 "왜 이런 답이 나왔나" 추적용)."""
        return {"name": self.name, "prompt_strategy": self.prompt_strategy, "use_rag": self.use_rag}


# ① 위기: 안전검사가 위험으로 판정하면 무조건 이 정책 (구획 3 의 고정 메시지로 연결)
CRISIS_POLICY = ContextPolicy("crisis_override", "cbt_label_guided", use_rag=False, is_crisis=True)

# ④ 기본값: 라벨별 지침이 담긴 CBT 프롬프트 + 참고자료 4건
DEFAULT_POLICY = ContextPolicy("cbt_label_guided", "cbt_label_guided")

# ② 저확신 강등: 왜곡 라벨인데 확신이 하한 미만이면 이 정책으로.
#    이름을 따로 둔 이유 — 세션 policy 메타데이터에서 "하한 때문에 강등된 턴"을 집계하기 위해.
LOW_CONFIDENCE_POLICY = ContextPolicy("low_confidence_clarify", "clarify", use_rag=False)

# ③ 라벨별 예외 — 여기를 편집해서 라벨별 응답 방식을 조정한다
POLICIES: dict[str, ContextPolicy] = {
    # 일반 발화: 왜곡 교정을 시도하지 않고 지지·공감 중심. 참고자료는 2건만 가볍게
    "정상": ContextPolicy("normal_supportive", "supportive", rag_top_n=2),
    # 판단 불가 발화: 참고자료 없이, 상황을 더 물어보는 명확화 질문 중심
    "불충분": ContextPolicy("insufficient_clarify", "clarify", use_rag=False),
    # 예) 특정 라벨만 참고자료를 6건으로 늘리고 싶다면 아래처럼 한 줄 추가:
    # "흑백 사고": ContextPolicy("dichotomous_deep", "cbt_label_guided", rag_top_n=6),
}

# '불충분' 완화 사다리 — 병합 재분류(flow 의 twopass)를 거치고도 '불충분'이 연속되면
# 같은 질문을 되풀이하지 않고 단계적으로 태도를 바꾼다. 단계 수는 이번 턴 포함
# 연속 횟수(ladder_step)이고, settings.INSUFFICIENT_ESCAPE_AFTER(기본 4)째부터는
# "질문 자체를 멈추는" 수용·동행 모드다 — 4연속 불충분은 정보 부족이 아니라
# 발화 회피 신호라는 실측(회피형 100% vs 비회피 0.9% 도달)에 근거한다.
INSUFFICIENT_LADDER: dict[int, ContextPolicy] = {
    1: POLICIES["불충분"],  # 1차: 현행 그대로 — 구체적으로 물어본다
    2: ContextPolicy("insufficient_clarify_alt", "clarify_alt", use_rag=False),      # 2차: 다른 각도
    3: ContextPolicy("insufficient_clarify_light", "clarify_light", use_rag=False),  # 3차: 부담 낮춤
}
# 4차(ESCAPE_AFTER)부터: 질문 중단 · 감정 반영 · 머무르기. RAG 도 쓰지 않는다
ACCOMPANY_POLICY = ContextPolicy("insufficient_accompany", "accompany", use_rag=False)


def resolve(safety: dict, classification: dict, ladder_step: int = 0) -> ContextPolicy:
    """안전검사 결과 + 대표 라벨(+확신 하한) → 이번 턴에 적용할 정책 하나를 고른다.

    ladder_step: 이번 턴 포함 연속 '불충분' 횟수 (flow 가 세션 기록으로 계산해 전달).
                 0/1 이면 현행과 동일하게 동작한다 — 기존 호출부 호환.
    """
    if not safety.get("safe", True):
        return CRISIS_POLICY
    primary = classification.get("primary", "")
    # 저확신 강등 — 멀티라벨의 구멍을 여기서 막는다 (2026-07-04 전수검수 확정 결함):
    # 멀티라벨에서 primary 는 sigmoid argmax 라 threshold 검사를 받지 않는다. 즉 12라벨
    # 전부 threshold(0.55) 미만인 "미검출" 발화도 argmax 왜곡이 primary 로 온다.
    # 그래서 "왜곡으로 단정해도 되는가"의 하한을 라우팅에서 완성한다:
    #   하한 = max(POLICY_MIN_CONFIDENCE, 분류기 응답의 threshold — 배포 0.55)
    # 배포 실측 확신값은 0.8~0.99 대라 정상적인 왜곡 발화는 영향받지 않고,
    # OOD/애매 발화(예: 최고점 0.41)만 단정 대신 clarify 로 강등된다.
    if primary not in ("정상", "불충분", ""):
        confidence = next((l.get("score", 0.0) for l in classification.get("labels", [])
                           if l.get("label") == primary), 0.0)
        try:
            model_threshold = float(classification.get("threshold") or 0.0)
        except (TypeError, ValueError):
            model_threshold = 0.0
        if confidence < max(settings.POLICY_MIN_CONFIDENCE, model_threshold):
            return LOW_CONFIDENCE_POLICY
    # 연속 '불충분'이면 사다리에서 단계에 맞는 정책을 고른다.
    # ESCAPE_AFTER <= 0 은 사다리 전체 끔 (flow 가 ladder_step 을 0 으로 유지하지만
    # 외부 호출자가 값을 넘겨도 안전하도록 여기서도 이중으로 막는다).
    if primary == "불충분" and ladder_step >= 2 and settings.INSUFFICIENT_ESCAPE_AFTER > 0:
        if ladder_step >= settings.INSUFFICIENT_ESCAPE_AFTER:
            return ACCOMPANY_POLICY
        return INSUFFICIENT_LADDER.get(ladder_step, INSUFFICIENT_LADDER[3])
    return POLICIES.get(primary, DEFAULT_POLICY)


# ══════════════════════════════════════════════════════════════════════════
# [구획 2] 시스템 프롬프트 — AI 상담사의 말투·태도·라벨별 접근법
#
# "시스템 프롬프트" = AI 에게 답변 생성 전에 주는 지시문. 답변 스타일을 바꾸고
# 싶으면 아래 문자열들(PERSONA / STYLE_RULES / SAFETY_RULES / LABEL_GUIDANCE)만
# 고치면 된다. 어떤 발화에 어떤 전략(build_xxx)을 쓸지는 구획 1 의 POLICIES 가 정한다.
# ══════════════════════════════════════════════════════════════════════════

# AI 상담사가 "누구인지" — 모든 전략의 프롬프트 맨 앞에 들어간다.
# 서비스 이름을 붙이려면 아래 문장에 원하는 이름을 넣으면 된다(예: "... 보조자 'OOO'입니다.").
PERSONA = (
    "당신은 한국어로 응답하는 인지행동치료(CBT) 기반 심리상담 보조자입니다. "
    "내담자의 이야기를 판단 없이 경청하고, 따뜻하지만 과장되지 않은 태도를 유지합니다."
)

# 말투·형식 규칙 — 답변의 겉모습을 통제한다
STYLE_RULES = (
    "답변 스타일 규칙:\n"
    "- 존댓말(해요체), 상담사다운 차분한 어조.\n"
    "- 먼저 1~2문장으로 감정을 공감·반영한 뒤 본론.\n"
    "- 답변은 3~6문장 내외. 목록은 꼭 필요할 때만.\n"
    "- 전문용어(예: '인지왜곡', '흑백사고')를 내담자에게 낙인처럼 붙이지 않기.\n"
    "- 끝에는 내담자가 이어 말할 수 있는 부드러운 질문 하나."
)

# 안전 규칙 — 완화하지 말 것 (상담 서비스의 윤리적 하한선)
SAFETY_RULES = (
    "안전 규칙:\n"
    "- 의학적 진단·약물 조언 금지.\n"
    "- 내담자의 생각을 단정하거나 비난하지 않기.\n"
    "- 자해/자살 위험 신호가 보이면 전문 기관 상담 안내.\n"
    "- 확실하지 않은 사실을 지어내지 않기."
)

# 인지왜곡 12분류 라벨별 상담 접근법.
# key 는 분류기가 내보내는 라벨 그대로 — 지침 문구를 다듬으면 해당 라벨의 답변이 바뀐다.
LABEL_GUIDANCE: dict[str, str] = {
    "흑백 사고": "모 아니면 도 사이의 회색지대를 함께 찾고, 0~100 척도로 다시 보게 돕습니다.",
    "과잉 일반화": "한 번의 경험이 '항상/절대'로 확장된 지점을 짚고 반례를 함께 떠올립니다.",
    "성급한 판단": "결론 전에 확인된 사실과 추측을 구분하도록 돕습니다.",
    "확대와 축소": "부정은 크게, 긍정은 작게 보고 있지 않은지 균형 있게 재평가합니다.",
    "감정적 추론": "'그렇게 느끼니까 사실'이라는 연결을 풀고 감정과 사실을 분리합니다.",
    "개인화": "모든 책임을 자신에게 돌리는 부분에서 상황·타인 요인을 함께 봅니다.",
    "낙인찍기": "행동 하나를 정체성 전체('나는 실패자')로 붙이지 않도록 분리합니다.",
    "부정적 편향": "잘 된 부분·중립적인 부분도 시야에 들어오게 균형 잡힌 회고를 돕습니다.",
    "긍정 축소화": "성취를 '운'으로 깎아내리는 패턴을 짚고 그대로 인정하게 돕습니다.",
    "'해야 한다' 진술": "'반드시 ~해야 한다'의 유연한 대안('~하면 좋겠다')을 함께 만듭니다.",
    "정상": "교정하려 들지 말고 지지와 공감 중심으로 반응합니다.",
    "불충분": "단정하지 말고 상황을 더 들려달라고 부드럽게 요청합니다.",
}
# 목록에 없는 라벨이 오면 쓰는 기본 지침
DEFAULT_GUIDANCE = "단정하지 말고, 공감 후 근거를 함께 살펴보는 CBT 접근을 사용합니다."

# 검색된 참고자료를 프롬프트에 붙일 때의 머리말
RAG_HEADER = "[참고 자료]\n검색된 상담 기법 자료입니다. 자연스럽게 녹여 쓰고 그대로 나열하지 않습니다."


def _base() -> str:
    """모든 전략이 공유하는 공통 앞부분 (페르소나 + 스타일 + 안전)."""
    return "\n\n".join([PERSONA, STYLE_RULES, SAFETY_RULES])


def _rag(chunks: list[dict]) -> str:
    """검색된 참고자료를 '- 내용' 목록으로 붙인다. 자료가 없으면 빈 문자열."""
    if not chunks:
        return ""
    return f"\n\n{RAG_HEADER}\n" + "\n".join(f"- {c['content']}" for c in chunks)


def build_cbt_label_guided(primary: str, chunks: list[dict], secondary: tuple = ()) -> str:
    """기본 전략: 분류 라벨의 접근 지침 + 참고자료를 포함한 CBT 상담 프롬프트.

    secondary: 멀티라벨 분류기가 primary 와 "함께 선택한" 부차 왜곡 라벨들
    (flow 가 score 내림차순·상한 적용까지 끝내서 넘겨준다). 주 지침이 상담의
    중심을 잡고, 보조 지침은 함께 관찰된 패턴을 참고로만 덧붙인다 — 인접
    왜곡(예: 확대와 축소 ↔ 긍정 축소화)이 흔들려도 상담 방향이 안정되는 효과.
    """
    guidance = LABEL_GUIDANCE.get(primary, DEFAULT_GUIDANCE)
    head = f"[이번 발화의 분류] {primary}"
    if secondary:
        head += f" (함께 관찰됨: {', '.join(secondary)})"
    body = f"\n[접근 지침] {guidance}"
    for label in secondary:
        body += (f"\n[보조 지침 — {label}] {LABEL_GUIDANCE.get(label, DEFAULT_GUIDANCE)} "
                 "(주 접근을 유지하며 참고만 합니다)")
    return f"{_base()}\n\n{head}{body}{_rag(chunks)}"


def build_supportive(primary: str, chunks: list[dict], secondary: tuple = ()) -> str:
    """'정상' 발화용: 왜곡 교정 없이 지지·공감 중심. (secondary 미사용)"""
    return f"{_base()}\n\n[접근 지침] 인지왜곡 교정을 시도하지 말고 지지·공감·감정 반영 중심으로 응답합니다.{_rag(chunks)}"


def build_clarify(primary: str, chunks: list[dict], secondary: tuple = ()) -> str:
    """'불충분'/저확신 발화용: 단정하지 않고 상황을 더 물어보는 명확화 질문 중심."""
    return (f"{_base()}\n\n[접근 지침] 상황 정보가 부족합니다. 짧게 공감한 뒤, "
            "무슨 일이 있었는지 구체적으로 들려달라는 명확화 질문 중심으로 응답합니다.")


# --- '불충분' 완화 사다리 2~4차 전략 (구획 1 의 INSUFFICIENT_LADDER 와 짝) ---
# 같은 명확화 질문을 반복하면 취조처럼 느껴진다 — 단계마다 질문의 각도와 무게를 바꾼다.

def build_clarify_alt(primary: str, chunks: list[dict], secondary: tuple = ()) -> str:
    """2차: 직전에 물었던 것과 '다른 각도'로 접근한다 (상황을 물었으면 감정을, 감정을
    물었으면 상황·시점을). 같은 질문의 반복이라는 인상을 지우는 게 목적."""
    return (f"{_base()}\n\n[접근 지침] 조금 전에도 상황을 여쭤봤지만 아직 구체적인 "
            "이야기가 나오지 않았습니다. 같은 질문을 반복하지 말고, 이전과 다른 각도에서 "
            "하나만 물어봅니다 — 상황을 물었다면 이번엔 그때의 감정이나 몸의 반응을, "
            "감정을 물었다면 언제부터였는지를. 질문은 한 개만, 부드럽게.")


def build_clarify_light(primary: str, chunks: list[dict], secondary: tuple = ()) -> str:
    """3차: 대답의 부담 자체를 낮춘다. 긴 설명을 요구하지 않는다."""
    return (f"{_base()}\n\n[접근 지침] 내담자가 말을 아끼고 있습니다. 자세한 설명을 "
            "요구하지 말고, 아주 가볍게 답할 수 있는 질문 하나로 낮춥니다 — "
            "\"한 단어로도 괜찮아요\", \"예/아니오로만 답하셔도 돼요\" 같은 식으로. "
            "대답하지 않아도 괜찮다는 여지를 함께 남깁니다.")


def build_accompany(primary: str, chunks: list[dict], secondary: tuple = ()) -> str:
    """4차(수용·동행 모드): 질문을 멈춘다. 연속된 짧은 대답은 말하기 어렵다는 신호로
    해석하고, 캐묻는 대신 곁에 머무른다. '잘 지내고 있다'고 가정하지 않는다."""
    return (f"{_base()}\n\n[접근 지침] 내담자가 여러 차례 짧게만 답했습니다. 이는 지금 "
            "말로 꺼내기 어렵다는 신호일 수 있습니다. 이번 답변에서는 질문을 하지 않습니다. "
            "대신: ① 말하기 어려울 수 있음을 있는 그대로 인정하고 ② 지금까지 보인 감정을 "
            "짧게 반영하며 ③ 준비될 때까지 기다리겠다고, 여기 있겠다고 전합니다. "
            "밝은 화제 전환이나 해결책 제시를 하지 않습니다. 2~4문장, 물음표 없이.")


# 전략 이름 → 함수 매핑. 새 전략을 추가하면 여기에 등록하고 구획 1 에서 이름으로 쓴다
PROMPT_STRATEGIES = {
    "cbt_label_guided": build_cbt_label_guided,
    "supportive": build_supportive,
    "clarify": build_clarify,
    "clarify_alt": build_clarify_alt,        # 사다리 2차
    "clarify_light": build_clarify_light,    # 사다리 3차
    "accompany": build_accompany,            # 사다리 4차 — 질문 중단·수용·동행
}


def build_llm_messages(strategy: str, primary: str, chunks: list[dict],
                       prior_messages: list[dict], user_text: str,
                       secondary_labels: list[str] | None = None) -> list[dict]:
    """LLM 에 보낼 최종 메시지 목록: [시스템 프롬프트, 이전 대화..., 이번 발화].

    secondary_labels: 멀티라벨 분류기가 함께 선택한 부차 왜곡들 (cbt_label_guided
    전략만 보조 지침으로 사용, 나머지 전략은 무시). flow 가 상한 적용 후 전달.
    """
    build = PROMPT_STRATEGIES.get(strategy, build_cbt_label_guided)
    return [{"role": "system", "content": build(primary, chunks, tuple(secondary_labels or ()))},
            *prior_messages,
            {"role": "user", "content": user_text}]


# ══════════════════════════════════════════════════════════════════════════
# [구획 3] 위기 대응 — 위험(자살/자해) 발화 감지 시의 고정 응답
#
# 이때는 AI(LLM)에게 답변을 맡기지 않는다 — 잘못된 생성 답변의 위험을 없애기 위해
# 사람이 미리 써 둔 메시지와 전문 상담 핫라인을 그대로 내보낸다.
# 메시지 문구와 연락처를 바꾸려면 아래 CRISIS_MESSAGE / HOTLINES 만 수정하면 된다.
# ══════════════════════════════════════════════════════════════════════════

# 24시간 전국 공통 위기 상담 창구 (지역별 창구는 아래 가이드 참고)
HOTLINES = [
    {"name": "자살예방상담전화", "phone": "1393", "hours": "24시간"},
    {"name": "정신건강위기상담전화", "phone": "1577-0199", "hours": "24시간"},
    {"name": "청소년전화", "phone": "1388", "hours": "24시간"},
]

# 위험 감지 시 그대로 출력되는 고정 메시지 (운영 정책에 맞게 수정)
CRISIS_MESSAGE = (
    "지금 많이 힘들고 고통스러우신 것 같아요. 무엇보다 당신의 안전이 가장 중요합니다. "
    "혼자 견디지 마시고, 아래 전문 상담 창구에 지금 연락해 주세요. 24시간 언제든 도움을 받을 수 있어요."
)


# 지역 연락처를 전국 공통 앞에 최대 몇 개까지 붙일지 (사람 편집)
REGIONAL_HOTLINES_MAX = 3

# 배포 DB(kfsp_centers)의 한글 필드 → 프론트로 나가는 영문 키 매핑.
# 스키마가 바뀌면 이 표만 고치면 된다 (파티션키=시도, hours 필드는 없어 주소로 대체).
KFSP_FIELD_MAP = {"name": "기관명", "phone": "전화", "address": "주소", "type": "유형"}

_hotline_container = None  # Cosmos 컨테이너 — 첫 조회 때 1회 생성해 재사용


def _get_cosmos_client():
    """세션 저장소와 같은 Cosmos 계정(COSMOS_*)에 접속. 이 기능을 켠 경우에만 SDK 를 import."""
    from azure.cosmos import CosmosClient

    conn = os.getenv("COSMOS_CONNECTION_STRING", "")
    if conn:
        return CosmosClient.from_connection_string(conn)
    endpoint, key = os.getenv("COSMOS_ENDPOINT", ""), os.getenv("COSMOS_KEY", "")
    if not endpoint or not key:
        raise ValueError("Cosmos 조회에는 COSMOS_ENDPOINT + COSMOS_KEY (또는 COSMOS_CONNECTION_STRING) 필요")
    return CosmosClient(endpoint, credential=key)


def _get_hotline_container():
    """지역 연락처 컨테이너 연결. 컨테이너 이름만 HOTLINE_CONTAINER 로 지정한다.
    배포 DB 는 kfsp_centers (파티션키 /시도)."""
    global _hotline_container
    if _hotline_container is None:
        database = settings.HOTLINE_DATABASE or os.getenv("COSMOS_DATABASE", "")
        if not database:
            raise ValueError("hotline lookup requires HOTLINE_DATABASE or COSMOS_DATABASE")
        _hotline_container = _get_cosmos_client().get_database_client(database) \
                                                 .get_container_client(settings.HOTLINE_CONTAINER)
    return _hotline_container


def lookup_regional_hotlines(region: str | None, district: str | None = None) -> list[dict]:
    """내담자 지역(시도)의 상담기관 연락처 조회 (블로킹 — crisis_payload 가 to_thread 로 감싼다).

    구현 완료·기본 잠금 상태: HOTLINE_CONTAINER 가 비어 있으면 빈 목록(기능 꺼짐).
    켜는 법: ① .env 에 HOTLINE_CONTAINER=kfsp_centers
            ② 프론트가 요청 metadata.region 에 정규 시도명("서울특별시" 등)을 넣어 보내면 끝.
    배포 DB(kfsp_centers) 문서 예(값은 예시 플레이스홀더 — 실제 연락처는 DB 에만 둔다):
        {"시도":"강원특별자치도","시군구":"○○시","기관명":"○○시자살예방센터",
         "전화":"033-000-0000","주소":"강원특별자치도 ○○시 ○○로 00"}

    district(시군구): 지금은 껍데기(seam) — 값이 오면 시도 안에서 한 번 더 좁힌다.
        위치 기반 기능이 생기기 전까지는 보통 None 이라 시도 단위로만 조회한다.
    """
    region = (region or "").strip()
    if not settings.HOTLINE_CONTAINER or not region:
        return []
    # 파티션 키(시도) 조회 — 지역 하나만 읽는 가장 싼 쿼리. 한글 필드라 c["필드"] 표기.
    query = 'SELECT c["기관명"], c["전화"], c["주소"], c["유형"] FROM c WHERE c["시도"] = @region'
    params = [{"name": "@region", "value": region}]
    district = (district or "").strip()
    if district:  # 껍데기: 시군구까지 좁힘 (도 지역에서 인접 시·군 잡음을 줄이려는 용도)
        query += ' AND c["시군구"] = @district'
        params.append({"name": "@district", "value": district})
    rows = _get_hotline_container().query_items(
        query=query, parameters=params, partition_key=region)
    out = []
    for row in rows:
        out.append({key: row.get(field, "") for key, field in KFSP_FIELD_MAP.items()})
        if len(out) >= REGIONAL_HOTLINES_MAX:
            break
    return out


# --- region 프로필 조회 루트: 설문으로 저장된 지역을 읽는다 (metadata.region 다음 우선순위) ---

def _region_from_profile(user_id: str) -> tuple[str | None, str | None]:
    """프로필 저장소에서 (시도, 시군구) 를 읽는다. 없으면 (None, None) — 블로킹 가능.

    저장소(app/profile.py)를 경유한다 — memory(개발)든 Cosmos user_profiles(배포)든
    같은 코드로 동작한다. 지역은 실제 등록 문서의 형태 그대로 location.sido/sigungu
    에서 읽는다 (설문 페이지가 저장하는 모양과 동일). 프로필이 없으면 조용히 (None, None)."""
    item = profile_repository.get(user_id)
    if not item:
        return (None, None)
    location = item.get("location") or {}
    return (location.get("sido") or None, location.get("sigungu") or None)


def resolve_region(input_meta: dict, user_id: str | None = None) -> tuple[str | None, str | None]:
    """이번 요청의 (시도, 시군구) 를 정한다 — 우선순위 체인.

    1순위: metadata.region / metadata.district (프론트 명시 override, 테스트·수동 선택 경로)
    2순위: 프로필 조회 — 설문(/v1/profile/survey)에 저장된 지역 (가상 ID/oid 기준)
    "정리" 시점: 위치 기반/프로필이 정식화되면 1순위 override 를 테스트 전용으로 강등하고
                2순위를 승격 — 코드 삭제 없이 우선순위만 바꾸면 된다.

    user_id 배선 완료(가구현 로그인): route(current_user) → flow → 여기까지 전달된다.
    가구현 단계에서는 "이미 등록된 ID 면 동작"이 원칙이라 anonymous(가상 ID 미전송)도
    등록된 프로필이 있으면 그 지역을 쓴다 — 단 anonymous 는 전 사용자가 공유하는
    계정이므로, 다중 사용자 환경이 되면 프론트가 반드시 x-user-id 를 보내야 한다.
    조회 실패는 경고만 남기고 지역 없이 진행한다 (위기 응답은 실패 금지가 최우선).
    """
    meta = input_meta.get("metadata") or {}
    region = (meta.get("region") or "").strip() or None
    district = (meta.get("district") or "").strip() or None
    if region:
        return (region, district)
    if user_id:
        try:
            return _region_from_profile(user_id)
        except Exception as exc:
            logger.warning("프로필 지역 조회 실패 — 지역 없이 진행: %s", exc)
    return (None, None)


async def crisis_payload(reason: str | None = None, region: str | None = None,
                         district: str | None = None) -> dict:
    """프론트로 보낼 위기 이벤트 한 덩어리 (flow.respond_stream 이 LLM 대신 이것을 출력).

    지역 조회는 어떤 경우에도 위기 응답을 막지 못한다 — 실패·타임아웃이면
    조용히 전국 공통 창구만 내보낸다 (위기 응답은 실패 금지가 최우선 원칙).
    """
    regional: list[dict] = []
    if settings.HOTLINE_CONTAINER and region:
        try:
            # 블로킹 Cosmos 조회를 스레드로 오프로딩 + 상한시간 초과 시 포기
            regional = await asyncio.wait_for(
                asyncio.to_thread(lookup_regional_hotlines, region, district),
                timeout=settings.HOTLINE_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.warning("지역 연락처 조회 실패 — 전국 공통 창구만 출력: %s", exc)
    return {
        "type": "crisis",
        "blocked": True,          # 이 턴은 AI 답변이 차단됐다는 표시
        "reason": reason,         # 차단 사유 (예: self_harm_signal)
        "message": CRISIS_MESSAGE,
        "resources": [*regional, *HOTLINES],  # 지역 창구 먼저, 전국 공통 다음
    }
