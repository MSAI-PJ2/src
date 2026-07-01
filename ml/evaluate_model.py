"""
evaluate_model_multi.py  -  멀티라벨 best 모델 성능 재확인 (+ 라우팅 관점 평가)

위쪽: 기존과 동일한 f1_micro / f1_macro / f1_samples / 클래스별 리포트
아래쪽: 제품이 실제 요구하는 3갈래 라우팅 평가 (왜곡 / 정상 / 불충분)
       멀티라벨 규칙: 왜곡 라벨이 하나라도 켜지면 '왜곡', 아니면 '불충분', 아니면 '정상'
"""
import os
import json
import numpy as np
import torch
from torch.utils.data import DataLoader
from transformers import (AutoTokenizer, AutoModelForSequenceClassification,
                          default_data_collator)
from sklearn.metrics import (f1_score, accuracy_score,
                             classification_report, confusion_matrix)
from data_utils_multi import load_splits, build_tokenize_fn

MODEL_DIR = "outputs/multi-large-ft/best"
MAX_LEN = 128
# THRESHOLD: None 이면 best/threshold.json(학습이 튜닝해 저장한 값)을 자동으로 읽음.
# 특정 값으로 강제하려면 숫자(예: 0.5)를 직접 넣으세요.
THRESHOLD = None

NORMAL_LABEL = "정상"
INSUF_LABEL = "불충분"
ROUTE_ORDER = ["distorted", "normal", "insufficient"]


def build_router(id2label):
    """12차원 0/1 벡터 -> 3갈래 라우트로 바꾸는 함수를 만든다."""
    dist_ids = [i for i, n in id2label.items() if n not in (NORMAL_LABEL, INSUF_LABEL)]
    insuf_id = next(i for i, n in id2label.items() if n == INSUF_LABEL)

    def route_of_vec(vec):
        if any(vec[i] == 1 for i in dist_ids):
            return "distorted"
        if vec[insuf_id] == 1:
            return "insufficient"
        return "normal"
    return route_of_vec


def print_routing_report(true_routes, pred_routes):
    tr, pr = np.array(true_routes), np.array(pred_routes)
    print("\n" + "=" * 50)
    print("라우팅 관점 평가 (제품이 실제 요구하는 정확도)")
    print("=" * 50)
    print(f"라우팅 정확도(3갈래): {accuracy_score(tr, pr):.3f}")
    print(f"라우팅 macro-F1     : {f1_score(tr, pr, average='macro', labels=ROUTE_ORDER, zero_division=0):.3f}\n")
    print("--- 갈래별 상세 ---")
    print(classification_report(tr, pr, labels=ROUTE_ORDER, digits=3, zero_division=0))
    print("--- 라우팅 혼동 행렬 (행=실제, 열=예측) ---")
    print("         " + "  ".join(f"{r:>11}" for r in ROUTE_ORDER))
    cm = confusion_matrix(tr, pr, labels=ROUTE_ORDER)
    for i, r in enumerate(ROUTE_ORDER):
        print(f"{r:>11} " + "  ".join(f"{v:>11d}" for v in cm[i]))
    print("\n--- 치명적 오류 (낮을수록 좋음) ---")
    n_normal = (tr == "normal").sum()
    over = ((tr == "normal") & (pr == "distorted")).sum()
    n_dist = (tr == "distorted").sum()
    missed = ((tr == "distorted") & (pr == "normal")).sum()
    n_insuf = (tr == "insufficient").sum()
    insuf_wrong = ((tr == "insufficient") & (pr != "insufficient")).sum()

    def rate(a, b):
        return f"{a}/{b} = {a/b:.1%}" if b else "해당 샘플 없음"

    print(f"과잉 개입률 (정상 -> 왜곡으로 오인): {rate(over, n_normal)}")
    print(f"개입 누락률 (왜곡 -> 정상으로 오인): {rate(missed, n_dist)}")
    print(f"불충분 오라우팅 (불충분 -> 다른 갈래): {rate(insuf_wrong, n_insuf)}")


def get_device():
    if torch.backends.mps.is_available():
        return torch.device("mps")
    if torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


def resolve_threshold():
    """THRESHOLD 가 None 이면 best/threshold.json 에서 학습이 튜닝한 값을 읽는다."""
    if THRESHOLD is not None:
        return float(THRESHOLD), "수동 지정"
    path = os.path.join(MODEL_DIR, "threshold.json")
    if os.path.exists(path):
        with open(path) as f:
            t = float(json.load(f)["threshold"])
        return t, f"threshold.json 자동 로드"
    return 0.5, "기본값(파일 없음)"


def main():
    device = get_device()
    thr, thr_src = resolve_threshold()
    print(f"[임계값] {thr:.2f} ({thr_src})")
    _, _, test_ds, label2id, id2label = load_splits()

    tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR)
    model = AutoModelForSequenceClassification.from_pretrained(MODEL_DIR).to(device)
    model.eval()

    tok = build_tokenize_fn(tokenizer, max_length=MAX_LEN)
    test_ds = test_ds.map(tok, batched=True)
    keep = {"input_ids", "attention_mask", "token_type_ids", "labels"}
    test_ds = test_ds.remove_columns([c for c in test_ds.column_names if c not in keep])
    loader = DataLoader(test_ds, batch_size=32, collate_fn=default_data_collator)

    all_logits, all_labels = [], []
    with torch.no_grad():
        for batch in loader:
            labels = batch.pop("labels")
            batch = {k: v.to(device) for k, v in batch.items()}
            all_logits.append(model(**batch).logits.cpu().numpy())
            all_labels.append(labels.numpy())
    logits = np.concatenate(all_logits)
    y_true = np.concatenate(all_labels).astype(int)
    probs = 1.0 / (1.0 + np.exp(-logits))
    y_pred = (probs >= thr).astype(int)

    names = [id2label[i] for i in range(len(id2label))]

    # ===== 기존 멀티라벨 지표 (그대로 유지) =====
    print(f"\nf1_micro   : {f1_score(y_true, y_pred, average='micro', zero_division=0):.3f}")
    print(f"f1_macro   : {f1_score(y_true, y_pred, average='macro', zero_division=0):.3f}")
    print(f"f1_samples : {f1_score(y_true, y_pred, average='samples', zero_division=0):.3f}\n")
    print("=== 클래스별 상세 ===")
    print(classification_report(y_true, y_pred, target_names=names, digits=3, zero_division=0))

    # ===== 추가: 라우팅 관점 평가 =====
    router = build_router(id2label)
    true_routes = [router(v) for v in y_true]
    pred_routes = [router(v) for v in y_pred]
    print_routing_report(true_routes, pred_routes)


if __name__ == "__main__":
    main()