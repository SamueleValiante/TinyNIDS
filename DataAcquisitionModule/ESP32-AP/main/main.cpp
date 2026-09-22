#include <Arduino.h>
#include <WiFi.h>
#include <cstring>
#include "esp_wifi.h"
#include "lwip/lwip_napt.h"
#include "lwip/tcpip.h"
#include "lwip/inet.h"
#include "dhcpserver/dhcpserver.h"

#define ATTACK_MODE 2   // 0 = normale (include ARP legittimo), 1 = solo SYN flood, 2 = solo ARP (MITM)

char SSID[] = "ESP32_WiFi";
char STA_SSID[] = "<YOUR WIFI SSID>";  // Riempire il seguente campo
char STA_PSWD[] = "<YOUR WIFI PSW>";   // Riempire il seguente campo

uint8_t apMac[6];   // MAC del nostro AP, usato dal filtro BSSID nello sniffer

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
    IPAddress senderIP(arp[14], arp[15], arp[16], arp[17]);
    IPAddress targetIP(arp[24], arp[25], arp[26], arp[27]);
    Serial.printf("%s, %s, %d, %d\n", senderIP.toString().c_str(), targetIP.toString().c_str(), 900 + operation, len);
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

  IPAddress srcIP(ipPacket[12], ipPacket[13], ipPacket[14], ipPacket[15]);
  IPAddress dstIP(ipPacket[16], ipPacket[17], ipPacket[18], ipPacket[19]);
  Serial.printf("%s, %s, %d, %d\n", srcIP.toString().c_str(), dstIP.toString().c_str(), protocol, len);
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
