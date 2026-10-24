/* SonicGrid - nod ESP32 pentru placa STM32U5.
 *
 * ESP32-ul nu asculta si nu clasifica nimic. STM32-ul face toata treaba si e
 * treaz permanent; ESP32-ul doarme in deep sleep si se trezeste doar ca sa
 * trimita un pachet prin ESP-NOW. Doua motive de trezire:
 *
 *   1. STM32 ridica pinul de trezire -> a clasificat un eveniment -> citim
 *      detaliile pe UART si trimitem un pachet EVENT.
 *   2. Timer -> cerem un status de la STM32 si trimitem un HEARTBEAT, ca
 *      serverul sa stie ca nodul traieste si cat de zgomotos e acolo.
 *
 * Un ciclu complet dureaza 250..400 ms, din care radioul e pornit ~100 ms.
 *
 * Pachetul ESP-NOW e identic cu cel din nod_autonom.ino, deci
 * gateway_espnow.ino si gateway_bridge.py nu se modifica deloc. Singurul
 * lucru de facut pe gateway e adaugarea MAC-ului acestei placi in NODURI[],
 * altfel pachetele criptate sunt aruncate fara niciun mesaj de eroare.
 *
 * Cablare catre STM32 (NUCLEO-U5A5ZJ-Q, toate pe CN9):
 *   ESP32 GPIO16 (RX2) <- STM32 PD5  (USART2_TX, CN9-6)
 *   ESP32 GPIO17 (TX2) -> STM32 PD6  (USART2_RX, CN9-4)
 *   ESP32 GPIO33       <- STM32 PG0  (trezire,   CN9-29)
 *   ESP32 GND          <-> GND       (CN9-12)
 *
 * Atentie la modul de ESP32: pe ESP32-WROVER, GPIO16 si GPIO17 sunt folositi
 * de PSRAM si nu pot fi luati pentru UART. Pe WROOM sunt liberi. Daca ai
 * WROVER, muta RX2/TX2 pe alta pereche (de exemplu 25 si 26) si schimba
 * STM_RX / STM_TX mai jos.
 *
 * De schimbat la fiecare placa: NODE_ID.
 *
 * Testat pentru Arduino-ESP32 core 3.x.
 */
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <esp_sleep.h>
#include <driver/rtc_io.h>
#include <stdint.h>

// ---------------------------------------------------------------- setari
#define NODE_ID        "n4"          // <-- UNIC pe fiecare placa
#define ESPNOW_CHANNEL 1             // acelasi pe noduri si pe gateway
uint8_t GATEWAY_MAC[6] = {0x70, 0x4B, 0xCA, 0x90, 0x54, 0x50};

#define WAKE_PIN       GPIO_NUM_33   // trebuie pin RTC: 0,2,4,12-15,25-27,32-39
#define STM_RX         16            // RX ESP32 <- TX STM32
#define STM_TX         17            // TX ESP32 -> RX STM32
#define HEARTBEAT_S    60            // cat de des se trezeste singur
#define MAX_RETRIES    3

// ------------------------------------------------- jurnal UART pe USB
// Tipareste pe portul serial tot ce vine de la STM32: cati octeti, ce octeti
// (in hexa) si cadrul despachetat. Exista ca sa poti deosebi intre ele cele
// trei cauze care arata identic in "dbfs = -100":
//
//   0 octeti                  -> STM32 nealimentat, sau firul lui de TX nu
//                                ajunge in GPIO16, sau e pus pe GPIO1
//   octeti dar niciun A5 5A   -> baud gresit, sau firele inversate, sau
//                                primesti zgomot de pe un pin in aer
//   A5 5A dar CRC gresit      -> firmware vechi pe STM32, cu alta lungime
//                                de cadru decat LEG_CADRU de aici
//
// Pune 0 pe teren. Nu schimba comportamentul, dar tine radioul pornit cu
// cateva milisecunde mai mult la fiecare trezire.
#define DEBUG_UART     1
#define DEBUG_MAX_OCT  64            // cat din flux intra in jurnal

// ------------------------------------------------- criptare ESP-NOW
// TREBUIE identice cu cele din gateway_espnow.ino. Daca difera, sau daca
// CRIPTARE e 1 aici si 0 pe gateway (sau invers), gateway-ul primeste
// pachetele si le arunca in tacere: nodul vede doar "fara ACK".
//
// SCHIMBA AMBELE CHEI inainte de teren si NU le urca pe GitHub.
#define CRIPTARE       1
#define ESPNOW_PMK     "SG_pmk_schimba!!"   // exact 16 caractere
#define ESPNOW_LMK     "SG_lmk_schimba!!"   // exact 16 caractere
static_assert(sizeof(ESPNOW_PMK) == 17, "PMK trebuie sa aiba exact 16 caractere");
static_assert(sizeof(ESPNOW_LMK) == 17, "LMK trebuie sa aiba exact 16 caractere");

#define LONG_RANGE     0             // 1 = raza mai mare, dar si pe gateway

// ------------------------------------------------- baterie
// Divizor rezistiv pe un pin ADC. Pana il montezi, 0 mV inseamna "nu stiu",
// nu "baterie goala" - asa il trateaza si Pi-ul.
#define ARE_DIVIZOR    0
#define PIN_BATERIE    35            // ADC1, merge si cu WiFi pornit
#define RAPORT_DIV     2.0f          // (R1+R2)/R2

// ---------------------------------------------- protocol cu STM32
// Vezi sg_legatura.h din proiectul STM32 pentru descrierea completa.
#define LEG_CADRU      18
#define LEG_NR_CLASE   6             // octetii 11..16 din cadru
#define LEG_MAGIC_1    0xA5
#define LEG_MAGIC_2    0x5A
#define LEG_TIP_STATUS 1
#define LEG_TIP_EVENT  2
#define LEG_ACK        0x06
#define LEG_CERERE     0x05

#define ASTEPT_EVENT_MS  900         // cat asteptam un cadru dupa trezirea pe pin
#define ASTEPT_STATUS_MS 300         // cat asteptam raspunsul la cererea de status
#define ASTEPT_WAKE_MS  2000         // cat asteptam ca STM32 sa coboare pinul

// ---------------------------------------------------------------- pachet
// Identic, octet cu octet, cu Packet din gateway_espnow.ino si nod_autonom.ino.
enum : uint8_t { PKT_HEARTBEAT = 1, PKT_EVENT = 2 };

typedef struct __attribute__((packed)) {
  uint8_t  type;
  char     node[4];
  uint16_t seq;
  uint32_t age_ms;
  uint8_t  cls;
  uint8_t  conf;
  int8_t   dbfs;
  uint16_t batt_mv;
  uint8_t  proc[LEG_NR_CLASE];       // procentul fiecarei clase, 0..100
} Packet;
// Daca schimbi structura asta, schimba-o IDENTIC si in gateway_espnow.ino.
// ESP-NOW nu verifica nimic: gateway-ul citeste pur si simplu octetii in
// structura lui, deci o nepotrivire da valori plauzibile dar false.
static_assert(sizeof(Packet) == 22, "pachet desincronizat cu gateway-ul");

// Clasele STM32 sunt 0..5 (tipat, sticla, claxon, alarma, grup, trafic), dar
// pe fir le trimitem deplasate cu 1. Motivul: nodurile autonome trimit cls=0
// cu sensul "nedeterminat", si daca 0 ar insemna si "tipat", dashboard-ul ar
// eticheta fiecare eveniment de pe nodurile vechi ca tipat.
#define CLS_OFFSET     1

/* Cadrul primit de la STM32, despachetat.
 *
 * Trebuie declarat INAINTE de prima functie din fisier. Arduino genereaza
 * singur prototipurile tuturor functiilor si le insereaza imediat inainte de
 * prima definitie de functie, deci un tip declarat mai jos nu exista inca
 * pentru prototipul lui citesteCadru. Eroarea arata ca "'Cadru' was not
 * declared in this scope" chiar pe linia unde tipul e clar vizibil. */
struct Cadru {
  uint8_t  tip, clasa, incredere, contrast;
  uint16_t secventa, varsta_ms;
  int8_t   dbfs;
  uint8_t  proc[LEG_NR_CLASE];
};

RTC_DATA_ATTR uint16_t seq = 0;      // supravietuieste deep sleep-ului

// ---------------------------------------------------------------- ESP-NOW

volatile int sendStatus = -1;        // -1 asteapta, 1 ok, 0 esuat

#if ESP_ARDUINO_VERSION_MAJOR >= 3
void onSent(const wifi_tx_info_t*, esp_now_send_status_t s) { sendStatus = (s == ESP_NOW_SEND_SUCCESS); }
#else
void onSent(const uint8_t*, esp_now_send_status_t s) { sendStatus = (s == ESP_NOW_SEND_SUCCESS); }
#endif

bool espnowInit() {
  WiFi.mode(WIFI_STA);
  esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);
#if LONG_RANGE
  esp_wifi_set_protocol(WIFI_IF_STA, WIFI_PROTOCOL_LR);
#endif
  if (esp_now_init() != ESP_OK) return false;
  esp_now_register_send_cb(onSent);
#if CRIPTARE
  if (esp_now_set_pmk((uint8_t*)ESPNOW_PMK) != ESP_OK) return false;
#endif
  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, GATEWAY_MAC, 6);
  peer.channel = ESPNOW_CHANNEL;
#if CRIPTARE
  peer.encrypt = true;
  memcpy(peer.lmk, ESPNOW_LMK, 16);
#else
  peer.encrypt = false;
#endif
  return esp_now_add_peer(&peer) == ESP_OK;
}

// age_ms se recalculeaza la fiecare incercare: gateway-ul face
// t = rx_ms - age_ms, deci o retrimitere intarziata nu muta momentul.
bool trimite(Packet& p, uint32_t varstaLaCitire, uint32_t tCitire) {
  for (int i = 0; i < MAX_RETRIES; i++) {
    if (p.type == PKT_EVENT) p.age_ms = varstaLaCitire + (millis() - tCitire);
    sendStatus = -1;
    esp_now_send(GATEWAY_MAC, (uint8_t*)&p, sizeof(p));
    uint32_t t0 = millis();
    while (sendStatus == -1 && millis() - t0 < 50) delay(1);
    if (sendStatus == 1) return true;
    delay(10 + random(20));          // evita coliziunea cu alte noduri
  }
  return false;
}

// ---------------------------------------------------------------- STM32

uint8_t crc8(const uint8_t* d, int n) {
  uint8_t c = 0x00;
  for (int i = 0; i < n; i++) {
    c ^= d[i];
    for (int b = 0; b < 8; b++) c = (c & 0x80) ? (uint8_t)((c << 1) ^ 0x07) : (uint8_t)(c << 1);
  }
  return c;
}

#if DEBUG_UART
static uint8_t  s_brut[DEBUG_MAX_OCT];
static int      s_brut_n     = 0;    // cat am reusit sa pastrez
static uint32_t s_brut_total = 0;    // cat a venit de fapt
static uint16_t s_crc_rele   = 0;

static void brutReset() { s_brut_n = 0; s_brut_total = 0; s_crc_rele = 0; }

static void brutPune(uint8_t v) {
  s_brut_total++;
  if (s_brut_n < DEBUG_MAX_OCT) s_brut[s_brut_n++] = v;
}

// Tiparit DUPA bucla de citire, niciodata in timpul ei. Un Serial.printf pe
// fiecare octet dureaza mai mult decat intervalul dintre doi octeti la
// 115200, iar cat timp tipareste, STM32 continua sa trimita: bufferul de RX
// al lui Serial2 se umple si pierzi exact cadrul pe care il depanai.
static void brutTipareste() {
  if (s_brut_total == 0) {
    Serial.println("# UART STM32: 0 octeti, nimic pe fir");
    return;
  }
  Serial.printf("# UART STM32: %lu octeti", (unsigned long)s_brut_total);
  if (s_crc_rele) Serial.printf(", %u cadre cu CRC gresit", s_crc_rele);
  Serial.print(" |");
  for (int i = 0; i < s_brut_n; i++) Serial.printf(" %02X", s_brut[i]);
  if (s_brut_total > (uint32_t)s_brut_n) Serial.print(" ...");
  Serial.println();

  if (s_crc_rele) {
    // Daca am ajuns la verificarea CRC, inseamna ca A5 5A s-a potrivit. Deci
    // firele si baud-ul sunt bune, si totusi cadrul nu se verifica: aproape
    // sigur cele doua placi au firmware cu lungimi de cadru diferite.
    Serial.printf("# sincronizare buna dar CRC gresit: STM32 pare sa aiba "
                  "firmware vechi. Aici LEG_CADRU = %d\n", LEG_CADRU);
  }
}
#else
static inline void brutReset() {}
static inline void brutPune(uint8_t) {}
static inline void brutTipareste() {}
#endif

// Caut sincronizarea octet cu octet, nu presupun ca primul octet primit e
// inceputul cadrului: ESP-ul se trezeste in mijlocul unei retrimiteri, deci
// de obicei prinde o bucata din cadrul precedent.
bool citesteCadru(Cadru& out, uint32_t timeoutMs) {
  uint8_t b[LEG_CADRU];
  int n = 0;
  uint32_t t0 = millis();

  brutReset();

  while (millis() - t0 < timeoutMs) {
    if (!Serial2.available()) { delay(1); continue; }
    uint8_t v = (uint8_t)Serial2.read();
    brutPune(v);

    if (n == 0) { if (v == LEG_MAGIC_1) b[n++] = v; continue; }
    if (n == 1) {
      if (v == LEG_MAGIC_2) { b[n++] = v; }
      else                  { n = (v == LEG_MAGIC_1) ? 1 : 0; }   // poate e un nou inceput
      continue;
    }

    b[n++] = v;
    if (n < LEG_CADRU) continue;

    n = 0;
    if (crc8(b, LEG_CADRU - 1) != b[LEG_CADRU - 1]) {
#if DEBUG_UART
      s_crc_rele++;                   // il raporteaza brutTipareste, dupa bucla
#else
      Serial.println("# cadru cu CRC gresit, il arunc");
#endif
      continue;                       // nu e o eroare fatala: STM32 retrimite
    }

    out.tip       = b[2];
    out.clasa     = b[3];
    out.incredere = b[4];
    out.secventa  = (uint16_t)b[5] | ((uint16_t)b[6] << 8);
    out.dbfs      = (int8_t)b[7];
    out.contrast  = b[8];
    out.varsta_ms = (uint16_t)b[9] | ((uint16_t)b[10] << 8);
    for (int k = 0; k < LEG_NR_CLASE; k++) out.proc[k] = b[11 + k];
    return true;
  }
  return false;
}

uint16_t citesteBaterie() {
#if ARE_DIVIZOR
  uint32_t s = 0;
  for (int i = 0; i < 8; i++) s += analogReadMilliVolts(PIN_BATERIE);
  return (uint16_t)((s / 8) * RAPORT_DIV);
#else
  return 0;
#endif
}

// ---------------------------------------------------------------- somn

void culcaTe() {
  // Pinul de trezire e activ pe nivel, nu pe front: daca ne-am culca cat
  // timp STM32 il mai tine sus, ne-am trezi imediat, la nesfarsit. Deci
  // asteptam sa-l coboare. (Il coboara cand primeste ack-ul nostru.)
  uint32_t t0 = millis();
  while (digitalRead((int)WAKE_PIN) == HIGH && millis() - t0 < ASTEPT_WAKE_MS) delay(5);
  const bool pinBlocat = (digitalRead((int)WAKE_PIN) == HIGH);
  if (pinBlocat)
    Serial.println("# pinul de trezire a ramas sus, dorm doar pe timer");

  Serial2.end();

  // Pulldown pe linia de trezire: cat timp STM32 e nealimentat, pinul lui e
  // in aer si fara pulldown ESP-ul s-ar trezi pe zgomot, la fiecare cateva
  // secunde, pana goleste bateria.
  rtc_gpio_pullup_dis(WAKE_PIN);
  rtc_gpio_pulldown_en(WAKE_PIN);

  // Daca pinul a ramas sus, NU activam trezirea pe el pentru ciclul asta.
  //
  // Pinul e activ pe nivel, nu pe front. STM32 il coboara numai cand primeste
  // ACK-ul nostru, iar noi trimitem ACK numai dupa un cadru valid. Deci daca
  // legatura UART e rupta, sau cele doua placi au firmware cu cadre de
  // lungimi diferite, se formeaza o bucla: adormim, ne trezim imediat pe pinul
  // care e tot sus, nu primim nimic, adormim iar. Masurat pe un log real:
  // sapte treziri la rand, niciuna din timer, deci consum continuu in loc de
  // cateva secunde pe minut. Pe baterie, nodul moare intr-o noapte.
  //
  // Cu ext0 dezactivat, nodul cade inapoi pe heartbeat-ul din timer si ramane
  // viu pe radio. Pierde evenimentele cat timp legatura e rupta, dar asta se
  // vede oricum in dbfs = -100 care ajunge la gateway, si bateria tine.
  if (!pinBlocat) {
    esp_sleep_enable_ext0_wakeup(WAKE_PIN, 1);
  }
  esp_sleep_enable_timer_wakeup((uint64_t)HEARTBEAT_S * 1000000ULL);
  Serial.flush();
  esp_deep_sleep_start();
}

// ---------------------------------------------------------------- main

void setup() {
  Serial.begin(115200);
  pinMode((int)WAKE_PIN, INPUT);
  Serial2.begin(115200, SERIAL_8N1, STM_RX, STM_TX);
#if ARE_DIVIZOR
  analogSetPinAttenuation(PIN_BATERIE, ADC_11db);
#endif

  const esp_sleep_wakeup_cause_t motiv = esp_sleep_get_wakeup_cause();
  const bool dinSTM = (motiv == ESP_SLEEP_WAKEUP_EXT0);

#if DEBUG_UART
  Serial.printf("\n# trezire %s\n",
                dinSTM ? "pe pin, deci STM32 a clasificat ceva"
                       : "din timer, cer eu status");
#endif

  Packet p = {};
  strncpy(p.node, NODE_ID, sizeof(p.node));
  p.seq     = ++seq;
  p.batt_mv = citesteBaterie();

  Cadru c = {};
  uint32_t tCitire = 0;
  bool am = false;

  if (dinSTM) {
    // STM32 retrimite cadrul la fiecare 60 ms, deci nu trebuie sa ne grabim.
    am = citesteCadru(c, ASTEPT_EVENT_MS);
  } else {
    // Trezire din timer sau pornire la rece: cerem noi un status.
    Serial2.write((uint8_t)LEG_CERERE);
    am = citesteCadru(c, ASTEPT_STATUS_MS);
  }
  tCitire = millis();

  if (am) {
    Serial2.write((uint8_t)LEG_ACK);   // ca STM32 sa coboare pinul de trezire
    Serial2.flush();
  }

  // Abia acum tiparim: ACK-ul pleaca primul, ca STM32 sa nu mai tina pinul sus
  // cat timp noi scriem pe USB.
  brutTipareste();
#if DEBUG_UART
  if (am) {
    Serial.printf("# cadru: tip=%u (%s) clasa=%u incredere=%u%% dbfs=%d "
                  "contrast=%u varsta=%u ms seq=%u\n",
                  c.tip,
                  c.tip == LEG_TIP_EVENT  ? "EVENT"  :
                  c.tip == LEG_TIP_STATUS ? "STATUS" : "tip necunoscut",
                  c.clasa, c.incredere, c.dbfs, c.contrast,
                  c.varsta_ms, c.secventa);
    Serial.printf("#   tipat=%u%% sticla=%u%% claxon=%u%% alarma=%u%% "
                  "grup=%u%% trafic=%u%%\n",
                  c.proc[0], c.proc[1], c.proc[2],
                  c.proc[3], c.proc[4], c.proc[5]);
  }
#endif

  // Procentele pe clasa merg la fel in eveniment si in heartbeat: dashboard-ul
  // le arata in ambele cazuri. Daca nu am primit cadru, rama n zero.
  if (am) {
    for (int k = 0; k < LEG_NR_CLASE; k++) p.proc[k] = c.proc[k];
  }

  if (am && c.tip == LEG_TIP_EVENT) {
    p.type = PKT_EVENT;
    p.cls  = c.clasa + CLS_OFFSET;
    p.conf = c.incredere;
    p.dbfs = c.dbfs;
  } else if (am && c.tip == LEG_TIP_STATUS) {
    p.type = PKT_HEARTBEAT;
    p.cls  = 0;
    // conf = contrastul fata de fond. gateway_bridge.py afiseaza
    // fond = dbfs - conf, deci asa vede si fondul estimat de poarta.
    p.conf = c.contrast;
    p.dbfs = c.dbfs;
  } else {
    // Nu am primit nimic. Trimitem oricum un heartbeat, cu dbfs = -100 ca
    // semn convenit pentru "radioul merge, STM32 nu raspunde". Altfel nodul
    // ar parea complet mort si n-ai sti care din cele doua placi e de vina.
    p.type = PKT_HEARTBEAT;
    p.cls  = 0;
    p.conf = 0;
    p.dbfs = -100;
    Serial.printf("# STM32 nu a raspuns pe UART (trezire %s)\n",
                  dinSTM ? "pe pin" : "din timer");
  }

  bool ok = false;
  if (espnowInit()) {
    ok = trimite(p, am ? c.varsta_ms : 0, tCitire);
  } else {
    Serial.println("# ESP-NOW nu a pornit");
  }

  Serial.printf("%s %s seq=%u cls=%u conf=%u db=%d age=%lu "
                "proc %u/%u/%u/%u/%u/%u -> %s | MAC %s\n",
                NODE_ID, p.type == PKT_EVENT ? "EVENT" : "HB",
                p.seq, p.cls, p.conf, p.dbfs, (unsigned long)p.age_ms,
                p.proc[0], p.proc[1], p.proc[2],
                p.proc[3], p.proc[4], p.proc[5],
                ok ? "ACK" : "fara ACK", WiFi.macAddress().c_str());

  culcaTe();
}

void loop() {}   // nu ajunge niciodata aici: setup se termina in deep sleep
