# Gateway Container Share Guide

이 문서는 `gateway-container/` 폴더를 공유하거나 검토할 때 필요한 요약 안내입니다.
실제 키/비밀값은 포함하지 않습니다.

## 포함 범위

```text
api-gateway/      FastAPI gateway application
common/           LLM, Azure Search, Speech shared clients
retrieve/         Retriever provider wrapper
scripts/          Gateway regression test scripts
API_CONTRACT.md   API/SSE contract for frontend and tests
.env.example      Safe environment variable template
```

## 배포 빌드 경로

리포지토리 루트 기준:

```bash
az acr build \
  -r "$ACR" \
  -t gateway:<TAG> \
  -f api-gateway/Dockerfile \
  gateway-container
```

`gateway-container/.dockerignore`는 기본 차단 방식입니다. 컨테이너 빌드에는 `api-gateway/`, `common/`, `retrieve/`만 들어갑니다.

## 주요 API

상세 계약은 `API_CONTRACT.md`를 기준으로 합니다.

```text
GET  /healthz
POST /v1/classify
POST /v1/batch-classify
POST /v1/respond
POST /v1/sessions
GET  /v1/sessions/{session_id}
```

`/healthz`를 제외한 주요 API는 `x-api-key`가 필요합니다.

## 보안 주의

```text
- .env 파일은 커밋하지 않는다.
- API_KEY, Azure OpenAI key, Content Safety key, Search key, Speech key, Cosmos key는 secretref로 주입한다.
- 프론트엔드에는 gateway API key 또는 Azure key를 직접 넣지 않는다.
- Cosmos DB 세션 기록은 개인정보/상담기록 보존 정책 확정 전까지 최소 수집 원칙을 따른다.
```

## 테스트 스크립트

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
