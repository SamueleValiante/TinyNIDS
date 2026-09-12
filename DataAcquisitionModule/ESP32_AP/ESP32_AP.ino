#include <WiFi.h>
#include "esp_wifi.h"

#define ATTACK_MODE 1   // 0 = normale (ora include ARP legittimo), 1 = solo SYN flood, 2 = solo ARP (MITM)

char SSID[] = "ESP32_WiFi";
uint8_t apMac[6];   // MAC del nostro AP, usato per scartare frame di reti vicine

void snifferCallBack(void *buf, wifi_promiscuous_pkt_type_t type) {
  if (type != WIFI_PKT_DATA) return;   // ci interessano solo i frame dati

  wifi_promiscuous_pkt_t *pkt = (wifi_promiscuous_pkt_t*)buf;
  uint8_t *frame = pkt->payload;
  int len = pkt->rx_ctrl.sig_len;

  // scarta i frame che non appartengono alla nostra rete (filtro BSSID)
  uint8_t *addr1 = frame + 4, *addr2 = frame + 10, *addr3 = frame + 16;
  bool isOurAP = memcmp(addr1, apMac, 6) == 0 || memcmp(addr2, apMac, 6) == 0 || memcmp(addr3, apMac, 6) == 0;
  if (!isOurAP) return;

  // calcola dove finisce l'header 802.11 (+QoS se presente) e inizia il payload
  bool isQoS = (frame[0] & 0x80) != 0;
  int headerLen = (isQoS ? 26 : 24) + 8;
  if (len <= headerLen) return;

  // EtherType: dice se dentro c'è un pacchetto IP (0x0800) o ARP (0x0806)
  uint16_t etherType = (frame[headerLen-2] << 8) | frame[headerLen-1];

  // ramo ARP: attivo in modalità normale e MITM, escluso solo durante il DDoS
  // (differenza rispetto a prima: prima girava solo in ATTACK_MODE==2)
  if (etherType == 0x0806 && ATTACK_MODE != 1) {
    uint8_t *arp = frame + headerLen;
    uint16_t operation = (arp[6] << 8) | arp[7];       // 1=request, 2=reply
    IPAddress senderIP(arp[14], arp[15], arp[16], arp[17]);
    IPAddress targetIP(arp[24], arp[25], arp[26], arp[27]);
    // 900+operation: codice fittizio fuori dal range IP normale, stesso formato a 4 campi
    Serial.printf("%s, %s, %d, %d\n", senderIP.toString().c_str(), targetIP.toString().c_str(), 900 + operation, len);
    return;
  }

  if (ATTACK_MODE == 2) return;   // in modalità MITM ci interessa solo l'ARP: il resto si scarta qui

  // da qui in poi si processano solo pacchetti IP
  if (etherType != 0x0800) return;
  uint8_t *ipPacket = frame + headerLen;
  if ((ipPacket[0] >> 4) != 4) return;   // solo IPv4
  uint8_t protocol = ipPacket[9];

  // filtro SYN-only, attivo solo durante la simulazione del DDoS
  if (ATTACK_MODE == 1) {
    if (protocol != 6) return;                          // solo TCP
    int ipHeaderLen = 20;
    if (len <= headerLen + ipHeaderLen + 14) return;     // troppo corto per contenere i flag TCP
    uint8_t *tcpHeader = ipPacket + ipHeaderLen;
    if (!(tcpHeader[13] & 0x02)) return;                 // bit del flag SYN
  }

  // estrazione ed emissione, uguale per traffico normale e DDoS
  IPAddress srcIP(ipPacket[12], ipPacket[13], ipPacket[14], ipPacket[15]);
  IPAddress dstIP(ipPacket[16], ipPacket[17], ipPacket[18], ipPacket[19]);
  Serial.printf("%s, %s, %d, %d\n", srcIP.toString().c_str(), dstIP.toString().c_str(), protocol, len);
}

void setup() {
  Serial.begin(115200);
  delay(1000);

  WiFi.softAP(SSID);
  delay(1000);   // lascia stabilizzare la radio prima di toccare la UART

  Serial.print("ESP32 avviato come Access Point: IP: ");
  Serial.println(WiFi.softAPIP());
  WiFi.softAPmacAddress(apMac);   // salva il nostro MAC, serve al filtro BSSID sopra

  esp_wifi_set_promiscuous_rx_cb(&snifferCallBack);
  wifi_promiscuous_filter_t filter = { .filter_mask = WIFI_PROMIS_FILTER_MASK_DATA };
  esp_wifi_set_promiscuous_filter(&filter);   // scarta beacon/probe/ack a livello driver
  esp_wifi_set_promiscuous(true);
}

void loop() {}