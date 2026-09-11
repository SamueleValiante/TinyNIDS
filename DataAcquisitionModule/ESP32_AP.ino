#include <WiFi.h>
#include "esp_wifi.h"

#define ATTACK_MODE 2   // 0 = normale, 1 = solo SYN flood, 2 = solo ARP (MITM)

char SSID[] = "Ape_50_WiFi";
uint8_t apMac[6];

void snifferCallBack(void *buf, wifi_promiscuous_pkt_type_t type) {
  if (type != WIFI_PKT_DATA) return;

  wifi_promiscuous_pkt_t *pkt = (wifi_promiscuous_pkt_t*)buf;
  uint8_t *frame = pkt->payload;
  int len = pkt->rx_ctrl.sig_len;

  uint8_t *addr1 = frame + 4, *addr2 = frame + 10, *addr3 = frame + 16;
  bool isOurAP = memcmp(addr1, apMac, 6) == 0 || memcmp(addr2, apMac, 6) == 0 || memcmp(addr3, apMac, 6) == 0;
  if (!isOurAP) return;

  bool isQoS = (frame[0] & 0x80) != 0;
  int headerLen = (isQoS ? 26 : 24) + 8;
  if (len <= headerLen) return;

  uint16_t etherType = (frame[headerLen-2] << 8) | frame[headerLen-1];   // 0x0800=IP, 0x0806=ARP

  if (ATTACK_MODE == 2) {
    if (etherType != 0x0806) return;               // solo pacchetti ARP

    uint8_t *arp = frame + headerLen;
    uint16_t operation = (arp[6] << 8) | arp[7];    // 1=request, 2=reply
    IPAddress senderIP(arp[14], arp[15], arp[16], arp[17]);
    IPAddress targetIP(arp[24], arp[25], arp[26], arp[27]);

    // uso 900+operation come "protocollo" fittizio, fuori dal range IP normale (0-255),
    // così il CSV resta nello stesso formato a 4 campi senza dover toccare lo script Python
    Serial.printf("%s, %s, %d, %d\n", senderIP.toString().c_str(), targetIP.toString().c_str(), 900 + operation, len);
    return;
  }

  if (etherType != 0x0800) return;                  // in tutte le altre modalità, ignora ciò che non è IP
  uint8_t *ipPacket = frame + headerLen;
  if ((ipPacket[0] >> 4) != 4) return;
  uint8_t protocol = ipPacket[9];

  if (ATTACK_MODE == 1) {
    if (protocol != 6) return;
    int ipHeaderLen = 20;
    if (len <= headerLen + ipHeaderLen + 14) return;
    uint8_t *tcpHeader = ipPacket + ipHeaderLen;
    if (!(tcpHeader[13] & 0x02)) return;
  }

  IPAddress srcIP(ipPacket[12], ipPacket[13], ipPacket[14], ipPacket[15]);
  IPAddress dstIP(ipPacket[16], ipPacket[17], ipPacket[18], ipPacket[19]);
  Serial.printf("%s, %s, %d, %d\n", srcIP.toString().c_str(), dstIP.toString().c_str(), protocol, len);
}

void setup() {
  Serial.begin(115200);
  delay(1000);
  WiFi.softAP(SSID);
  delay(1000);
  Serial.print("ESP32 avviato come Access Point: IP: ");
  Serial.println(WiFi.softAPIP());
  WiFi.softAPmacAddress(apMac);

  esp_wifi_set_promiscuous_rx_cb(&snifferCallBack);
  wifi_promiscuous_filter_t filter = { .filter_mask = WIFI_PROMIS_FILTER_MASK_DATA };
  esp_wifi_set_promiscuous_filter(&filter);
  esp_wifi_set_promiscuous(true);
}

void loop() {}