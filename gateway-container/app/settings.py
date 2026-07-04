"""[설정] 서버의 모든 조정값을 환경변수에서 읽는 곳.

환경변수 = 코드 밖(.env 파일이나 Azure 설정)에서 주입하는 값. 키/주소처럼
환경마다 다르거나 비밀인 값을 코드에 하드코딩하지 않기 위해 쓴다.
os.getenv("이름", "기본값") = 환경변수가 없으면 기본값 사용.
새 설정을 추가하면 .env.example 에도 같이 기록해 팀원이 알 수 있게 한다.
"""
import os


def _bool(name: str, default: bool = False) -> bool:
    """환경변수는 전부 문자열이라, "true"/"1" 을 파이썬 불리언으로 바꿔 준다."""
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in ("true", "1")


# --- 분류기: 인지왜곡 분류 모델이 떠 있는 내부 컨테이너 주소 ---
KLUE_API_URL = os.getenv("KLUE_API_URL", "http://cogdist:8000")
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))

# --- 인증 / CORS ---
# API_KEY_REQUIRED=true 면 모든 /v1 요청에 x-api-key 헤더가 있어야 한다
API_KEY = os.getenv("API_KEY", "")
API_KEY_REQUIRED = _bool("API_KEY_REQUIRED", False)
# AUTH_MODE: api_key(현행) | entra(아래 ENTRA_* 3개만 채우면 즉시 켜짐 — api/v1.py 구획 2)
AUTH_MODE = os.getenv("AUTH_MODE", "api_key").strip().lower()
# Microsoft Entra External ID (AUTH_MODE=entra 일 때만 사용)
ENTRA_TENANT_ID = os.getenv("ENTRA_TENANT_ID", "")   # 테넌트 GUID (ISSUER 생략 시 여기서 유도)
ENTRA_CLIENT_ID = os.getenv("ENTRA_CLIENT_ID", "")   # 이 API 앱 등록의 client id (토큰 aud)
ENTRA_ISSUER = os.getenv("ENTRA_ISSUER", "")         # 예: https://{테넌트GUID}.ciamlogin.com/{테넌트GUID}/v2.0
# 브라우저에서 이 서버를 호출할 수 있는 프론트엔드 주소 목록 (쉼표 구분)
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173").split(",")
    if origin.strip()
]

# --- Azure Content Safety: 위험(자살/자해 등) 발화 탐지 서비스 ---
CONTENT_SAFETY_ENABLED = _bool("CONTENT_SAFETY_ENABLED", False)
CONTENT_SAFETY_ENDPOINT = os.getenv("CONTENT_SAFETY_ENDPOINT", "")
CONTENT_SAFETY_KEY = os.getenv("CONTENT_SAFETY_KEY", "")
# 위험 점수(severity)가 이 값 이상이면 차단. Azure 기준 0/2/4/6 단계 (0=안전, 2=mild, 4=medium, 6=high)
#   2 → 4 상향 (2026-07 실험 브랜치): threshold 2 는 자해 언급이 없는 강한 자기비하까지
#   위기로 오차단했다 — 2026-07-04 턴제 실험에서 낙인찍기 시나리오 5턴 중 4턴이 차단되어
#   정작 CBT 상담이 가장 필요한 사용자가 핫라인 카드만 받는 문제 확인(보고서 발견 1).
#   4 = medium 이상만 차단. 명시적 위기 문장이 새어 나가지 않는지(미차단 0건)
#   scripts/safety_threshold_probe.py 로 검증한 뒤 팀 논의로 확정한다.
#   주의: 배포(ACA) 환경변수에 CONTENT_SAFETY_THRESHOLD 가 설정돼 있으면 그 값이
#   이 기본값보다 우선한다 — 브랜치 테스트 배포 시 env 를 지우거나 4 로 맞출 것.
CONTENT_SAFETY_THRESHOLD = int(os.getenv("CONTENT_SAFETY_THRESHOLD", "4"))
CONTENT_SAFETY_TIMEOUT = float(os.getenv("CONTENT_SAFETY_TIMEOUT", "5"))

# --- RAG: 검색된 참고자료 중 프롬프트에 넣을 문서 개수 ---
# 구 이름(RERANK_TOP_N)으로 배포된 환경도 계속 동작하도록 둘 다 읽는다.
# (라벨 가산점 rerank 는 2026-07 제거 — respond/flow.py [구획 3] 주석 참고)
RAG_TOP_N = int(os.getenv("RAG_TOP_N", os.getenv("RERANK_TOP_N", "4")))

# --- 컨텍스트 정책: 저확신 강등 하한 ---
# 왜곡 라벨인데 확신이 이 값 미만이면 CBT 프롬프트 대신 명확화(clarify)로 강등.
# 0 = 꺼짐(현행). sigmoid multi_label 모델은 점수가 낮게 깔리므로 값 설정 시 주의.
POLICY_MIN_CONFIDENCE = float(os.getenv("POLICY_MIN_CONFIDENCE", "0.0"))

# --- 위기 지역 연락처 DB (respond/policy.py 구획 3) ---
# HOTLINE_CONTAINER 를 채우면 켜짐 — 세션과 같은 Cosmos 계정(COSMOS_*)을 쓴다.
# 실제 배포 DB 예: 컨테이너 kfsp_centers (파티션키 /시도, 필드 기관명·전화·주소·시도·시군구).
HOTLINE_CONTAINER = os.getenv("HOTLINE_CONTAINER", "")
HOTLINE_DATABASE = os.getenv("HOTLINE_DATABASE", "")            # 비우면 COSMOS_DATABASE 사용
HOTLINE_TIMEOUT_SECONDS = float(os.getenv("HOTLINE_TIMEOUT_SECONDS", "3"))  # 초과 시 전국 공통만

# --- 사용자 프로필 DB (respond/policy.py 구획 3 의 region DB 조회 루트) ---
# USER_PROFILE_CONTAINER 를 채우면 region 을 프로필에서도 조회한다(우선순위는 metadata.region 이 위).
# 예: 컨테이너 user_profiles (파티션키 /user_id, 필드 시도·시군구). 비어 있으면 이 경로는 휴면.
USER_PROFILE_CONTAINER = os.getenv("USER_PROFILE_CONTAINER", "")
USER_PROFILE_DATABASE = os.getenv("USER_PROFILE_DATABASE", "")  # 비우면 COSMOS_DATABASE 사용
USER_PROFILE_TIMEOUT_SECONDS = float(os.getenv("USER_PROFILE_TIMEOUT_SECONDS", "3"))

# --- 세션(대화 기록) 저장소: memory(개발/테스트용, 서버 재시작 시 소멸) | cosmos(운영 DB) ---
SESSION_REPOSITORY = os.getenv("SESSION_REPOSITORY", "memory")
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "3600"))      # 세션 유효시간(초)
SESSION_MAX_TURNS = int(os.getenv("SESSION_MAX_TURNS", "20"))            # 세션당 최대 저장 턴 수
SESSION_CONTEXT_TURNS = int(os.getenv("SESSION_CONTEXT_TURNS", "6"))     # LLM 에 주는 최근 대화 수
