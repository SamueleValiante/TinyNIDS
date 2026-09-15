"""
Conversione del Tiny Transformer in formato TFLite (int8) per ESP32/TFLite Micro.

Il modello Keras (tiny_nids_transformer.keras) viene:
  1) caricato, fornendo i layer custom (non riconosciuti automaticamente
     da Keras in fase di deserializzazione);
  2) convertito in flatbuffer .tflite, con quantizzazione full-integer
     (pesi E attivazioni in int8), calibrata su un campione del
     training set (representative dataset);
  3) verificato con un confronto diretto Keras vs TFLite su alcuni
     esempi del test set, per assicurarsi che la quantizzazione non
     abbia degradato in modo inaccettabile le predizioni;
  4) esportato anche come header C (byte array), pronto per essere
     incluso nel firmware ESP32.

Se la conversione fallisce, il messaggio d'errore di TensorFlow elenca
esplicitamente le operazioni non supportate (es. "Some ops are not
supported... EinSum"): e' il segnale che uno o piu' layer custom
(MultiHeadLinearAttention in particolare, per via degli tf.einsum)
richiedono una riscrittura prima di poter proseguire.
"""

import numpy as np
import tensorflow as tf
from tensorflow import keras

from model import MultiHeadLinearAttention, EncoderBlock

MODEL_PATH = "tiny_nids_transformer.keras"
TFLITE_PATH = "tiny_nids_transformer.tflite"
HEADER_PATH = "tiny_nids_transformer_model.h"
N_CALIBRATION_SAMPLES = 200  # dimensione del representative dataset

# --- 1) Caricamento del modello Keras -------------------------------------
# I layer custom non hanno un metodo get_config/from_config definito
# esplicitamente in model.py: vanno quindi passati esplicitamente a
# custom_objects, altrimenti Keras non sa come ricostruirli dal file salvato.
model = keras.models.load_model(
    MODEL_PATH,
    custom_objects={
        "MultiHeadLinearAttention": MultiHeadLinearAttention,
        "EncoderBlock": EncoderBlock,
    },
)
model.summary()

# --- 2) Representative dataset ---------------------------------------------
data = np.load("dataset_preprocessato.npz")
X_train = data["X_train"].astype(np.float32)
X_test = data["X_test"].astype(np.float32)
y_test = data["y_test"]

rng = np.random.default_rng(seed=42)
calib_idx = rng.choice(len(X_train), size=N_CALIBRATION_SAMPLES, replace=False)
calib_samples = X_train[calib_idx]


def representative_dataset():
    for sample in calib_samples:
        # ogni esempio va fornito con la batch dimension: (1, 20, 16)
        yield [sample[np.newaxis, :, :]]


# --- 3) Conversione + quantizzazione full-integer ---------------------------
converter = tf.lite.TFLiteConverter.from_keras_model(model)
converter.optimizations = [tf.lite.Optimize.DEFAULT]
converter.representative_dataset = representative_dataset

# Forza la quantizzazione integer-only: se qualche operazione non puo'
# essere rappresentata in int8, la conversione fallisce esplicitamente
# invece di ricadere silenziosamente su un kernel float.
converter.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
converter.inference_input_type = tf.int8
converter.inference_output_type = tf.int8

print("\nConversione in corso (qui emergono eventuali op non supportate)...")
tflite_model = converter.convert()

with open(TFLITE_PATH, "wb") as f:
    f.write(tflite_model)
print(f"Modello convertito salvato in {TFLITE_PATH} ({len(tflite_model)/1024:.1f} KB)")

# --- 4) Verifica: confronto predizioni Keras vs TFLite ----------------------
interpreter = tf.lite.Interpreter(model_path=TFLITE_PATH)
interpreter.allocate_tensors()
input_details = interpreter.get_input_details()[0]
output_details = interpreter.get_output_details()[0]

# Parametri di quantizzazione dell'input: servono per convertire i float
# originali nell'int8 atteso dal modello quantizzato (stessa formula
# usata internamente dal convertitore: q = round(x / scale) + zero_point).
in_scale, in_zero_point = input_details["quantization"]
out_scale, out_zero_point = output_details["quantization"]

n_check = 200
keras_preds = model.predict(X_test[:n_check], verbose=0).flatten()
tflite_preds = np.zeros(n_check)

for i in range(n_check):
    x = X_test[i:i + 1]
    x_q = np.round(x / in_scale + in_zero_point).astype(np.int8)
    interpreter.set_tensor(input_details["index"], x_q)
    interpreter.invoke()
    out_q = interpreter.get_tensor(output_details["index"])
    # out_q ha shape (1, 1): con NumPy >= 2.0 l'assegnazione implicita di un
    # array non scalare a un singolo elemento non e' piu' permessa, va quindi
    # estratto esplicitamente lo scalare con reshape(-1)[0] (o .item()).
    tflite_preds[i] = (out_q.reshape(-1)[0].astype(np.float32) - out_zero_point) * out_scale

mae = np.mean(np.abs(keras_preds - tflite_preds))
keras_acc = np.mean((keras_preds > 0.5) == y_test[:n_check])
tflite_acc = np.mean((tflite_preds > 0.5) == y_test[:n_check])

print(f"\nConfronto su {n_check} esempi di test:")
print(f"  MAE tra probabilita' Keras e TFLite: {mae:.4f}")
print(f"  Accuracy Keras (float32):  {keras_acc:.4f}")
print(f"  Accuracy TFLite (int8):    {tflite_acc:.4f}")

# --- 5) Esportazione come header C per il firmware ESP32 --------------------
array_name = "g_tiny_nids_model"
with open(HEADER_PATH, "w") as f:
    f.write("// File generato automaticamente da convert.py -- non modificare a mano.\n")
    f.write("#ifndef TINY_NIDS_MODEL_H\n#define TINY_NIDS_MODEL_H\n\n")
    f.write(f"alignas(8) const unsigned char {array_name}[] = {{\n")
    for i in range(0, len(tflite_model), 12):
        chunk = tflite_model[i:i + 12]
        f.write("  " + ", ".join(f"0x{b:02x}" for b in chunk) + ",\n")
    f.write("};\n")
    f.write(f"const unsigned int {array_name}_len = {len(tflite_model)};\n\n")
    f.write("#endif  // TINY_NIDS_MODEL_H\n")

print(f"Header C salvato in {HEADER_PATH} ({len(tflite_model)} byte, array '{array_name}')")
