"""Addestramento del Tiny Transformer su dataset_preprocessato.npz."""

import numpy as np
import matplotlib.pyplot as plt
from tensorflow import keras

from model import build_model

data = np.load("dataset_preprocessato.npz")
X_train, y_train = data["X_train"], data["y_train"]
X_val, y_val = data["X_val"], data["y_val"]
X_test, y_test = data["X_test"], data["y_test"]

# Pesi di classe ricalcolati da y_train cosi' restano corretti anche se il dataset viene rigenerato.
n_normale = np.sum(y_train == 0)
n_attacco = np.sum(y_train == 1)
class_weights = {
    0: len(y_train) / (2 * n_normale),
    1: len(y_train) / (2 * n_attacco),
}
print(f"Class weights: {class_weights}")

# Architettura: d_model=64, 2 layer encoder, 8 teste di attenzione, ff_dim=128, dropout=0.35
model = build_model(
    seq_len=X_train.shape[1],
    input_dim=X_train.shape[2],
    d_model=64,
    ff_dim=128,
    num_layers=2,
    num_heads=8,
    dropout_rate=0.35,
)
model.summary()

n_params = model.count_params()
print(f"Parametri totali: {n_params}  |  rapporto param/esempi training: {n_params / len(y_train):.2f}")

# AdamW: aggiunge weight decay (L2 "disaccoppiato"), utile per contenere l'overfitting
optimizer = keras.optimizers.AdamW(learning_rate=1e-3, weight_decay=1e-4, clipnorm=1.0)

# Label smoothing leggero sulla BCE: evita che il modello diventi overconfident (probabilita' spinte a 0/1)
loss = keras.losses.BinaryCrossentropy(label_smoothing=0.05)

model.compile(
    optimizer=optimizer,
    loss=loss,
    metrics=["accuracy", keras.metrics.Precision(name="precision"), keras.metrics.Recall(name="recall")],
)

early_stop = keras.callbacks.EarlyStopping(
    monitor="val_loss", patience=20, restore_best_weights=True
)

history = model.fit(
    X_train, y_train,
    validation_data=(X_val, y_val),
    epochs=200,
    batch_size=32,
    class_weight=class_weights,
    callbacks=[early_stop],
    verbose=2,
)

print("\nValutazione sul test set:")
test_loss, test_acc, test_prec, test_rec = model.evaluate(X_test, y_test, verbose=0)
print(f"loss={test_loss:.4f}  accuracy={test_acc:.4f}  precision={test_prec:.4f}  recall={test_rec:.4f}")

model.save("tiny_nids_transformer.keras")
print("Modello salvato in tiny_nids_transformer.keras")

# Confronto diretto training vs validation, sulle epoche effettivamente eseguite
train_loss_final = history.history["loss"][-1]
val_loss_final = history.history["val_loss"][-1]
gap = val_loss_final - train_loss_final
print(f"\nLoss finale: training={train_loss_final:.4f}  validation={val_loss_final:.4f}  gap={gap:.4f}")

# Grafico delle curve, salvato su file per ispezione visiva.
plt.figure(figsize=(8, 5))
plt.plot(history.history["loss"], label="training loss")
plt.plot(history.history["val_loss"], label="validation loss")
plt.xlabel("epoca")
plt.ylabel("binary crossentropy")
plt.title("Curve di addestramento")
plt.legend()
plt.tight_layout()
plt.savefig("training_curves.png", dpi=120)
print("Grafico salvato in training_curves.png")
