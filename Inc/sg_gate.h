/* Poarta de energie adaptiva.

   Ruleaza continuu si ieftin pe fiecare cadru mel, ca sa decida cand merita
   pornit clasificatorul. In arhitectura cu somn adanc, asta e singura parte
   care nu doarme niciodata, deci trebuie sa fie aproape gratuita.

   Trei decizii de proiectare:

   1. Se uita doar pe benzile 12..33, adica intre 1005 si 3838 Hz. Zgomotul
      de camera, ventilatie si trafic sta sub 1 kHz, iar tipetele au energia
      concentrata exact in intervalul asta. O poarta pe energia totala ar fi
      declansata permanent de fondul de jos.

   2. Pragul e relativ la fondul curent, nu absolut. Intr-un depozit gol si
      intr-o piata aglomerata nivelul absolut difera cu 30 de dB, dar
      contrastul unui tipat fata de ce era inainte ramane comparabil.

   3. Fondul e urmarit prin statistici de minim, nu prin medie. Un tipat de
      doua secunde ar trage media in sus si ar face poarta surda exact cand
      conteaza. Minimul pe o fereastra glisanta nu are problema asta. In plus
      inghetam actualizarea fondului cat timp poarta e deschisa. */
#pragma once

#include <stdint.h>
#include <stdbool.h>

#include "mel_table.h"

/* Benzile mel care acopera 1..4 kHz, calculate din device_spec.json
   (40 benzi, 200..6000 Hz, scara Slaney). b12 = 1005 Hz, b33 = 3838 Hz. */
#define SG_GATE_B_LO        12
#define SG_GATE_B_HI        33

/* Iesirea frontendului e log10 al puterii, intins de la -6 la 2 pe 0..255.
   Deci un pas de cuantizare inseamna 80 / 255 decibeli de putere. */
#define SG_GATE_DB_PE_PAS   (80.0f / 255.0f)

/* 25 de cadre a 20 ms = 0.5 s per subfereastra, 8 subferestre = 4 s de memorie. */
#define SG_GATE_SUB_CADRE   25
#define SG_GATE_SUB_NR      8

/* Histerezis: deschidem greu, inchidem si mai greu.

   Valorile vin din masuratori pe nodul real, nu din teorie:
     liniste                      difMAX  1..3
     vorbit, miscare, zgomot uzual difMAX  4..12
     evenimente puternice          difMAX 18..26
   Pragul de 14 cade in golul dintre ultimele doua regimuri. */
#define SG_GATE_ON_DB       14.0f
#define SG_GATE_OFF_DB      4.0f
#define SG_GATE_ON_CADRE    3      /* 60 ms peste prag ca sa deschida */
#define SG_GATE_OFF_CADRE   15     /* 300 ms sub prag ca sa inchida */

/* Cat poate sta poarta deschisa inainte sa accepte ca fondul s-a schimbat.
 *
 * Fondul se invata doar cat poarta e inchisa, altfel evenimentul isi ridica
 * singur pragul si poarta se inchide in mijlocul tipatului. Dar daca sunetul
 * e continuu, fondul ramane inghetat, contrastul nu mai scade niciodata si
 * poarta ramane deschisa la infinit. Masurat pe nod: 100% din linii DESCHIS
 * cu un singur eveniment numarat, pe un fond de banda larga la -52 dBFS.
 *
 * Cand se intampla asta, nu mai e un eveniment, s-a schimbat mediul: a pornit
 * traficul, un ventilator, a inceput ploaia. Fortam inchiderea si reinvatam
 * fondul de la nivelul curent.
 *
 * Aceeasi solutie e in nod_autonom.ino, cu MAX_EVENT_MS 2000.
 *
 * 2 secunde la 50 de cadre pe secunda. Clasificatorul a avut deja patru
 * ferestre in care sa decida, iar SG_ALARMA_CADRE cere doua, deci nu pierdem
 * nicio decizie. Un sunet care tine mai mult de doua secunde si devine fond
 * nou nu mai e un eveniment nici conceptual. */
#define SG_GATE_MAX_CADRE   100

/* Garda absoluta: sub nivelul asta nu deschidem oricat ar fi contrastul.
   Intr-o camera foarte linistita, fondul coboara atat de jos incat si
   fosnetul hainelor ar da 9 dB de contrast. */
#define SG_GATE_NIVEL_MIN   18.0f  /* in pasi de cuantizare */

typedef struct
{
  float    nivel;                       /* nivelul benzii, in pasi */
  float    fond;                        /* fondul estimat, in pasi */
  float    min_curent;                  /* minimul subferestrei in curs */
  uint16_t sub_cadre;
  float    inel[SG_GATE_SUB_NR];        /* minimele ultimelor subferestre */
  uint8_t  inel_idx;
  uint8_t  inel_umplut;
  uint16_t c_on;
  uint16_t c_off;
  uint16_t c_activ;                     /* cadre scurse de la deschidere */
  bool     activ;
  bool     front;                       /* true doar pe cadrul care a deschis */
  uint32_t evenimente;
  uint32_t fonduri_noi;                 /* de cate ori fondul a fost fortat */
} sg_gate_t;

void  sg_gate_init(sg_gate_t *g);

/* Consuma un cadru mel. Intoarce true cat timp poarta e deschisa. */
bool  sg_gate_push(sg_gate_t *g, const uint8_t *mel);

float sg_gate_nivel_db(const sg_gate_t *g);
float sg_gate_fond_db(const sg_gate_t *g);
float sg_gate_contrast_db(const sg_gate_t *g);
