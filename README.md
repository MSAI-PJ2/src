# API Gateway Latest Share

공유 목적: GitHub 연동용 최신 Gateway 코드 묶음입니다.

## 현재 반영 상태
- Framework: FastAPI + Uvicorn
- Container: Azure Container Apps / ACR
- Auth: `x-api-key` 게이트웨이 API 키 필수
- Classifier: internal `cogdistmodel` 호출
- Safety: Azure AI Content Safety
- RAG: Azure AI Search `cbt-rag-search` / `cbt-rag-index`
- LLM: Azure OpenAI `gpt-4.1-mini` Chat Completions
- LLM length control: `/v1/respond` optional `llm.max_completion_tokens`, server-side capped. MVP 권장 env는 기본 `1200`, Gateway 상한 `12000`.
- Speech: Azure Speech SDK + pydub + ffmpeg 방식 유지
- TTS: SSE `tts` event에서 `audio.data`를 canonical로 사용하고 `audio_base64`는 호환 alias로 제공
- STT: `audio.kind=url|base64` 지원. `stt` SSE event로 processing/completed/error를 명시
- Refactor: DAG 보조 로직을 `events.py`, `safety.py`, `tts.py`, `ranking.py`로 분리 완료
- Refactor 2: `payloads.py`, `turns.py`, `prompts.py`로 SSE payload/session turn/LLM message builder 분리 완료
- Refactor 3-1: `request_context.py`, `repositories/session_repository.py`로 Cosmos DB 교체 전 입력 정규화/세션 저장소 경계 생성 및 Azure 회귀 테스트 완료
- Refactor 3-2: `adapters.py` service adapter boundary completed and Azure regression tested
- Refactor 3-3 준비: `SESSION_REPOSITORY=memory|cosmos` 선택형 세션 저장소. Cosmos DB는 이미 배포된 DB/컨테이너에 연결하는 방식.

## 주요 경로
- `src/gateway-container/api-gateway/app/main.py`: FastAPI entrypoint
- `src/gateway-container/api-gateway/app/dag.py`: respond orchestration / STT→DAG 연결
- `src/gateway-container/api-gateway/app/adapters.py`: 외부 서비스 adapter boundary
- `src/gateway-container/api-gateway/app/request_context.py`: `/v1/respond` 입력 정규화 context
- `src/gateway-container/api-gateway/app/repositories/session_repository.py`: in-memory/Cosmos DB 세션 repository boundary
- `src/gateway-container/api-gateway/app/payloads.py`: SSE/API payload builder
- `src/gateway-container/api-gateway/app/turns.py`: session turn builder
- `src/gateway-container/api-gateway/app/prompts.py`: LLM message builder
- `src/gateway-container/api-gateway/app/events.py`: SSE serialization helper
- `src/gateway-container/api-gateway/app/safety.py`: Content Safety + keyword fallback
- `src/gateway-container/api-gateway/app/tts.py`: TTS SSE payload builder
- `src/gateway-container/api-gateway/app/ranking.py`: RAG candidate rerank
- `src/gateway-container/api-gateway/app/schemas.py`: request schema
- `src/gateway-container/common/llm_client.py`: Azure OpenAI client
- `src/gateway-container/common/retrieve_client.py`: Azure AI Search client
- `src/gateway-container/common/speech_client.py`: Azure Speech STT/TTS client
- `API_CONTRACT.md`: 프론트/테스트용 API 계약 문서
- `scripts/gateway_sse_*.py`: Azure 회귀 테스트 스크립트

## 보안 주의
- 이 폴더에는 실제 secret/key를 넣지 않았습니다.
- 배포 시 secret은 Azure Container Apps secretref로 주입해야 합니다.
- 외부 호출은 `x-api-key` 헤더가 필요합니다.
- Cosmos DB 키도 `.env`/프론트에 넣지 말고 Azure Container Apps secretref로만 주입합니다.

## Cosmos DB 세션 저장소 설정
- 기본값은 `SESSION_REPOSITORY=memory`입니다. 기존 동작과 동일하게 revision/replica 변경 시 세션이 사라질 수 있습니다.
- Cosmos DB를 사용할 때는 `SESSION_REPOSITORY=cosmos`로 바꾸고 아래 값을 컨테이너 앱 환경변수/secretref로 설정합니다.
- 예상 컨테이너 파티션 키는 `/session_id`입니다. 문서 `id`도 `session_id`와 동일하게 저장합니다.

```env
SESSION_REPOSITORY=cosmos
COSMOS_ENDPOINT=https://<account>.documents.azure.com:443/
COSMOS_KEY=<secretref 권장>
COSMOS_DATABASE=<database>
COSMOS_CONTAINER=<container>
```

## 최신 검증
- main 병합 커밋: `32f0174 게이트웨이 3차 2 서비스 어댑터 리팩터링 병합`
- Azure 테스트 기준: 3차-2 service adapter boundary 배포 후 회귀 테스트
- Azure 상태: Healthy / Traffic 100
- 회귀 테스트 PASS: health, auth 401, classify, respond text, session read, crisis, transcript, TTS, audio STT success, audio STT failure
- 로컬 검증: `py_compile` 및 `git diff --check` 통과

## 다음 작업: Document Intelligence OCR 연결
- 목표: `DOCINTEL_*` 환경변수를 사용해 Azure AI Document Intelligence `prebuilt-read` OCR을 Gateway에 연결한다.
- 예상 입력: `/v1/respond`의 `input_type=document`, `document.kind=base64|url`.
- 예상 SSE: `document(processing) -> document(completed|error) -> meta/chunks/token... -> done`.
- 보안 주의: URL 입력은 SSRF 방어, base64 입력은 크기 제한, OCR 원문 로그 출력 금지.
