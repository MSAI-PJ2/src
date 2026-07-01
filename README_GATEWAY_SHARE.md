# API Gateway 최신 공유 코드

이 폴더는 Azure Container Apps `api-gateway` 공유용 최신 코드입니다. 실제 비밀 키는 포함하지 않았습니다.

## 현재 상태

```text
Gateway revision: api-gateway--0000016
Git main: b8b6084
Framework: FastAPI + Uvicorn
Python: 3.11
LLM: Azure OpenAI gpt-4.1-mini
LLM token parameter: max_completion_tokens via AZURE_OPENAI_MAX_COMPLETION_TOKENS=4096
Auth: x-api-key required
Cogdist: cogdistmodel--0000004, Azure Files subPath=v2
RAG: cbt-rag-search / cbt-rag-index
Speech: Azure Speech SDK + pydub + ffmpeg 유지
Status: Azure 회귀 테스트 통과
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
services/api-gateway/app/events.py    SSE serialization
services/api-gateway/app/safety.py    Content Safety + keyword fallback
services/api-gateway/app/tts.py       TTS payload builder
services/api-gateway/app/ranking.py   RAG rerank helper
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

## 최근 Azure 검증

```text
revision: api-gateway--0000016
healthz: PASS
auth 401: PASS
classify: PASS
respond text: PASS
crisis: PASS
TTS: PASS
audio STT success: PASS
audio STT failure: PASS
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
- `.env`, `__pycache__`, `*.pyc`, smoke output은 공유 대상에서 제외했습니다.
