# Conversione del Tiny Transformer ottimizzato (pruning + weight clustering) in formato TFLite int8 per ESP32/TFLite Micro

import numpy as np
import tensorflow as tf
from tensorflow import keras

from model import MultiHeadLinearAttention, EncoderBlock, get_prunable_dense_layers, build_model

MODEL_PATH = "tiny_nids_transformer_optimized.keras"
TFLITE_PATH = "tiny_nids_transformer.tflite"
HEADER_PATH = "tiny_nids_transformer_model.h"
N_CALIBRATION_SAMPLES = 200

# Caricamento del modello Keras
model = keras.models.load_model(
    MODEL_PATH,
    custom_objects={"MultiHeadLinearAttention": MultiHeadLinearAttention, "EncoderBlock": EncoderBlock},
)
model.summary()

# post-quantizzazione (zeri del pruning, centroidi del clustering)
prunable_layers_pre = get_prunable_dense_layers(model)
zeros_pre = sum(int(np.sum(l.kernel.numpy() == 0)) for l in prunable_layers_pre)
weights_pre = sum(l.kernel.numpy().size for l in prunable_layers_pre)
print(f"Prima della conversione: sparsita' = {zeros_pre/weights_pre:.1%}, "
      f"valori distinti nel primo layer = {len(np.unique(prunable_layers_pre[0].kernel.numpy()))}")

# Representative dataset
data = np.load("dataset_preprocessato.npz")
X_train = data["X_train"].astype(np.float32)
X_test = data["X_test"].astype(np.float32)
y_test = data["y_test"]

rng = np.random.default_rng(seed=42)
calib_idx = rng.choice(len(X_train), size=N_CALIBRATION_SAMPLES, replace=False)
calib_samples = X_train[calib_idx]


def representative_dataset():
    for sample in calib_samples:
        yield [sample[np.newaxis, :, :]]


# Conversione + quantizzazione int8, con DIV escluso
fixed_batch_model = build_model(
    seq_len=20, input_dim=16, d_model=64, ff_dim=128,
    num_layers=2, num_heads=8, dropout_rate=0.35, batch_size=1,
)
fixed_batch_model(np.zeros((1, 20, 16), dtype=np.float32))

# istanzia le variabili
fixed_batch_model.set_weights(model.get_weights())

converter = tf.lite.TFLiteConverter.from_keras_model(fixed_batch_model)
converter.optimizations = [tf.lite.Optimize.DEFAULT]
converter.representative_dataset = representative_dataset

# Permette il fallback a float32 per gli op non quantizzati esplicitamente
converter.target_spec.supported_ops = [
    tf.lite.OpsSet.TFLITE_BUILTINS_INT8,
    tf.lite.OpsSet.TFLITE_BUILTINS,
]

print("\nConversione in corso...")
debug_options = tf.lite.experimental.QuantizationDebugOptions(denylisted_ops=["DIV"])
debugger = tf.lite.experimental.QuantizationDebugger(
    converter=converter, debug_dataset=representative_dataset, debug_options=debug_options,
)
tflite_model = debugger.get_nondebug_quantized_model()

with open(TFLITE_PATH, "wb") as f:
    f.write(tflite_model)
print(f"Modello convertito salvato in {TFLITE_PATH} ({len(tflite_model)/1024:.1f} KB)")

# --- 4) Verifica: confronto predizioni Keras vs TFLite ----------------------
interpreter = tf.lite.Interpreter(model_path=TFLITE_PATH)
interpreter.allocate_tensors()
input_details = interpreter.get_input_details()[0]
output_details = interpreter.get_output_details()[0]
print(f"\nTipo tensore input: {input_details['dtype'].__name__}  "
      f"output: {output_details['dtype'].__name__}  ")

# Valutazione sull'intero test set
n_check = len(X_test)
keras_preds = model.predict(X_test, verbose=0).flatten()
tflite_preds = np.zeros(n_check)

for i in range(n_check):
    x = X_test[i:i + 1].astype(input_details["dtype"])
    interpreter.set_tensor(input_details["index"], x)
    interpreter.invoke()
    out_val = interpreter.get_tensor(output_details["index"])
    tflite_preds[i] = float(np.asarray(out_val).flatten()[0])

mae = np.mean(np.abs(keras_preds - tflite_preds))
keras_acc = np.mean((keras_preds > 0.5) == y_test[:n_check])
tflite_acc = np.mean((tflite_preds > 0.5) == y_test[:n_check])
tp = np.sum((tflite_preds > 0.5) & (y_test[:n_check] == 1))
fp = np.sum((tflite_preds > 0.5) & (y_test[:n_check] == 0))
fn = np.sum((tflite_preds <= 0.5) & (y_test[:n_check] == 1))
tflite_prec = tp / max(tp + fp, 1)
tflite_rec = tp / max(tp + fn, 1)

print(f"\nConfronto su tutto il test set ({n_check} esempi):")
print(f"  MAE tra probabilita' Keras e TFLite: {mae:.4f}")
print(f"  Accuracy Keras (float32):  {keras_acc:.4f}")
print(f"  Accuracy TFLite:           {tflite_acc:.4f}  precision={tflite_prec:.4f}  recall={tflite_rec:.4f}")
print(f"  Keras  preds: min={keras_preds.min():.4f} max={keras_preds.max():.4f} "
      f"mean={keras_preds.mean():.4f} std={keras_preds.std():.4f}")
print(f"  TFLite preds: min={tflite_preds.min():.4f} max={tflite_preds.max():.4f} "
      f"mean={tflite_preds.mean():.4f} std={tflite_preds.std():.4f}")

if tflite_acc < 0.9:
    print("\n  ATTENZIONE: l'accuracy resta bassa anche con DIV esclusa.")

# Verifica pruning/clustering sopravvissuti alla quantizzazione
print("\nControllo pruning/clustering sui pesi quantizzati:")
all_details = interpreter.get_tensor_details()
checked = 0
for t in all_details:
    if t["dtype"] == np.int8 and len(t["shape"]) == 2 and t["shape"][0] == 64 and t["shape"][1] == 64:
        try:
            w = interpreter.get_tensor(t["index"])
        except ValueError:
            continue  # tensore non costante (es. attivazione), salta
        sparsity = np.mean(w == 0)
        n_unique = len(np.unique(w))
        print(f"  {t['name']:60s} shape={t['shape']}  sparsita'={sparsity:.1%}  valori distinti={n_unique}")
        checked += 1
        if checked >= 4:
            break
if checked == 0:
    print("  Nessun tensore 64x64 trovato con questo criterio")

# Esportazione come header C per il firmware ESP32
array_name = "g_tiny_nids_model"
with open(HEADER_PATH, "w") as f:
    f.write("// File generato automaticamente da convert.py, non modificare a mano.\n")
    f.write("// il firmware deve passare/leggere tensori float32,\n")
    f.write("// non int8, in ingresso e in uscita all'interprete TFLite Micro.\n")
    f.write("#ifndef TINY_NIDS_MODEL_H\n#define TINY_NIDS_MODEL_H\n\n")
    f.write(f"alignas(8) const unsigned char {array_name}[] = {{\n")
    for i in range(0, len(tflite_model), 12):
        chunk = tflite_model[i:i + 12]
        f.write("  " + ", ".join(f"0x{b:02x}" for b in chunk) + ",\n")
    f.write("};\n")
    f.write(f"const unsigned int {array_name}_len = {len(tflite_model)};\n\n")
    f.write("#endif  // TINY_NIDS_MODEL_H\n")

print(f"\nHeader C salvato in {HEADER_PATH} ({len(tflite_model)} byte, array '{array_name}')")
