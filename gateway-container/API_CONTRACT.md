# API Gateway API 계약서

이 문서는 `main` 기준 Gateway API 계약 정리본이다. 2026-07-01 기준 Azure Container Apps 배포와 회귀 테스트 결과를 반영한다.

## 1. 기준 상태

```text
Repository: https://github.com/MSAI-PJ2/src.git
Gateway folder: gateway-container/
Git branch: main
Gateway base URL: https://api-gateway.icybush-95bf9b25.koreacentral.azurecontainerapps.io
Auth: x-api-key 임시 필수
Runtime: FastAPI + Uvicorn
Container: Azure Container Apps
Azure revision: api-gateway--0000023
Gateway image: acrregistry001.azurecr.io/gateway:refactor-3-3-cosmos-session-20260701
Classifier: cogdistmodel internal
Safety: Azure Content Safety
RAG: Azure AI Search cbt-rag-index
LLM: Azure OpenAI gpt-4.1-mini
Speech: Azure Speech SDK + pydub + ffmpeg
Session store: Azure Cosmos DB NoSQL, cbt-cosmos / cbt-db / conversations, partition key /session_id
Document Intelligence: env 예시만 반영됨, Gateway API 연결은 다음 단계
```

검증 상태:

```text
healthz PASS
auth 401 PASS
classify PASS
respond text PASS
crisis PASS
transcript PASS
TTS PASS
audio STT success PASS
audio STT failure PASS
Cosmos session persistence PASS: turn_count 2 -> 4 누적 확인
```

## 2. 공통 헤더

```http
Content-Type: application/json
x-api-key: <GATEWAY_API_KEY>
```

`/healthz`를 제외한 주요 API는 `x-api-key`가 없거나 틀리면 401을 반환한다.

## 3. Health

```http
GET /healthz
```

응답:

```json
{"status":"ok"}
```

주의: Gateway 프로세스 상태만 확인한다. cogdist/RAG/LLM/Speech/Cosmos 전체 상태 검사는 아니다.

## 4. Classify

```http
POST /v1/classify
```

요청:

```json
{
  "text": "사람들 앞에 서면 다 망칠 것 같아요"
}
```

응답 구조:

```json
{
  "text": "사람들 앞에 서면 다 망칠 것 같아요",
  "mode": "multi_label",
  "model": "klue/roberta-base",
  "model_version": "multi_large_v2",
  "threshold": 0.5,
  "primary": "불충분",
  "labels": [
    {"label":"불충분", "score":0.5244, "selected":true}
  ]
}
```

필드 주석:

```text
primary: Gateway가 RAG/LLM 프롬프트에 사용하는 대표 분류 라벨.
labels: 전체 라벨별 점수 목록.
selected: threshold 기준으로 선택된 라벨 여부.
주의: 모델 교체 시 primary 값은 달라질 수 있으므로 프론트는 특정 라벨명을 하드코딩하지 않는다.
```

## 5. Batch Classify

```http
POST /v1/batch-classify
```

요청:

```json
{
  "texts": [
    "사람들 앞에 서면 다 망칠 것 같아요",
    "나는 늘 실패해요"
  ]
}
```

응답은 단일 classify 결과를 담은 `results[]` 구조다.

## 6. Respond 공통

```http
POST /v1/respond
```

응답 타입:

```text
text/event-stream
```

SSE 형식:

```text
data: {"type":"meta", ...}

data: {"type":"token", ...}

data: {"type":"done", ...}
```

공통 필드:

```text
type: 이벤트 종류. 프론트는 type 기준으로 분기한다.
session_id: 같은 대화 흐름을 묶는 세션 식별자.
주의: SSE는 여러 이벤트가 순차적으로 도착하므로 done 전까지 연결을 유지한다.
```

### 6.1 LLM 생성 길이 제어

요청 body에 선택적으로 `llm.max_completion_tokens`를 넣을 수 있다.

```json
{
  "session_id": "long-answer-test-1",
  "text": "자세히 단계별로 설명해 주세요.",
  "llm": {
    "max_completion_tokens": 2048
  }
}
```

서버 적용 규칙:

```text
기본값: AZURE_OPENAI_MAX_COMPLETION_TOKENS       # MVP 권장 1200
상한값: AZURE_OPENAI_MAX_COMPLETION_TOKENS_LIMIT # Gateway 권장 상한 12000
요청값: llm.max_completion_tokens
최종값: min(요청값 또는 기본값, 서버 상한값)
```

주의: crisis 분기에서는 일반 LLM 생성을 차단하므로 이 옵션이 적용되지 않는다.

## 7. Respond - 텍스트 입력

요청:

```json
{
  "session_id": "text-test-1",
  "text": "사람들 앞에 서면 다 망칠 것 같아요"
}
```

정상 이벤트 순서:

```text
meta -> chunks -> token... -> done
```

`meta` 예시:

```json
{
  "type": "meta",
  "session_id": "text-test-1",
  "turn_count": 1,
  "primary": "불충분",
  "mode": "multi_label",
  "labels": [],
  "input": {"input_type":"text"},
  "tts": null
}
```

## 8. Respond - transcript 입력

브라우저 또는 별도 STT가 이미 transcript를 만든 경우 사용한다.

```json
{
  "session_id": "transcript-test-1",
  "input_type": "transcript",
  "stt": {
    "provider": "mock",
    "language": "ko-KR",
    "transcript": "사람들 앞에 서면 다 망칠 것 같아요",
    "confidence": 0.93
  }
}
```

## 9. Respond - audio 입력/STT

```json
{
  "session_id": "stt-audio-test-1",
  "input_type": "audio",
  "audio": {
    "kind": "base64",
    "data": "<AUDIO_BASE64>",
    "mime_type": "audio/wav",
    "language": "ko-KR"
  },
  "stt": {
    "provider": "azure",
    "language": "ko-KR"
  }
}
```

STT 성공 이벤트 순서:

```text
stt(processing) -> stt(completed) -> meta -> chunks -> token... -> done
```

STT 실패 이벤트 순서:

```text
stt(processing) -> stt(error|no_match) -> input_required -> done
```

주의: base64 payload는 커질 수 있으므로 운영에서는 Blob/SAS URL 방식이 더 적합하다.

## 10. Respond - TTS

```json
{
  "session_id": "tts-test-1",
  "text": "사람들 앞에 서면 다 망칠 것 같아요",
  "tts": {
    "enabled": true,
    "provider": "azure",
    "voice": "ko-KR-SunHiNeural",
    "format": "wav"
  }
}
```

이벤트 순서:

```text
meta -> chunks -> token... -> tts -> done
```

프론트 권장 처리:

```python
audio_data = event.get("audio", {}).get("data") or event.get("audio_base64")
mime_type = event.get("audio", {}).get("mime_type") or event.get("mime_type")
```

## 11. Crisis 분기

자해/자살 위험 신호가 감지되면 일반 LLM 응답 대신 crisis 이벤트를 반환한다.

```text
meta -> crisis -> done
```

```json
{
  "type": "crisis",
  "blocked": true,
  "reason": "self_harm",
  "message": "지금 많이 힘들고 고통스러우신 것 같아요...",
  "resources": [
    {"name":"자살예방상담전화", "phone":"1393", "hours":"24시간"}
  ]
}
```

주의: crisis 이벤트에서는 일반 token 응답을 기대하지 않는다.

## 12. Session

```http
POST /v1/sessions
GET /v1/sessions/{session_id}
```

세션 저장소는 `SESSION_REPOSITORY` 환경변수로 선택한다.

```text
memory: 기본값. Container App revision/replica 변경 시 세션이 사라질 수 있는 임시 저장소.
cosmos: Azure Cosmos DB NoSQL 컨테이너에 세션 turn을 저장하는 지속 저장소.
```

현재 Azure 검증값:

```text
SESSION_REPOSITORY=cosmos
COSMOS_ENDPOINT=https://cbt-cosmos.documents.azure.com:443/
COSMOS_DATABASE=cbt-db
COSMOS_CONTAINER=conversations
partition key=/session_id
```

검증 결과:

```text
session_id=cosmos-session-smoke-1
1차 실행: BEFORE exists=False, AFTER turn_count=2
2차 실행: BEFORE exists=True, BEFORE turn_count=2, AFTER turn_count=4
```

주의: Cosmos DB를 사용하더라도 개인정보/상담기록 보존 기간, 삭제 정책, 접근 권한 정책은 별도 운영 정책으로 확정해야 한다.

## 13. Document Intelligence - 다음 작업 예정 계약

현재 `.env.example`에는 Document Intelligence OCR 설정 예시만 반영되어 있다. Gateway API 연결은 다음 구현 단계다.

```env
DOCINTEL_ENDPOINT=https://<your-doc-intel-resource>.cognitiveservices.azure.com/
DOCINTEL_KEY=<set-in-azure-secret-or-local-env>
DOCINTEL_API_VERSION=2024-11-30
DOCINTEL_MODEL_ID=prebuilt-read
```

예상 입력 계약 초안:

```json
{
  "session_id": "doc-test-1",
  "input_type": "document",
  "document": {
    "kind": "base64",
    "data": "<IMAGE_OR_PDF_BASE64>",
    "mime_type": "image/png",
    "language": "ko-KR"
  }
}
```

예상 이벤트 순서 초안:

```text
document(processing) -> document(completed|error) -> meta -> chunks -> token... -> done
```

초기 구현 범위:

```text
- prebuilt-read 기반 OCR
- 이미지/문서에서 텍스트 추출
- OCR 결과를 transcript/text처럼 Gateway DAG에 연결
- bounding box는 프론트 디버그/하이라이트용으로 보존 가능
```

보안 주의:

```text
- document.url을 받을 경우 SSRF 방어가 필요하다.
- base64 업로드는 크기 제한이 필요하다.
- OCR 원문은 개인정보 가능성이 높으므로 로그에 그대로 남기지 않는다.
- DOCINTEL_KEY는 Azure Container Apps secretref로만 주입한다.
```

## 14. 프론트 SSE 처리 예시

```python
import json
import requests

resp = requests.post(
    f"{BASE}/v1/respond",
    headers={"Content-Type":"application/json", "x-api-key": GATEWAY_API_KEY},
    json={"session_id":"ui-test-1", "text":"사람들 앞에 서면 다 망칠 것 같아요"},
    stream=True,
    timeout=120,
)

answer_parts = []
for raw in resp.iter_lines(decode_unicode=True):
    if not raw or not raw.startswith("data: "):
        continue

    event = json.loads(raw[6:])
    typ = event.get("type")

    if typ == "stt":
        stt_status = event.get("status")
        transcript = event.get("transcript")
        stt_error = event.get("error") or event.get("reason")
    elif typ == "document":
        doc_status = event.get("status")
        ocr_text = event.get("text")
        doc_error = event.get("error") or event.get("reason")
    elif typ == "meta":
        primary = event.get("primary")
    elif typ == "chunks":
        chunks = event.get("chunks", [])
    elif typ == "token":
        answer_parts.append(event.get("text", ""))
    elif typ == "tts":
        audio_data = event.get("audio", {}).get("data") or event.get("audio_base64")
        mime_type = event.get("audio", {}).get("mime_type") or event.get("mime_type")
    elif typ == "crisis":
        crisis_message = event.get("message")
    elif typ == "input_required":
        input_required_reason = event.get("reason")
    elif typ == "done":
        break

answer = "".join(answer_parts)
```

## 15. Azure 테스트 체크리스트

```text
1. /healthz 200
2. /v1/classify 200
3. /v1/respond text: meta/chunks/token/done
4. /v1/respond crisis: meta/crisis/done
5. /v1/respond transcript: meta/chunks/token/done
6. /v1/respond audio wav: stt processing/completed/meta/chunks/token/done
7. /v1/respond tts: tts status=completed, audio.data 존재
8. STT 실패 샘플: stt error 또는 no_match, input_required, done
9. Cosmos session persistence: 같은 session_id 재호출 시 turn_count 증가
10. Document Intelligence 구현 후: document processing/completed/meta/chunks/token/done
```
