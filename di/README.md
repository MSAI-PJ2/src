# DI — KakaoTalk OCR Pipeline

KakaoTalk screenshot → text extraction → structured conversation log, using **Azure AI Document Intelligence**.

Part of the **생각갈피 (MindMark)** CBT chatbot multimodal input pipeline. The output of this pipeline feeds directly into the cognitive distortion classifier in `ml/`.

---

## Directory Structure

```
di/
├── kakao_ocr_pipeline.py   # Standalone OCR pipeline (local use only)
├── test_client.py          # Server test client with user-friendly output
├── .env                    # Local credentials (not committed)
└── di_test_image.jpeg      # Sample KakaoTalk screenshot for testing (not committed)
```

---

## How It Works

```
KakaoTalk screenshot (.jpeg / .png)
        ↓
Azure Document Intelligence (prebuilt-read)
        ↓  text lines + bounding box coordinates (polygon)
Line classifier
        ↓  message / timestamp / sender_name
Speaker assignment (x-coordinate midpoint)
        ↓  "나" (right side) / sender name (left side)
Timestamp matching (y-coordinate proximity)
        ↓
Structured conversation log
        ↓
Cognitive distortion classifier (ml/)
```

### Speaker Detection Logic

KakaoTalk places the user's own messages on the **right side** of the screen and the other person's messages on the **left side**. The pipeline uses the x-coordinate of each text line's bounding box to automatically assign the speaker:

- `x_left < page_width / 2` → other person (상대방)
- `x_left ≥ page_width / 2` → me (나)

### Timestamp Matching Logic

Timestamps ("오전 11:15", "오후 3:42") are matched to their nearest message by **y-coordinate proximity**, not reading order. This correctly handles cases where the timestamp appears before the message text in the OCR output (common for right-aligned messages).

---

## Setup

### 1. Install dependencies

```bash
pip install azure-ai-documentintelligence python-dotenv httpx
```

### 2. Configure `.env`

Create a `.env` file in this folder:

```dotenv
DOCINTEL_ENDPOINT=https://<your-resource>.cognitiveservices.azure.com/
DOCINTEL_KEY=<your-key>
DOCINTEL_API_VERSION=2024-11-30
DOCINTEL_MODEL_ID=prebuilt-read
```

> The Azure Document Intelligence resource used in this project is `team3-doc-intel` (Korea Central, Free F0 tier, resource group `10ai_2nd_team3`).

---

## Usage

### Option A — Standalone OCR (local, no server needed)

Uses `kakao_ocr_pipeline.py` directly with Azure Document Intelligence.

```bash
# Run with default test image (di_test_image.jpeg)
python3 kakao_ocr_pipeline.py

# Run with a specific image
python3 kakao_ocr_pipeline.py kakao_capture.jpeg
```

Output: prints reconstructed conversation and saves `conversation_output.json`.

---

### Option B — Server test client (requires gateway server running)

Uses `test_client.py` to send the image to the API gateway server and display
the full pipeline result (OCR → safety check → distortion classification → LLM response)
in a user-friendly format.

#### Step 1: Start the gateway server (Terminal 1 — do not close)

```bash
cd ~/Desktop/MSAI2/src/gateway-container
uvicorn app.main:app --reload --port 8000
```

Wait for `Application startup complete.` before proceeding.

#### Step 2: Run the test client (Terminal 2)

```bash
cd ~/Desktop/MSAI2/src/di

# Analyze a KakaoTalk screenshot
python3 test_client.py --doc di_test_image.jpeg

# Send text directly
python3 test_client.py "요즘 뭘 해도 안 될 것 같고 다 내 잘못인 것 같아"

# Continue a previous conversation (multi-turn)
python3 test_client.py "추가 질문" --session <session-id>

# With API key (if server requires it)
python3 test_client.py --doc di_test_image.jpeg --api-key <your-key>

# With TTS enabled
python3 test_client.py --doc di_test_image.jpeg --tts
```

#### Example output

```
📸  이미지 파일: di_test_image.jpeg
🌐  서버: http://localhost:8000/v1/respond
=======================================================
⏳  카톡 대화 인식 중...
✅  인식 완료 — 총 9개 메시지

┌─ 인식된 카톡 대화 ──────────────────────────────────
│  [오전 11:15]  감동받은 어피치: 야 오늘 과제 제출했어?
│                          응 아까 냈어.  [오전 11:15]
│  [오전 11:15]  감동받은 어피치: 오 다행이다 크크
└─────────────────────────────────────────────────────

┌─ 인지왜곡 분석 결과 ────────────────────────────────
│  주요 판정: 불충분 — 문맥이 짧아 판단하기 어려운 발화
│
│  전체 라벨 확률:
│    불충분       ███████████████  95.7%
│    정상         █░░░░░░░░░░░░░░   7.4%
└─────────────────────────────────────────────────────

🤖  AI 응답:
대화 내용을 보니...

=======================================================
✔   완료  |  세션 ID: ae7d5db3-...
=======================================================
```

---

## Cognitive Distortion Labels

| Label | Description |
|---|---|
| 정상 | No cognitive distortion detected |
| 불충분 | Insufficient context to determine distortion |
| '해야 한다' 진술 | Should/Must statements |
| 감정적 추론 | Treating emotions as facts |
| 개인화 | Blaming oneself for everything |
| 과잉 일반화 | Overgeneralizing from one event |
| 긍정 축소화 | Discounting positive experiences |
| 낙인찍기 | Attaching negative labels to self or others |
| 부정적 편향 | Focusing only on the negative |
| 성급한 판단 | Jumping to conclusions without evidence |
| 확대와 축소 | Magnifying negatives, minimizing positives |
| 흑백 사고 | All-or-nothing thinking |

---

## Integration with Cognitive Distortion Classifier

```bash
# Step 1: OCR (standalone)
python3 di/kakao_ocr_pipeline.py kakao_capture.jpeg

# Step 2: Classification
python3 ml/classify_conversation.py \
    --conversation di/conversation_output.json \
    --model_dir ml/outputs/multi_large/best \
    --output conversation_classified.json
```

---

## Requirements

```
azure-ai-documentintelligence
python-dotenv
httpx
```