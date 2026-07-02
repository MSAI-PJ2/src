"""위기 핫라인 — 위험(자살/자해) 탐지 시 LLM 생성을 우회하고 고정 메시지를 출력한다.

호출 경로: safety_check(탐지) → context_policy.resolve → CRISIS_POLICY
          → crisis_payload()(이 모듈) → SSE 즉시 출력 후 종료.
메시지/연락처는 운영 정책에 맞게 이 파일에서 수정한다.
"""

# 24시간 위기 상담 창구 (전국 공통 — 지역 창구는 아래 가이드 참고)
HOTLINES = [
    {"name": "자살예방상담전화", "phone": "1393", "hours": "24시간"},
    {"name": "정신건강위기상담전화", "phone": "1577-0199", "hours": "24시간"},
    {"name": "청소년전화", "phone": "1388", "hours": "24시간"},
]

# 위험 탐지 시 출력할 고정 메시지 (운영 정책에 맞게 수정)
CRISIS_MESSAGE = (
    "지금 많이 힘들고 고통스러우신 것 같아요. 무엇보다 당신의 안전이 가장 중요합니다. "
    "혼자 견디지 마시고, 아래 전문 상담 창구에 지금 연락해 주세요. 24시간 언제든 도움을 받을 수 있어요."
)


def lookup_regional_hotlines(region: str | None) -> list[dict]:
    """지역 기반 유관기관 연락처 조회 — 아직 미구현(전국 공통 창구만 사용).

    ── [사람 작업 가이드] 위치 기반 유관기관 연락처 DB 조회 (도입 예정) ──────────
    목표: 내담자 지역의 정신건강복지센터/상담기관을 전국 공통 창구보다 먼저 노출.

    1. 데이터: 유관기관 연락처 DB (Cosmos DB 권장)
         컨테이너 예: hotline-directory, partition key = /region
         문서 예: {"region": "서울특별시 강남구", "name": "강남구 정신건강복지센터",
                   "phone": "02-XXX-XXXX", "hours": "평일 09-18시"}
    2. 입력: 프론트가 RespondIn.metadata 에 region 을 넣어 보낸다
         (예: {"metadata": {"region": "서울특별시 강남구"}})
         respond_flow 에서 input_meta["metadata"] 로 접근 가능.
    3. 구현: 이 함수에서 region 으로 Cosmos 를 조회해 list[dict] 반환.
         - Cosmos SDK 는 블로킹 → session/cosmos_repository.py 처럼 asyncio.to_thread 로
           감싸고, 이 함수와 crisis_payload 를 async 로 바꾼 뒤 respond_flow 의
           호출부에 await 를 붙인다.
         - 조회 실패/미등록 지역이면 반드시 빈 리스트를 반환해 전국 공통 창구가
           그대로 나가게 한다 (위기 응답은 어떤 경우에도 실패하면 안 됨).
    4. 반영: crisis_payload 의 resources 가 [지역 창구..., 전국 공통...] 순서가 된다.
    ──────────────────────────────────────────────────────────────────────────
    """
    return []


def crisis_payload(reason: str | None = None, region: str | None = None) -> dict:
    """위기 SSE 이벤트(고정 메시지). respond_stream 이 LLM 생성 대신 이것을 출력하고 종료한다."""
    return {
        "type": "crisis",
        "blocked": True,
        "reason": reason,
        "message": CRISIS_MESSAGE,
        "resources": [*lookup_regional_hotlines(region), *HOTLINES],
    }
