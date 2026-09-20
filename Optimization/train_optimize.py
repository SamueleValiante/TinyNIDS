"""
Addestramento + ottimizzazione (pruning e weight clustering) del Tiny
Transformer, in un unico script -- implementazione manuale, senza
tensorflow-model-optimization (incompatibile con l'ambiente disponibile).

Tre fasi in sequenza:
  1) Addestramento normale (identico a train.py) -> modello base.
  2) Fine-tuning con pruning magnitude-based: ogni epoca si azzerano i
     pesi piu' piccoli in valore assoluto fino a raggiungere una
     sparsita' target crescente (schedule cubico, stesso andamento del
     PolynomialDecay di tfmot), e una maschera li mantiene a zero ad
     ogni batch successivo mentre il resto della rete continua ad
     allenarsi.
  3) Fine-tuning con weight clustering: i pesi non nulli di ciascun
     Dense vengono raggruppati in un numero fisso di centroidi
     (k-means 1D scritto a mano), poi vincolati a coincidere con il
     centroide piu' vicino per tutta la fase di fine-tuning (i pesi
     azzerati dal pruning restano a zero, non vengono clusterizzati).

Ogni fase viene valutata sul test set, cosi' il confronto base -> pruned
-> pruned+clustered e' esplicito. Nessuna dipendenza oltre a quelle gia'
usate in train.py: stesso ambiente, nessun venv separato necessario.
"""

import numpy as np
import matplotlib.pyplot as plt
import tensorflow as tf
from tensorflow import keras

from model import build_model, get_prunable_dense_layers

PRUNING_FINAL_SPARSITY = 0.5
PRUNING_FINE_TUNE_EPOCHS = 10
CLUSTERING_N_CLUSTERS = 16
CLUSTERING_FINE_TUNE_EPOCHS = 6
CLUSTERING_KMEANS_ITERS = 25

FINAL_MODEL_PATH = "tiny_nids_transformer_optimized.keras"

data = np.load("dataset_preprocessato.npz")
X_train, y_train = data["X_train"], data["y_train"]
X_val, y_val = data["X_val"], data["y_val"]
X_test, y_test = data["X_test"], data["y_test"]

n_normale = np.sum(y_train == 0)
n_attacco = np.sum(y_train == 1)
class_weights = {
    0: len(y_train) / (2 * n_normale),
    1: len(y_train) / (2 * n_attacco),
}
print(f"Class weights: {class_weights}")


def evaluate_and_print(model, label):
    loss, acc, prec, rec = model.evaluate(X_test, y_test, verbose=0)
    print(f"[{label}] loss={loss:.4f}  accuracy={acc:.4f}  precision={prec:.4f}  recall={rec:.4f}")
    return {"loss": loss, "accuracy": acc, "precision": prec, "recall": rec}


def make_optimizer(lr):
    return keras.optimizers.AdamW(learning_rate=lr, weight_decay=1e-4, clipnorm=1.0)


def compile_model(model, lr=1e-4):
    model.compile(
        optimizer=make_optimizer(lr),
        loss=keras.losses.BinaryCrossentropy(label_smoothing=0.05),
        metrics=["accuracy", keras.metrics.Precision(name="precision"), keras.metrics.Recall(name="recall")],
    )


# ============================================================================
# FASE 1: addestramento normale (identico a train.py)
# ============================================================================
model = build_model(
    seq_len=X_train.shape[1], input_dim=X_train.shape[2],
    d_model=64, ff_dim=128, num_layers=2, num_heads=8, dropout_rate=0.35,
)
model.summary()
n_params = model.count_params()
print(f"Parametri totali: {n_params}  |  rapporto param/esempi training: {n_params / len(y_train):.2f}")

compile_model(model, lr=1e-3)

early_stop = keras.callbacks.EarlyStopping(monitor="val_loss", patience=20, restore_best_weights=True)

history = model.fit(
    X_train, y_train,
    validation_data=(X_val, y_val),
    epochs=200,
    batch_size=32,
    class_weight=class_weights,
    callbacks=[early_stop],
    verbose=2,
)

plt.figure(figsize=(8, 5))
plt.plot(history.history["loss"], label="training loss")
plt.plot(history.history["val_loss"], label="validation loss")
plt.xlabel("epoca")
plt.ylabel("binary crossentropy")
plt.title("Curve di addestramento (fase 1: modello base)")
plt.legend()
plt.tight_layout()
plt.savefig("training_curves.png", dpi=120)

print("\n=== Fase 1 completata: modello base ===")
base_metrics = evaluate_and_print(model, "base")


# ============================================================================
# FASE 2: fine-tuning con pruning magnitude-based (manuale)
# ============================================================================
class MagnitudePruningCallback(keras.callbacks.Callback):
    """
    Ad ogni inizio epoca ricalcola la soglia di magnitudine per la
    sparsita' target di quell'epoca (schedule cubico, come il
    PolynomialDecay di default di tfmot) e azzera i pesi sotto soglia
    in ciascun kernel monitorato. Ad ogni fine batch riapplica la
    maschera corrente, cosi' l'ottimizzatore non puo' far "risalire"
    da zero i pesi gia' potati nel frattempo.
    """

    def __init__(self, dense_layers, final_sparsity, total_epochs):
        super().__init__()
        self.dense_layers = dense_layers
        self.final_sparsity = final_sparsity
        self.total_epochs = total_epochs
        self.masks = [tf.Variable(tf.ones_like(l.kernel), trainable=False) for l in dense_layers]

    def _sparsity_for_epoch(self, epoch):
        progress = epoch / max(self.total_epochs - 1, 1)
        return self.final_sparsity * (1 - (1 - progress) ** 3)

    def on_epoch_begin(self, epoch, logs=None):
        target_sparsity = self._sparsity_for_epoch(epoch)
        for layer, mask in zip(self.dense_layers, self.masks):
            kernel = layer.kernel.numpy()
            k = int(target_sparsity * kernel.size)
            if k > 0:
                threshold = np.partition(np.abs(kernel).flatten(), k - 1)[k - 1]
                new_mask = (np.abs(kernel) > threshold).astype(np.float32)
            else:
                new_mask = np.ones_like(kernel, dtype=np.float32)
            mask.assign(new_mask)
            layer.kernel.assign(kernel * new_mask)
        print(f"  [pruning] epoca {epoch}: sparsita' target = {target_sparsity:.1%}")

    def on_train_batch_end(self, batch, logs=None):
        for layer, mask in zip(self.dense_layers, self.masks):
            layer.kernel.assign(layer.kernel * mask)


pruned_model = build_model(
    seq_len=X_train.shape[1], input_dim=X_train.shape[2],
    d_model=64, ff_dim=128, num_layers=2, num_heads=8, dropout_rate=0.35,
)
pruned_model(X_train[:1])                      # istanzia le variabili
pruned_model.set_weights(model.get_weights())  # riparte dai pesi della fase 1
compile_model(pruned_model, lr=1e-4)

prunable_layers = get_prunable_dense_layers(pruned_model)
print(f"\nDense sottoposti a pruning: {len(prunable_layers)}")

pruning_cb = MagnitudePruningCallback(
    prunable_layers, final_sparsity=PRUNING_FINAL_SPARSITY, total_epochs=PRUNING_FINE_TUNE_EPOCHS)

print(f"\n=== Fase 2: fine-tuning con pruning (sparsita' target {PRUNING_FINAL_SPARSITY:.0%}) ===")
pruned_model.fit(
    X_train, y_train,
    validation_data=(X_val, y_val),
    epochs=PRUNING_FINE_TUNE_EPOCHS,
    batch_size=32,
    class_weight=class_weights,
    callbacks=[pruning_cb],  # niente EarlyStopping qui: deve completare lo schedule di
                              # sparsita' fino in fondo, non fermarsi al miglior val_loss
                              # (che favorirebbe sempre una sparsita' bassa, vedi discussione)
    verbose=2,
)

# sparsita' effettivamente raggiunta (dopo l'eventuale restore_best_weights)
total_zeros = sum(int(np.sum(l.kernel.numpy() == 0)) for l in prunable_layers)
total_weights = sum(l.kernel.numpy().size for l in prunable_layers)
print(f"Sparsita' complessiva sui kernel avvolti: {total_zeros/total_weights:.1%}")

pruned_metrics = evaluate_and_print(pruned_model, "dopo pruning")


# ============================================================================
# FASE 3: fine-tuning con weight clustering (manuale, k-means 1D)
# ============================================================================
def kmeans_1d(values, k, n_iter=CLUSTERING_KMEANS_ITERS):
    """K-means su un array 1D di pesi. Inizializzazione via quantili
    (deterministica, evita di dipendere da un seed casuale)."""
    quantiles = np.linspace(0, 1, k)
    centroids = np.quantile(values, quantiles)
    for _ in range(n_iter):
        distances = np.abs(values[:, None] - centroids[None, :])
        assignment = np.argmin(distances, axis=1)
        new_centroids = centroids.copy()
        for c in range(k):
            members = values[assignment == c]
            if len(members) > 0:
                new_centroids[c] = members.mean()
        if np.allclose(new_centroids, centroids):
            break
        centroids = new_centroids
    return centroids


class ClusteringCallback(keras.callbacks.Callback):
    """
    All'inizio del fine-tuning calcola, per ciascun kernel monitorato, i
    centroidi via k-means SOLO sui pesi non nulli (preserva la sparsita'
    del pruning). Ad ogni fine batch, i pesi non nulli vengono
    "agganciati" al centroide piu' vicino tra quelli fissati -- i pesi
    gia' a zero restano a zero.
    """

    def __init__(self, dense_layers, n_clusters):
        super().__init__()
        self.dense_layers = dense_layers
        self.n_clusters = n_clusters
        self.centroids = []
        self.zero_masks = []  # congelata all'inizio: NON va ricalcolata dal kernel
                               # corrente, altrimenti un peso potato che si sposta
                               # anche di poco per effetto del gradiente smette di
                               # essere riconosciuto come zero e viene "riassorbito"
                               # nel cluster piu' vicino invece di restare a zero.

    def on_train_begin(self, logs=None):
        for layer in self.dense_layers:
            kernel = layer.kernel.numpy()
            zero_mask = (kernel == 0)
            self.zero_masks.append(zero_mask)
            nonzero = kernel[~zero_mask]
            if len(nonzero) >= self.n_clusters:
                centroids = kmeans_1d(nonzero, self.n_clusters)
            else:
                centroids = np.unique(nonzero) if len(nonzero) > 0 else np.array([0.0])
            self.centroids.append(centroids)
            self._snap(layer, centroids, zero_mask)

    def _snap(self, layer, centroids, zero_mask):
        kernel = layer.kernel.numpy()
        distances = np.abs(kernel[:, :, None] - centroids[None, None, :])
        nearest = centroids[np.argmin(distances, axis=-1)]
        nearest[zero_mask] = 0.0     # maschera fissa: i pesi potati restano a zero
        layer.kernel.assign(nearest)

    def on_train_batch_end(self, batch, logs=None):
        for layer, centroids, zero_mask in zip(self.dense_layers, self.centroids, self.zero_masks):
            self._snap(layer, centroids, zero_mask)


clustered_model = build_model(
    seq_len=X_train.shape[1], input_dim=X_train.shape[2],
    d_model=64, ff_dim=128, num_layers=2, num_heads=8, dropout_rate=0.35,
)
clustered_model(X_train[:1])
clustered_model.set_weights(pruned_model.get_weights())  # riparte dai pesi pruned
compile_model(clustered_model, lr=1e-4)

clusterable_layers = get_prunable_dense_layers(clustered_model)
clustering_cb = ClusteringCallback(clusterable_layers, n_clusters=CLUSTERING_N_CLUSTERS)

print(f"\n=== Fase 3: fine-tuning con weight clustering ({CLUSTERING_N_CLUSTERS} centroidi) ===")
clustered_model.fit(
    X_train, y_train,
    validation_data=(X_val, y_val),
    epochs=CLUSTERING_FINE_TUNE_EPOCHS,
    batch_size=32,
    class_weight=class_weights,
    callbacks=[clustering_cb],  # stesso motivo della fase 2: niente EarlyStopping
    verbose=2,
)

# verifica: sparsita' (deve essere rimasta al target del pruning) e numero
# di valori distinti per kernel (dovrebbe essere <= n_clusters + lo zero)
post_zeros = sum(int(np.sum(l.kernel.numpy() == 0)) for l in clusterable_layers)
post_total = sum(l.kernel.numpy().size for l in clusterable_layers)
print(f"  Sparsita' dopo il clustering: {post_zeros/post_total:.1%} (deve restare ~{PRUNING_FINAL_SPARSITY:.0%})")
for layer in clusterable_layers[:2]:
    n_unique = len(np.unique(layer.kernel.numpy()))
    print(f"  {layer.name}: {n_unique} valori distinti nel kernel")

clustered_metrics = evaluate_and_print(clustered_model, "dopo pruning + clustering")


# ============================================================================
# Riepilogo e salvataggio
# ============================================================================
print("\n=== Confronto finale ===")
print(f"{'metrica':12s} {'base':>10s} {'pruned':>10s} {'pruned+clustered':>18s}")
for k in ["loss", "accuracy", "precision", "recall"]:
    print(f"{k:12s} {base_metrics[k]:10.4f} {pruned_metrics[k]:10.4f} {clustered_metrics[k]:18.4f}")

clustered_model.save(FINAL_MODEL_PATH)
print(f"\nModello ottimizzato (pruning + clustering) salvato in {FINAL_MODEL_PATH}")
print("Prossimo passo: scrivere/lanciare la conversione TFLite su questo file.")
