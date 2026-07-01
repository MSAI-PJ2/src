# API Gateway 최신 공유 코드

이 폴더는 Azure Container Apps `api-gateway` 공유용 최신 코드입니다. 실제 비밀 키는 포함하지 않았습니다.

## 현재 상태

```text
Gateway revision: api-gateway--0000023
Gateway image: acrregistry001.azurecr.io/gateway:refactor-3-3-cosmos-session-20260701
Git branch: main
Git commit: 32f0174 기준 + 3-3 Cosmos session branch 작업 중
Framework: FastAPI + Uvicorn
Python: 3.11
LLM: Azure OpenAI gpt-4.1-mini
LLM token parameter: request llm.max_completion_tokens, capped by AZURE_OPENAI_MAX_COMPLETION_TOKENS_LIMIT
Auth: x-api-key required
Cogdist: cogdistmodel--0000004, Azure Files subPath=v2
RAG: cbt-rag-search / cbt-rag-index
Speech: Azure Speech SDK + pydub + ffmpeg 유지
Status: refactor 3-3 Cosmos session persistence PASS
```

## 포함 항목

```text
services/api-gateway/   FastAPI gateway
services/common/        LLM client, including GPT-4.1 mini max_completion_tokens patch
services/retrieve/      Azure AI Search retriever client
.env.example            공유용 환경변수 샘플, 실제 키 없음
API_CONTRACT.md         프론트/테스트용 API 계약
scripts/                Gateway SSE 회귀 테스트 스크립트
```

## 내부 모듈 구조

```text
services/api-gateway/app/main.py      FastAPI route entrypoint
services/api-gateway/app/dag.py       respond orchestration
services/api-gateway/app/adapters.py  Classifier/Safety/Retriever/LLM/Speech service adapter boundary
services/api-gateway/app/request_context.py
                                      /v1/respond 입력 정규화 context
services/api-gateway/app/repositories/session_repository.py
                                      memory/Cosmos DB session repository boundary
services/api-gateway/app/payloads.py  SSE/API payload builder
services/api-gateway/app/turns.py     session turn builder
services/api-gateway/app/prompts.py   LLM message builder
services/api-gateway/app/events.py    SSE serialization
services/api-gateway/app/safety.py    Content Safety + keyword fallback
services/api-gateway/app/tts.py       TTS payload builder
services/api-gateway/app/ranking.py   RAG rerank helper
```

## 3차-1 리팩터링 의도

```text
목표: Cosmos DB 연결 직전에 dag.py/main.py가 저장소 구현에 직접 의존하지 않게 경계를 만든다.
현재: repositories/session_repository.py가 기존 in-memory sessions.py를 감싼다.
다음: Cosmos DB adapter를 추가해도 /v1/respond, /v1/sessions API 계약은 유지한다.
주의: 이번 단계는 DB 연결이 아니라 교체 가능한 경계 생성이다.
```

## 3차-2 리팩터링 의도

```text
목표: dag.py/main.py가 외부 서비스 호출 세부사항에 직접 의존하지 않도록 adapter boundary를 만든다.
대상: classifier, safety, retriever, LLM, speech STT/TTS
현재: adapters.py가 기존 구현을 감싸므로 API 계약과 런타임 동작은 유지한다.
다음: Cosmos DB 연결 전 Gateway orchestration 비대화를 줄인다.
```

## 3차-3 Cosmos DB 세션 저장소 의도

```text
목표: 이미 배포된 Cosmos DB NoSQL 컨테이너를 Gateway 세션 저장소로 선택 연결한다.
기본: SESSION_REPOSITORY=memory는 기존 임시 메모리 저장소 유지
전환: SESSION_REPOSITORY=cosmos이면 Cosmos DB에 session turn 저장
컨테이너 파티션 키: /session_id
문서 id: session_id
주의: Cosmos key는 파일/프론트에 넣지 말고 Azure Container App secretref로 주입
```

## 주요 엔드포인트

```text
GET  /healthz
POST /v1/classify
POST /v1/batch-classify
POST /v1/respond
POST /v1/sessions
GET  /v1/sessions/{session_id}
```

## 호출 시 필수 헤더

```text
x-api-key: <GATEWAY_API_KEY>
```

## SSE 계약 요약

```text
text:       meta -> chunks -> token... -> done
audio STT: stt(processing) -> stt(completed/error) -> meta/chunks/token... 또는 input_required -> done
TTS:        meta -> chunks -> token... -> tts -> done
crisis:     meta -> crisis -> done
```

상세 계약은 `API_CONTRACT.md`를 기준으로 합니다.

## LLM 응답 길이 제어

`/v1/respond` 요청 body에 선택적으로 `llm.max_completion_tokens`를 넣을 수 있습니다.

```json
{
  "session_id": "long-answer-test-1",
  "text": "사람들 앞에 서면 다 망칠 것 같아요",
  "llm": {
    "max_completion_tokens": 2048
  }
}
```

서버는 요청값을 그대로 무제한 반영하지 않습니다.

```text
기본값: AZURE_OPENAI_MAX_COMPLETION_TOKENS       # MVP 권장 1200
상한값: AZURE_OPENAI_MAX_COMPLETION_TOKENS_LIMIT # Gateway 권장 상한 12000
요청값: llm.max_completion_tokens
실제값: min(요청값, 서버 상한값)
```

## 최근 Azure 검증

```text
revision: api-gateway--0000023
image: acrregistry001.azurecr.io/gateway:refactor-3-3-cosmos-session-20260701
healthz: PASS
auth 401: PASS
classify: PASS
respond text: PASS
long max_completion_tokens: PASS
short max_completion_tokens: PASS
session read: PASS
crisis: PASS
transcript: PASS
TTS: PASS
audio STT success: PASS
audio STT failure: PASS
Cosmos session persistence: PASS
```

## 컨테이너 빌드 예시

```bash
cd /path/to/api-gateway-latest
az acr build \
  -g 10ai_2nd_team3 \
  -r acrregistry001 \
  -t gateway:<tag> \
  -f services/api-gateway/Dockerfile \
  .
```

## 보안 주의

- `API_KEY`, `CONTENT_SAFETY_KEY`, `AZURE_SEARCH_API_KEY`, `AZURE_OPENAI_API_KEY`는 절대 파일에 넣지 말고 Azure Container App secret 또는 로컬 환경변수로 주입하세요.
- `COSMOS_KEY`도 동일하게 Azure Container App secret 또는 로컬 환경변수로만 주입하세요.
- `.env`, `__pycache__`, `*.pyc`, smoke output은 공유 대상에서 제외했습니다.

## 다음 작업: Document Intelligence OCR 연결

```text
목표: Azure AI Document Intelligence prebuilt-read를 Gateway 입력 경로에 연결한다.
현재: .env.example에 DOCINTEL_ENDPOINT / DOCINTEL_KEY / DOCINTEL_API_VERSION / DOCINTEL_MODEL_ID 예시 반영 완료
예상 입력: input_type=document, document.kind=base64|url
예상 SSE: document(processing) -> document(completed|error) -> meta/chunks/token... -> done
보안: DOCINTEL_KEY는 secretref, document.url은 SSRF 방어, base64는 크기 제한
```
