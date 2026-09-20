"""
Tiny Transformer per TinyNIDS -- versione ridisegnata sul dataset esteso.

Decisioni di design (aggiornate per rendere necessaria l'ottimizzazione
ESP32 -- in float32 il modello supera i 320 KB di SRAM disponibili, in
int8 post-quantizzazione rientra comodamente): d_model=64, 2 layer
encoder, 8 teste di attenzione, linear attention (feature map elu+1)
multi-head, codifica posizionale sinusoidale fissa, feed-forward
ff_dim=128, dropout=0.35, Pre-LayerNorm (per stabilita' con la
profondita' aumentata), max pooling per l'aggregazione finale.

Il numero di teste non aumenta il conteggio dei parametri (le proiezioni
Q/K/V/O restano d_model x d_model, solo suddivise tra le teste): e' quindi
"gratuito" in termini di rapporto parametri/esempi e viene usato per dare
al modello piu' capacita' rappresentativa senza aumentare il rischio di
overfitting.

Pruning e weight clustering vengono applicati direttamente sui pesi di
questo modello (vedi train_optimize.py), senza passare da
tensorflow-model-optimization: nessuna dipendenza aggiuntiva qui.
"""

import numpy as np
import tensorflow as tf
from tensorflow import keras
from tensorflow.keras import layers


def sinusoidal_positional_encoding(seq_len, d_model):
    """Codifica posizionale fissa (nessun parametro da addestrare)."""
    positions = np.arange(seq_len)[:, np.newaxis]
    dims = np.arange(d_model)[np.newaxis, :]
    angle_rates = 1 / np.power(10000, (2 * (dims // 2)) / np.float32(d_model))
    angles = positions * angle_rates

    pe = np.zeros((seq_len, d_model), dtype=np.float32)
    pe[:, 0::2] = np.sin(angles[:, 0::2])
    pe[:, 1::2] = np.cos(angles[:, 1::2])
    return tf.constant(pe)


class MultiHeadLinearAttention(layers.Layer):
    """
    Self-attention multi-head con complessita' O(N) invece di O(N^2):
    grazie all'associativita' della moltiplicazione tra matrici, per ogni
    testa si calcola prima K^T*V (una matrice head_dim x head_dim, piccola)
    e solo dopo si moltiplica per Q -- la matrice N x N dell'attenzione
    standard non viene mai costruita.

    d_model deve essere divisibile per num_heads.
    """

    def __init__(self, d_model, num_heads, **kwargs):
        super().__init__(**kwargs)
        assert d_model % num_heads == 0, "d_model deve essere divisibile per num_heads"
        self.d_model = d_model
        self.num_heads = num_heads
        self.head_dim = d_model // num_heads

        self.wq = layers.Dense(d_model)
        self.wk = layers.Dense(d_model)
        self.wv = layers.Dense(d_model)
        self.wo = layers.Dense(d_model)

    def get_config(self):
        # Necessario perche' Keras sappia ricostruire il layer (con i
        # giusti d_model/num_heads) al caricamento di un file .keras.
        config = super().get_config()
        config.update({"d_model": self.d_model, "num_heads": self.num_heads})
        return config

    def feature_map(self, x):
        # elu(x)+1: garantisce valori non negativi, richiesti perche' il
        # trucco della linear attention si comporti come dei "pesi" validi.
        return tf.nn.elu(x) + 1.0

    def split_heads(self, x):
        # (batch, N, d_model) -> (batch, N, num_heads, head_dim)
        # -1 al posto di tf.shape(x)[0]: la dimensione di batch resta
        # libera senza generare un'operazione SHAPE nel grafo. La
        # sequence length (x.shape[1]) e' invece nota staticamente in
        # fase di costruzione del modello (finestre a lunghezza fissa),
        # quindi va usata come intero Python, non ricalcolata a runtime:
        # questo evita la catena SHAPE/GATHER/REDUCE_PROD/PACK che
        # altrimenti il convertitore TFLite genera per ricostruire le
        # dimensioni dinamicamente.
        seq_len = x.shape[1]
        x = tf.reshape(x, (-1, seq_len, self.num_heads, self.head_dim))
        return x

    def call(self, x):
        q = self.feature_map(self.split_heads(self.wq(x)))   # (batch, N, h, dh)
        k = self.feature_map(self.split_heads(self.wk(x)))   # (batch, N, h, dh)
        v = self.split_heads(self.wv(x))                      # (batch, N, h, dh)

        kv = tf.einsum("bnhd,bnhe->bhde", k, v)               # (batch, h, dh, dh): mai N x N
        k_sum = tf.reduce_sum(k, axis=1)                       # (batch, h, dh)

        numerator = tf.einsum("bnhd,bhde->bnhe", q, kv)        # (batch, N, h, dh)
        denominator = tf.einsum("bnhd,bhd->bnh", q, k_sum)
        denominator = tf.expand_dims(denominator, -1) + 1e-6   # evita divisione per zero

        out = numerator / denominator                          # (batch, N, h, dh)
        out = tf.reshape(out, (-1, out.shape[1], self.d_model))  # concat teste, no tf.shape()

        return self.wo(out)


class EncoderBlock(layers.Layer):
    """
    Un blocco encoder Pre-LN: LayerNorm applicata PRIMA di attenzione e
    feed-forward (non dopo, come nella versione precedente Post-LN), con
    connessione residua attorno a ciascun sotto-blocco. Il Pre-LN da'
    gradienti piu' stabili quando si impilano piu' layer, evitando che
    l'aumento di profondita' renda il training instabile.
    """

    def __init__(self, d_model, num_heads, ff_dim, dropout_rate, **kwargs):
        super().__init__(**kwargs)
        self.attention = MultiHeadLinearAttention(d_model, num_heads)
        self.dropout1 = layers.Dropout(dropout_rate)
        self.norm1 = layers.LayerNormalization()

        self.ff = keras.Sequential([
            layers.Dense(ff_dim, activation="relu"),
            layers.Dense(d_model),
        ])
        self.dropout2 = layers.Dropout(dropout_rate)
        self.norm2 = layers.LayerNormalization()

        # salvati per get_config: servono a ricostruire attention/ff con
        # le stesse dimensioni al caricamento di un file .keras
        self._d_model = d_model
        self._num_heads = num_heads
        self._ff_dim = ff_dim
        self._dropout_rate = dropout_rate

    def get_config(self):
        config = super().get_config()
        config.update({
            "d_model": self._d_model,
            "num_heads": self._num_heads,
            "ff_dim": self._ff_dim,
            "dropout_rate": self._dropout_rate,
        })
        return config

    def call(self, x, training=False):
        attn_out = self.attention(self.norm1(x))               # Pre-LN attorno all'attenzione
        x = x + self.dropout1(attn_out, training=training)

        ff_out = self.ff(self.norm2(x))                         # Pre-LN attorno al feed-forward
        x = x + self.dropout2(ff_out, training=training)
        return x


def build_model(seq_len=20, input_dim=16, d_model=64, ff_dim=128,
                 num_layers=2, num_heads=8, dropout_rate=0.35, batch_size=None):
    # batch_size=None (default): usato per training/valutazione normali,
    # la dimensione di batch resta dinamica. batch_size=1: usato in fase
    # di conversione per ESP32, dove l'inferenza avviene sempre una
    # finestra alla volta -- un Input a batch fisso permette al
    # convertitore TFLite di eliminare la "macchina di calcolo-shape a
    # runtime" (SHAPE/GATHER/REDUCE_PROD/PACK) legata alla dimensione di
    # batch dinamica, senza dover tracciare manualmente una tf.function
    # (approccio piu' fragile, vedi nota in convert.py).
    if batch_size is None:
        inputs = layers.Input(shape=(seq_len, input_dim))
    else:
        inputs = layers.Input(batch_shape=(batch_size, seq_len, input_dim))

    x = layers.Dense(d_model)(inputs)                        # proiezione 16 -> d_model
    x = x + sinusoidal_positional_encoding(seq_len, d_model)  # inietta l'ordine nella sequenza

    for i in range(num_layers):
        x = EncoderBlock(d_model, num_heads, ff_dim, dropout_rate,
                          name=f"encoder_block_{i}")(x)

    x = layers.LayerNormalization(name="final_norm")(x)  # norm finale, prassi comune col Pre-LN

    x = layers.GlobalMaxPooling1D()(x)   # max pooling: cattura sia il pattern diffuso (SYN flood)
                                          # sia il singolo pacchetto anomalo (MITM)

    outputs = layers.Dense(1, activation="sigmoid")(x)

    return keras.Model(inputs, outputs, name="tiny_nids_transformer")


def get_prunable_dense_layers(model):
    """
    Raccoglie i Dense "interni" su cui applicare pruning/clustering manuali:
    query/key/value/output di ciascun blocco di attenzione, e i due Dense
    della feed-forward di ciascun EncoderBlock. Esclude deliberatamente la
    proiezione iniziale (16->d_model) e il classificatore finale (d_model->1):
    sono piccoli, il grosso dei parametri sta in questi 6 per blocco.
    """
    dense_layers = []
    for layer in model.layers:
        if isinstance(layer, EncoderBlock):
            dense_layers.extend([
                layer.attention.wq, layer.attention.wk,
                layer.attention.wv, layer.attention.wo,
                layer.ff.layers[0], layer.ff.layers[1],
            ])
    return dense_layers


if __name__ == "__main__":
    model = build_model()
    model.summary()
