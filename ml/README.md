# ML — Cognitive Distortion Classifier

Multi-label cognitive distortion classification model and inference pipeline for the **생각갈피 (MindMark)** CBT chatbot project.

Built on **KLUE/RoBERTa-large**, trained to detect 10 types of cognitive distortion plus two routing classes (normal / insufficient context) from Korean text.

---

## Model Performance

| Metric | Score |
|---|---|
| F1 Micro | 0.864 |
| F1 Macro | 0.815 |
| Routing Accuracy (3-way) | 0.992 |
| Missed Intervention Rate | 1.1% |
| Over-Intervention Rate | 0.0% |

---

## Labels (12 Classes)

| ID | Label (Korean) | Description |
|---|---|---|
| 0 | '해야 한다' 진술 | Should/Must statements |
| 1 | 감정적 추론 | Emotional reasoning |
| 2 | 개인화 | Personalization |
| 3 | 과잉 일반화 | Overgeneralization |
| 4 | 긍정 축소화 | Disqualifying the positive |
| 5 | 낙인찍기 | Labeling |
| 6 | 부정적 편향 | Negative bias |
| 7 | 불충분 | Insufficient context (routing class) |
| 8 | 성급한 판단 | Jumping to conclusions |
| 9 | 정상 | Normal (routing class) |
| 10 | 확대와 축소 | Magnification and minimization |
| 11 | 흑백 사고 | All-or-nothing thinking |

---

## Directory Structure

```
ml/
├── outputs/
│   └── multi_large/
│       └── best/                   # Best checkpoint (production model)
│           ├── config.json         # Model architecture & label map
│           ├── model.safetensors   # Model weights (~1.35 GB, tracked via Git LFS)
│           ├── threshold.json      # Optimal classification threshold (0.55)
│           ├── tokenizer.json
│           ├── tokenizer_config.json
│           └── training_args.bin
├── train_trainer.py                # Multi-label training script
├── data_utils.py                   # Dataset preparation & tokenization
├── evaluate_model.py               # Evaluation with routing metrics
├── predict.py                      # Inference class (CogDistClassifier)
└── classify_conversation.py        # Batch classifier for OCR conversation output
```

> **Note:** `model.safetensors` (~1.35 GB) is tracked via **Git LFS** and committed to this repository. Ensure Git LFS is installed before cloning (`git lfs install`), or the file will be downloaded as a pointer stub rather than the actual weights.

---

## Model Architecture

| Item | Detail |
|---|---|
| Base model | `klue/roberta-large` |
| Problem type | Multi-label classification |
| Hidden size | 1024 |
| Attention heads | 16 |
| Hidden layers | 24 |
| Max input length | 160 tokens |
| Classification threshold | 0.55 (auto-tuned on validation set) |

**Key training techniques:**
- Layer-wise Learning Rate Decay (LLRD, factor 0.9)
- Cosine LR scheduler with warmup
- Positive class weighting (`pos_weight`, capped at 5) for rare label recall
- Automatic threshold tuning on validation set → saved to `threshold.json`

---

## Scripts

### `train_trainer.py` — Training

Trains the multi-label RoBERTa-large model on the cognitive distortion dataset.

```bash
python train_trainer.py \
    --model_id klue/roberta-large \
    --epochs 14 \
    --batch_size 16 \
    --lr 1.5e-5 \
    --llrd 0.9 \
    --pos_weight \
    --tune_threshold \
    --output_dir outputs/multi_large
```

Key arguments:

| Argument | Default | Description |
|---|---|---|
| `--model_id` | `klue/roberta-large` | Pretrained model to fine-tune |
| `--epochs` | 14 | Number of training epochs |
| `--lr` | 1.5e-5 | Base learning rate |
| `--llrd` | 0.9 | Layer-wise LR decay factor |
| `--pos_weight` | False | Enable positive class weighting |
| `--pos_weight_cap` | 10.0 | Cap for pos_weight values |
| `--tune_threshold` | False | Auto-tune classification threshold |
| `--early_stopping` | False | Enable early stopping |

---

### `data_utils.py` — Dataset Preparation

Loads and prepares the multi-label training dataset.

**Expected data files:**
```
data/
├── cogdist_10k_flat_v1_1.csv              # Main dataset (~10K samples)
└── insufficient_context_balanced_train_v1_2.csv   # Augmentation for "insufficient" class
```

CSV format:
```
utterance | cognitive_distortion | cognitive_distortions
```
- `cognitive_distortion`: single primary label (used to define the 12-class set)
- `cognitive_distortions`: pipe-separated multi-labels (e.g. `과잉 일반화|낙인찍기`)

Train / Validation / Test split: **80 / 10 / 10**

---

### `evaluate_model.py` — Evaluation

Evaluates the best checkpoint with both standard multi-label metrics and 3-way routing metrics.

```bash
python evaluate_model.py
```

Output includes:
- F1 Micro / Macro / Samples
- Per-class classification report
- 3-way routing accuracy (distorted / normal / insufficient)
- Over-intervention rate and missed intervention rate

---

### `predict.py` — Single-sentence Inference

```bash
python predict.py \
    --model_dir outputs/multi_large/best \
    --text "이번 시험 한 번 망쳤으니 난 완전히 실패자야"
```

Example output:
```
입력: 이번 시험 한 번 망쳤으니 난 완전히 실패자야
threshold: 0.55

채택된 라벨 (threshold 이상):
  낙인찍기              87.3%
  과잉 일반화           71.2%
```

The `CogDistClassifier` class can also be imported directly:

```python
from predict import CogDistClassifier

classifier = CogDistClassifier("outputs/multi_large/best")
result = classifier.predict("역시 나는 뭘 해도 안 되는 사람인가봐")
print(result["labels"])
# [('낙인찍기', 0.912), ('과잉 일반화', 0.834)]
```

---

### `classify_conversation.py` — Conversation Batch Classification

Reads a `conversation_output.json` produced by the KakaoTalk OCR pipeline (`di/kakao_ocr_pipeline.py`) and attaches cognitive distortion labels to each message turn.

```bash
python classify_conversation.py \
    --conversation conversation_output.json \
    --model_dir outputs/multi_large/best \
    --output conversation_classified.json
```

By default, only the **user's own messages** (`"speaker": "나"`) are classified. To classify all speakers:

```bash
python classify_conversation.py --classify_all
```

Output format (`conversation_classified.json`):
```json
[
  {
    "speaker": "나",
    "content": "역시 나는 뭘 해도 안 되는 사람인가봐",
    "time": "오전 11:15",
    "cogdist_labels": [
      {"label": "낙인찍기", "score": 0.912},
      {"label": "과잉 일반화", "score": 0.834}
    ]
  }
]
```

---

## Full Pipeline (OCR → Classification)

```
KakaoTalk screenshot
        ↓
di/kakao_ocr_pipeline.py       (Azure Document Intelligence OCR)
        ↓
conversation_output.json       (speaker-tagged conversation log)
        ↓
ml/classify_conversation.py    (CogDistClassifier inference)
        ↓
conversation_classified.json   (conversation + distortion labels)
```

---

## Requirements

```
torch
transformers
datasets
scikit-learn
pandas
numpy
python-dotenv
```

Install:
```bash
pip install torch transformers datasets scikit-learn pandas numpy python-dotenv
```

---

## Notes

- Inference uses **sigmoid** (not softmax) since each label is predicted independently.
- If no label exceeds the threshold, the highest-scoring label is returned as a fallback to prevent empty predictions.
- The model was trained on Google Colab (GPU) and the best checkpoint is stored in `outputs/multi_large/best/`.