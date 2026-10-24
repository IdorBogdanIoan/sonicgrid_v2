/* SonicGrid - legatura dintre STM32 si ESP32.
 *
 * STM32 asculta si clasifica permanent. ESP32 doarme si nu face nimic pana
 * cand STM32 are ceva de raportat; atunci e trezit pe un pin, primeste
 * detaliile pe UART si le trimite prin ESP-NOW la gateway.
 *
 * Doua fire de date, un fir de trezire, o masa comuna. SG_LEG_UART din
 * sg_legatura.c alege intre doua perechi de pini:
 *
 * SG_LEG_UART 2, implicit, LPUART1 pe CN10. Pinii de UART standard Arduino,
 * inscriptionati pe placa, usor de gasit:
 *
 *   STM32 PG7  (LPUART1_TX, CN10-14, "D1 TX") ->  ESP32 GPIO16  (RX2)
 *   STM32 PG8  (LPUART1_RX, CN10-16, "D0 RX") <-  ESP32 GPIO17  (TX2)
 *
 * SG_LEG_UART 1, USART2 pe CN9, acelasi conector cu microfonul:
 *
 *   STM32 PD5  (USART2_TX, CN9-6, "D53") ->  ESP32 GPIO16  (RX2)
 *   STM32 PD6  (USART2_RX, CN9-4, "D52") <-  ESP32 GPIO17  (TX2)
 *
 * Pinul de trezire, ales de SG_LEG_WAKE:
 *
 *   SG_LEG_WAKE 2, implicit: PF15, CN10-12, "D2"  ->  ESP32 GPIO33
 *   SG_LEG_WAKE 1:           PG0,  CN9-29,  "D65" ->  ESP32 GPIO33
 *
 *   GND (CN10-5 sau CN9-12) <-> ESP32 GND
 *
 * Cu varianta implicita, toate trei firele de semnal sunt pe CN10, cu
 * etichetele D0, D1 si D2.
 *
 * Firele de date se INCRUCISEAZA: TX-ul unei placi merge la RX-ul celeilalte.
 *
 * Amandoua placile merg la 3.3 V, deci firele se leaga direct, fara translator
 * de nivel. Masa comuna nu e optionala.
 *
 * Atentie la varianta 2: PG2..PG15 sunt alimentati din VDDIO2, care se
 * porneste din software. sg_leg_init cheama HAL_PWREx_EnableVddIO2 pentru asta.
 * Fara apelul ala, UART-ul se initializeaza perfect si transmite in gol.
 *
 * De ce PG0 si nu un pin oarecare din portul G: pe STM32U5, pinii PG2..PG15
 * sunt alimentati din VDDIO2, care trebuie pornit separat din PWR. PG0 si PG1
 * stau pe VDD, deci merg imediat.
 *
 * De ce USART2 si nu altul: USART1 (PA9/PA10) e deja luat de consola de
 * depanare prin ST-LINK, adica tot ce scrie printf. USART2 e singurul alt
 * UART scos pe conectoarele placii.
 *
 * ---------------------------------------------------------------- protocol
 *
 * Cadru de 18 octeti, acelasi format in ambele sensuri:
 *
 *   0   0xA5         sincronizare
 *   1   0x5A         sincronizare
 *   2   tip          1 = status, 2 = eveniment
 *   3   clasa        indicele din sg_nume_clase (0..5), clasa dominanta
 *   4   incredere    0..100, increderea clasei dominante
 *   5   secventa     octetul mai putin semnificativ
 *   6   secventa     octetul mai semnificativ
 *   7   nivel        dBFS, cu semn (-90..0)
 *   8   contrast     dB peste fondul estimat de poarta (0..255)
 *   9   varsta_ms    octetul mai putin semnificativ
 *   10  varsta_ms    octetul mai semnificativ
 *   11  procent[0]   tipat,  0..100
 *   12  procent[1]   sticla
 *   13  procent[2]   claxon
 *   14  procent[3]   alarma
 *   15  procent[4]   grup
 *   16  procent[5]   trafic
 *   17  CRC-8        polinom 0x07, peste octetii 0..16
 *
 * Octetii 3 si 4 sunt redundanti fata de 11..16, fiindca clasa dominanta e
 * argmax-ul lor. Ii pastram oricum: gateway-ul si Pi-ul au nevoie de o
 * singura clasa pentru alerte si gruparea pe ferestre de timp, iar un argmax
 * recalculat in trei locuri diferite e un loc in plus unde se poate strecura
 * o nepotrivire.
 *
 * CRC-ul nu e paranoia: firul trece pe langa o alimentare in comutatie, si un
 * singur bit intors in octetul 3 transforma "trafic" in "tipat". Mai bine
 * aruncam cadrul si il retrimitem.
 *
 * varsta_ms = cate milisecunde au trecut de la detectie pana la trimiterea
 * cadrului. Gateway-ul face t = rx_ms - age_ms, deci o retrimitere intarziata
 * nu muta momentul evenimentului.
 *
 * ---------------------------------------------------------------- dialogul
 *
 * Eveniment (STM32 porneste):
 *   1. STM32 ridica PG0 si il tine sus.
 *   2. ESP32 se trezeste (300 ms de la somn pana in setup).
 *   3. STM32 retrimite cadrul la fiecare 60 ms, maximum 2 secunde.
 *   4. ESP32 prinde un cadru cu CRC bun si raspunde cu un octet 0x06.
 *   5. STM32 coboara PG0. ESP32 trimite prin ESP-NOW si se culca.
 *
 * Retrimiterea inlocuieste un dialog de tipul "sunt gata, trimite acum".
 * STM32 nu are cum sa stie cand a terminat ESP-ul de pornit, iar daca ar
 * trimite o singura data si ESP-ul nu ar fi ajuns inca la UART, evenimentul
 * s-ar pierde in tacere.
 *
 * Status (ESP32 porneste):
 *   1. ESP32 se trezeste din timer si scrie un octet 0x05.
 *   2. STM32 raspunde imediat cu un cadru de tip status: varful de nivel si
 *      de contrast de la ultimul status incoace, plus clasa dominanta.
 *   3. ESP32 trimite heartbeat-ul si se culca.
 *
 * Asa heartbeat-ul duce nivelul real masurat de microfon, nu o valoare
 * inventata: ESP-ul nu are microfon, STM32 e oricum treaz.
 */
#ifndef SG_LEGATURA_H
#define SG_LEGATURA_H

#include <stdbool.h>
#include <stdint.h>

#define SG_LEG_CADRU       18u
#define SG_LEG_NR_CLASE     6u
#define SG_LEG_MAGIC_1     0xA5u
#define SG_LEG_MAGIC_2     0x5Au

#define SG_LEG_TIP_STATUS  1u   /* acelasi cod ca PKT_HEARTBEAT pe ESP32 */
#define SG_LEG_TIP_EVENT   2u   /* acelasi cod ca PKT_EVENT */

#define SG_LEG_ACK         0x06u  /* ESP32 -> STM32: cadru primit si valid */
#define SG_LEG_CERERE      0x05u  /* ESP32 -> STM32: da-mi un status */

/* Cat de des retrimitem cadrul de eveniment si cat timp insistam. */
#define SG_LEG_PAS_MS      60u
#define SG_LEG_RABDARE_MS  2000u

void     sg_leg_init(void);

/* Scrie trei octeti si verifica daca se intorc. Se cheama cu un fir intre TX
   si RX pe placa, fara ESP-ul legat, ca sa separe o problema de placa de una
   de cablaj. */
bool     sg_leg_test_bucla(void);

/* Porneste trimiterea unui eveniment. Nu blocheaza: ridica pinul de trezire
   si lasa sg_leg_tick sa duca treaba la capat. Intoarce false daca o
   trimitere e deja in curs, caz in care evenimentul nou se pierde. */
/* prob = tabloul celor SG_LEG_NR_CLASE probabilitati din ultima rulare, in
   0..1. Poate fi NULL, caz in care procentele pleaca zero. */
bool     sg_leg_eveniment(uint8_t clasa, uint8_t incredere,
                          float dbfs, float contrast_db, const float *prob);

/* De chemat la fiecare cadru mel: retine varfurile pentru status. */
void     sg_leg_nivel(float dbfs, float contrast_db);

/* De chemat dupa fiecare rulare a modelului: clasa dominanta si toate
   probabilitatile, pentru status. Pentru fiecare clasa retine varful de la
   ultimul status incoace, nu ultima valoare: status-ul pleaca o data pe minut,
   iar modelul ruleaza de doua ori pe secunda, deci ultima valoare ar fi un
   esantion la intamplare din 120. */
void     sg_leg_context(uint8_t clasa, uint8_t incredere, const float *prob);

/* De chemat des din bucla principala. Face toata treaba. */
void     sg_leg_tick(void);

bool     sg_leg_ocupat(void);
uint32_t sg_leg_trimise(void);
uint32_t sg_leg_confirmate(void);
uint32_t sg_leg_cereri(void);

#endif /* SG_LEGATURA_H */
