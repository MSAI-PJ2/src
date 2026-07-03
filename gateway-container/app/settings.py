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
# 위험 점수(severity)가 이 값 이상이면 차단. Azure 기준 0/2/4/6 단계
CONTENT_SAFETY_THRESHOLD = int(os.getenv("CONTENT_SAFETY_THRESHOLD", "2"))
CONTENT_SAFETY_TIMEOUT = float(os.getenv("CONTENT_SAFETY_TIMEOUT", "5"))

# --- RAG: 검색된 참고자료 중 프롬프트에 넣을 문서 개수 ---
RERANK_TOP_N = int(os.getenv("RERANK_TOP_N", "4"))
# 라벨 일치 문서 가산점의 크기와 발동 조건 (기본값 = 현행 동작, respond/flow.py 구획 3 참고)
RERANK_BIAS_WEIGHT = float(os.getenv("RERANK_BIAS_WEIGHT", "0.3"))
RERANK_BIAS_MIN_CONFIDENCE = float(os.getenv("RERANK_BIAS_MIN_CONFIDENCE", "0.5"))
# 발동 판정 기준: score(확신 점수 기준, 현행 기본) | selected | either
# ※ cogdist v2(ml/cogdist-server)부터 primary 라벨은 항상 selected=true 로 오므로
#   selected 소스는 왜곡 발화에 무조건 발동한다(신뢰도 게이트 없음).
#   v2 multi 모델에서는 score 소스 + RERANK_BIAS_MIN_CONFIDENCE=0.55(서버 threshold와
#   동일값) 권장 — 저확신 primary(예: 0.43)에 가산점이 붙는 것을 막는다.
RERANK_BIAS_SOURCE = os.getenv("RERANK_BIAS_SOURCE", "score").strip().lower()

# --- 컨텍스트 정책: 저확신 강등 하한 ---
# 왜곡 라벨인데 확신이 이 값 미만이면 CBT 프롬프트 대신 명확화(clarify)로 강등.
# 0 = 꺼짐(현행). sigmoid multi_label 모델은 점수가 낮게 깔리므로 값 설정 시 주의.
POLICY_MIN_CONFIDENCE = float(os.getenv("POLICY_MIN_CONFIDENCE", "0.0"))

# --- 위기 지역 연락처 DB (respond/policy.py 구획 3) ---
# HOTLINE_CONTAINER 를 채우면 켜짐 — 세션과 같은 Cosmos 계정(COSMOS_*)을 쓴다.
HOTLINE_CONTAINER = os.getenv("HOTLINE_CONTAINER", "")
HOTLINE_DATABASE = os.getenv("HOTLINE_DATABASE", "")            # 비우면 COSMOS_DATABASE 사용
HOTLINE_TIMEOUT_SECONDS = float(os.getenv("HOTLINE_TIMEOUT_SECONDS", "3"))  # 초과 시 전국 공통만

# --- 세션(대화 기록) 저장소: memory(개발/테스트용, 서버 재시작 시 소멸) | cosmos(운영 DB) ---
SESSION_REPOSITORY = os.getenv("SESSION_REPOSITORY", "memory")
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "3600"))      # 세션 유효시간(초)
SESSION_MAX_TURNS = int(os.getenv("SESSION_MAX_TURNS", "20"))            # 세션당 최대 저장 턴 수
SESSION_CONTEXT_TURNS = int(os.getenv("SESSION_CONTEXT_TURNS", "6"))     # LLM 에 주는 최근 대화 수
