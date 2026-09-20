#include <Arduino.h>
#include <WiFi.h>
#include <cstring>
#include "esp_wifi.h"
#include "lwip/lwip_napt.h"
#include "lwip/tcpip.h"
#include "lwip/inet.h"
#include "dhcpserver/dhcpserver.h"

#include "tensorflow/lite/c/common.h"
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/micro/micro_log.h"
#include "tensorflow/lite/schema/schema_generated.h"
#include "tiny_nids_transformer_model.h"   // g_tiny_nids_model, g_tiny_nids_model_len

// 0 = normale (include ARP legittimo), 1 = solo SYN flood, 2 = solo ARP (MITM)
// Per il deployment/inferenza live va lasciato a 0: vogliamo vedere TUTTO
// il traffico (ARP + qualunque protocollo IP), non un solo tipo come
// durante la raccolta dati per il dataset.
#define ATTACK_MODE 0

char SSID[] = "ESP32_WiFi";
char STA_SSID[] = "TP-LINK_8972";
char STA_PSWD[] = "38089541";

uint8_t apMac[6];   // MAC del nostro AP, usato dal filtro BSSID nello sniffer

// ============================================================
// TinyNIDS: inferenza -- coda tra sniffer callback e task dedicato
// ============================================================
// La callback dello sniffer resta minimale (estrae i campi, li mette in
// coda); un task separato, a priorita' piu' bassa, consuma la coda,
// accumula la finestra di 20 pacchetti e invoca l'interprete TFLite
// Micro. Cosi' il lavoro pesante non gira mai nel contesto (time-critical)
// del driver WiFi.

#define SEQ_LEN     20
#define N_FEATURES  16
#define WINDOW_STEP 10   // overlap tra finestre consecutive, come nel preprocessing Python

// Valori da norm_params.json (fit sul training set in preprocessing.py):
#define LEN_MIN    66.0f
#define LEN_MAX    1538.0f
#define DT_LOG_MIN 0.0f
#define DT_LOG_MAX 8.51579221050061f

struct RawPacket {
  uint8_t srcIP[4];
  uint8_t dstIP[4];
  uint16_t protocol;   // 6/17/1/2 = TCP/UDP/ICMP/IGMP, 901/902 = ARP request/reply
  uint16_t len;
  int64_t timestamp_us;
};

static QueueHandle_t packetQueue;

static float windowBuffer[SEQ_LEN][N_FEATURES];
static int windowFill = 0;          // quanti pacchetti validi ci sono ora nel buffer
static int64_t lastPacketTime = -1; // per calcolare l'intervallo temporale

namespace {
constexpr int kTensorArenaSize = 64 * 1024;
alignas(16) uint8_t tensor_arena[kTensorArenaSize];

const tflite::Model* model = nullptr;
tflite::MicroInterpreter* interpreter = nullptr;
TfLiteTensor* input = nullptr;
TfLiteTensor* output = nullptr;
}  // namespace

// Converte un pacchetto grezzo nel vettore a 16 feature atteso dal modello,
// con le stesse formule/ordine di preprocessing.py.
static void packetToFeatures(const RawPacket &pkt, float out[N_FEATURES]) {
  for (int i = 0; i < 4; i++) out[i]     = pkt.srcIP[i] / 255.0f;
  for (int i = 0; i < 4; i++) out[4 + i] = pkt.dstIP[i] / 255.0f;

  // one-hot protocollo su 6 valori, ordine ESATTO da preprocessing.py:
  // PROTOCOLS = [1, 2, 6, 17, 901, 902] = [ICMP, IGMP, TCP, UDP, ARP_req, ARP_reply]
  for (int i = 0; i < 6; i++) out[8 + i] = 0.0f;
  switch (pkt.protocol) {
    case 1:   out[8 + 0] = 1.0f; break;  // ICMP
    case 2:   out[8 + 1] = 1.0f; break;  // IGMP
    case 6:   out[8 + 2] = 1.0f; break;  // TCP
    case 17:  out[8 + 3] = 1.0f; break;  // UDP
    case 901: out[8 + 4] = 1.0f; break;  // ARP request
    case 902: out[8 + 5] = 1.0f; break;  // ARP reply
  }

  float lenNorm = (pkt.len - LEN_MIN) / (LEN_MAX - LEN_MIN);
  out[14] = lenNorm;

  // delta in MILLISECONDI (coerente con preprocessing.py: total_seconds()*1000),
  // poi log1p come nello script Python.
  float delta_ms = (lastPacketTime < 0) ? 0.0f : (pkt.timestamp_us - lastPacketTime) / 1000.0f;
  float dtLog = log1pf(delta_ms);
  float dtNorm = (dtLog - DT_LOG_MIN) / (DT_LOG_MAX - DT_LOG_MIN);
  out[15] = dtNorm;

  lastPacketTime = pkt.timestamp_us;
}

static void setupInference() {
  model = tflite::GetModel(g_tiny_nids_model);
  if (model->version() != TFLITE_SCHEMA_VERSION) {
    MicroPrintf("Versione schema modello non corrispondente!");
    return;
  }

  // Elenco DEFINITIVO, ricavato da list_ops.py sul .tflite finale
  // (batch fisso a 1, divisione esclusa dalla quantizzazione -- per
  // questo motivo il grafo contiene comunque QUANTIZE/DEQUANTIZE
  // internamente, anche se l'input/output esterni sono float32): 17
  // operatori distinti, 146 nodi totali nel grafo.
  static tflite::MicroMutableOpResolver<17> resolver;
  resolver.AddAdd();
  resolver.AddMul();
  resolver.AddFullyConnected();
  resolver.AddDequantize();
  resolver.AddReshape();
  resolver.AddQuantize();
  resolver.AddMean();
  resolver.AddTranspose();
  resolver.AddBatchMatMul();
  resolver.AddNeg();
  resolver.AddSquaredDifference();
  resolver.AddRsqrt();
  resolver.AddElu();
  resolver.AddSum();
  resolver.AddDiv();
  resolver.AddReduceMax();
  resolver.AddLogistic();

  static tflite::MicroInterpreter static_interpreter(
      model, resolver, tensor_arena, kTensorArenaSize);
  interpreter = &static_interpreter;

  if (interpreter->AllocateTensors() != kTfLiteOk) {
    MicroPrintf("AllocateTensors() fallita -- probabile tensor_arena troppo piccolo");
    return;
  }

  input = interpreter->input(0);
  output = interpreter->output(0);

  // L'input/output del .tflite sono FLOAT32 (non int8): il
  // QuantizationDebugger usato in fase di conversione, per escludere
  // selettivamente l'operazione DIV dalla quantizzazione, non rispetta
  // inference_input_type/inference_output_type -- restano al default
  // float32. Il resto del grafo (i Dense interni, l'attenzione) e'
  // comunque int8 internamente: le op QUANTIZE/DEQUANTIZE nel resolver
  // gestiscono la conversione ai confini, non serve farla a mano qui.
  Serial.printf("[DEBUG] input tensor type=%d bytes=%d, output tensor type=%d bytes=%d\n",
                (int)input->type, (int)input->bytes, (int)output->type, (int)output->bytes);
  Serial.printf("Tensor arena usata: %d / %d byte\n",
                (int)interpreter->arena_used_bytes(), kTensorArenaSize);
}

static void runInference() {
  // Input float32: si scrive direttamente il buffer della finestra,
  // nessuna quantizzazione manuale (la fa il grafo internamente).
  memcpy(input->data.f, windowBuffer, sizeof(windowBuffer));

  if (interpreter->Invoke() != kTfLiteOk) {
    MicroPrintf("Invoke() fallita");
    return;
  }

  // Output float32: e' gia' la probabilita', nessuna dequantizzazione.
  float prob = output->data.f[0];

  bool isAttack = prob > 0.5f;
  Serial.printf("[TinyNIDS] probabilita'=%.4f -> %s\n", prob, isAttack ? "ATTACCO" : "normale");
  // TODO: qui puoi agganciare l'azione desiderata (LED, log strutturato,
  // notifica, ecc.) invece del solo Serial.printf.
}

// Task che consuma la coda, accumula la finestra e invoca l'inferenza
// ogni WINDOW_STEP pacchetti dopo il primo riempimento.
static void inferenceTask(void *pv) {
  setupInference();

  RawPacket pkt;
  int packetsSinceLastInference = 0;

  while (true) {
    if (xQueueReceive(packetQueue, &pkt, portMAX_DELAY) == pdTRUE) {

      float features[N_FEATURES];
      packetToFeatures(pkt, features);

      // finestra scorrevole: shift a sinistra di una posizione, nuovo
      // pacchetto in coda (equivalente all'overlap usato in preprocessing)
      if (windowFill < SEQ_LEN) {
        memcpy(windowBuffer[windowFill], features, sizeof(features));
        windowFill++;
      } else {
        memmove(windowBuffer[0], windowBuffer[1], sizeof(float) * (SEQ_LEN - 1) * N_FEATURES);
        memcpy(windowBuffer[SEQ_LEN - 1], features, sizeof(features));
      }

      if (windowFill == SEQ_LEN) {
        packetsSinceLastInference++;
        if (packetsSinceLastInference >= WINDOW_STEP) {
          runInference();
          packetsSinceLastInference = 0;
        }
      }
    }
  }
}

static void startInferencePipeline() {
  packetQueue = xQueueCreate(64, sizeof(RawPacket));
  xTaskCreate(inferenceTask, "tinynids_inference", 8192, nullptr, 1, nullptr);
}

// ============================================================
// Sniffer / cattura promiscua
// ============================================================
void snifferCallBack(void *buf, wifi_promiscuous_pkt_type_t type) {
  if (type != WIFI_PKT_DATA) return;

  wifi_promiscuous_pkt_t *pkt = (wifi_promiscuous_pkt_t*)buf;
  uint8_t *frame = pkt->payload;
  int len = pkt->rx_ctrl.sig_len;

  // scarta i frame che non appartengono alla nostra rete (filtro BSSID)
  uint8_t *addr1 = frame + 4, *addr2 = frame + 10, *addr3 = frame + 16;
  bool isOurAP = memcmp(addr1, apMac, 6) == 0 || memcmp(addr2, apMac, 6) == 0 || memcmp(addr3, apMac, 6) == 0;
  if (!isOurAP) return;

  bool isQoS = (frame[0] & 0x80) != 0;
  int headerLen = (isQoS ? 26 : 24) + 8;
  if (len <= headerLen) return;

  uint16_t etherType = (frame[headerLen-2] << 8) | frame[headerLen-1];

  // ramo ARP: attivo in modalità normale e MITM, escluso solo durante il DDoS
  if (etherType == 0x0806 && ATTACK_MODE != 1) {
    uint8_t *arp = frame + headerLen;
    uint16_t operation = (arp[6] << 8) | arp[7];

    RawPacket rp;
    memcpy(rp.srcIP, arp + 14, 4);   // sender IP
    memcpy(rp.dstIP, arp + 24, 4);   // target IP
    rp.protocol = 900 + operation;   // 901 = request, 902 = reply
    rp.len = (uint16_t)len;
    rp.timestamp_us = esp_timer_get_time();
    xQueueSend(packetQueue, &rp, 0);   // non bloccante: se la coda e' piena, scarta

    return;
  }

  if (ATTACK_MODE == 2) return;   // in MITM ci interessa solo l'ARP

  if (etherType != 0x0800) return;
  uint8_t *ipPacket = frame + headerLen;
  if ((ipPacket[0] >> 4) != 4) return;
  uint8_t protocol = ipPacket[9];

  if (ATTACK_MODE == 1) {
    if (protocol != 6) return;
    int ipHeaderLen = 20;
    if (len <= headerLen + ipHeaderLen + 14) return;
    uint8_t *tcpHeader = ipPacket + ipHeaderLen;
    if (!(tcpHeader[13] & 0x02)) return;   // bit del flag SYN
  }

  RawPacket rp;
  memcpy(rp.srcIP, ipPacket + 12, 4);
  memcpy(rp.dstIP, ipPacket + 16, 4);
  rp.protocol = protocol;
  rp.len = (uint16_t)len;
  rp.timestamp_us = esp_timer_get_time();
  xQueueSend(packetQueue, &rp, 0);
}

void setup() {
  Serial.begin(115200);
  delay(1000);

  bool iswifi = WiFi.mode(WIFI_AP_STA);
  delay(1000);
  if (iswifi == true) {
    Serial.printf("\n\nWifi mode is on\n");
  }

  // connessione alla rete wifi d'appoggio (STA)
  WiFi.begin(STA_SSID, STA_PSWD);
  Serial.printf("Connessione verso il wifi d'appoggio...");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.print("\nConnesso! IP STA: ");
  Serial.println(WiFi.localIP());

  // access point
  WiFi.softAP(SSID);
  delay(1000);
  Serial.print("ESP32 avviato come Access Point: IP: ");
  Serial.println(WiFi.softAPIP());
  WiFi.softAPmacAddress(apMac);   // salva il MAC dell'AP, serve al filtro BSSID sopra

  esp_netif_t *sta_netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
  esp_netif_t *ap_netif  = esp_netif_get_handle_from_ifkey("WIFI_AP_DEF");

  esp_netif_set_default_netif(sta_netif);

  if (esp_netif_napt_enable(ap_netif) == ESP_OK) {
    Serial.println("NAPT abilitato con successo!");
  } else {
    Serial.println("Errore nell'abilitazione di NAPT!");
  }

  // fermo il DHCP server prima di riconfigurarlo
  esp_netif_dhcps_stop(ap_netif);

  esp_netif_dns_info_t dns_info;
  dns_info.ip.type = ESP_IPADDR_TYPE_V4;
  dns_info.ip.u_addr.ip4.addr = ipaddr_addr("8.8.8.8");
  esp_netif_set_dns_info(ap_netif, ESP_NETIF_DNS_MAIN, &dns_info);

  dhcps_offer_t dns_offer = OFFER_DNS;
  esp_err_t err = esp_netif_dhcps_option(ap_netif, ESP_NETIF_OP_SET, ESP_NETIF_DOMAIN_NAME_SERVER,
                                          &dns_offer, sizeof(dns_offer));
  Serial.printf("Risultato esp_netif_dhcps_option: %s\n", esp_err_to_name(err));

  // riavvio il DHCP server con la configurazione DNS applicata
  esp_netif_dhcps_start(ap_netif);
  Serial.println("DHCP server riavviato con DNS configurato");

  // TinyNIDS: coda + task di inferenza, DEVE essere pronta prima di
  // attivare lo sniffer (altrimenti la prima callback potrebbe trovare
  // packetQueue non ancora creata).
  startInferencePipeline();

  // sniffer / promiscuous mode
  esp_wifi_set_promiscuous_rx_cb(&snifferCallBack);
  wifi_promiscuous_filter_t filter = { .filter_mask = WIFI_PROMIS_FILTER_MASK_DATA };
  esp_wifi_set_promiscuous_filter(&filter);
  esp_wifi_set_promiscuous(true);
}

void loop()
{}

extern "C" void app_main()
{
  initArduino();
  setup();
  for (;;) {
    loop();
    vTaskDelay(1);
  }
}