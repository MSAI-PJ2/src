"""
data_utils_multi.py  -  멀티 라벨 버전 데이터 준비
"""

import os
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import pandas as pd
from datasets import Dataset

TEXT_COL = "utterance"
SINGLE_COL = "cognitive_distortion"      # 라벨 종류(12종)를 정하는 기준
MULTI_COL = "cognitive_distortions"      # 실제 멀티 라벨 (파이프 | 로 구분)

MAIN_CSV = "data/cogdist_10k_flat_v1_1.csv"
INSUF_CSV = "data/insufficient_context_balanced_train_v1_2.csv"


def _read_csv(path):
    df = pd.read_csv(path, encoding="utf-8-sig")[[TEXT_COL, SINGLE_COL, MULTI_COL]].fillna("")
    df[TEXT_COL] = df[TEXT_COL].astype(str).str.strip()
    return df[df[TEXT_COL] != ""]


def get_label_maps(labels_iterable):
    labels = sorted(set(labels_iterable))
    label2id = {name: i for i, name in enumerate(labels)}
    id2label = {i: name for name, i in label2id.items()}
    return label2id, id2label


def load_splits(seed=42, subset_size=None, use_insufficient_aug=True):
    df = _read_csv(MAIN_CSV)
    if use_insufficient_aug and os.path.exists(INSUF_CSV):
        df = pd.concat([df, _read_csv(INSUF_CSV)], ignore_index=True)
    df = df.drop_duplicates(subset=[TEXT_COL]).reset_index(drop=True)

    if subset_size is not None:
        df = df.sample(n=min(subset_size, len(df)), random_state=seed).reset_index(drop=True)

    # 라벨 종류는 단일 라벨 열(12종) 기준으로 고정
    label2id, id2label = get_label_maps(df[SINGLE_COL])
    n = len(label2id)

    def to_vec(row):
        raw = str(row[MULTI_COL]).strip()
        names = [x.strip() for x in raw.split("|") if x.strip()] if raw else []
        if not names:                         # 복수열 비면 단일 라벨(정상/불충분)로 채움
            names = [row[SINGLE_COL]]
        vec = [0.0] * n
        for name in names:
            if name in label2id:
                vec[label2id[name]] = 1.0
        return vec

    df["labels"] = df.apply(to_vec, axis=1)
    ds = Dataset.from_pandas(df[[TEXT_COL, "labels"]], preserve_index=False)

    split1 = ds.train_test_split(test_size=0.2, seed=seed)
    train_ds = split1["train"]
    split2 = split1["test"].train_test_split(test_size=0.5, seed=seed)
    valid_ds, test_ds = split2["train"], split2["test"]

    print(f"train {len(train_ds)} / valid {len(valid_ds)} / test {len(test_ds)} | 라벨 {n}종 (멀티라벨)")
    return train_ds, valid_ds, test_ds, label2id, id2label


def build_tokenize_fn(tokenizer, max_length=128):
    # 멀티라벨은 default_data_collator 와 함께 쓰려고 길이를 통일(max_length 패딩)
    def tokenize_fn(examples):
        return tokenizer(examples[TEXT_COL], truncation=True,
                         max_length=max_length, padding="max_length")
    return tokenize_fn