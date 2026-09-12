"""
Preprocessing del dataset TinyNIDS: CSV grezzo -> sequenze numeriche per il Tiny Transformer.

Vettore per pacchetto (16 dim): 4 ottetti IP src + 4 ottetti IP dst + 6 one-hot
protocollo (1,2,6,17,901,902) + lunghezza + delta temporale dal pacchetto precedente.
Sequenza: N=20, step=10. Finestre per sessione, etichetta "attacco" se contiene
almeno un pacchetto d'attacco. Split 60/20/20 per sessione poi unito. Normalizzazione
(min-max, fit solo su train) su lunghezza e delta (quest'ultimo con log1p prima).
"""

import csv
import re
import numpy as np
from datetime import datetime
from collections import Counter

# ============================================================
# CONFIGURAZIONE
# ============================================================
CSV_PATH = "dataset.csv"

SEQUENCE_LENGTH = 20
WINDOW_STEP = 10
SESSION_GAP_THRESHOLD_S = 5

TRAIN_FRAC = 0.6
VAL_FRAC = 0.2

PROTOCOLS = [1, 2, 6, 17, 901, 902]   # ordine fisso per il one-hot
PROTOCOL_INDEX = {p: i for i, p in enumerate(PROTOCOLS)}
NUM_PROTOCOLS = len(PROTOCOLS)

VECTOR_DIM = 4 + 4 + NUM_PROTOCOLS + 1 + 1   # = 16
IDX_LENGTH = 14
IDX_DELTA = 15


# ============================================================
# STEP 0: lettura CSV
# ============================================================
def fix_timestamp(ts):
    """Normalizza timestamp con componenti non paddati (es. '17:30:4' -> '17:30:04.000')."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})T(\d{1,2}):(\d{1,2}):(\d{1,2})(?:\.(\d+))?", ts)
    if not m:
        raise ValueError(f"Timestamp non riconosciuto: {ts!r}")
    date, h, mi, s, frac = m.groups()
    frac = (frac or "0").ljust(3, "0")[:3]
    return f"{date}T{int(h):02d}:{int(mi):02d}:{int(s):02d}.{frac}"


def load_dataset(path):
    packets = []
    with open(path, newline="") as f:
        for row in csv.reader(f):
            if len(row) != 6:
                continue
            ts_raw, src_ip, dst_ip, protocol, length, label = row
            packets.append({
                "timestamp": datetime.fromisoformat(fix_timestamp(ts_raw.strip())),
                "src_ip": src_ip.strip(),
                "dst_ip": dst_ip.strip(),
                "protocol": int(protocol.strip()),
                "length": int(length.strip()),
                "label": label.strip(),
            })
    packets.sort(key=lambda p: p["timestamp"])
    return packets


# ============================================================
# STEP 1: sessioni di cattura
# ============================================================
def split_into_sessions(packets, gap_threshold_s=SESSION_GAP_THRESHOLD_S):
    """Separa in sessioni distinte dove il gap tra pacchetti supera la soglia."""
    sessions = []
    current = [packets[0]]
    for prev, curr in zip(packets, packets[1:]):
        gap = (curr["timestamp"] - prev["timestamp"]).total_seconds()
        if gap > gap_threshold_s:
            sessions.append(current)
            current = []
        current.append(curr)
    sessions.append(current)
    return sessions


# ============================================================
# STEP 2: encoding del pacchetto
# ============================================================
def ip_to_octets(ip_str):
    return [int(part) for part in ip_str.split(".")]


def encode_packet(packet, prev_timestamp):
    """Vettore grezzo (non normalizzato) di un pacchetto."""
    vec = []
    vec.extend(ip_to_octets(packet["src_ip"]))   # 0-3
    vec.extend(ip_to_octets(packet["dst_ip"]))   # 4-7

    one_hot = [0] * NUM_PROTOCOLS
    one_hot[PROTOCOL_INDEX[packet["protocol"]]] = 1
    vec.extend(one_hot)                           # 8-13

    vec.append(packet["length"])                  # 14

    if prev_timestamp is None:
        delta_ms = None
    else:
        delta_ms = (packet["timestamp"] - prev_timestamp).total_seconds() * 1000
    vec.append(delta_ms)                           # 15

    return vec


def encode_session(session_packets):
    """Codifica i pacchetti di una sessione, escludendo il primo (nessun delta valido)."""
    vectors = []
    prev_ts = None
    for i, packet in enumerate(session_packets):
        vec = encode_packet(packet, prev_ts)
        prev_ts = packet["timestamp"]
        if i == 0:
            continue
        vectors.append((vec, packet["label"]))
    return vectors


# ============================================================
# STEP 3: finestre/sequenze
# ============================================================
def build_windows(vectors_with_labels, seq_len=SEQUENCE_LENGTH, step=WINDOW_STEP):
    """Finestre scorrevoli su UNA sessione. Etichetta 'attacco' se presente almeno un pacchetto d'attacco."""
    windows = []
    n = len(vectors_with_labels)
    for start in range(0, n - seq_len + 1, step):
        chunk = vectors_with_labels[start:start + seq_len]
        vecs = [v for v, _ in chunk]
        labels = [lbl for _, lbl in chunk]
        window_label = "attacco" if "attacco" in labels else "normale"
        windows.append((vecs, window_label))
    return windows


# ============================================================
# STEP 4: split train/val/test (per sessione, poi unito)
# ============================================================
def split_windows(windows, train_frac=TRAIN_FRAC, val_frac=VAL_FRAC):
    """Split cronologico (no shuffle) sulle finestre di una sessione."""
    n = len(windows)
    n_train = int(n * train_frac)
    n_val = int(n * val_frac)
    train = windows[:n_train]
    val = windows[n_train:n_train + n_val]
    test = windows[n_train + n_val:]
    return train, val, test


# ============================================================
# STEP 5: normalizzazione (fit solo su train)
# ============================================================
def fit_normalization_params(train_windows):
    all_lengths = []
    all_deltas_log = []
    for vecs, _ in train_windows:
        for vec in vecs:
            all_lengths.append(vec[IDX_LENGTH])
            all_deltas_log.append(np.log1p(vec[IDX_DELTA]))

    return {
        "length_min": min(all_lengths),
        "length_max": max(all_lengths),
        "delta_log_min": min(all_deltas_log),
        "delta_log_max": max(all_deltas_log),
    }


def normalize_vector(vec, params):
    out = list(vec)

    for i in range(8):                             # ottetti IP: range fisso 0-255
        out[i] = out[i] / 255.0

    length_range = params["length_max"] - params["length_min"]
    out[IDX_LENGTH] = (out[IDX_LENGTH] - params["length_min"]) / length_range

    delta_log = np.log1p(out[IDX_DELTA])
    delta_range = params["delta_log_max"] - params["delta_log_min"]
    out[IDX_DELTA] = (delta_log - params["delta_log_min"]) / delta_range

    return out


def normalize_windows(windows, params):
    return [([normalize_vector(v, params) for v in vecs], label) for vecs, label in windows]


# ============================================================
# STEP 6: class weights (solo su train)
# ============================================================
def compute_class_weights(train_windows):
    counts = Counter(label for _, label in train_windows)
    total = sum(counts.values())
    n_classes = len(counts)
    return {label: total / (n_classes * count) for label, count in counts.items()}


# ============================================================
# STEP 7: conversione in array numpy
# ============================================================
def windows_to_arrays(windows):
    X = np.array([vecs for vecs, _ in windows], dtype=np.float32)
    y = np.array([1.0 if label == "attacco" else 0.0 for _, label in windows], dtype=np.float32)
    return X, y


# ============================================================
# PIPELINE COMPLETA
# ============================================================
def main():
    print("Caricamento dataset...")
    packets = load_dataset(CSV_PATH)
    print(f"  {len(packets)} pacchetti letti")

    sessions = split_into_sessions(packets)
    print(f"  {len(sessions)} sessioni individuate: "
          f"{[len(s) for s in sessions]} pacchetti ciascuna")

    train_windows, val_windows, test_windows = [], [], []

    for session in sessions:
        vectors_with_labels = encode_session(session)
        windows = build_windows(vectors_with_labels)
        tr, va, te = split_windows(windows)
        train_windows.extend(tr)
        val_windows.extend(va)
        test_windows.extend(te)

    print(f"Finestre totali: train={len(train_windows)}, "
          f"val={len(val_windows)}, test={len(test_windows)}")

    norm_params = fit_normalization_params(train_windows)
    train_windows = normalize_windows(train_windows, norm_params)
    val_windows = normalize_windows(val_windows, norm_params)
    test_windows = normalize_windows(test_windows, norm_params)

    class_weights = compute_class_weights(train_windows)
    print(f"Pesi di classe (dal training set): {class_weights}")

    X_train, y_train = windows_to_arrays(train_windows)
    X_val, y_val = windows_to_arrays(val_windows)
    X_test, y_test = windows_to_arrays(test_windows)

    print(f"X_train: {X_train.shape}, y_train: {y_train.shape}")
    print(f"X_val:   {X_val.shape}, y_val:   {y_val.shape}")
    print(f"X_test:  {X_test.shape}, y_test:  {y_test.shape}")

    np.savez("dataset_preprocessato.npz",
             X_train=X_train, y_train=y_train,
             X_val=X_val, y_val=y_val,
             X_test=X_test, y_test=y_test)
    print("Salvato in dataset_preprocessato.npz")


if __name__ == "__main__":
    main()
