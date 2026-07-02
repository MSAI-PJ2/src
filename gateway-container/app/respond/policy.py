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
    rag_top_n: int | None = None  # 참고자료 개수 (None = settings.RERANK_TOP_N, 기본 4)
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

# [도입 예정] '불충분' 최근 N턴 재분류: 한 문장으로 판단이 안 되면 최근 사용자 발화
# 여러 개를 이어붙여 다시 분류하고, 왜곡 라벨이 나오면 그 라벨의 정책을 적용하는 기능.
# 구현 위치는 flow.respond_stream 의 resolve() 호출 직후. 분류기 호출이 1회 늘어나므로
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


# ══════════════════════════════════════════════════════════════════════════
# [구획 2] 시스템 프롬프트 — AI 상담사의 말투·태도·라벨별 접근법
#
# "시스템 프롬프트" = AI 에게 답변 생성 전에 주는 지시문. 답변 스타일을 바꾸고
# 싶으면 아래 문자열들(PERSONA / STYLE_RULES / SAFETY_RULES / LABEL_GUIDANCE)만
# 고치면 된다. 어떤 발화에 어떤 전략(build_xxx)을 쓸지는 구획 1 의 POLICIES 가 정한다.
# ══════════════════════════════════════════════════════════════════════════

# AI 상담사가 "누구인지" — 모든 전략의 프롬프트 맨 앞에 들어간다
PERSONA = (
    "당신은 한국어로 응답하는 인지행동치료(CBT) 기반 심리상담 보조자 '심서리'입니다. "
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


def build_cbt_label_guided(primary: str, chunks: list[dict]) -> str:
    """기본 전략: 분류 라벨의 접근 지침 + 참고자료를 포함한 CBT 상담 프롬프트."""
    guidance = LABEL_GUIDANCE.get(primary, DEFAULT_GUIDANCE)
    return f"{_base()}\n\n[이번 발화의 분류] {primary}\n[접근 지침] {guidance}{_rag(chunks)}"


def build_supportive(primary: str, chunks: list[dict]) -> str:
    """'정상' 발화용: 왜곡 교정 없이 지지·공감 중심."""
    return f"{_base()}\n\n[접근 지침] 인지왜곡 교정을 시도하지 말고 지지·공감·감정 반영 중심으로 응답합니다.{_rag(chunks)}"


def build_clarify(primary: str, chunks: list[dict]) -> str:
    """'불충분'/저확신 발화용: 단정하지 않고 상황을 더 물어보는 명확화 질문 중심."""
    return (f"{_base()}\n\n[접근 지침] 상황 정보가 부족합니다. 짧게 공감한 뒤, "
            "무슨 일이 있었는지 구체적으로 들려달라는 명확화 질문 중심으로 응답합니다.")


# 전략 이름 → 함수 매핑. 새 전략을 추가하면 여기에 등록하고 구획 1 에서 이름으로 쓴다
PROMPT_STRATEGIES = {
    "cbt_label_guided": build_cbt_label_guided,
    "supportive": build_supportive,
    "clarify": build_clarify,
}


def build_llm_messages(strategy: str, primary: str, chunks: list[dict],
                       prior_messages: list[dict], user_text: str) -> list[dict]:
    """LLM 에 보낼 최종 메시지 목록: [시스템 프롬프트, 이전 대화..., 이번 발화]."""
    build = PROMPT_STRATEGIES.get(strategy, build_cbt_label_guided)
    return [{"role": "system", "content": build(primary, chunks)},
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

_hotline_container = None  # Cosmos 컨테이너 — 첫 조회 때 1회 생성해 재사용


def _get_hotline_container():
    """지역 연락처 Cosmos 컨테이너 연결. 세션 저장소와 같은 계정(COSMOS_*)을 쓰고
    컨테이너 이름만 HOTLINE_CONTAINER 로 지정한다 (파티션 키 = /region 권장)."""
    global _hotline_container
    if _hotline_container is None:
        from azure.cosmos import CosmosClient  # cosmos 는 이 기능을 켠 경우에만 필요

        conn = os.getenv("COSMOS_CONNECTION_STRING", "")
        if conn:
            client = CosmosClient.from_connection_string(conn)
        else:
            endpoint, key = os.getenv("COSMOS_ENDPOINT", ""), os.getenv("COSMOS_KEY", "")
            if not endpoint or not key:
                raise ValueError("hotline lookup requires COSMOS_ENDPOINT + COSMOS_KEY (or COSMOS_CONNECTION_STRING)")
            client = CosmosClient(endpoint, credential=key)
        database = settings.HOTLINE_DATABASE or os.getenv("COSMOS_DATABASE", "")
        if not database:
            raise ValueError("hotline lookup requires HOTLINE_DATABASE or COSMOS_DATABASE")
        _hotline_container = client.get_database_client(database) \
                                   .get_container_client(settings.HOTLINE_CONTAINER)
    return _hotline_container


def lookup_regional_hotlines(region: str | None) -> list[dict]:
    """내담자 지역의 상담기관 연락처 조회 (블로킹 — crisis_payload 가 to_thread 로 감싼다).

    구현 완료·기본 잠금 상태: HOTLINE_CONTAINER 설정이 비어 있으면 빈 목록(기능 꺼짐).
    켜는 법: ① Cosmos 에 컨테이너 생성(예: hotline-directory, 파티션키 /region)
            문서 예 {"region":"서울특별시 강남구","name":"강남구 정신건강복지센터",
                     "phone":"02-...","hours":"평일 09-18시"}
           ② .env 에 HOTLINE_CONTAINER=hotline-directory
           ③ 프론트가 요청의 metadata.region 에 지역명을 넣어 보내면 끝.
    """
    region = (region or "").strip()
    if not settings.HOTLINE_CONTAINER or not region:
        return []
    rows = _get_hotline_container().query_items(
        query="SELECT c.name, c.phone, c.hours FROM c WHERE c.region = @region",
        parameters=[{"name": "@region", "value": region}],
        partition_key=region,  # 파티션 키 조회 — 지역 하나만 읽는 가장 싼 쿼리
    )
    out = []
    for row in rows:
        out.append({"name": row.get("name", ""), "phone": row.get("phone", ""),
                    "hours": row.get("hours", "")})
        if len(out) >= REGIONAL_HOTLINES_MAX:
            break
    return out


async def crisis_payload(reason: str | None = None, region: str | None = None) -> dict:
    """프론트로 보낼 위기 이벤트 한 덩어리 (flow.respond_stream 이 LLM 대신 이것을 출력).

    지역 조회는 어떤 경우에도 위기 응답을 막지 못한다 — 실패·타임아웃이면
    조용히 전국 공통 창구만 내보낸다 (위기 응답은 실패 금지가 최우선 원칙).
    """
    regional: list[dict] = []
    if settings.HOTLINE_CONTAINER and region:
        try:
            # 블로킹 Cosmos 조회를 스레드로 오프로딩 + 상한시간 초과 시 포기
            regional = await asyncio.wait_for(
                asyncio.to_thread(lookup_regional_hotlines, region),
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
