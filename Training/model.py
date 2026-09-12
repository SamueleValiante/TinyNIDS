"""
Tiny Transformer per TinyNIDS.

Decisioni di design: d_model=16, 1 layer encoder, 1 testa di attenzione,
linear attention (feature map elu+1), codifica posizionale sinusoidale fissa,
feed-forward ff_dim=16, dropout=0.2, max pooling per l'aggregazione finale.
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


class LinearAttention(layers.Layer):
    """
    Self-attention con complessita' O(N) invece di O(N^2): grazie
    all'associativita' della moltiplicazione tra matrici, si calcola prima
    K^T*V (una matrice d x d, piccola) e solo dopo si moltiplica per Q --
    la matrice N x N dell'attenzione standard non viene mai costruita.
    """

    def __init__(self, d_model, **kwargs):
        super().__init__(**kwargs)
        self.d_model = d_model
        self.wq = layers.Dense(d_model)
        self.wk = layers.Dense(d_model)
        self.wv = layers.Dense(d_model)
        self.wo = layers.Dense(d_model)

    def feature_map(self, x):
        # elu(x)+1: garantisce valori non negativi, richiesti perche' il
        # trucco della linear attention si comporti come dei "pesi" validi.
        return tf.nn.elu(x) + 1.0

    def call(self, x):
        q = self.feature_map(self.wq(x))   # (batch, N, d_model)
        k = self.feature_map(self.wk(x))   # (batch, N, d_model)
        v = self.wv(x)                      # (batch, N, d_model)

        kv = tf.einsum("bnd,bne->bde", k, v)         # (batch, d_model, d_model): mai N x N
        k_sum = tf.reduce_sum(k, axis=1)              # (batch, d_model)

        numerator = tf.einsum("bnd,bde->bne", q, kv)  # (batch, N, d_model)
        denominator = tf.einsum("bnd,bd->bn", q, k_sum)
        denominator = tf.expand_dims(denominator, -1) + 1e-6  # evita divisione per zero

        return self.wo(numerator / denominator)


class EncoderBlock(layers.Layer):
    """Un blocco encoder: linear attention + feed-forward, entrambi con connessione residua."""

    def __init__(self, d_model, ff_dim, dropout_rate, **kwargs):
        super().__init__(**kwargs)
        self.attention = LinearAttention(d_model)
        self.dropout1 = layers.Dropout(dropout_rate)
        self.norm1 = layers.LayerNormalization()

        self.ff = keras.Sequential([
            layers.Dense(ff_dim, activation="relu"),
            layers.Dense(d_model),
        ])
        self.dropout2 = layers.Dropout(dropout_rate)
        self.norm2 = layers.LayerNormalization()

    def call(self, x, training=False):
        attn_out = self.dropout1(self.attention(x), training=training)
        x = self.norm1(x + attn_out)          # residua attorno all'attenzione

        ff_out = self.dropout2(self.ff(x), training=training)
        x = self.norm2(x + ff_out)            # residua attorno al feed-forward
        return x


def build_model(seq_len=20, input_dim=16, d_model=16, ff_dim=16, dropout_rate=0.2):
    inputs = layers.Input(shape=(seq_len, input_dim))

    x = layers.Dense(d_model)(inputs)                        # proiezione 16 -> d_model
    x = x + sinusoidal_positional_encoding(seq_len, d_model)  # inietta l'ordine nella sequenza

    x = EncoderBlock(d_model, ff_dim, dropout_rate)(x)

    x = layers.GlobalMaxPooling1D()(x)   # max pooling: cattura sia il pattern diffuso (SYN flood)
                                          # sia il singolo pacchetto anomalo (MITM)

    outputs = layers.Dense(1, activation="sigmoid")(x)

    return keras.Model(inputs, outputs, name="tiny_nids_transformer")


if __name__ == "__main__":
    model = build_model()
    model.summary()
