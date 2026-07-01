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

## 주요 경로
- `services/api-gateway/app/main.py`: FastAPI entrypoint
- `services/api-gateway/app/dag.py`: respond orchestration / STT→DAG 연결
- `services/api-gateway/app/adapters.py`: 외부 서비스 adapter boundary
- `services/api-gateway/app/request_context.py`: `/v1/respond` 입력 정규화 context
- `services/api-gateway/app/repositories/session_repository.py`: 현재 in-memory session store를 감싸는 repository boundary
- `services/api-gateway/app/payloads.py`: SSE/API payload builder
- `services/api-gateway/app/turns.py`: session turn builder
- `services/api-gateway/app/prompts.py`: LLM message builder
- `services/api-gateway/app/events.py`: SSE serialization helper
- `services/api-gateway/app/safety.py`: Content Safety + keyword fallback
- `services/api-gateway/app/tts.py`: TTS SSE payload builder
- `services/api-gateway/app/ranking.py`: RAG candidate rerank
- `services/api-gateway/app/schemas.py`: request schema
- `services/common/llm_client.py`: Azure OpenAI client
- `services/common/retrieve_client.py`: Azure AI Search client
- `services/common/speech_client.py`: Azure Speech STT/TTS client
- `API_CONTRACT.md`: 프론트/테스트용 API 계약 문서
- `scripts/gateway_sse_*.py`: Azure 회귀 테스트 스크립트

## 보안 주의
- 이 폴더에는 실제 secret/key를 넣지 않았습니다.
- 배포 시 secret은 Azure Container Apps secretref로 주입해야 합니다.
- 외부 호출은 `x-api-key` 헤더가 필요합니다.

## 최신 검증
- main 병합 커밋: `659f91f 게이트웨이 request context session repository 리팩터링 병합`
- Azure 테스트 기준: 3차-1 request context/session repository boundary 배포 후 회귀 테스트
- Azure 상태: Healthy / Traffic 100
- 회귀 테스트 PASS: health, auth 401, classify, respond text, session read, crisis, transcript, TTS, audio STT success, audio STT failure
- 로컬 검증: `py_compile` 및 `git diff --check` 통과
