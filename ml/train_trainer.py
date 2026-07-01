"""
train_trainer_multi.py  -  멀티 라벨 학습 (재현율 개선 미세조정 추가)
"""

import argparse
import json
import math
import re
import numpy as np
import torch
from torch.optim import AdamW
from sklearn.metrics import f1_score, classification_report
from transformers import (
    AutoTokenizer,
    AutoModelForSequenceClassification,
    TrainingArguments,
    Trainer,
    EarlyStoppingCallback,
    get_scheduler,
    default_data_collator,
)
from data_utils_multi import load_splits, build_tokenize_fn

SPLIT_SEED = 42


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--model_id", default="klue/roberta-large")
    p.add_argument("--epochs", type=int, default=14)
    p.add_argument("--batch_size", type=int, default=16)
    p.add_argument("--eval_batch_size", type=int, default=32)
    p.add_argument("--lr", type=float, default=1.5e-5)
    p.add_argument("--max_length", type=int, default=128)
    p.add_argument("--warmup_ratio", type=float, default=0.1)
    p.add_argument("--weight_decay", type=float, default=0.01)
    p.add_argument("--grad_accum", type=int, default=2)
    p.add_argument("--subset", type=int, default=0)
    p.add_argument("--early_stopping", action="store_true")
    p.add_argument("--output_dir", default=None)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--patience", type=int, default=4)
    p.add_argument("--metric_for_best", default="f1_micro",
                   help="f1_micro / f1_macro / f1_samples")
    p.add_argument("--lr_scheduler", default="cosine")
    p.add_argument("--llrd", type=float, default=0.9)
    p.add_argument("--threshold", type=float, default=0.5)
    p.add_argument("--save_total_limit", type=int, default=1)
    # --- 재현율 개선 미세조정 옵션 ---
    p.add_argument("--pos_weight", action="store_true",
                   help="드문 라벨 양성에 가중치 부여(재현율 향상)")
    p.add_argument("--pos_weight_cap", type=float, default=10.0)
    p.add_argument("--tune_threshold", action="store_true",
                   help="검증셋에서 macro-F1 최대 임계값 자동 탐색")
    return p.parse_args()


def build_llrd_optimizer(model, base_lr, weight_decay, llrd):
    no_decay = ["bias", "LayerNorm.weight"]
    prefix = model.base_model_prefix
    num_layers = model.config.num_hidden_layers

    def lr_of(name):
        if name.startswith(f"{prefix}.embeddings"):
            return base_lr * (llrd ** (num_layers + 1))
        m = re.search(rf"{re.escape(prefix)}\.encoder\.layer\.(\d+)\.", name)
        if m:
            return base_lr * (llrd ** (num_layers - int(m.group(1))))
        if name.startswith(prefix + "."):
            return base_lr * (llrd ** (num_layers + 1))
        return base_lr

    groups, n_covered = {}, 0
    for name, param in model.named_parameters():
        if not param.requires_grad:
            continue
        lr = lr_of(name)
        wd = 0.0 if any(nd in name for nd in no_decay) else weight_decay
        groups.setdefault((lr, wd), []).append(param)
        n_covered += 1
    n_trainable = sum(1 for _, pp in model.named_parameters() if pp.requires_grad)
    assert n_covered == n_trainable, f"LLRD 누락! {n_covered} != {n_trainable}"
    param_groups = [{"params": ps, "lr": lr, "weight_decay": wd}
                    for (lr, wd), ps in groups.items()]
    return AdamW(param_groups, lr=base_lr)


class WeightedMultiLabelTrainer(Trainer):
    """멀티라벨 BCE 손실에 pos_weight 를 적용하는 Trainer."""
    def __init__(self, *args, pos_weight=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.pos_weight = pos_weight

    def compute_loss(self, model, inputs, return_outputs=False, **kwargs):
        # inputs 를 변형하지 않도록 labels 는 꺼내되 원본은 보존 (Trainer 의 지표 수집 위해)
        labels = inputs.get("labels")
        model_inputs = {k: v for k, v in inputs.items() if k != "labels"}
        outputs = model(**model_inputs)
        logits = outputs.logits
        pw = self.pos_weight.to(logits.device) if self.pos_weight is not None else None
        loss_fct = torch.nn.BCEWithLogitsLoss(pos_weight=pw)
        loss = loss_fct(logits, labels.float())
        return (loss, outputs) if return_outputs else loss


def find_best_threshold(probs, labels):
    """검증셋에서 macro-F1 을 최대로 만드는 전역 임계값 탐색."""
    best_t, best_f1 = 0.5, -1.0
    for t in np.arange(0.10, 0.91, 0.05):
        preds = (probs >= t).astype(int)
        f1 = f1_score(labels, preds, average="macro", zero_division=0)
        if f1 > best_f1:
            best_f1, best_t = f1, float(t)
    return best_t, best_f1


def report_at(tag, probs, labels, thr, names):
    preds = (probs >= thr).astype(int)
    print(f"\n----- {tag} (threshold={thr:.2f}) -----")
    print(f"f1_micro {f1_score(labels, preds, average='micro', zero_division=0):.3f} | "
          f"f1_macro {f1_score(labels, preds, average='macro', zero_division=0):.3f} | "
          f"f1_samples {f1_score(labels, preds, average='samples', zero_division=0):.3f}")
    print(classification_report(labels, preds, target_names=names, digits=3, zero_division=0))


def main():
    args = parse_args()
    subset = args.subset if args.subset > 0 else None
    out_dir = args.output_dir or f"outputs/multi-{args.model_id.split('/')[-1]}"
    metric = {"macro_f1": "f1_macro", "micro_f1": "f1_micro"}.get(args.metric_for_best, args.metric_for_best)

    train_ds, valid_ds, test_ds, label2id, id2label = load_splits(seed=SPLIT_SEED, subset_size=subset)

    tokenizer = AutoTokenizer.from_pretrained(args.model_id)
    tok = build_tokenize_fn(tokenizer, max_length=args.max_length)
    train_ds = train_ds.map(tok, batched=True)
    valid_ds = valid_ds.map(tok, batched=True)
    test_ds = test_ds.map(tok, batched=True)

    model = AutoModelForSequenceClassification.from_pretrained(
        args.model_id, num_labels=len(label2id), id2label=id2label, label2id=label2id,
        problem_type="multi_label_classification")

    # pos_weight 계산 (옵션): 클래스별 neg/pos 비율, 상한 적용
    pos_weight = None
    if args.pos_weight:
        Y = np.array(train_ds["labels"], dtype=np.float32)   # (N, 12)
        pos = Y.sum(axis=0)
        neg = len(Y) - pos
        pw = neg / np.clip(pos, 1.0, None)
        pw = np.clip(pw, 0.0, args.pos_weight_cap)
        pos_weight = torch.tensor(pw, dtype=torch.float)
        print("[pos_weight] 클래스별 가중치(상한 %.1f):" % args.pos_weight_cap)
        for i in range(len(id2label)):
            print(f"  {id2label[i]:<14} {pw[i]:.2f}")

    def compute_metrics(eval_pred):
        logits, labels = eval_pred
        probs = 1.0 / (1.0 + np.exp(-logits))
        preds = (probs >= args.threshold).astype(int)
        labels = labels.astype(int)
        return {
            "f1_micro":   f1_score(labels, preds, average="micro",   zero_division=0),
            "f1_macro":   f1_score(labels, preds, average="macro",   zero_division=0),
            "f1_samples": f1_score(labels, preds, average="samples", zero_division=0),
        }

    targs = TrainingArguments(
        output_dir=out_dir,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=args.eval_batch_size,
        gradient_accumulation_steps=args.grad_accum,
        learning_rate=args.lr,
        warmup_ratio=args.warmup_ratio,
        weight_decay=args.weight_decay,
        lr_scheduler_type=args.lr_scheduler,
        seed=args.seed,
        eval_strategy="epoch",
        save_strategy="epoch",
        load_best_model_at_end=True,
        metric_for_best_model=metric,
        greater_is_better=True,
        save_total_limit=args.save_total_limit,
        logging_steps=50,
        report_to="none",
    )

    optimizer = build_llrd_optimizer(model, args.lr, args.weight_decay, args.llrd)
    steps_per_epoch = math.ceil(len(train_ds) / args.batch_size)
    update_steps_per_epoch = max(steps_per_epoch // args.grad_accum, 1)
    num_training_steps = update_steps_per_epoch * args.epochs
    num_warmup_steps = int(num_training_steps * args.warmup_ratio)
    scheduler = get_scheduler(name=args.lr_scheduler, optimizer=optimizer,
                              num_warmup_steps=num_warmup_steps, num_training_steps=num_training_steps)
    print(f"[스케줄러] type={args.lr_scheduler} | 총 {num_training_steps} 스텝 | warmup {num_warmup_steps} 스텝 "
          f"| pos_weight={'ON' if args.pos_weight else 'OFF'} | (멀티라벨)")

    callbacks = []
    if args.early_stopping:
        callbacks.append(EarlyStoppingCallback(early_stopping_patience=args.patience))

    trainer = WeightedMultiLabelTrainer(
        model=model,
        args=targs,
        train_dataset=train_ds,
        eval_dataset=valid_ds,
        processing_class=tokenizer,
        data_collator=default_data_collator,
        compute_metrics=compute_metrics,
        callbacks=callbacks,
        optimizers=(optimizer, scheduler),
        pos_weight=pos_weight,
    )

    trainer.train()

    names = [id2label[i] for i in range(len(id2label))]

    # 기본 임계값(0.5) 테스트 리포트 (기존과 동일하게 항상 보여줌)
    test_pred = trainer.predict(test_ds)
    test_probs = 1.0 / (1.0 + np.exp(-test_pred.predictions))
    test_true = test_pred.label_ids.astype(int)
    print("\n=== 테스트셋 (기본 threshold) ===")
    report_at("기본", test_probs, test_true, args.threshold, names)

    # 임계값 튜닝 (옵션): 검증셋에서 최적 임계값 찾고 테스트에 적용
    chosen_threshold = args.threshold
    if args.tune_threshold:
        valid_pred = trainer.predict(valid_ds)
        valid_probs = 1.0 / (1.0 + np.exp(-valid_pred.predictions))
        valid_true = valid_pred.label_ids.astype(int)
        best_t, best_f1 = find_best_threshold(valid_probs, valid_true)
        chosen_threshold = best_t
        print(f"\n[임계값 튜닝] 검증셋 최적 threshold={best_t:.2f} (검증 macro-F1 {best_f1:.3f})")
        report_at("튜닝 임계값", test_probs, test_true, best_t, names)

    # 저장 (+ 임계값 기록)
    best_dir = f"{out_dir}/best"
    trainer.save_model(best_dir)
    tokenizer.save_pretrained(best_dir)
    with open(f"{best_dir}/threshold.json", "w") as f:
        json.dump({"threshold": chosen_threshold}, f)
    print(f"\n저장 완료: {best_dir} (threshold={chosen_threshold:.2f})")


if __name__ == "__main__":
    main()