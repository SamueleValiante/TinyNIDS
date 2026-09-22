# TinyNIDS

Sistema di rilevamento delle intrusioni di rete (SYN Flood/DDoS e MITM/ARP Spoofing) basato su un Tiny Transformer, ottimizzato ed eseguito direttamente su un ESP32, che funge sia da access point sia da sniffer del traffico che attraversa la propria rete.

## Pipeline del progetto

```
Raccolta dati (ESP32) → Preprocessing → Training → Ottimizzazione → Conversione TFLite → Integrazione firmware → Misura prestazioni
```

## Struttura del repository

| Cartella | Contenuto |
|---|---|
| **`DataAcquisitionModule/`** | Firmware ESP32 (`ESP32-AP`) usato per raccogliere il traffico grezzo e generare `dataset.csv`: access point con NAT, sniffer promiscuo, `save_pkt_csv.py` per salvare il traffico ricevuto via seriale in CSV. |
| **`Preprocessing/`** | `preprocessing.py`: trasforma `dataset.csv` in sequenze numeriche normalizzate (`dataset_preprocessato.npz`) — finestre di 20 pacchetti, 16 feature ciascuno. |
| **`Training/`** | `model.py` (architettura del Tiny Transformer) e `train.py`: addestramento del modello base, produce `tiny_nids_transformer.keras` e le curve di addestramento. |
| **`Optimization/`** | `train_optimize.py`: applica pruning (50% sparsità) e weight clustering (16 centroidi) al modello addestrato, produce `tiny_nids_transformer_optimized.keras`. |
| **`Conversion/`** | `convert.py`: converte il modello ottimizzato in TFLite int8 (`tiny_nids_transformer.tflite`) e lo esporta come header C (`tiny_nids_transformer_model.h`) pronto per il firmware. |
| **`Integration/`** | Firmware ESP32 (`ESP32-AP`) con il modello integrato: inferenza in tempo reale sul traffico catturato, tramite TFLite Micro. |
| **`PerformanceMeasurement/`** | Stessa base di `Integration/`, con strumentazione aggiuntiva per misurare latenza di inferenza, backlog della coda e occupazione di memoria durante il funzionamento. |
| **`Docs/`** | Report del progetto (`Report_TinyML_TinyNIDS_ValianteSamuele.odt`). |

Ogni cartella `.../ESP32-AP` è un progetto **ESP-IDF** indipendente (creato con Espressif-IDE), con il proprio `main.cpp`, `CMakeLists.txt` e configurazione.

## Flashare e monitorare il firmware

Le tre versioni del firmware (`DataAcquisitionModule/ESP32-AP`, `Integration/ESP32-AP`, `PerformanceMeasurement/ESP32-AP`) si compilano e flashano allo stesso modo. 

- Apri Espressif-IDE su una delle cartelle menzionate
- importa la cartella `ESP32-AP` come progetto, poi usa i pulsanti Build / Flash / Monitor della toolbar.
- Se il monitor non si avvia dopo il flash da solo, avvialo dal terminale integrato nella IDE, da li digita `idf.py -p /dev/ttyUSB0 flash monitor`
- `/dev/ttyUSB0` è la porta seriale a cui è collegato il tuo esp32

**Quale versione flashare, a seconda di cosa vuoi fare:**

- **Raccogliere nuovo traffico per un dataset** → `DataAcquisitionModule/ESP32-AP`
- **Vedere il sistema classificare il traffico in tempo reale** → `Integration/ESP32-AP`
- **Misurare latenza, throughput e memoria sotto carico** → `PerformanceMeasurement/ESP32-AP`

Al primo utilizzo di una cartella, se `idf.py build` segnala partizione troppo piccola o dipendenze mancanti, verifica che `sdkconfig.defaults` sia presente nella cartella (definisce la tabella delle partizioni custom necessaria per contenere il modello) e che il component manager abbia scaricato `managed_components/` (avviene automaticamente al primo build, serve connessione a internet).
