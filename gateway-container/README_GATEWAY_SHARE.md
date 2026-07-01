# Gateway Container 공유 안내서

이 문서는 `gateway-container/` 폴더를 공유하거나 검토할 때 필요한 요약 안내입니다.
실제 키와 비밀값은 포함하지 않습니다.

## 포함 범위

```text
services/api-gateway/  FastAPI 게이트웨이 애플리케이션
services/common/       LLM, Azure Search, Speech 공통 클라이언트
services/retrieve/     Retriever provider 래퍼
scripts/               게이트웨이 회귀 테스트 스크립트
API_CONTRACT.md        프론트엔드와 테스트용 API/SSE 계약서
.env.example           안전한 환경변수 템플릿
```

## 빌드 경로

리포지토리 루트 기준으로 실행합니다.

```bash
az acr build \
  -r "$ACR" \
  -t gateway:<TAG> \
  -f services/api-gateway/Dockerfile \
  gateway-container
```

`gateway-container/.dockerignore`는 기본 차단 방식입니다. 컨테이너 빌드에는 `services/api-gateway/`, `services/common/`, `services/retrieve/`만 포함됩니다.

## 주요 API

자세한 계약은 `API_CONTRACT.md`를 기준으로 합니다.

```text
GET  /healthz
POST /v1/classify
POST /v1/respond
GET  /v1/sessions/{session_id}
```

테스트/운영 배포에서는 `/healthz`를 제외한 API에 `x-api-key` 헤더가 필요합니다.

## SSE 이벤트 요약

```text
meta            분류/세션 메타데이터
chunks          검색된 RAG 청크
token           스트리밍 LLM 텍스트 토큰
crisis          자해/위기 안전 배리어 응답
stt             speech-to-text 상태/결과
tts             text-to-speech 상태/결과
input_required  입력은 수락됐지만 transcript/text가 부족한 상태
done            스트림 완료
```

## 테스트 스크립트

리포지토리 루트 기준으로 실행합니다.

```bash
python gateway-container/scripts/gateway_sse_text_test.py
python gateway-container/scripts/gateway_sse_transcript_test.py
python gateway-container/scripts/gateway_sse_tts_test.py
python gateway-container/scripts/gateway_sse_audio_stt_test.py
python gateway-container/scripts/gateway_cosmos_session_test.py
```

필수 환경변수:

```bash
export GW_FQDN=<api-gateway-fqdn>
export API_KEY_VALUE=<gateway-api-key>
```
