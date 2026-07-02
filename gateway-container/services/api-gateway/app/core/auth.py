"""게이트웨이 인증 경계.

현행(운영): x-api-key 헤더 검사 (AUTH_MODE=api_key, 기본값).
도입 예정: Microsoft Entra External ID (OIDC) — 아래 [사람 작업 가이드] 참고.

라우터는 이 모듈의 require_api_key / current_user 만 사용한다.
인증 방식이 바뀌어도 라우터 코드는 수정할 필요가 없도록 유지한다.
"""
from fastapi import Header, HTTPException

from . import settings


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """현행 임시 인증: API_KEY_REQUIRED=true 이면 x-api-key 헤더를 검사한다."""
    if settings.API_KEY_REQUIRED and x_api_key != settings.API_KEY:
        raise HTTPException(401, "invalid api key")


async def current_user(authorization: str | None = Header(default=None)) -> str:
    """요청의 사용자 식별자(user_id)를 반환한다.

    현재는 로그인 없이 익명("anonymous")으로 동작하고, 세션은 클라이언트가 보내는
    session_id 로만 구분된다. Entra External ID 도입 시 이 함수가 JWT 에서
    user_id 를 추출하는 유일한 지점이 된다 (라우터/오케스트레이터는 그대로).

    ── [사람 작업 가이드] Microsoft Entra External ID(OIDC) 로그인 연동 ──────────
    전체 흐름 (I/O 다이어그램 "로그인·Identity" 참고):
      프론트가 Entra External ID 로 OIDC 로그인 → JWT(access token) 발급
      → 요청마다 Authorization: Bearer <token> 첨부
      → 게이트웨이가 토큰 검증(JWKS) 후 user_id 추출 → 세션을 user_id 로 스코프

    구현 순서:
      1. Entra External ID 테넌트에 앱 등록(SPA + API) 후 아래 환경변수 준비
           ENTRA_TENANT_ID       (예: <tenant>.ciamlogin.com 테넌트의 GUID)
           ENTRA_CLIENT_ID       (이 API 를 나타내는 앱 등록의 client id = aud)
           ENTRA_ISSUER          (예: https://<tenant>.ciamlogin.com/<tenant-id>/v2.0)
      2. requirements.txt 에 PyJWT[crypto] (또는 python-jose) 추가
      3. 이 함수에서:
           - Authorization 헤더에서 Bearer 토큰 파싱 (없으면 401)
           - jwt.PyJWKClient(f"{ENTRA_ISSUER}/discovery/v2.0/keys") 로 서명키 조회
             (JWKS 클라이언트는 모듈 전역에 1회 생성 — 요청마다 만들지 말 것)
           - jwt.decode(token, key, algorithms=["RS256"],
                        audience=ENTRA_CLIENT_ID, issuer=ENTRA_ISSUER)
           - 검증 실패 → HTTPException(401)
           - user_id = claims["oid"] (또는 "sub") 반환
      4. AUTH_MODE=entra 로 전환하고, 세션 저장소에서 session 문서에 user_id 를
         저장/조회 조건에 포함해 "내 세션만 접근" 을 보장 (session/ 참고)
      5. require_api_key 는 서버-서버 내부 호출용으로만 남기거나 제거
    ──────────────────────────────────────────────────────────────────────────
    """
    if settings.AUTH_MODE == "entra":
        # 위 가이드 구현 전까지는 명시적으로 실패시켜 설정 실수를 조기에 드러낸다.
        raise HTTPException(501, "AUTH_MODE=entra is not implemented yet (see core/auth.py)")
    return "anonymous"
