"""위기 핫라인 — 위험(자살/자해) 탐지 시 LLM 생성을 우회하고 '고정 메시지'를 출력하는 구조.

호출 경로: safety_check(탐지) → 안전 배리어 unsafe → crisis_payload()(이 모듈) → SSE 즉시 출력 후 종료.
탐지부는 현재 키워드 stub이며 Azure AI Content Safety로 교체 예정.
메시지/번호는 운영 정책에 맞게 이 파일에서 수정한다.
"""

# 24시간 위기 상담 창구 (고정 출력 메시지의 일부)
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


def crisis_payload(reason: str | None = None) -> dict:
    """위기 핫라인 SSE 이벤트(고정 메시지). respond_stream이 generate 대신 이것을 출력하고 종료한다."""
    return {
        "type": "crisis",
        "blocked": True,
        "reason": reason,
        "message": CRISIS_MESSAGE,
        "resources": HOTLINES,
    }
