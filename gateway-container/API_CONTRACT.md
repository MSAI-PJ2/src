# API Gateway API 계약서

이 문서는 Azure Container Apps `api-gateway`의 API/SSE 계약 정리본이다. 2026-07-02 기준. 게이트웨이 평탄화 구조와 cogdist v2(ml/cogdist-server) 계약을 반영한다.

## 1. 기준 상태

```text
Repository: https://github.com/MSAI-PJ2/src.git
Gateway folder: gateway-container/
Gateway source layout: gateway-container/app/{api,respond,services} + tests + scripts (평탄화 구조)
Gateway base URL: https://api-gateway.icybush-95bf9b25.koreacentral.azurecontainerapps.io
Auth: x-api-key 임시 필수
Runtime: FastAPI + Uvicorn
Container: Azure Container Apps
Last verified Azure revision: api-gateway--0000023
Last verified image: acrregistry001.azurecr.io/gateway:refactor-3-3-cosmos-session-20260701
Classifier: cogdistmodel internal Container App
Safety: Azure AI Content Safety + keyword fallback
RAG: Azure AI Search / cbt-rag-index
LLM: Azure OpenAI / gpt-4.1-mini
Speech: Azure Speech STT/TTS + pydub + ffmpeg
Session store: Azure Cosmos DB NoSQL
Document Intelligence: 채팅 캡쳐 이미지 입력(input_type=image)으로 Gateway 연결됨 (19장)
```

검증 상태:

```text
/healthz PASS
인증 누락 401 PASS
/v1/classify PASS
/v1/respond text PASS
/v1/respond crisis PASS
/v1/respond transcript PASS
/v1/respond TTS PASS
/v1/respond audio STT success PASS
/v1/respond audio STT failure PASS
Cosmos session persistence PASS: 동일 session_id 재호출 시 turn_count 누적 확인
```

## 2. 빌드와 폴더 계약

게이트웨이 코드는 리포지토리 루트가 아니라 `gateway-container/`를 Docker build context로 사용한다.

```text
gateway-container/
|-- Dockerfile
|-- requirements.txt
|-- app/          (서버 코드 전체 — 컨테이너에 이 폴더만 들어간다)
|-- tests/        (로컬 계약 테스트)
|-- scripts/      (배포본 회귀 테스트)
|-- .env.example
|-- .dockerignore
|-- API_CONTRACT.md
`-- docker-compose.yml
```

ACR 빌드 명령 (구조 평탄화로 -f 경로가 바뀌었다 — 이전: services/api-gateway/Dockerfile):

```bash
az acr build \
  -r "$ACR" \
  -t gateway:<TAG> \
  -f Dockerfile \
  gateway-container
```

주의:

```text
- build context는 반드시 gateway-container로 둔다.
- 리포지토리 루트의 다른 폴더는 게이트웨이 이미지에 포함하지 않는다.
- gateway-container/.dockerignore는 기본 차단 방식이며 requirements.txt와 app/만 허용한다.
```

## 3. 공통 HTTP 헤더

```http
Content-Type: application/json
x-api-key: <GATEWAY_API_KEY>
```

`/healthz`를 제외한 주요 API는 `x-api-key`가 없거나 틀리면 401을 반환한다.

401 예시:

```json
{"detail":"invalid api key"}
```

### 인증 모드 (AUTH_MODE)

```text
api_key(기본)  x-api-key 헤더 검사. current_user = "anonymous"
entra          Microsoft Entra External ID 의 JWT 검증 (코드 구현됨, 설정만으로 켜짐)
```

`AUTH_MODE=entra` 로 켜면 헤더가 바뀐다 — 프론트가 로그인 후 받은 액세스 토큰을 첨부:

```http
Authorization: Bearer <JWT>
```

서버는 서명(JWKS)·issuer·audience·만료를 검증하고 user_id(oid)를 추출한다.
필요 환경변수: `ENTRA_CLIENT_ID` + (`ENTRA_TENANT_ID` 또는 `ENTRA_ISSUER`).

```text
토큰 없음/형식 오류      -> 401 {"detail":"missing bearer token"}
서명·만료·aud/iss 불일치 -> 401 {"detail":"invalid or expired token"}
ENTRA_* 설정 누락        -> 500 {"detail":"entra auth misconfigured: ..."}
```

## 4. Health

```http
GET /healthz
```

응답:

```json
{"status":"ok"}
```

주의: Gateway 프로세스 상태만 확인한다. cogdist, RAG, LLM, Speech, Cosmos DB 전체 종속성 상태 검사는 아니다.

## 5. Classify

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
  "model": "klue/roberta-large",
  "model_version": "multi_large_v2",
  "threshold": 0.55,
  "primary": "불충분",
  "labels": [
    {"label":"불충분", "score":0.5244, "selected":true}
  ]
}
```

필드 설명:

```text
text: 분류 대상 원문.
mode: 분류 모드. 현재 multi_label.
model/model_version: cogdistmodel이 반환한 모델 식별 정보.
threshold: selected 판정 기준.
primary: Gateway가 RAG/LLM 프롬프트에 사용하는 대표 분류 라벨.
labels: 전체 라벨별 점수 목록.
selected: v2 배타 규칙으로 정리된 선택 여부 —
  ① primary 는 항상 selected=true
  ② primary 가 정상/불충분이면 그 라벨 하나만 true
  ③ primary 가 왜곡 라벨이면 정상/불충분은 false, 다른 왜곡은 score>=threshold
```

주의:

```text
- 모델 교체 후 primary 값은 바뀔 수 있다.
- 프론트엔드는 특정 라벨명을 하드코딩하지 말고 primary/labels 구조를 기준으로 처리한다.
```

### 정규화(normalize) 책임 — 자주 나오는 질문

과거 게이트웨이에는 `normalize_classify_result` 함수가 있어 여러 프로토타입 분류기의
응답 형태(primary_label/top_label/predictions 등)를 받아 정규화했다.
**지금은 그 역할이 분류기 서버로 이동했다:**

```text
정규화(canonical 형태 만들기)  → cogdist v2 서버 (ml/cogdist-server/app/model.py 의 _score_one
                                 + selection_policy.py 의 정상/불충분 배타 selected 정리)
게이트웨이                     → 위 응답 구조를 검증(strict)만 한다 (app/services/classifier.py)
```

따라서 새 분류 모델을 연결하려면 모델 서버가 위 "응답 구조"를 그대로 내보내야 한다
(필수: `primary` 문자열 + `labels` 12개 [{label, score, selected}]). 게이트웨이는
다른 형태를 받아주지 않는다 — 형태가 다르면 게이트웨이가 아니라 분류기 서버를 고친다.

## 6. Batch Classify

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

응답:

```json
{
  "results": [
    {"text":"...", "primary":"...", "labels":[]}
  ]
}
```

`results[]`의 각 원소는 `/v1/classify`의 단일 응답과 같은 구조를 따른다.

## 7. Respond 공통 계약

```http
POST /v1/respond
```

응답 타입:

```http
Content-Type: text/event-stream; charset=utf-8
```

SSE 형식:

```text
data: {"type":"meta", ...}

data: {"type":"chunks", ...}

data: {"type":"token", ...}

data: {"type":"done", ...}
```

공통 원칙:

```text
type: 이벤트 종류. 프론트엔드는 type 기준으로 분기한다.
session_id: 같은 대화 흐름을 묶는 세션 식별자.
done: 스트림 종료 이벤트. done 전까지 연결을 유지한다.
crisis: safety barrier에 의해 일반 LLM 응답 대신 반환된다.
```

## 8. Respond - LLM 생성 길이 제어

요청 body에 선택적으로 `llm.max_completion_tokens`를 넣을 수 있다.

```json
{
  "session_id": "long-answer-test-1",
  "text": "사람들 앞에 서면 다 망칠 것 같아요. 자세히 단계별로 설명해 주세요.",
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

테스트 결과:

```text
TEST_MAX_COMPLETION_TOKENS=128  -> 짧은 응답으로 제한됨
TEST_MAX_COMPLETION_TOKENS=4096 -> 더 긴 응답 허용됨
crisis 분기에서는 일반 LLM 생성이 차단되므로 이 옵션이 적용되지 않음
```

## 9. Respond - text 입력

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

## 10. Respond - transcript 입력

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

정상 이벤트 순서:

```text
meta -> chunks -> token... -> done
```

`meta.input.stt.transcript`에 정규화된 transcript가 포함된다.

## 11. Respond - audio 입력/STT

요청 예시:

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

`stt` 이벤트 예시:

```json
{
  "type": "stt",
  "session_id": "stt-audio-test-1",
  "status": "completed",
  "transcript": "사람들 앞에 서면 다 망칠 것 같아요.",
  "confidence": 0.92667097,
  "error": null
}
```

주의:

```text
- base64 payload는 크기 부담이 있으므로 운영에서는 Blob/SAS URL 방식 검토가 필요하다.
- /dev/null 같은 빈 입력은 stt(error)와 input_required로 종료되는 것이 정상이다.
```

## 12. Respond - TTS

요청 예시:

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

`tts` 이벤트 예시:

```json
{
  "type": "tts",
  "session_id": "tts-test-1",
  "status": "completed",
  "provider": "azure",
  "mime_type": "audio/wav",
  "audio": {
    "kind": "base64",
    "data": "<AUDIO_BASE64>",
    "mime_type": "audio/wav"
  }
}
```

프론트엔드 권장 처리:

```python
audio_data = event.get("audio", {}).get("data") or event.get("audio_base64")
mime_type = event.get("audio", {}).get("mime_type") or event.get("mime_type")
```

## 13. Crisis 분기

자해/자살 위험 신호가 감지되면 일반 LLM 응답 대신 crisis 이벤트를 반환한다.

이벤트 순서:

```text
meta -> crisis -> done
```

응답 예시:

```json
{
  "type": "crisis",
  "blocked": true,
  "reason": "self_harm",
  "message": "지금 많이 힘들고 고통스러우신 것 같아요. 무엇보다 당신의 안전이 가장 중요합니다...",
  "resources": [
    {"name":"자살예방상담전화", "phone":"1393", "hours":"24시간"},
    {"name":"정신건강위기상담전화", "phone":"1577-0199", "hours":"24시간"},
    {"name":"청소년전화", "phone":"1388", "hours":"24시간"}
  ]
}
```

주의:

```text
- crisis 이벤트에서는 일반 token 응답을 기대하지 않는다.
- crisis 메시지는 안전 배리어 정책에 따라 고정/템플릿화된 응답이다.
```

### 위치 기반 유관기관 연락처 (코드 구현됨, 설정만으로 켜짐)

프론트가 요청에 지역명을 넣어 보내면, 해당 지역 상담기관이 전국 공통 창구 **앞에** 붙는다:

```json
{
  "text": "...",
  "metadata": {"region": "서울특별시 강남구"}
}
```

```text
resources = [지역 창구(최대 3)... , 전국 공통 3종]
```

서버 설정: 세션과 같은 Cosmos 계정에 연락처 컨테이너(파티션키 /region,
문서 {"region","name","phone","hours"})를 만들고 `HOTLINE_CONTAINER=<컨테이너명>` 지정.
설정이 없거나 region 미전송, 조회 실패/타임아웃(기본 3초)이면 — 어떤 경우에도
위기 응답은 실패하지 않고 전국 공통 창구만 반환된다.

## 14. Session API와 Cosmos DB 계약

```http
POST /v1/sessions
GET /v1/sessions/{session_id}
```

세션 저장소는 `SESSION_REPOSITORY` 환경변수로 선택한다.

```text
memory: 기본/임시 저장소. Container App revision/replica 변경 시 세션이 사라질 수 있다.
cosmos: Azure Cosmos DB NoSQL 컨테이너에 세션 turn을 저장하는 지속 저장소.
```

현재 Azure 검증값:

```text
SESSION_REPOSITORY=cosmos
COSMOS_ENDPOINT=https://cbt-cosmos.documents.azure.com:443/
COSMOS_DATABASE=cbt-db
COSMOS_CONTAINER=conversations
COSMOS_KEY=secretref:cosmos-key
Cosmos account: cbt-cosmos
Cosmos database id: cbt-db
Cosmos container id: conversations
Partition key: /session_id
```

저장 문서 계약:

```json
{
  "id": "<session_id>",
  "session_id": "<session_id>",
  "created_at": "2026-07-01T00:00:00+00:00",
  "updated_at": "2026-07-01T00:00:10+00:00",
  "turn_count": 2,
  "turns": [
    {
      "role": "user",
      "text": "사람들 앞에 서면 다 망칠 것 같아요",
      "primary": "불충분",
      "safety": "safe",
      "safety_reason": null,
      "input": {"input_type":"text"},
      "tts": null,
      "ts": "2026-07-01T00:00:01+00:00"
    },
    {
      "role": "assistant",
      "text": "...",
      "event": "respond",
      "primary": "불충분",
      "rag_chunk_ids": ["asist-snuh-2025-021"],
      "ts": "2026-07-01T00:00:10+00:00"
    }
  ]
}
```

`GET /v1/sessions/{session_id}` 응답 예시:

```json
{
  "session_id": "cosmos-session-smoke-1",
  "created_at": "2026-07-01T05:00:00+00:00",
  "updated_at": "2026-07-01T05:01:00+00:00",
  "turn_count": 4,
  "turns": []
}
```

검증 결과:

```text
session_id=cosmos-session-smoke-1
1차 실행: BEFORE exists=False, AFTER turn_count=2
2차 실행: BEFORE exists=True, BEFORE turn_count=2, AFTER turn_count=4
```

보안/운영 주의:

```text
- COSMOS_KEY는 Azure Container Apps secretref로만 주입한다.
- 세션에는 상담성 대화가 포함될 수 있으므로 개인정보/민감정보 보관 정책이 필요하다.
- 보관 기간, 삭제 정책, 접근 권한, 감사 로그 정책은 별도 운영 정책으로 확정해야 한다.
- 향후 로그인/세션 인증이 붙으면 session_id 단독 조회를 금지하고 사용자 소유권 검증을 추가해야 한다.
```

## 15. RAG 계약

현재 Gateway는 `/v1/respond` 내부에서 Azure AI Search를 호출한다.

```text
AZURE_SEARCH_ENDPOINT=https://cbt-rag-search.search.windows.net
AZURE_SEARCH_INDEX=cbt-rag-index
AZURE_SEARCH_API_KEY=secretref:azure-search-key
AZURE_SEARCH_CONTENT_FIELD=content
AZURE_SEARCH_ID_FIELD=id
AZURE_SEARCH_SEMANTIC_CONFIG=cbt-semantic-config
```

`chunks` 이벤트 예시:

```json
{
  "type": "chunks",
  "session_id": "rag-test-1",
  "chunks": [
    {"id":"asist-snuh-2025-021", "content":"..."}
  ]
}
```

주의:

```text
- 프론트엔드는 chunks를 디버그/출처 표시용으로 사용할 수 있다.
- 최종 사용자에게 그대로 노출할지 여부는 UI 정책에 따른다.
```

## 16. Content Safety 계약

현재 Gateway는 `/v1/respond` 내부에서 Content Safety 및 키워드 fallback을 사용한다.

```text
CONTENT_SAFETY_ENABLED=true
CONTENT_SAFETY_ENDPOINT=https://cbt-content-safety.cognitiveservices.azure.com/
CONTENT_SAFETY_KEY=secretref:cs-key
CONTENT_SAFETY_THRESHOLD=2
```

분기 결과:

```text
safe: classify/RAG/LLM 응답 진행
blocked/self_harm: crisis 이벤트 반환 후 done
```

주의:

```text
- crisis 분기는 Gateway 안전 배리어에서 최종 차단한다.
- LLM provider의 자체 content filter 결과와 Gateway safety 결과는 별도 레이어다.
```

## 17. LLM 계약

현재 Gateway는 Azure OpenAI를 사용한다.

```text
AZURE_OPENAI_ENDPOINT=https://cbt-openai-00.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=gpt-4.1-mini
AZURE_OPENAI_API_VERSION=2024-12-01-preview
AZURE_OPENAI_API_KEY=secretref:azure-openai-key
AZURE_OPENAI_MAX_COMPLETION_TOKENS=1200
AZURE_OPENAI_MAX_COMPLETION_TOKENS_LIMIT=12000
```

응답은 SSE `token` 이벤트로 스트리밍된다.

```json
{"type":"token", "session_id":"text-test-1", "text":"응답 일부"}
```

## 18. Speech 계약

현재 Gateway는 Azure Speech를 사용한다.

```text
AZURE_SPEECH_KEY=secretref:azure-speech-key
AZURE_SPEECH_REGION=koreacentral
Speech resource: team3-speech
```

STT 입력:

```text
input_type=audio
audio.kind=base64
audio.mime_type=audio/wav 등
stt.provider=azure
```

TTS 출력:

```text
tts.enabled=true
tts.provider=azure
tts.voice=ko-KR-SunHiNeural
tts.format=wav 또는 mp3
```

## 19. Respond - image 입력 (OCR)

이미지를 보내면 Azure Document Intelligence 로 텍스트를 추출해 상담 입력으로 사용한다.
해석 방법은 `ocr.profile` 로 갈린다 (과거 초안의 `input_type=document` 명칭은 `image` 로 확정됨):

```text
generic (기본)  일반 이미지(일기·메모 등) — 추출 텍스트 전체를 사용자 발화로
kakao           카카오톡 캡쳐 — 화자 분리 후 "나"(내담자) 발화만 상담 입력으로
```

OCR 파이프라인 원본은 리포 루트 `di/kakao_ocr_pipeline.py`(DI 담당 팀원 작업물,
설명은 `di/README.md`)이며, Gateway 는 그 복제본 `app/services/document_ocr.py` 를 사용한다.

```env
DOCINTEL_ENDPOINT=https://<your-doc-intel-resource>.cognitiveservices.azure.com/
DOCINTEL_KEY=<set-in-azure-secret-or-local-env>
```

요청 (카톡 캡쳐):

```json
{
  "session_id": "image-test-1",
  "image": {
    "kind": "base64",
    "data": "<JPEG_OR_PNG_BASE64>",
    "mime_type": "image/png"
  },
  "ocr": {
    "profile": "kakao",
    "sender_names": ["감동받은 어피치"]
  }
}
```

요청 (일반 이미지): `ocr` 를 생략하거나 `{"profile": "generic"}` — sender_names 불필요.

- `image.kind`: `base64` | `url`
- `ocr.profile`: `generic`(기본) | `kakao`
- `ocr.sender_names`(kakao 전용, 선택): 채팅방 상단에 뜨는 상대 이름 목록. 지정하면 화자 판별 정확도가 올라간다.
- `text` 가 함께 오면 OCR 을 건너뛰고 text 를 사용한다 (text > image 우선순위).

이벤트 순서:

```text
성공:  ocr(processing) -> ocr(completed, profile·user_text[·conversation] 포함) -> meta -> chunks -> token... -> done
실패:  ocr(processing) -> ocr(error | no_user_messages(kakao) | no_text_found(generic)) -> input_required -> done
```

`ocr(completed)` 이벤트의 `user_text` = 상담 입력으로 쓸 텍스트
(generic: 추출 전체 / kakao: `speaker == "나"` 발화를 개행으로 연결).
kakao 프로파일의 `conversation` 형식 (di 파이프라인 출력과 동일):

```json
[
  {"speaker": "감동받은 어피치", "content": "야 오늘 과제 제출했어?", "time": "오전 11:15"},
  {"speaker": "나", "content": "응 아까 냈어.", "time": "오전 11:15"}
]
```

- OCR 성공 후에도 `meta` 이벤트·세션 턴의 `input.input_type` 은 `"image"` 로 유지되고,
  `input.ocr.profile` 에 해석 방법이 기록된다 (kakao 는 `input.ocr.conversation` 도 보존).
- kakao 에서 "나" 발화가 없으면 `no_user_messages`, generic 에서 텍스트가 없으면
  `no_text_found` 로 input_required 처리.
- 세션 턴에는 `input.image` 에서 원본 base64(`data`)를 제거하고 저장한다
  (Cosmos 문서 크기 한도 보호). `input.ocr.conversation` 은 보존된다.

보안 주의:

```text
- image.url 수신 시 SSRF 방어가 필요하다 (현재 미구현 — 프론트는 base64 사용 권장).
- base64 업로드는 크기 제한이 필요하다 (DI 한도 4MB).
- OCR 대화 원문은 개인정보 가능성이 높으므로 로그에 그대로 남기지 않는다.
- DOCINTEL_KEY는 Azure Container Apps secretref로만 주입한다.
```

## 20. 프론트엔드 SSE 처리 예시

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
        turn_count = event.get("turn_count")
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

## 21. Azure 회귀 테스트 체크리스트

```text
1. GET /healthz -> 200
2. POST /v1/classify without x-api-key -> 401
3. POST /v1/classify with x-api-key -> 200
4. POST /v1/respond text -> meta/chunks/token/done
5. POST /v1/respond crisis -> meta/crisis/done
6. POST /v1/respond transcript -> meta/chunks/token/done
7. POST /v1/respond audio wav -> stt processing/completed/meta/chunks/token/done
8. POST /v1/respond audio empty -> stt error/input_required/done
9. POST /v1/respond tts -> tts status=completed, audio.data 존재
10. GET /v1/sessions/{session_id} -> Cosmos 저장 turn_count 확인
11. max_completion_tokens 128/4096 비교 -> 응답 길이 제어 확인
12. Document Intelligence 구현 후 document processing/completed/meta/chunks/token/done 확인
```
